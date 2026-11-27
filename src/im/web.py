"""The neutral view (docs/decisions/0007-neutral-proposals.md; the start of Emanation, decision 0005).

Every cluster that passed the gates, newest first, each showing what's known about it: who said it (me, me by
diarization, not me; never a name), in how many conversations, when, its maturity, how I kept it, and its edges.
No score orders or filters anything. From a card I keep a cluster (canonical or pinned), un-keep it or dismiss it,
as feedback events, the same kinds Lattice's keep_idea wrote. A dismissal may say why (feedback.DismissReason), a
choice beside the plain dismiss that's never asked for.

Two more pages draw the same cards (docs/design/m6-emanation.md): Trails (`/trails`), the sky's map with each
conversation as the path it took from thought to thought, and Crossroads (`/crossroads/<id>`), one card with what was
said around it and what's like it; and Echoes (`/echoes`), every connection between thoughts said in different
conversations; and Threads (`/threads`), the neighborhoods of connected thoughts that run through several
conversations; and Apart (`/apart`), the near pairs the connection judge kept apart; and Tree (`/tree`), the Tree of
Life turned in three dimensions, and Grid (`/grid`), a cell a lens: each board shows the lenses I placed on it, and a
neighborhood stands in where I haven't placed one. They only read.

Lenses (decision 0013, im.lens) have their own pages: `/lenses` lists them and makes one, `/lens/<id>` shows one's
cards and why each is in, and edits it, keeps it at hand, archives it, or pins a card out; a card's page pins it into
or out of a lens; `/board/tree` and `/board/grid` put a lens in a board's place; `/sky?lens=<id>` lights its cards.
Each is my feedback event.

Demo mode is the app's one setting (pub.app_settings): `/demo` turns it on from any device, and off again only with
the token, so whoever I hand the phone to can't; `im demo on|off` does it from the shell, and `im web --demo` forces
it on. While it's on, every page reads the `demo` views instead of `pub`'s, so nothing sensitive is there to show:
the sky keeps a sensitive cluster's star, blank, with its edges, and the lists (the feed, a card's quotes and
connections, a lens's cards) leave it out. Nothing can be kept, dismissed or changed in demo mode.

It also serves the MCP server (im.mcp) at /mcp, behind the same token as a bearer header, demo mode included.

It connects as its own role (IM_WEB_DATABASE_URL, role lattice_app: read pub, append feedback), never as
im_pipeline, and logs in with its token (IM_WEB_TOKEN), exchanged once per device for a signed cookie. Loopback only:
tailscale serve publishes it to the tailnet (install/nas.sh). The one exception is the public MCP listener
(PublicHandler), opened only when IM_PUBLIC_URL is set: a Cloudflare tunnel publishes it, behind OAuth (im.oauth).
"""

import hashlib
import hmac
import html
import json
import math
import os
import re
import threading
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
from urllib.parse import parse_qs, urlencode, urlsplit

import psycopg

from im import mcp, oauth
from im.feedback import DismissReason

DATABASE_ENV = "IM_WEB_DATABASE_URL"
TOKEN_ENV = "IM_WEB_TOKEN"
COOKIE = "emanation_session"
PAGE = 50
SHOW = {  # which maturities the feed shows, and the button's name
    "candidates": (("candidate", "idea"), "candidates and ideas"),
    "ideas": (("idea",), "ideas"),
    "observations": (("observation",), "observations"),
    "all": (("observation", "candidate", "idea"), "everything"),
}
ACTIONS = {"canonical": ("idea_keep", '{"as": "canonical"}'), "pinned": ("idea_keep", '{"as": "pinned"}'),
           "unkeep": ("idea_unkeep", "{}"), "dismiss": ("idea_dismiss", "{}")}
REASONS = {DismissReason.NOT_A_THOUGHT: "not a thought", DismissReason.MISREPRESENTS: "misrepresents what I said",
           DismissReason.NOT_FOR_ME: "not for me", DismissReason.DUPLICATE: "a duplicate"}   # as a card offers them
SPEAKERS = {"me": "me", "me_diar": "me (diarization)", "not_me": "not me"}
# The app's icons and manifest (dev/make_icons.py draws them). Served without a login: a browser fetches the manifest
# without cookies, and the mark is no secret.
STATIC = {"/manifest.webmanifest": "application/manifest+json", "/icon.svg": "image/svg+xml",
          "/icon-192.png": "image/png", "/icon-512.png": "image/png", "/apple-touch-icon.png": "image/png"}
_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


def _token() -> str:
    t = os.environ.get(TOKEN_ENV, "")
    if len(t) < 16:
        raise SystemExit(f"{TOKEN_ENV} is not set, or shorter than 16 characters")
    return t


def _session() -> str:
    return hmac.new(_token().encode(), b"emanation-session-v1", hashlib.sha256).hexdigest()


def _cookie(cookie_header: str | None, name: str) -> str | None:
    for part in (cookie_header or "").split(";"):
        key, _, value = part.strip().partition("=")
        if key == name:
            return value
    return None


def logged_in(cookie_header: str | None) -> bool:
    value = _cookie(cookie_header, COOKIE)
    return value is not None and hmac.compare_digest(value.encode(), _session().encode())


def _views(demo: bool) -> str:
    """The prefix of the views with words a page reads: pub.demo_ in demo mode, else pub."""
    return "pub.demo_" if demo else "pub."


def _e(value) -> str:
    return html.escape("" if value is None else str(value))


def _when(ts) -> str:
    return ts.strftime("%Y-%m-%d") if ts else "—"


def _facts(r: dict) -> str:
    kept = f"kept as {r['kept_as']}" if r["kept_as"] else r["maturity"]
    said = [f"{n} {label}" for n, label in ((r["by_me"], "me"), (r["by_me_diar"], "me (diarization)"),
                                            (r["by_others"], "not me")) if n]
    parts = [kept + (" · proposed" if r["proposed"] else ""), ", ".join(said) or r["origin"],
             f"{r['conversations']} conversation{'s' * (r['conversations'] != 1)}" if r["conversations"] else "",
             f"{_when(r['first_said'])} → {_when(r['last_seen'])}" if r["first_said"] else _when(r["last_seen"]),
             f"{r['links']} edge{'s' * (r['links'] != 1)}" if r["links"] else ""]
    return " · ".join(p for p in parts if p)


def _rows(conn, sql: str, args=()) -> list[dict]:
    cur = conn.execute(sql, args)
    names = [c.name for c in cur.description]
    return [dict(zip(names, row)) for row in cur.fetchall()]


EMANATION_VIEWS = (("/sky", "Sky"), ("/trails", "Trails"), ("/echoes", "Echoes"), ("/threads", "Threads"),
                   ("/apart", "Apart"), ("/crossroads", "Crossroads"))
TRELLIS_VIEWS = (("/lenses", "Lenses"), ("/tree", "Tree"), ("/grid", "Grid"))


def feed(conn, show: str = "candidates", page: int = 0, demo: bool = False) -> str:
    show = show if show in SHOW else "candidates"
    maturities = SHOW[show][0]
    rows = _rows(conn, f"""SELECT * FROM {_views(demo)}cards WHERE maturity = ANY(%s) {"AND NOT redacted" if demo else ""}
                           ORDER BY last_seen DESC, idea_id LIMIT %s OFFSET %s""",
                 (list(maturities), PAGE + 1, page * PAGE))
    views = ("".join(f'<a class="btn view" href="{href}">{name}</a>' for href, name in EMANATION_VIEWS)
             + '<span class="sep" aria-hidden="true"></span>'
             + "".join(f'<a class="btn trellis" href="{href}">{name}</a>' for href, name in TRELLIS_VIEWS)
             + f'<a class="btn mode{" on" if demo else ""}" href="/demo">'
             + f'{"leave demo mode" if demo else "demo mode"}</a>')
    filters = "".join(f'<a class="pill" href="/?show={k}"{" aria-current=page" if k == show else ""}>{name}</a>'
                      for k, (_, name) in SHOW.items())
    cards = "".join(f'<li><a href="/idea/{r["idea_id"]}">{_e(r["title"])}</a><p>{_e(r["statement"])}</p>'
                    f'<small>{_e(_facts(r))}</small></li>' for r in rows[:PAGE])
    more = f'<a class="btn trellis" href="/?show={_e(show)}&page={page + 1}">older</a>' if len(rows) > PAGE else ""
    return _page("Idea Machine",
                 f'<nav class="views" aria-label="Views">{views}</nav>'
                 f'<nav class="filters" aria-label="Show">{filters}</nav>'
                 f"<ol>{cards or '<li>Nothing here.</li>'}</ol>{more}")


def _dismissed_ids(conn, idea_ids) -> set[str]:
    """Which of `idea_ids` are currently dismissed: latest keep/unkeep/dismiss event is `idea_dismiss`."""
    ids = sorted({str(i) for i in idea_ids if i is not None})
    if not ids:
        return set()
    return {str(i) for i, kind in conn.execute(
        """SELECT idea_id, kind FROM (
             SELECT DISTINCT ON (idea_id) idea_id, kind
             FROM pub.feedback_events
             WHERE idea_id = ANY(%s::uuid[]) AND kind IN ('idea_keep', 'idea_unkeep', 'idea_dismiss')
             ORDER BY idea_id, event_id DESC
           ) latest WHERE kind = 'idea_dismiss'""", (ids,))}


def _dismissal(conn, idea_id: str, demo: bool = False) -> dict | None:
    """Current dismissal state for `idea_id`, else None. Always None in demo mode."""
    if demo:
        return None
    rows = conn.execute(
        """SELECT kind, payload, at FROM pub.feedback_events
           WHERE idea_id = %s AND kind IN ('idea_keep', 'idea_unkeep', 'idea_dismiss')
           ORDER BY event_id DESC""", (idea_id,)).fetchall()
    if not rows or rows[0][0] != "idea_dismiss":
        return None
    reason, of = None, None
    for kind, payload, _ in rows:
        if kind != "idea_dismiss":
            break
        if isinstance(payload, dict) and payload.get("reason"):
            reason = payload["reason"]
            of = payload.get("of")
            break
    of_title = None
    if of:
        row = conn.execute("SELECT title FROM pub.cards WHERE idea_id = %s", (of,)).fetchone()
        of_title = row[0] if row else None
    return {"reason": reason, "of": of, "of_title": of_title, "at": rows[0][2]}


def _neighbors(conn, idea_id: str, demo: bool = False) -> dict:
    """Previous and next cards around `idea_id` in chronological transcript order, plus active-only skips."""
    v = _views(demo)
    shown = "AND NOT c.redacted" if demo else ""
    moments = _rows(conn, f"""SELECT m.idea_id::text AS idea_id, m.conversation_id, m.said_at, c.title
                              FROM {v}card_moments m JOIN {v}cards c USING (idea_id)
                              WHERE m.said_at IS NOT NULL {shown}
                              ORDER BY m.said_at, m.conversation_id, m.item_id, m.observation_id""")
    idx = max((n for n, m in enumerate(moments) if m["idea_id"] == idea_id), default=None)
    if idx is None:
        return {}
    anchor = moments[idx]
    dismissed = set() if demo else _dismissed_ids(conn, [m["idea_id"] for m in moments])

    def walk(step: int) -> tuple[dict | None, dict | None]:
        immediate, active, seen, skipped = None, None, {idea_id}, 0
        k = idx + step
        while 0 <= k < len(moments):
            m = moments[k]
            k += step
            mid = m["idea_id"]
            if mid in seen:
                continue
            seen.add(mid)
            is_dropped = mid in dismissed
            info = {
                "idea_id": mid,
                "title": m["title"] or "(untitled)",
                "gap": _apart(m["said_at"], anchor["said_at"]),
                "same_conv": m["conversation_id"] is not None and m["conversation_id"] == anchor["conversation_id"],
                "dismissed": is_dropped,
            }
            if immediate is None:
                immediate = info
            if not is_dropped:
                active = info | {"skipped": skipped}
                break
            skipped += 1
        return immediate, active

    prev_card, prev_active = walk(-1)
    next_card, next_active = walk(1)
    return {"prev": prev_card, "prev_active": prev_active, "next": next_card, "next_active": next_active}


def _card_nav(nav: dict, demo: bool = False) -> str:
    """Conversation prev/next bar with fixed button positions and active-only skip buttons."""
    if not nav:
        return ""
    p, pa, n, na = nav.get("prev"), nav.get("prev_active"), nav.get("next"), nav.get("next_active")

    def button(item: dict | None, elem_id: str, label: str, cls: str, skip: bool = False) -> str:
        if not item:
            return f'<span class="btn {cls} disabled" aria-disabled="true">{label}</span>'
        extra = " has-skip" if skip and item.get("skipped") else ""
        return f'<a class="btn {cls}{extra}" id="{elem_id}" href="/idea/{item["idea_id"]}">{label}</a>'

    def meta(item: dict | None, active: dict | None, direction: str) -> str:
        if not item:
            return '<small class="nav-empty">nothing ' + direction + '</small>'
        conv = "same conversation" if item["same_conv"] else f"{'earlier' if direction == 'earlier' else 'next'} conversation"
        tag = ' <span class="tag dropped">dropped</span>' if item["dismissed"] else ""
        line = (f'<small><a href="/idea/{item["idea_id"]}">{_e(item["title"])}</a> · '
                f'{_e(item["gap"])} {direction}, {conv}{tag}</small>')
        if not demo and active and active["idea_id"] != item["idea_id"]:
            act_conv = ("same conversation" if active["same_conv"]
                        else f"{'earlier' if direction == 'earlier' else 'next'} conversation")
            line += (f'<small class="nav-skip">Active (skips {active["skipped"]} dropped): '
                     f'<a href="/idea/{active["idea_id"]}">{_e(active["title"])}</a> · '
                     f'{_e(active["gap"])} {direction}, {act_conv}</small>')
        return line

    left_btns = button(p, "nav-prev", "← Previous", "trellis")
    right_btns = ""
    if not demo:
        left_btns += " " + button(pa, "nav-prev-active", "↞ Prev active", "view", skip=True)
        right_btns += button(na, "nav-next-active", "Next active ↠", "view", skip=True) + " "
    right_btns += button(n, "nav-next", "Next →", "trellis")
    shortcut = ('<small class="nav-hint"><kbd>←</kbd> <kbd>→</kbd> step · <kbd>Shift+←</kbd> <kbd>Shift+→</kbd>'
                ' skip dropped</small>') if not demo else '<small class="nav-hint"><kbd>←</kbd> <kbd>→</kbd> step</small>'
    key_map = ('e.key === "ArrowLeft" ? "nav-prev" : e.key === "ArrowRight" ? "nav-next" : null' if demo else
               'e.key === "ArrowLeft" ? (e.shiftKey ? "nav-prev-active" : "nav-prev") '
               ': e.key === "ArrowRight" ? (e.shiftKey ? "nav-next-active" : "nav-next") : null')
    script = f"""<script>
"use strict";
addEventListener("keydown", e => {{
  if (e.metaKey || e.ctrlKey || e.altKey) return;
  if (e.target && e.target.closest("input, textarea, select, button, [contenteditable]")) return;
  const id = {key_map};
  const link = id && document.getElementById(id);
  if (link) {{ e.preventDefault(); location.href = link.href; }}
}});
</script>"""
    return (f'<nav class="card-nav" aria-label="Conversation navigation">'
            f'<div class="nav-side nav-prev"><div class="nav-btns">{left_btns}</div>{meta(p, pa, "earlier")}</div>'
            f'<div class="nav-side nav-next"><div class="nav-btns">{right_btns}</div>{meta(n, na, "later")}</div>'
            f'</nav>{shortcut}{script}')


def _meta_chips(r: dict) -> str:
    chips = []
    said = [f"{n} {label}" for n, label in ((r["by_me"], "me"), (r["by_me_diar"], "me (diar)"),
                                            (r["by_others"], "not me")) if n]
    if said:
        chips.append(", ".join(said))
    if r["conversations"]:
        chips.append(f"{r['conversations']} conversation{'s' * (r['conversations'] != 1)}")
    if r["first_said"]:
        first, last = _when(r["first_said"]), _when(r["last_seen"])
        chips.append(first if first == last else f"{first} → {last}")
    elif r["last_seen"]:
        chips.append(_when(r["last_seen"]))
    if r["links"]:
        chips.append(f"{r['links']} edge{'s' * (r['links'] != 1)}")
    return "".join(f'<span class="chip">{_e(c)}</span>' for c in chips)


def _curation(idea_id: str, r: dict, near: list, dropped: dict | None) -> str:
    """Keep and Drop panels for curating a card."""
    canon_cls = " is-current" if r["kept_as"] == "canonical" else ""
    pin_cls = " is-current" if r["kept_as"] == "pinned" else ""
    restore = (f'<button class="btn-act restore" name="action" value="unkeep">'
               f'{"Restore to active" if dropped else "Unkeep"}</button>') if (dropped or r["kept_as"]) else ""
    keep_row = (f'<div class="curate-row"><span class="curate-label">Keep</span>'
                f'<form class="curate-btns" method="post" action="/idea/{idea_id}">'
                f'<button class="btn-act keep{canon_cls}" name="action" value="canonical">Keep as canonical</button>'
                f'<button class="btn-act keep{pin_cls}" name="action" value="pinned">Pin</button>'
                f'{restore}</form></div>')
    return f'<section class="panel curation" aria-label="Curate">{keep_row}{_why(idea_id, near, dropped)}</section>'


def card(conn, idea_id: str, demo: bool = False) -> str | None:
    v = _views(demo)
    shown = "AND NOT c.redacted" if demo else ""
    rows = _rows(conn, f"SELECT * FROM {v}cards WHERE idea_id = %s", (idea_id,))
    if not rows:
        return None
    r = rows[0]
    if demo and r["redacted"]:
        return _page("Hidden", f"<h1>Hidden in demo mode</h1><small>{_e(_facts(r))}</small>")
    dropped = _dismissal(conn, idea_id, demo)
    nav = _neighbors(conn, idea_id, demo)
    quotes = _rows(conn, f"""SELECT speaker, quote, gist, said_at, source, reading, claude_reading
                             FROM {v}card_evidence WHERE idea_id = %s ORDER BY said_at, item_id""", (idea_id,))
    links = _rows(conn, f"""SELECT l.kind, l.kinds, c.idea_id::text AS idea_id, c.title, j.why, j.analogy
                            FROM pub.card_links l
                            JOIN {v}cards c ON c.idea_id = CASE WHEN l.a = %s THEN l.b ELSE l.a END
                            LEFT JOIN pub.card_judgments j ON j.a = least(l.a, l.b) AND j.b = greatest(l.a, l.b)
                            WHERE %s IN (l.a, l.b) {shown} ORDER BY l.kind, c.title""", (idea_id, idea_id))
    lineage = _rows(conn, f"""SELECT CASE WHEN l.from_id = %s THEN l.kind WHEN l.kind = 'evolves' THEN 'evolved by'
                                          ELSE 'synthesized in' END AS kind,
                                     c.idea_id::text AS idea_id, c.title FROM pub.card_lineage l
                              JOIN {v}cards c ON c.idea_id = CASE WHEN l.from_id = %s THEN l.to_id ELSE l.from_id END
                              WHERE %s IN (l.from_id, l.to_id) {shown} ORDER BY 1, c.title""",
                    (idea_id, idea_id, idea_id))
    near = _rows(conn, f"""SELECT c.idea_id::text AS idea_id, c.title, n.similarity FROM pub.card_neighbours(%s) n
                           JOIN {v}cards c USING (idea_id) WHERE true {shown} ORDER BY n.similarity DESC""",
                 (idea_id,))
    neighbor_dismissed = set() if demo else _dismissed_ids(
        conn, [x["idea_id"] for x in links + lineage + near])
    dtag = lambda nid: ' <span class="tag dropped">dropped</span>' if nid in neighbor_dismissed else ""  # noqa: E731

    badges = []
    if dropped:
        badges.append('<span class="badge dropped">Dropped</span>')
        if dropped["reason"]:
            label = _e(REASONS.get(dropped["reason"], dropped["reason"]))
            if dropped["of"]:
                label += f' · <a href="/idea/{dropped["of"]}">{_e(dropped["of_title"] or "card")}</a>'
            badges.append(f'<span class="badge reason">{label}</span>')
        badges.append(f'<small class="badge-note">dismissed {_when(dropped["at"])}</small>')
    elif r["kept_as"]:
        badges.append(f'<span class="badge kept">Kept · {_e(r["kept_as"])}</span>')
    else:
        badges.append(f'<span class="badge stage">{_e(r["maturity"].capitalize())}</span>')
        if r["proposed"]:
            badges.append('<span class="badge proposed">Proposed</span>')
    if r["origin"] in ("mine", "archive"):
        badges.append(f'<span class="badge origin">{"Added by me" if r["origin"] == "mine" else "From archive"}</span>')

    hero_cls = "card-hero dropped" if dropped else "card-hero kept" if r["kept_as"] else "card-hero"
    explore = (f'<p class="explore-links"><a class="btn view" href="/crossroads/{idea_id}">Crossroads</a>'
               f'<a class="btn view" href="/sky?constellation={idea_id}">Constellation</a>'
               f'<a class="btn view" href="/sky?seed={idea_id}">Light the sky</a>'
               f'<a class="btn view" href="/trails?at={idea_id}">Trail</a>'
               + ("" if demo else f'<a class="btn trellis" href="/idea/{idea_id}/edit">Edit card &amp; readings</a>')
               + "</p>")
    hero = (f'<section class="{hero_cls}"><div class="badges">{"".join(badges)}</div>'
            f'<h1>{_e(r["title"])}</h1><p class="statement">{_e(r["statement"])}</p>'
            f'<div class="meta-chips">{_meta_chips(r)}</div>{explore}</section>')

    body = [_card_nav(nav, demo), hero, "" if demo else _curation(idea_id, r, near, dropped)]
    if r["my_note"]:
        body.append(f'<section class="card-section note-section"><h2>My note</h2><p>{_e(r["my_note"])}</p></section>')
    if quotes:
        said_rows = "".join(
            f'<li><blockquote>{_e(q["quote"])}</blockquote>'
            + (f'<p class="capture-gist"><small>capture gist:</small> {_e(q["gist"])}</p>'
               if q["gist"] and q["gist"].strip() != (r["statement"] or "").strip() else "")
            + f'{_reading(q)}<small>{_e(SPEAKERS.get(q["speaker"], q["speaker"]))} · {_when(q["said_at"])}'
            + (f' · {_e(q["source"])}' if q.get("source") and q["source"] != "speech" else "")
            + "</small></li>" for q in quotes)
        body.append(f'<section class="card-section"><h2>Said <span class="count">{len(quotes)}</span></h2>'
                    f'<ol>{said_rows}</ol></section>')
    if links:
        link_rows = "".join(
            f'<li><span class="rel-badge">{_e(", ".join([x["kind"], *x["kinds"]]))}</span> '
            f'<a href="/idea/{x["idea_id"]}">{_e(x["title"])}</a>{dtag(x["idea_id"])}'
            + (f'<p class="diag-note">{_e(x["why"] or "")}'
               + (f' <i>Same structure: {_e(x["analogy"])}</i>' if x["analogy"] else "") + "</p>"
               if x["why"] or x["analogy"] else "")
            + "</li>" for x in links)
        body.append(f'<section class="card-section"><h2>Connections <span class="count">{len(links)}</span></h2>'
                    f'<ul class="diag-list">{link_rows}</ul></section>')
    if lineage:
        lin_rows = "".join(
            f'<li><span class="rel-badge">{_e(x["kind"])}</span> '
            f'<a href="/idea/{x["idea_id"]}">{_e(x["title"])}</a>{dtag(x["idea_id"])}</li>' for x in lineage)
        body.append(f'<section class="card-section"><h2>Lineage <span class="count">{len(lineage)}</span></h2>'
                    f'<ul class="diag-list">{lin_rows}</ul></section>')
    if near:
        near_rows = "".join(
            f'<li><a href="/idea/{x["idea_id"]}">{_e(x["title"])}</a> '
            f'<span class="sim">{x["similarity"]:.2f}</span>{dtag(x["idea_id"])}</li>' for x in near)
        body.append(f'<section class="card-section"><h2>Near by meaning <span class="count">{len(near)}</span></h2>'
                    f'<ul class="diag-list">{near_rows}</ul></section>')
    body.append(_card_lenses(conn, idea_id, demo))
    return _page(r["title"], "".join(body))


def _reading(q: dict) -> str:
    """A capture's reading, folded away under its quote; marked when it's mine."""
    if not q["reading"]:
        return ""
    mine = " (mine)" if q["reading"] != q["claude_reading"] else ""
    return f"<details><summary>reading{mine}</summary><p>{_e(q['reading'])}</p></details>"


READING_MAX = 2000   # as migration 072 allows
EDIT_FORM_MAX = 1 << 18   # the edit page's form holds every capture's reading, URL-encoded


def _norm(text: str) -> str:
    return text.replace("\r\n", "\n").strip()


def edit_page(conn, idea_id: str) -> str | None:
    """/idea/<id>/edit: a card's title, statement and my note, and each capture's reading under its quote, to
    correct. None if there's no such card."""
    from im.idea import LIMITS

    rows = _rows(conn, "SELECT title, statement, my_note FROM pub.cards WHERE idea_id = %s", (idea_id,))
    if not rows:
        return None
    r = rows[0]
    said = _rows(conn, """SELECT item_id, speaker, quote, said_at, reading, claude_reading FROM pub.card_evidence
                          WHERE idea_id = %s AND item_id IS NOT NULL ORDER BY said_at, item_id""", (idea_id,))

    def area(name: str, text: str | None, limit: int, rows: int = 4) -> str:
        return f'<textarea name="{name}" maxlength="{limit}" rows="{rows}" cols="80">{_e(text or "")}</textarea>'

    readings = "".join(
        f"<li><blockquote>{_e(q['quote'])}</blockquote><small>{_e(SPEAKERS.get(q['speaker'], q['speaker']))} · "
        f"{_when(q['said_at'])}</small><br>{area('reading:' + str(q['item_id']), q['reading'], READING_MAX)}"
        + (f"<br><small>Claude's: {_e(q['claude_reading'] or '')}</small>"
           if q["reading"] != q["claude_reading"] else "") + "</li>" for q in said)
    body = (f'<h1>Edit</h1><p class="lede">An empty field goes back to Claude\'s; an empty note is no note. '
            f'<a href="/idea/{idea_id}">Back to the card</a></p>'
            f'<form method="post" action="/idea/{idea_id}/edit">'
            f'<p><label>Title<br><input name="title" maxlength="{LIMITS["title"]}" size="80" '
            f'value="{_e(r["title"] or "")}"></label></p>'
            f'<p><label>Statement<br>{area("statement", r["statement"], LIMITS["statement"])}</label></p>'
            f'<p><label>My note<br>{area("note", r["my_note"], LIMITS["note"], 3)}</label></p>'
            + (f"<h2>Readings</h2><p>Each capture's quote repaired and resolved. Mine counts for every capture of "
               f"the same moment.</p><ol>{readings}</ol>" if said else "")
            + '<p><label><input type="checkbox" name="meaning_changed" value="yes"> The meaning changed: a '
              "mishearing, or what it says turned around (not only the wording)</label></p>"
            "<p><small>Every correction counts at once, and the card goes back in the judges' queues.</small></p>"
            "<p><button>Save</button></p></form>")
    return _page(f"Edit: {r['title']}", body)


def correct(conn, idea_id: str, form: dict) -> None:
    """The edit page's corrections as my events, only what changed: one idea_correct with the title and statement,
    an idea_note for my note, an item_reading for each capture's reading; the corrections carry my mark of whether
    the meaning changed (`meaning_changed`). An empty field reverts to Claude's (no note, for the note). ValueError,
    writing nothing, for no such card, a field too long, or a capture not on it."""
    from im.idea import LIMITS

    rows = _rows(conn, "SELECT title, statement, my_note FROM pub.cards WHERE idea_id = %s", (idea_id,))
    if not rows:
        raise ValueError("no such card")
    now = {"title": rows[0]["title"] or "", "statement": rows[0]["statement"] or "", "note": rows[0]["my_note"] or ""}
    given = {k: _norm(form[k]) for k in now if k in form}
    readings = {k.removeprefix("reading:"): _norm(v) for k, v in form.items() if k.startswith("reading:")}
    held = {str(i): x or "" for i, x in conn.execute(
        "SELECT item_id, reading FROM pub.card_evidence WHERE idea_id = %s AND item_id IS NOT NULL", (idea_id,))}
    if any(len(v) > LIMITS[k] for k, v in given.items()) or any(
            i not in held or len(v) > READING_MAX for i, v in readings.items()):
        raise ValueError("a field too long, or a capture not on this card")
    meaning = {"meaning_changed": form.get("meaning_changed") == "yes"}
    fixed = {k: v for k, v in given.items() if k != "note" and v != now[k]}
    events = [("idea_correct", idea_id, None, fixed | meaning)] if fixed else []
    if "note" in given and given["note"] != now["note"]:
        events.append(("idea_note", idea_id, None, {"note": given["note"]}))
    events += [("item_reading", None, i, {"reading": v} | meaning) for i, v in readings.items() if v != held[i]]
    with conn.transaction():
        for kind, idea, item, payload in events:
            conn.execute("""INSERT INTO pub.feedback_events (kind, idea_id, item_id, payload, source)
                            VALUES (%s, %s, %s, %s, 'emanation')""", (kind, idea, item, json.dumps(payload)))


def _card_lenses(conn, idea_id: str, demo: bool) -> str:
    """The lenses a card is in, and why; and, outside demo mode, a lens to pin it into or out of."""
    v = _views(demo)
    rows = _rows(conn, f"""SELECT l.lens_id, l.name, m.why FROM {v}lens_members m JOIN pub.lenses l USING (lens_id)
                           WHERE m.idea_id = %s ORDER BY lower(l.name)""", (idea_id,))
    lenses = _rows(conn, "SELECT lens_id, name FROM pub.lenses WHERE state <> 'archived' ORDER BY lower(name)")
    if not rows and (demo or not lenses):
        return ""
    shown = '<ul class="diag-list">' + "".join(
        f'<li><a href="/lens/{x["lens_id"]}">{_e(x["name"])}</a> '
        f'<span class="rel-badge">{"pinned in" if x["why"] == "pin" else "by its query"}</span></li>' for x in rows) \
        + "</ul>" if rows else ""
    controls = ""
    if not demo and lenses:
        options = "".join(f'<option value="{x["lens_id"]}">{_e(x["name"])}</option>' for x in lenses)
        controls = (f'<form class="lens-form" method="post" action="/idea/{idea_id}"><select name="lens" aria-label="lens">'
                    f'{options}</select> <button class="btn-act" name="action" value="lens_in">Pin in</button>'
                    '<button class="btn-act" name="action" value="lens_out">Pin out</button>'
                    '<button class="btn-act" name="action" value="lens_unpin">Unpin</button></form>')
    return (f'<section class="card-section"><h2>Lenses <span class="count">{len(rows)}</span></h2>'
            f'{shown}{controls}</section>')


def _why(idea_id: str, near: list, dropped: dict | None = None) -> str:
    """Dismissing, saying why: one tap per reason directly on the card. A duplicate may name which card."""
    form = f'<form class="curate-btns" method="post" action="/idea/{idea_id}"><input type="hidden" name="action" value="dismiss">'
    cur = lambda r: " is-current" if dropped and dropped["reason"] == r else ""  # noqa: E731
    reasons = "".join(f'<button class="btn-act drop{cur(x)}" name="reason" value="{x}">{REASONS[x]}</button>'
                      for x in DismissReason if x != DismissReason.DUPLICATE)
    plain = f'<button class="btn-act drop subtle{" is-current" if dropped and dropped["reason"] is None else ""}">drop (no reason)</button>'
    which = "".join(f'<option value="{x["idea_id"]}">{_e(x["title"])}</option>' for x in near)
    which = (f'<select name="of" aria-label="of which card"><option value="">of which? (optional)</option>{which}'
             "</select>") if near else ""
    dup = (f'<button class="btn-act drop{cur(DismissReason.DUPLICATE)}" name="reason" '
           f'value="{DismissReason.DUPLICATE}">{REASONS[DismissReason.DUPLICATE]}</button>')
    return (f'<div class="curate-row"><span class="curate-label">Drop (dismiss, saying why)</span>'
            f'<div class="curate-controls">{form}{reasons}{plain}</form>{form}{dup}{which}</form></div></div>')


SIZES = {"observation": 0, "candidate": 1, "idea": 2}   # a canonical idea is one bigger than a pinned one


def sky(conn, seed: str | None = None, demo: bool = False, lens_id: str | None = None,
        constellation: str | None = None) -> str | None:
    """Every card at its place on the one map (decision 0005, amended). With a seed, the view from it lights what's
    relevant to it; with a constellation, only that card's connection fan-out is drawn (with a toggle for the dimmed
    backdrop); with a lens, the lens draws its cards the same way; with none, the encodings light everything. None if
    the seed or constellation isn't on the map, or there's no such lens. Each star carries its moment timestamps so
    the page can bound the sky to an inclusive date slice without hiding a recurring card said inside it. In demo mode
    a sensitive card keeps its star, blank."""
    from im import lens

    v = _views(demo)
    rows = _rows(conn, f"""SELECT c.*, p.x, p.y FROM {v}cards c JOIN pub.card_positions p USING (idea_id)
                           ORDER BY c.idea_id""")
    index = {str(r["idea_id"]): n for n, r in enumerate(rows)}
    if (seed is not None and seed not in index) or (constellation is not None and constellation not in index):
        return None
    shown = None
    if lens_id is not None:
        shown = _rows(conn, "SELECT lens_id, name FROM pub.lenses WHERE lens_id = %s", (lens_id,))
        if not shown:
            return None
    moments: dict[str, list[int]] = defaultdict(list)
    for i, t in conn.execute(f"""SELECT idea_id::text, said_at FROM {v}card_moments
                                 WHERE said_at IS NOT NULL ORDER BY said_at"""):
        if i in index:
            ts = int(t.timestamp())
            if not moments[i] or moments[i][-1] != ts:
                moments[i].append(ts)
    now = datetime.now(timezone.utc)
    stars = []
    for r in rows:
        sid = str(r["idea_id"])
        first = int(r["first_said"].timestamp()) if r["first_said"] else None
        times = ([] if first is None else
                 moments.get(sid) or sorted({int(t.timestamp()) for t in (r["first_said"], r["last_seen"]) if t}))
        stars.append([sid, round(float(r["x"]), 4), round(float(r["y"]), 4),
                      SIZES[r["maturity"]] + (r["kept_as"] == "canonical"), r["conversations"],
                      max((now - r["last_seen"]).days, 0) if r["last_seen"] else 365, r["title"] or "", _facts(r),
                      first, times])
    links = sorted({tuple(sorted((index[a], index[b]))) for a, b in conn.execute(
        """SELECT a::text, b::text FROM pub.card_links UNION ALL SELECT from_id::text, to_id::text FROM pub.card_lineage""")
        if a in index and b in index and a != b})
    if seed:
        rel = _relevance(conn, seed, index)
    elif shown:
        rel = {str(m["idea_id"]): 1 for m in lens.members(conn, lens_id, demo) if str(m["idea_id"]) in index}
    else:
        rel = None
    data = json.dumps({"stars": stars, "links": links, "rel": rel, "seed": seed, "constellation": constellation,
                       "lens": {"id": lens_id, "name": shown[0]["name"]} if shown else None}, separators=(",", ":"))
    title = ((rows[index[seed]]["title"] or "Sky") if seed
             else (rows[index[constellation]]["title"] or "Sky") if constellation
             else shown[0]["name"] if shown else "Sky")
    return _SKY.replace("{title}", _e(title)).replace("{data}", data.replace("</", "<\\/"))


def _relevance(conn, seed: str, on_map) -> dict[str, float]:
    from im import lens

    return {k: v for k, v in lens.relevance(conn, seed).items() if k in on_map}


def sky_lens(conn, seed: str, demo: bool = False) -> dict | None:
    """What the sky needs to light itself from `seed` without loading the map again: its relevance to every card on
    the map, and the page's title. None if the seed isn't on the map."""
    v = _views(demo)
    rows = _rows(conn, f"""SELECT c.idea_id::text AS id, c.title FROM {v}cards c
                           JOIN pub.card_positions p USING (idea_id)""")
    on_map = {r["id"]: r["title"] for r in rows}
    if seed not in on_map:
        return None
    return {"seed": seed, "rel": _relevance(conn, seed, on_map), "title": on_map[seed] or "Sky"}


WHO = {"me": 0, "me_diar": 1, "not_me": 2}


def trails(conn, at: str | None = None, demo: bool = False) -> str | None:
    """The sky's map, drawn through time (docs/design/m6-emanation.md, Trails): every card where the sky puts it, and
    every moment (pub.card_moments: a card, said in a conversation at a time), so the page can draw each conversation
    as the trail it took across the map. A moment carries the arousal in my voice, where it was measured (a fact about
    the voice, 048), and the connections come too, so a stop can say what it returns to: a card it's connected to,
    first said earlier in another conversation. Conversations are sent as numbers, never their ids. `at` opens the page on a
    card's conversation; None if that card isn't on the map. In demo mode a sensitive card keeps its star, blank, and
    has no moments, so a trail passes it by."""
    v = _views(demo)
    rows = _rows(conn, f"""SELECT c.*, p.x, p.y FROM {v}cards c JOIN pub.card_positions p USING (idea_id)
                           ORDER BY c.idea_id""")
    index = {str(r["idea_id"]): n for n, r in enumerate(rows)}
    if at is not None and at not in index:
        return None
    stars = [[str(r["idea_id"]), round(float(r["x"]), 4), round(float(r["y"]), 4),
              SIZES[r["maturity"]] + (r["kept_as"] == "canonical"), r["title"] or "", _facts(r)] for r in rows]
    conversations: dict[str, int] = {}
    moments = [[index[i], conversations.setdefault(c, len(conversations)), int(t.timestamp()), WHO.get(who, 2),
                -1 if heat is None else round(min(max(float(heat), 0.0), 1.0), 2)]
               for i, c, t, who, heat in conn.execute(
                   f"""SELECT idea_id::text, conversation_id, said_at, speaker, arousal FROM {v}card_moments
                       WHERE conversation_id IS NOT NULL AND said_at IS NOT NULL
                       ORDER BY said_at, conversation_id, item_id, observation_id""") if i in index]
    links = sorted((*sorted((index[a], index[b])), ", ".join(kinds)) for a, b, kinds in conn.execute(
        "SELECT a::text, b::text, kinds FROM pub.card_links") if a in index and b in index and a != b)
    data = json.dumps({"stars": stars, "moments": moments, "links": links, "at": at}, separators=(",", ":"))
    return _TRAILS.replace("{data}", data.replace("</", "<\\/"))


TREE_LENSES = 32   # the Tree of Life's lenses: the ten sephirot and the 22 paths
TREE_ALSO = 2      # the stand-in for a card on several lenses: this many connections into another's neighborhood


def tree(conn, demo: bool = False) -> str:
    """The Tree of Life as a Trellis board (docs/design/m6-emanation.md, Tree): every card, and which of the tree's 32
    lenses it's on, for the page to gather around the sephirot and the paths. A place holds the lens I put there (the
    Tree board, lens.places); until I put one, a graph neighbourhood stands in, the place's as before: the n-th
    largest (lens.neighbourhoods) at the n-th place, the largest on Kether. A card is also on a stand-in when
    TREE_ALSO of its connections lead into that one's neighbourhood, so the page has cards on several lenses to draw.
    Which neighbourhood is on which place means nothing. `named` is the first card said in each stand-in, `placed`
    each place's lens ([name, id]). In demo mode a sensitive card is on no lens, blank, with no connections."""
    from im import lens

    rows = _rows(conn, f"SELECT * FROM {_views(demo)}cards ORDER BY idea_id")
    index = {str(r["idea_id"]): n for n, r in enumerate(rows) if not (demo and r["redacted"])}
    pairs = sorted({tuple(sorted((a, b))) for a, b in conn.execute("SELECT a::text, b::text FROM pub.card_links")
                    if a in index and b in index and a != b})
    near: dict = defaultdict(set)
    for a, b in pairs:
        near[a].add(b)
        near[b].add(a)
    held = {lens.TREE_PLACES.index(p): x for p, x in lens.places(conn, "tree").items()}
    placed = [[held[n]["name"], held[n]["id"]] if n in held else None for n in range(TREE_LENSES)]
    members: dict = defaultdict(set)
    for n, x in held.items():
        for m in lens.members(conn, x["id"], demo):
            if str(m["idea_id"]) in index:
                members[str(m["idea_id"])].add(n)
    on, named = {}, [""] * TREE_LENSES
    for n, group in enumerate(lens.neighbourhoods(pairs)[:TREE_LENSES]):
        if n in held:
            continue
        on.update((i, n) for i in group)
        first = min(group, key=lambda i: (rows[index[i]]["first_said"] is None, rows[index[i]]["first_said"], i))
        named[n] = rows[index[first]]["title"] or ""

    def lenses(i: str) -> list[int]:
        standing = []
        if i in on:
            into = Counter(on[n] for n in near[i] if n in on and on[n] != on[i])
            standing = [on[i], *(n for n, k in into.items() if k >= TREE_ALSO)]
        return sorted(members.get(i, set()) | set(standing))

    stars = [[str(r["idea_id"]), r["title"] or "", lenses(str(r["idea_id"]))] for r in rows]
    order = {str(r["idea_id"]): n for n, r in enumerate(rows)}
    data = json.dumps({"stars": stars, "links": [[order[a], order[b]] for a, b in pairs], "named": named,
                       "placed": placed}, separators=(",", ":"))
    return _TREE.replace("{data}", data.replace("</", "<\\/"))


GRID_CARDS = 3   # the least a stand-in cell of the grid holds


def grid(conn, demo: bool = False) -> str:
    """A grid of lenses, the plainer Trellis board (docs/design/m6-emanation.md, Grid): one cell a lens, its cards
    drawn as a small constellation, each where the sky puts it relative to the others, with the connections among
    them. First the lenses I placed on the Grid board, in the order of their cells (lens.places); then, standing in
    for the lenses not made yet, each graph neighbourhood of at least GRID_CARDS cards (lens.neighbourhoods), named by
    the first card said in it, in the order they began. A star opens its card's crossroads; a cell's cards can be lit
    on the sky or the tree. In demo mode a sensitive card is in no cell."""
    from im import lens

    v = _views(demo)
    cards = {str(r["idea_id"]): r for r in _rows(
        conn, f"SELECT c.*, p.x, p.y FROM {v}cards c LEFT JOIN pub.card_positions p USING (idea_id)")
        if not (demo and r["redacted"])}
    pairs = sorted({tuple(sorted((a, b))) for a, b in conn.execute("SELECT a::text, b::text FROM pub.card_links")
                    if a in cards and b in cards and a != b})
    at = {i: (float(r["x"]), -float(r["y"])) for i, r in cards.items() if r["x"] is not None}
    mx, my = (sum(p[0] for p in at.values()) / len(at), sum(p[1] for p in at.values()) / len(at)) if at else (0, 0)
    when = lambda i: (cards[i]["first_said"] is None, cards[i]["first_said"], i)   # noqa: E731

    def cell(order: list[str], name: str, facts: str) -> str:
        if not order:
            return f'<section><svg viewBox="0 0 100 100" role="img" aria-label="no cards"></svg>{name}{facts}</section>'
        inside = set(order)
        # Each card where the sky puts it, the group scaled to fill the cell; one the sky hasn't placed sits in a ring.
        spot = {i: at.get(i) or (mx + math.cos(n), my + math.sin(n)) for n, i in enumerate(order)}
        x0, y0 = min(p[0] for p in spot.values()), min(p[1] for p in spot.values())
        wide = max(max(p[0] for p in spot.values()) - x0, max(p[1] for p in spot.values()) - y0, 1e-6)
        ox, oy = (wide - (max(p[0] for p in spot.values()) - x0)) / 2, (wide - (max(p[1] for p in spot.values()) - y0)) / 2
        put = {i: (round(14 + 72 * (p[0] - x0 + ox) / wide, 1), round(14 + 72 * (p[1] - y0 + oy) / wide, 1))
               for i, p in spot.items()}
        lines = "".join(f'<line x1="{put[a][0]}" y1="{put[a][1]}" x2="{put[b][0]}" y2="{put[b][1]}"/>'
                        for a, b in pairs if a in inside and b in inside)
        stars = "".join(
            f'<a href="/crossroads/{i}"><circle cx="{put[i][0]}" cy="{put[i][1]}" '
            f'r="{2 + .7 * (SIZES[cards[i]["maturity"]] + (cards[i]["kept_as"] == "canonical")):.1f}" style="--hue: '
            f'{round(math.degrees(math.atan2(spot[i][1] - my, spot[i][0] - mx)) % 360)}; --d: {n % 7}">'
            f'<title>{_e(cards[i]["title"])}</title></circle></a>' for n, i in enumerate(order))
        lit = ",".join(order)
        return (f'<section><svg viewBox="0 0 100 100" role="img" aria-label="{len(order)} connected cards">{lines}'
                f'{stars}</svg>{name}{facts}<small><a href="/sky?lit={lit}">on the sky</a>'
                f'<a href="/tree?lit={lit}">on the tree</a></small></section>')

    placed = []
    for place, x in sorted(lens.places(conn, "grid").items(), key=lambda p: int(p[0])):
        order = sorted((str(m["idea_id"]) for m in lens.members(conn, x["id"], demo) if str(m["idea_id"]) in cards),
                       key=when)
        placed.append(cell(order, f'<b><a href="/lens/{x["id"]}">{_e(x["name"])}</a></b>',
                           f'<small>{len(order)} card{"s" * (len(order) != 1)} · a lens</small>'))
    standing = []
    for group in lens.neighbourhoods(pairs):
        if len(group) < GRID_CARDS:
            continue
        order = sorted(group, key=when)
        standing.append((when(order[0]), cell(order, f'<b>{_e(cards[order[0]]["title"])}</b>',
                                              f'<small>{len(order)} cards · began '
                                              f'{_when(cards[order[0]]["first_said"])}</small>')))
    standing.sort(key=lambda c: c[0])
    cells = placed + [c for _, c in standing]
    body = (f'<h1>Grid</h1><p class="lede">{len(placed)} lens{"es" * (len(placed) != 1)} I placed '
            f'(<a href="/board/grid">the board</a>), then {len(standing)} neighborhood{"s" * (len(standing) != 1)} '
            f'of connected cards standing in for the lenses not made yet, each named by the first thing said in it, '
            f'in the order they began. A star is a card, placed as the sky places it among the others.</p>'
            + (f'<div class="grid">{"".join(cells)}</div>' if cells else "<p>Nothing here.</p>"))
    return _GRID.replace("{body}", body)


def _apart(a, b) -> str:
    """How far apart two times are, in words: "4 minutes", "3 days", "5 months"."""
    s = abs((a - b).total_seconds())
    for size, name in ((86400 * 365, "year"), (86400 * 30.4, "month"), (86400, "day"), (3600, "hour"), (60, "minute")):
        if s >= size:
            n = round(s / size)
            return f"{n} {name}{'s' * (n != 1)}"
    return "moments"


def _door(way: str, r: dict, why: str, quotes: dict, turn: float | None = None) -> str:
    """A door to another card's crossroads. `turn` is the way that card lies on the map from this one, in degrees
    clockwise from east as the sky draws it, shown as a small arrow."""
    quote = quotes.get(r["idea_id"])
    arrow = "" if turn is None else (f'<svg class="way" viewBox="-8 -8 16 16" aria-hidden="true"><path '
                                     f'transform="rotate({turn:.0f})" d="M-5,0H5M2,-3L5,0L2,3"/></svg>')
    return (f'<a class="door" id="d{r["idea_id"]}" data-way="{way}" href="/crossroads/{r["idea_id"]}">'
            f'<small>{_e(why)}</small>'
            f'<b>{_e(r["title"])}</b>' + (f"<q>{_e(quote)}</q>" if quote else "") + arrow + "</a>")


ECHOES = 4   # how many doors each way in meaning


def crossroads(conn, idea_id: str, demo: bool = False) -> str | None:
    """One card with the two kinds of neighbor it has (docs/design/m6-emanation.md, Crossroads). Across: what was said
    just before and just after it in its own conversations, usually about something else. Up and down: what's like
    it, said at another time (its connections, lineage and nearest by meaning, less the doors across), earlier above
    and later below. Every door leads to that card's own crossroads. Nothing orders a door but time, and likeness within one direction.
    None if there's no such card; in demo mode a sensitive one says only that it's hidden, and no door leads to one."""
    v = _views(demo)
    shown = "AND NOT c.redacted" if demo else ""
    rows = _rows(conn, f"SELECT * FROM {v}cards WHERE idea_id = %s", (idea_id,))
    if not rows:
        return None
    r = rows[0]
    if demo and r["redacted"]:
        return _page("Hidden", f"<h1>Hidden in demo mode</h1><small>{_e(_facts(r))}</small>")
    # Across: every moment of the conversations this card was said in, in the order said.
    said = _rows(conn, f"""SELECT m.idea_id, m.conversation_id, m.said_at, m.speaker, m.quote, c.title
                           FROM {v}card_moments m JOIN {v}cards c USING (idea_id)
                           WHERE m.conversation_id IN (SELECT conversation_id FROM {v}card_moments WHERE idea_id = %s)
                             AND m.said_at IS NOT NULL
                           ORDER BY m.conversation_id, m.said_at, m.item_id, m.observation_id""", (idea_id,))
    mine = [m for m in said if str(m["idea_id"]) == idea_id]
    before, after, seen = [], [], {idea_id}
    for n, m in enumerate(said):
        if str(m["idea_id"]) != idea_id:
            continue
        for found, step in ((before, -1), (after, 1)):
            k = n + step
            while 0 <= k < len(said) and said[k]["conversation_id"] == m["conversation_id"] \
                    and str(said[k]["idea_id"]) == idea_id:
                k += step
            if 0 <= k < len(said) and said[k]["conversation_id"] == m["conversation_id"] \
                    and (step, said[k]["idea_id"]) not in seen:
                seen.add((step, said[k]["idea_id"]))
                found.append((said[k], f"{_apart(said[k]['said_at'], m['said_at'])} {'earlier' if step < 0 else 'later'}"
                                       ", same conversation"))
    # Up and down: connections and lineage first, then the nearest by meaning, each once.
    like = _rows(conn, f"""SELECT c.idea_id, c.title, c.first_said,
                                  coalesce(nullif(array_to_string(l.kinds, ', '), ''), 'connected') AS why,
                                  2::real AS similarity
                           FROM pub.card_links l
                           JOIN {v}cards c ON c.idea_id = CASE WHEN l.a = %s THEN l.b ELSE l.a END
                           WHERE %s IN (l.a, l.b) {shown}
                           UNION ALL
                           SELECT c.idea_id, c.title, c.first_said, 'lineage', 2 FROM pub.card_lineage l
                           JOIN {v}cards c ON c.idea_id = CASE WHEN l.from_id = %s THEN l.to_id ELSE l.from_id END
                           WHERE %s IN (l.from_id, l.to_id) {shown}
                           UNION ALL
                           SELECT c.idea_id, c.title, c.first_said, 'near by meaning', n.similarity
                           FROM pub.card_neighbours(%s, %s) n JOIN {v}cards c USING (idea_id) WHERE true {shown}
                           ORDER BY similarity DESC, idea_id""", (idea_id,) * 5 + (4 * ECHOES,))
    earlier, later, met = [], [], {r["idea_id"]} | {x["idea_id"] for x, _ in before + after}
    for x in like:
        if x["idea_id"] in met:
            continue
        met.add(x["idea_id"])
        if r["first_said"] and x["first_said"]:
            past = x["first_said"] < r["first_said"]
            why = f"{x['why']} · {_apart(x['first_said'], r['first_said'])} {'earlier' if past else 'later'}"
        else:
            past, why = False, x["why"]
        side = earlier if past else later
        if len(side) < ECHOES:
            side.append((x, why))
    doors = [x["idea_id"] for x, _ in before + after + earlier + later]
    quotes = dict(conn.execute(f"""SELECT DISTINCT ON (idea_id) idea_id, quote FROM {v}card_moments
                                   WHERE idea_id = ANY(%s) ORDER BY idea_id, said_at, item_id""", (doors,)).fetchall())

    # Where each door is on the map from here: its direction, and how far next to the others.
    at = {i: (float(x), -float(y)) for i, x, y in conn.execute(
        "SELECT idea_id, x, y FROM pub.card_positions WHERE idea_id = ANY(%s)", (doors + [r["idea_id"]],))}
    here_at, turn, far = at.get(r["idea_id"]), {}, {}
    for i in doors:
        if here_at and i in at and at[i] != here_at:
            turn[i] = math.degrees(math.atan2(at[i][1] - here_at[1], at[i][0] - here_at[0]))
            far[i] = math.hypot(at[i][1] - here_at[1], at[i][0] - here_at[0])
    longest = max(far.values(), default=1.0)
    rays = "".join(
        f'<a href="#d{x["idea_id"]}"><line class="{axis}" x2="{18 + 26 * far[x["idea_id"]] / longest:.1f}" '
        f'transform="rotate({turn[x["idea_id"]]:.0f})"><title>{_e(x["title"])}</title></line></a>'
        for axis, found in (("time", before + after), ("like", earlier + later))
        for x, _ in found if x["idea_id"] in turn)
    rose = (f'<svg class="rose" viewBox="-50 -50 100 100" role="img" aria-label="Where its doors are on the map">'
            f'{rays}<circle r="3"/></svg><small>where its doors are on the map</small>') if rays else ""

    def arm(way: str, name: str, found: list) -> str:
        inner = "".join(_door(way, x, why, quotes, turn.get(x["idea_id"])) for x, why in found) or "<i>nothing</i>"
        return f'<section class="{way}"><h2>{name}</h2>{inner}</section>'

    here = "".join(f"<blockquote><p>{_e(m['quote'])}</p><small>{_e(SPEAKERS.get(m['speaker'], m['speaker']))} · "
                   f"{_when(m['said_at'])}</small></blockquote>" for m in mine[:3])
    body = (arm("up", "Like it, earlier", earlier) + arm("left", "Just before", before)
            + f'<main><h1>{_e(r["title"])}</h1>{here}<p>{_e(r["statement"])}</p><small>{_e(_facts(r))}</small>{rose}'
              f'<nav><a href="/idea/{idea_id}">Card</a><a href="/trails?at={idea_id}">Its trail</a>'
              f'<a href="/sky?seed={idea_id}">Light the sky</a><a href="/crossroads">Somewhere else</a></nav></main>'
            + arm("right", "Just after", after) + arm("down", "Like it, later", later))
    return _CROSSROADS.replace("{title}", _e(r["title"] or "Crossroads")).replace("{body}", body)


GAPS = {"any": (0, "any gap"), "week": (7, "a week or more"), "month": (30, "a month or more")}
ECHO_KINDS = {"all": "every kind", "elaborates": "elaborates", "supports": "supports", "tension": "tension",
              "analogy": "analogous"}


def _arcs(found: list, first, last) -> str:
    """Every echo as an arc over the archive's time, from when the earlier card was first said to the later one."""
    width, base, top = 1000.0, 250.0, 240.0
    span = max((last - first).total_seconds(), 1.0)
    at = lambda t: round(8 + (width - 16) * (t - first).total_seconds() / span, 1)   # noqa: E731
    out = []
    for n, e in enumerate(found):
        x0, x1 = at(e["earlier"]["first_said"]), at(e["later"]["first_said"])
        r = max((x1 - x0) / 2, 1.5)
        out.append(f'<a href="#e{n}"><path pathLength="1" style="--d: {len(found) - 1 - n}" '
                   f'class="{" ".join(e["kinds"]) or "plain"}" d="M{x0},{base}A{r:.1f},'
                   f'{min(r, top):.1f} 0 0 1 {x1},{base}"><title>{_e(e["earlier"]["title"])} … '
                   f'{_e(e["later"]["title"])} ({_e(e["gap"])})</title></path></a>')
    ticks, t = [], first.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    while t <= last:
        if t >= first:
            ticks.append(f'<line x1="{at(t)}" x2="{at(t)}" y1="{base}" y2="{base + 6}"/>'
                         f'<text x="{at(t) + 4}" y="{base + 16}">{t.strftime("%b %Y")}</text>')
        t = (t.replace(day=28) + timedelta(days=4)).replace(day=1)
    return (f'<svg viewBox="0 0 {width:.0f} {base + 22:.0f}" role="img" aria-label="Each echo as an arc from when '
            f'the earlier thought was said to when the later one was"><line class="axis" x1="0" x2="{width:.0f}" '
            f'y1="{base}" y2="{base}"/>{"".join(ticks)}{"".join(out)}</svg>')


def echoes(conn, kind: str = "all", gap: str = "any", demo: bool = False) -> str:
    """What I came back to (docs/design/m6-emanation.md, Echoes): every connection between two cards said in
    different conversations, the earlier beside the later, with how far apart they were said, the connection's kinds
    and the judge's reason. Almost every capture founds its own cluster, so this is where recurrence shows. Newest
    first, by when the later one was said: nothing else orders it (decision 0007). `kind` and `gap` only narrow it.
    In demo mode a pair with a sensitive card is left out."""
    v = _views(demo)
    kind, gap = (kind if kind in ECHO_KINDS else "all"), (gap if gap in GAPS else "any")
    cards = {r["idea_id"]: r for r in _rows(conn, f"SELECT * FROM {v}cards WHERE first_said IS NOT NULL")
             if not (demo and r["redacted"])}
    said: dict = {}   # card -> its conversations, and its first quote
    for i, c, q in conn.execute(f"""SELECT idea_id, conversation_id, quote FROM {v}card_moments
                                    ORDER BY said_at, item_id, observation_id"""):
        convs, _ = said.setdefault(i, (set(), q))
        convs.add(c)
    found = []
    for a, b, kinds, direction, why, analogy in conn.execute(
            """SELECT l.a, l.b, l.kinds, j.direction, j.why, j.analogy FROM pub.card_links l
               LEFT JOIN pub.card_judgments j ON j.a = least(l.a, l.b) AND j.b = greatest(l.a, l.b)
               ORDER BY l.a, l.b"""):
        if a not in cards or b not in cards or said.get(a, (set(),))[0] & said.get(b, (set(),))[0]:
            continue
        earlier, later = sorted((cards[a], cards[b]), key=lambda r: (r["first_said"], r["idea_id"]))
        days = (later["first_said"] - earlier["first_said"]).days
        if days < GAPS[gap][0] or not (kind == "all" or kind in kinds or (kind == "analogy" and analogy)):
            continue
        how = []
        for k in kinds:   # a>b reads "the lower id supports (or elaborates) the higher"
            subject = min(a, b) if direction == "a>b" else max(a, b) if direction == "b>a" else None
            how.append(k if subject is None or k == "tension" else
                       f"the later {k} the earlier" if subject == later["idea_id"] else f"the earlier {k} the later")
        found.append({"earlier": earlier, "later": later, "kinds": kinds, "why": why, "analogy": analogy,
                      "gap": _apart(later["first_said"], earlier["first_said"]), "how": ", ".join(how) or "connected"})
    found.sort(key=lambda e: (e["later"]["first_said"], e["later"]["idea_id"], e["earlier"]["idea_id"]), reverse=True)

    def side(r: dict) -> str:
        quote = said.get(r["idea_id"], (None, None))[1]
        return (f'<a class="side" href="/crossroads/{r["idea_id"]}"><small>{_when(r["first_said"])}</small>'
                f'<b>{_e(r["title"])}</b>' + (f"<q>{_e(quote)}</q>" if quote else "") + "</a>")

    def chips(name: str, options: dict, current: str, other: str) -> str:
        return "".join(f'<a href="/echoes?{name}={k}&{other}"{" aria-current=true" if k == current else ""}>'
                       f'{_e(label if isinstance(label, str) else label[1])}</a>' for k, label in options.items())

    rows = "".join(
        f'<li id="e{n}" class="{" ".join(e["kinds"]) or "plain"}">{side(e["earlier"])}'
        f'<div class="gap"><b>{_e(e["gap"])}</b><small>{_e(e["how"])}</small></div>{side(e["later"])}'
        + (f'<p>{_e(e["why"])}' + (f' <i>Same structure: {_e(e["analogy"])}</i>' if e["analogy"] else "") + "</p>"
           if e["why"] or e["analogy"] else "") + "</li>" for n, e in enumerate(found))
    times = [r["first_said"] for r in cards.values()]
    body = (f'<h1>Echoes</h1><p class="lede">{len(found)} connection{"s" * (len(found) != 1)} between thoughts said '
            f'in different conversations: what I came back to. Newest first.</p>'
            f'<nav>{chips("kind", ECHO_KINDS, kind, f"gap={gap}")}<span></span>'
            f'{chips("gap", GAPS, gap, f"kind={kind}")}</nav>'
            + (_arcs(found, min(times), max(times)) if found else "")
            + (f"<ol>{rows}</ol>" if found else "<p>Nothing here.</p>"))
    return _ECHOES.replace("{body}", body)


THREAD_CARDS, THREAD_CONVERSATIONS = 3, 2   # the least a thread holds, and the least it runs through


def threads(conn, demo: bool = False) -> str:
    """What runs through the conversations (docs/design/m6-emanation.md, Threads): each graph neighbourhood
    (lens.neighbourhoods, over the connections) of at least THREAD_CARDS cards said in at least THREAD_CONVERSATIONS
    conversations, as a row across every conversation in the order they began, with a knot where it was said. A
    thread is named by the first card said in it, and they come in the order they began: nothing ranks them
    (decision 0007). In demo mode a sensitive card is in no thread."""
    from im import lens

    v = _views(demo)
    cards = {str(r["idea_id"]): r for r in _rows(
        conn, f"SELECT c.*, p.x, p.y FROM {v}cards c LEFT JOIN pub.card_positions p USING (idea_id)")
        if not (demo and r["redacted"])}
    said: dict = defaultdict(list)   # card -> its moments, in the order said
    columns: dict = {}               # conversation -> its column, in the order they began; and when that was
    for i, c, t, quote in conn.execute(f"""SELECT idea_id::text, conversation_id, said_at, quote FROM {v}card_moments
                                           WHERE conversation_id IS NOT NULL AND said_at IS NOT NULL
                                           ORDER BY said_at, conversation_id, item_id, observation_id"""):
        if i in cards:
            said[i].append((t, columns.setdefault(c, (len(columns), t))[0], quote))
    placed = [(float(r["x"]), -float(r["y"])) for r in cards.values() if r["x"] is not None]
    mx, my = (sum(p[0] for p in placed) / len(placed), sum(p[1] for p in placed) / len(placed)) if placed else (0, 0)
    pairs = [(a, b) for a, b in conn.execute("SELECT a::text, b::text FROM pub.card_links") if a in said and b in said]
    found = []
    for group in lens.neighbourhoods(pairs):
        order = sorted(group, key=lambda i: (said[i][0][0], i))
        knots: dict = defaultdict(list)   # column -> the thread's cards said in that conversation, first said first
        for i in order:
            for _, column, _ in said[i]:
                if i not in knots[column]:
                    knots[column].append(i)
        if len(group) < THREAD_CARDS or len(knots) < THREAD_CONVERSATIONS:
            continue
        at = [(float(cards[i]["x"]), -float(cards[i]["y"])) for i in group if cards[i]["x"] is not None]
        # The sky's color: the direction from the map's center, here of the thread's middle.
        hue = round(math.degrees(math.atan2(sum(p[1] for p in at) / len(at) - my,
                                            sum(p[0] for p in at) / len(at) - mx)) % 360) if at else 0
        found.append({"cards": order, "knots": knots, "hue": hue, "began": said[order[0]][0][0],
                      "ended": max(m[0] for i in group for m in said[i])})
    found.sort(key=lambda t: (t["began"], t["cards"][0]))
    step, n = 14, max(len(columns), 1)
    months, last = [], None
    for column, t in sorted(columns.values()):
        if (t.year, t.month) != last:
            last = (t.year, t.month)
            if months and column - months[-1][0] < 3:   # two months starting side by side: the later one's name
                months.pop()
            months.append((column, f'<line x1="{column * step}" x2="{column * step}" y1="0" y2="22"/>'
                                   f'<text x="{column * step + 3}" y="10">{t.strftime("%b")}</text>'))
    rows = []
    for t in found:
        first, cols = cards[t["cards"][0]], sorted(t["knots"])
        knots = "".join(
            f'<a href="/trails?at={t["knots"][c][0]}"><circle cx="{c * step + step / 2}" cy="11" '
            f'r="{min(2 + 1.6 * len(t["knots"][c]) ** .5, 9):.1f}"><title>{len(t["knots"][c])} said here: '
            f'{_e("; ".join(cards[i]["title"] or "" for i in t["knots"][c][:4]))}</title></circle></a>' for c in cols)
        listed = "".join(
            f'<li><a href="/crossroads/{i}"><small>{_when(said[i][0][0])}</small><b>{_e(cards[i]["title"])}</b>'
            f'<q>{_e(said[i][0][2])}</q></a></li>' for i in t["cards"])
        rows.append(
            f'<details style="--hue: {t["hue"]}"><summary><span><b>{_e(first["title"])}</b><small>{len(t["cards"])} '
            f'cards · {len(cols)} conversations · over {_apart(t["ended"], t["began"])}</small></span>'
            f'<svg viewBox="0 0 {n * step} 22" preserveAspectRatio="none"><line x1="{cols[0] * step + step / 2}" '
            f'x2="{cols[-1] * step + step / 2}" y1="11" y2="11"/></svg>'
            f'<svg viewBox="0 0 {n * step} 22">{knots}</svg></summary><p class="on">Light its cards '
            f'<a href="/sky?lit={",".join(t["cards"])}">on the sky</a> '
            f'<a href="/tree?lit={",".join(t["cards"])}">on the tree</a></p><ol>{listed}</ol></details>')
    body = (f'<h1>Threads</h1><p class="lede">{len(found)} thread{"s" * (len(found) != 1)}: neighborhoods of connected '
            f'thoughts that run through more than one conversation. Each row crosses all {len(columns)} conversations '
            f'in order, with a knot where the thread was said; it&#x27;s named by the first thing said in it. '
            f'In the order they began.</p>'
            + (f'<div class="loom"><div class="months"><span></span><svg viewBox="0 0 {n * step} 22">'
               f'{"".join(m for _, m in months)}</svg></div>{"".join(rows)}</div>' if found else "<p>Nothing here.</p>"))
    return _THREADS.replace("{body}", body)


def apart(conn, page: int = 0, demo: bool = False) -> str:
    """The negative space (docs/design/m6-emanation.md, Apart): pairs of cards near enough by meaning to be asked
    about, that the connection judge kept apart (a `none` in pub.card_judgments), side by side with its reason. A
    shared subject is no connection (decision 0010), and the reason says where the judge drew the line. Newest
    first, by when the later one was said. Nothing here is a connection, and nothing can be made one from this
    page. In demo mode a pair with a sensitive card is left out."""
    v = _views(demo)
    cards = {r["idea_id"]: r for r in _rows(conn, f"SELECT * FROM {v}cards") if not (demo and r["redacted"])}
    found = []
    for a, b, why in conn.execute("SELECT a, b, why FROM pub.card_judgments WHERE verdict = 'none' ORDER BY a, b"):
        if a in cards and b in cards:
            earlier, later = sorted((cards[a], cards[b]), key=lambda r: (r["first_said"] or r["last_seen"], r["idea_id"]))
            found.append((earlier, later, why))
    found.sort(key=lambda e: (e[1]["first_said"] or e[1]["last_seen"], e[1]["idea_id"], e[0]["idea_id"]), reverse=True)
    shown = found[page * PAGE:(page + 1) * PAGE]
    said: dict = {}   # card -> its conversations, and its first quote
    for i, c, q in conn.execute(f"""SELECT idea_id, conversation_id, quote FROM {v}card_moments WHERE idea_id = ANY(%s)
                                    ORDER BY said_at, item_id, observation_id""",
                                ([r["idea_id"] for e in shown for r in e[:2]],)):
        said.setdefault(i, (set(), q))[0].add(c)

    def side(r: dict) -> str:
        quote = said.get(r["idea_id"], (None, None))[1]
        return (f'<a class="side" href="/crossroads/{r["idea_id"]}"><small>{_when(r["first_said"])}</small>'
                f'<b>{_e(r["title"])}</b>' + (f"<q>{_e(quote)}</q>" if quote else "") + "</a>")

    def gap(earlier: dict, later: dict) -> str:
        if said.get(earlier["idea_id"], (set(),))[0] & said.get(later["idea_id"], (set(),))[0]:
            return "same conversation"
        both = earlier["first_said"] and later["first_said"]
        return f"{_apart(later['first_said'], earlier['first_said'])} apart" if both else "—"

    rows = "".join(f'<li class="apart">{side(earlier)}<div class="gap"><b>{_e(gap(earlier, later))}</b>'
                   f'<small>kept apart</small></div>{side(later)}' + (f"<p>{_e(why)}</p>" if why else "") + "</li>"
                   for earlier, later, why in shown)
    turn = " ".join(f'<a href="/apart?page={n}">{name}</a>' for n, name, there in (
        (page - 1, "newer", page > 0), (page + 1, "older", len(found) > (page + 1) * PAGE)) if there)
    body = (f'<h1>Apart</h1><p class="lede">{len(found)} pair{"s" * (len(found) != 1)} near by meaning that the '
            f'connection judge looked at and kept apart, each with its reason. A shared subject is no connection. '
            f'Newest first.</p>' + (f"<ol>{rows}</ol><nav>{turn}</nav>" if shown else "<p>Nothing here.</p>"))
    return _ECHOES.replace("<title>Echoes</title>", "<title>Apart</title>").replace("{body}", body)


def anywhere(conn, demo: bool = False) -> str | None:
    """A card picked at random, for a crossroads to start from: nothing chooses it (decision 0007)."""
    row = conn.execute(f"""SELECT idea_id::text FROM {_views(demo)}cards c
                           WHERE true {"AND NOT c.redacted" if demo else ""} ORDER BY random() LIMIT 1""").fetchone()
    return row[0] if row else None


# Where each of the Tree's paths runs, counted from 1 along lens.TREE_PLACES's sephirot (tree.html, PATHS).
TREE_ENDS = ((1, 2), (1, 3), (1, 6), (2, 3), (2, 6), (2, 4), (3, 6), (3, 5), (4, 5), (4, 6), (4, 7), (5, 6), (5, 8),
             (6, 7), (6, 9), (6, 8), (7, 8), (7, 9), (7, 10), (8, 9), (8, 10), (9, 10))


def _place_name(drawing: str, place: str) -> str:
    """A board's place as the page names it: a sephira, a path with its letter and ends, or a numbered cell."""
    from im import lens

    if drawing == "grid":
        return f"Cell {place}"
    n = lens.TREE_PLACES.index(place)
    if n < 10:
        return place.capitalize()
    a, b = TREE_ENDS[n - 10]
    return (f"Path {n + 1}, {place.capitalize()} ({lens.TREE_PLACES[a - 1].capitalize()} to "
            f"{lens.TREE_PLACES[b - 1].capitalize()})")


def _lens_fields(x: dict | None = None) -> str:
    """The fields that make or edit a lens: its name and description, its kind, and the filters an `all` or `jev`
    lens takes. A `jev` lens's questions are built offline and imported (`im lens import`), so its form keeps them.
    A filter left as it comes (every box ticked, either) admits everything."""
    from im import lens

    x = x or {"name": "", "description": "", "kind": "all", "filters": {}}
    f = x["filters"] or {}
    offered = (*lens.MADE_HERE, "jev") if x["kind"] == "jev" else lens.MADE_HERE
    kinds = "".join(f'<label><input type="radio" name="kind" value="{k}"{" checked" * (x["kind"] == k)}> {k}: '
                    f'{_e("keep its questions" if k == "jev" else lens.KINDS[k])}</label><br>' for k in offered)

    def boxes(key: str, values: tuple) -> str:
        return "".join(f'<label><input type="checkbox" name="{key}" value="{v}"'
                       f'{" checked" * (v in f.get(key, values))}> {v}</label> ' for v in values)

    def either(key: str, label: str, yes: str, no: str) -> str:
        now = {True: "yes", False: "no"}.get(f.get(key), "")
        return (f'<label>{label} <select name="{key}">' + "".join(
            f'<option value="{v}"{" selected" * (v == now)}>{says}</option>'
            for v, says in (("", "either"), ("yes", yes), ("no", no))) + "</select></label>")

    return (f'<p><label>Name <input name="name" maxlength="80" required value="{_e(x["name"])}"></label></p>'
            f'<p><label>Description<br><textarea name="description" rows="3" cols="40" maxlength="2000">'
            f'{_e(x["description"])}</textarea></label></p><p>{kinds}</p>'
            f'<fieldset><legend>Filters (an all or Jev lens)</legend>'
            f'<p>Maturity: {boxes("maturity", lens.MATURITIES)}</p><p>Kept: {boxes("kept_as", lens.KEPT)}</p>'
            f'<p>{either("said_by_me", "Who", "said by me", "not said by me")} '
            f'{either("sensitive", "Sensitive", "sensitive", "not sensitive")}</p><p><label>Said since '
            f'<input type="date" '
            f'name="said_since" value="{_e(f.get("said_since", ""))}"></label> <label>until <input type="date" '
            f'name="said_until" value="{_e(f.get("said_until", ""))}"></label></p></fieldset>')


def lens_payload(form: dict, lists: dict, kind_now: str | None = None) -> dict:
    """A lens's name, description, query and filters from its form (`lists`: every value of each field, for the
    boxes). A `jev` lens edited as `jev` keeps its query (none is sent). ValueError for no name, or a kind the form
    can't make."""
    from im import lens

    name, kind = form.get("name", "").strip(), form.get("kind", "all")
    if not name or kind not in lens.MADE_HERE and not (kind == "jev" and kind_now == "jev"):
        raise ValueError("a lens needs a name, and a kind made here (a Jev lens is imported: im lens import)")
    filters: dict = {}
    if kind != "none":
        for key, values in (("maturity", lens.MATURITIES), ("kept_as", lens.KEPT)):
            chosen = [v for v in values if v in lists.get(key, [])]
            if 0 < len(chosen) < len(values):
                filters[key] = chosen
        for key in ("said_by_me", "sensitive"):
            if form.get(key) in ("yes", "no"):
                filters[key] = form[key] == "yes"
        for key in ("said_since", "said_until"):
            if form.get(key, "").strip():
                filters[key] = form[key].strip()
    return ({"name": name, "description": form.get("description", "").strip(), "filters": filters}
            | ({} if kind == "jev" else {"query": {"kind": kind}}))


def _name_taken(conn, name: str, lens_id: str | None = None) -> bool:
    return conn.execute("SELECT 1 FROM pub.lenses WHERE lower(btrim(name)) = lower(btrim(%s)) AND lens_id::text <> %s",
                        (name, lens_id or "")).fetchone() is not None


def lenses_page(conn, demo: bool = False) -> str:
    """/lenses: every lens, those kept at hand first, with how many cards each holds; and, outside demo mode, a lens
    to make."""
    from im import lens

    items = "".join(
        f'<li><a href="/lens/{x["id"]}">{_e(x["name"])}</a> <small>{x["cards"]} card{"s" * (x["cards"] != 1)} · '
        f'{_e(lens.describe(x))} · {lens.STATES[x["state"]]}</small>'
        + (f'<p>{_e(x["description"])}</p>' if x["description"] else "") + "</li>" for x in lens.stored(conn, demo))
    make = "" if demo else (f'<h2>Make a lens</h2><form method="post" action="/lenses">{_lens_fields()}'
                            "<button>Make it</button></form>")
    return _page("Lenses", '<nav><a href="/">Feed</a><a href="/board/tree">the Tree board</a><a href="/board/grid">'
                           f'the Grid board</a></nav><h1>Lenses</h1><ol>{items or "<li>No lenses yet.</li>"}</ol>'
                           f'{make}')


def _in_by(member: dict) -> str:
    """Why a card is in a lens, in words: my pin, or the query, with its score where it gave one."""
    if member["why"] == "pin":
        return "pinned in"
    return "by its query" + ("" if member["score"] is None else f" at {member['score']:.2f}")


def lens_page(conn, lens_id: str, page: int = 0, demo: bool = False) -> str | None:
    """/lens/<id>: a lens, its cards and why each is in (my pin or its query), how much of the map it has judged and
    where it's placed; outside demo mode, to keep it at hand, archive it, edit it, and pin a card out or unpin it.
    None if there's no such lens."""
    from im import lens

    found = [x for x in lens.stored(conn, demo) if x["id"] == lens_id]
    if not found:
        return None
    x = found[0]
    cards = lens.members(conn, lens_id, demo)
    asked, of = lens.judged(conn, x, demo)
    where = ", ".join(f'<a href="/{d}">{_e(_place_name(d, p))} on the {d}</a>'
                      for d, p in conn.execute("""SELECT b.drawing, p.place FROM pub.board_lenses p
                                                  JOIN pub.boards b USING (board_id) WHERE p.lens_id = %s
                                                  ORDER BY b.drawing, p.place""", (lens_id,)))
    body = [f'<nav><a href="/lenses">Lenses</a></nav><h1>{_e(x["name"])}</h1>',
            f'<p>{_e(x["description"])}</p>' if x["description"] else "",
            f'<small>{_e(lens.describe(x))} · {lens.STATES[x["state"]]} · judged {asked:,} of {of:,} · '
            f'{len(cards)} card{"s" * (len(cards) != 1)}</small>',
            f'<p><a href="/sky?lens={lens_id}">On the sky</a>' + (f" · {where}" if where else "") + "</p>"]
    if x["kind"] == "jev":
        body.append("<details><summary>its questions</summary><ol>" + "".join(
            f"<li>{_e(q)}" + (f"<br><small>yes: {_e(c['true'])} · no: {_e(c['false'])}</small>" if c else "") + "</li>"
            for q, c in x["query"]["deciding"].values()) + "</ol></details>")
    if not demo:
        states = "".join(f'<button name="state" value="{s}">{says}</button>'
                         for s, says in (("pinned", "keep at hand"), ("active", "make active"), ("archived", "archive"))
                         if s != x["state"])
        body += [f'<form method="post" action="/lens/{lens_id}"><input type="hidden" name="action" value="state">'
                 f'{states}</form>',
                 f'<details><summary>edit</summary><form method="post" action="/lens/{lens_id}"><input type="hidden" '
                 f'name="action" value="edit">{_lens_fields(x)}<button>Save</button></form></details>']

    def unpick(idea: str, action: str, says: str) -> str:
        return ("" if demo else f'<form method="post" action="/lens/{lens_id}"><input type="hidden" name="idea" '
                f'value="{idea}"><button name="action" value="{action}">{says}</button></form>')

    shown = cards[page * PAGE:(page + 1) * PAGE]
    body.append("<ol>" + "".join(
        f'<li><a href="/idea/{c["idea_id"]}">{_e(c["title"])}</a><p>{_e(c["statement"])}</p><small>{_e(_facts(c))} · '
        f'{_in_by(c)}</small>'
        + unpick(c["idea_id"], "unpin" if c["why"] == "pin" else "pin_out", "unpin" if c["why"] == "pin" else "pin out")
        + "</li>" for c in shown) + "</ol>" if shown else "<p>No cards in it.</p>")
    if len(cards) > (page + 1) * PAGE:
        body.append(f'<a href="/lens/{lens_id}?page={page + 1}">more</a>')
    if not demo:
        out = _rows(conn, """SELECT c.idea_id, c.title FROM pub.lens_pins p JOIN pub.cards c USING (idea_id)
                             WHERE p.lens_id = %s AND p.pin = 'out' ORDER BY c.title""", (lens_id,))
        body.append("<h2>Pinned out</h2><ul>" + "".join(
            f'<li><a href="/idea/{c["idea_id"]}">{_e(c["title"])}</a>{unpick(c["idea_id"], "unpin", "unpin")}</li>'
            for c in out) + "</ul>" if out else "")
    return _page(x["name"], "".join(body))


def lens_act(conn, lens_id: str, form: dict, lists: dict) -> None:
    """A lens's form, sent as my event: keep it at hand, make it active or archive it; edit it; pin a card in or out
    of it, or unpin one. ValueError for anything else, or a name another lens has."""
    from im import lens

    action = form.get("action")
    if action == "state" and form.get("state") in lens.STATES:
        lens.record(conn, "lens_state", {"lens_id": lens_id, "state": form["state"]})
    elif action == "edit":
        now = conn.execute("SELECT kind FROM pub.lenses WHERE lens_id = %s", (lens_id,)).fetchone()
        payload = lens_payload(form, lists, now[0] if now else None)
        if _name_taken(conn, payload["name"], lens_id):
            raise ValueError("another lens has that name")
        lens.record(conn, "lens_edit", {"lens_id": lens_id} | payload)
    elif action in ("pin_in", "pin_out", "unpin") and _UUID.match(form.get("idea", "")):
        if action == "unpin":
            lens.record(conn, "lens_unpin", {"lens_id": lens_id}, form["idea"])
        else:
            lens.record(conn, "lens_pin", {"lens_id": lens_id, "pin": action.removeprefix("pin_")}, form["idea"])
    else:
        raise ValueError("not a lens action")


def board_page(conn, drawing: str, demo: bool = False) -> str | None:
    """/board/tree, /board/grid: a board's places and the lens each holds; outside demo mode, a lens to put in each,
    or none to leave its stand-in. The Grid has as many cells as I fill, and one more. None for another drawing."""
    from im import lens

    if drawing not in ("tree", "grid"):
        return None
    held = lens.places(conn, drawing)
    if drawing == "tree":
        spots = list(lens.TREE_PLACES)
    else:
        taken = sorted(int(p) for p in held)
        spots = [str(n) for n in taken] + [str(max(taken, default=0) + 1)]
    choices = [x for x in lens.stored(conn, demo) if x["state"] != "archived"]

    def pick(place: str) -> str:
        now = held.get(place, {}).get("id", "")
        options = '<option value="">a stand-in</option>' + "".join(
            f'<option value="{x["id"]}"{" selected" * (x["id"] == now)}>{_e(x["name"])}</option>' for x in choices)
        return (f'<form method="post" action="/board/{drawing}"><input type="hidden" name="place" value="{place}">'
                f'<select name="lens" aria-label="lens">{options}</select> <button>Put</button></form>')

    rows = "".join(
        f'<li><b>{_e(_place_name(drawing, p))}</b> <small>'
        + (f'<a href="/lens/{held[p]["id"]}">{_e(held[p]["name"])}</a>' if p in held else "a stand-in")
        + "</small>" + ("" if demo else pick(p)) + "</li>" for p in spots)
    return _page(f"The {drawing} board", f'<nav><a href="/{drawing}">The {drawing}</a><a href="/lenses">Lenses</a>'
                                         f'</nav><h1>The {drawing} board</h1><ol>{rows}</ol>')


def place(conn, drawing: str, spot: str, lens_id: str) -> None:
    """Put a lens in a board's place, or none (`lens_id` empty) to leave the place's stand-in. ValueError for a place
    the drawing hasn't."""
    from im import lens

    if not (drawing == "tree" and spot in lens.TREE_PLACES
            or drawing == "grid" and re.fullmatch(r"[1-9]\d{0,2}", spot)):
        raise ValueError("no such place")
    board = lens.board(conn, drawing)
    if lens_id:
        lens.record(conn, "board_place", {"board_id": board, "place": spot, "lens_id": lens_id})
    else:
        lens.record(conn, "board_remove", {"board_id": board, "place": spot})


def act(conn, idea_id: str, action: str, reason: str | None = None, of: str | None = None) -> None:
    """Write a card action as my feedback event. A dismissal may say why (feedback.DismissReason), and a duplicate
    which card it duplicates (`of`); neither is asked for. ValueError for a reason on anything but a dismissal, one
    not in the list, or an `of` that isn't another card on a duplicate."""
    kind, payload = ACTIONS[action]
    if reason is not None or of is not None:
        if action != "dismiss" or reason not in DismissReason or of is not None and (
                reason != DismissReason.DUPLICATE or not _UUID.match(of) or of == idea_id
                or not conn.execute("SELECT 1 FROM pub.cards WHERE idea_id = %s", (of,)).fetchone()):
            raise ValueError("not a reason for a dismissal")
        payload = json.dumps({"reason": reason} | ({"of": of} if of else {}))
    conn.execute("INSERT INTO pub.feedback_events (kind, idea_id, payload, source) VALUES (%s, %s, %s, 'emanation')",
                 (kind, idea_id, payload))


def set_demo(conn, on: bool, source: str = "emanation") -> None:
    """Turn demo mode, the app's one setting, on or off: my feedback event, the latest of which wins."""
    conn.execute("INSERT INTO pub.feedback_events (kind, payload, source) VALUES (%s, '{}', %s)",
                 ("demo_on" if on else "demo_off", source))


def demo_page(on: bool, forced: bool, message: str = "") -> str:
    """/demo: turn demo mode on, or off with the token."""
    if forced:
        body = "<p>Demo mode is on, and this server was started with --demo: it can't be turned off here.</p>"
    elif on:
        body = ('<p>Demo mode is on for every page and the MCP server. Turning it off takes the token.</p>'
                f'<p>{_e(message)}</p><form method="post" action="/demo"><input type="hidden" name="action" '
                'value="off"><input type="password" name="token" autofocus> <button>Turn it off</button></form>')
    else:
        body = ("<p>Demo mode hides everything sensitive from every page and the MCP server, on every device, until "
                "it's turned off with the token.</p><form method=\"post\" action=\"/demo\"><input type=\"hidden\" "
                "name=\"action\" value=\"on\"><button>Turn it on</button></form>")
    return _page("Demo mode", body)



def _page(title: str, body: str) -> str:
    return f"""<!doctype html><html lang="en"><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>{_e(title)}</title>
<link rel="manifest" href="/manifest.webmanifest"><link rel="icon" href="/icon.svg" type="image/svg+xml">
<link rel="apple-touch-icon" href="/apple-touch-icon.png"><meta name="apple-mobile-web-app-title" content="Idea Machine">
<meta name="theme-color" content="#ffffff" media="(prefers-color-scheme: light)">
<meta name="theme-color" content="#161618" media="(prefers-color-scheme: dark)"><style>
:root {{ --bg: #fff; --fg: #1d1d1f; --muted: #6e6e73; --line: #e5e5ea; --surface: #f5f5f7; --accent: #0a58ca;
  --view-bg: #eef4ff; --view-line: #c9dcff; --warn-bg: #fff4e5; --warn-fg: #8a4b00; --warn-line: #f5c98b; }}
@media (prefers-color-scheme: dark) {{ :root {{ --bg: #161618; --fg: #ececf0; --muted: #9a9aa2; --line: #2c2c30;
  --surface: #202024; --accent: #7aa7ff; --view-bg: #172033; --view-line: #283b63; --warn-bg: #33240f;
  --warn-fg: #ffd28a; --warn-line: #66481d; }} }}
body {{ background: var(--bg); color: var(--fg); font: 16px/1.5 system-ui, sans-serif; max-width: 46rem;
  margin: 0 auto; padding: 1rem; }}
a {{ color: var(--accent); }} small {{ color: var(--muted); }}
nav {{ display: flex; flex-wrap: wrap; align-items: center; gap: .4rem; margin-bottom: .65rem; }}
nav > a:not(.btn):not(.pill) {{ display: inline-block; padding: .22rem .65rem; border-radius: 6px;
  background: var(--surface); border: 1px solid var(--line); color: var(--fg); text-decoration: none; font-size: .9rem; }}
nav > a:not(.btn):not(.pill):hover {{ border-color: var(--accent); color: var(--accent); }}
nav a[aria-current] {{ font-weight: 600; text-decoration: none; color: var(--fg); }}
.btn {{ display: inline-block; padding: .24rem .68rem; border-radius: 6px; text-decoration: none; font-size: .88rem;
  line-height: 1.35; border: 1px solid transparent; }}
.btn.view {{ background: var(--view-bg); border-color: var(--view-line); color: var(--accent); font-weight: 500; }}
.btn.view:hover {{ border-color: var(--accent); }}
.btn.trellis {{ background: var(--surface); border-color: var(--line); color: var(--fg); }}
.btn.trellis:hover {{ border-color: var(--accent); color: var(--accent); }}
.btn.mode {{ margin-left: auto; border: 1px dashed var(--line); color: var(--muted); font-size: .84rem; }}
.btn.mode:hover {{ border-color: var(--fg); color: var(--fg); }}
.btn.mode.on {{ border-style: solid; background: var(--warn-bg); border-color: var(--warn-line); color: var(--warn-fg);
  font-weight: 600; }}
.views .sep {{ width: 1px; height: 1.1rem; background: var(--line); margin: 0 .15rem; }}
.filters {{ padding-top: .2rem; padding-bottom: .35rem; border-bottom: 1px solid var(--line); margin-bottom: 1rem; }}
.pill {{ display: inline-block; padding: .2rem .75rem; border-radius: 999px; border: 1px solid var(--line);
  color: var(--fg); text-decoration: none; font-size: .88rem; }}
.pill:hover {{ border-color: var(--fg); }}
nav a.pill[aria-current] {{ background: var(--fg); color: var(--bg); border-color: var(--fg); font-weight: 600; }}
ol, ul {{ padding-left: 1.2rem; }} li {{ margin: 0 0 1rem; }} li p {{ margin: .2rem 0; }}
blockquote {{ margin: 0; padding-left: .8rem; border-left: 3px solid var(--line); }}
button {{ margin: 0 .4rem .4rem 0; font: inherit; padding: .3rem .8rem; }} select {{ font: inherit; }}
summary {{ color: var(--muted); cursor: pointer; margin-bottom: .4rem; }} details button {{ font-size: .9em; }}
header {{ display: flex; align-items: center; gap: .5rem; margin-bottom: .5rem; }}
header a {{ color: var(--fg); text-decoration: none; font-weight: 600; }}
.btn.view.has-skip {{ border-width: 1.5px; border-color: var(--accent); font-weight: 600; }}
.btn.disabled {{ opacity: .4; cursor: default; pointer-events: none; }}
.card-nav {{ display: grid; grid-template-columns: 1fr 1fr; gap: .6rem 1rem; margin-bottom: .25rem;
  align-items: start; }}
.nav-side {{ display: flex; flex-direction: column; gap: .25rem; min-width: 0; }}
.nav-next {{ align-items: flex-end; text-align: right; }}
.nav-btns {{ display: flex; flex-wrap: wrap; gap: .35rem; }}
.nav-next .nav-btns {{ justify-content: flex-end; }}
.nav-side small {{ font-size: .82rem; line-height: 1.35; overflow-wrap: anywhere; }}
.nav-skip {{ color: var(--accent); }}
.nav-hint {{ display: block; margin-bottom: .8rem; font-size: .78rem; }}
kbd {{ font: inherit; border: 1px solid var(--line); border-radius: 4px; padding: 0 .3rem; }}
.card-hero {{ --drop-bg: #fff1f2; --drop-border: #fecdd3; --drop-fg: #b42318;
  --keep-bg: #ecfdf3; --keep-border: #abefc6; --keep-fg: #067647; padding: 1rem 1.15rem;
  border: 1px solid var(--line); border-radius: 10px; background: var(--surface); margin-bottom: .9rem; }}
@media (prefers-color-scheme: dark) {{ .card-hero, .panel.curation, .card-nav, .card-section {{
  --drop-bg: #2a1216; --drop-border: #6e242c; --drop-fg: #fda4af;
  --keep-bg: #0f261b; --keep-border: #1d573a; --keep-fg: #6ce9a6; }} }}
.card-hero.dropped {{ background: var(--drop-bg); border-color: var(--drop-border); border-left: 4px solid var(--drop-fg); }}
.card-hero.kept {{ border-left: 4px solid var(--keep-fg); }}
.card-hero h1 {{ margin: .35rem 0 .45rem; font-size: 1.55rem; line-height: 1.25; }}
.card-hero .statement {{ margin: 0 0 .7rem; font-size: 1.05rem; }}
.badges {{ display: flex; flex-wrap: wrap; align-items: center; gap: .35rem; }}
.badge {{ display: inline-block; padding: .14rem .55rem; border-radius: 999px; font-size: .78rem; font-weight: 600;
  border: 1px solid var(--line); background: var(--bg); color: var(--fg); }}
.badge.dropped {{ background: var(--drop-fg); border-color: var(--drop-fg); color: #fff; }}
.badge.reason {{ background: var(--drop-bg); border-color: var(--drop-border); color: var(--drop-fg); font-weight: 500; }}
.badge.kept {{ background: var(--keep-bg); border-color: var(--keep-border); color: var(--keep-fg); }}
.badge.proposed {{ background: var(--warn-bg); border-color: var(--warn-line); color: var(--warn-fg); }}
.tag {{ display: inline-block; padding: .04rem .38rem; border-radius: 4px; font-size: .75rem; font-weight: 600;
  vertical-align: baseline; --drop-bg: #fff1f2; --drop-border: #fecdd3; --drop-fg: #b42318; }}
@media (prefers-color-scheme: dark) {{ .tag {{ --drop-bg: #2a1216; --drop-border: #6e242c; --drop-fg: #fda4af; }} }}
.tag.dropped {{ background: var(--drop-bg); border: 1px solid var(--drop-border); color: var(--drop-fg); }}
.meta-chips {{ display: flex; flex-wrap: wrap; gap: .35rem; margin-bottom: .75rem; }}
.chip {{ padding: .12rem .5rem; border-radius: 5px; background: var(--bg); border: 1px solid var(--line);
  font-size: .82rem; color: var(--muted); }}
.explore-links {{ display: flex; flex-wrap: wrap; gap: .35rem; margin: 0; }}
.panel.curation {{ --drop-bg: #fff1f2; --drop-border: #fecdd3; --drop-fg: #b42318;
  --keep-bg: #ecfdf3; --keep-border: #abefc6; --keep-fg: #067647; border: 1px solid var(--line); border-radius: 10px;
  padding: .8rem 1rem; margin-bottom: 1rem; display: flex; flex-direction: column; gap: .65rem; }}
.curate-row {{ display: flex; flex-direction: column; gap: .3rem; }}
.curate-label {{ font-size: .75rem; font-weight: 600; text-transform: uppercase; letter-spacing: .06em; color: var(--muted); }}
.curate-controls {{ display: flex; flex-direction: column; gap: .35rem; }}
.curate-btns {{ display: flex; flex-wrap: wrap; gap: .35rem; align-items: center; margin: 0; }}
.btn-act {{ margin: 0; padding: .28rem .65rem; border-radius: 6px; border: 1px solid var(--line); background: var(--surface);
  color: var(--fg); font-size: .88rem; cursor: pointer; }}
.btn-act:hover {{ border-color: var(--fg); }}
.btn-act.keep:hover, .btn-act.keep.is-current {{ background: var(--keep-bg); border-color: var(--keep-border);
  color: var(--keep-fg); font-weight: 600; }}
.btn-act.restore {{ background: var(--view-bg); border-color: var(--view-line); color: var(--accent); font-weight: 500; }}
.btn-act.drop {{ border-color: var(--drop-border); background: var(--bg); color: var(--drop-fg); }}
.btn-act.drop:hover {{ background: var(--drop-bg); }}
.btn-act.drop.subtle {{ color: var(--muted); border-color: var(--line); }}
.btn-act.drop.is-current {{ background: var(--drop-fg); border-color: var(--drop-fg); color: #fff; font-weight: 600; }}
.curate-btns select {{ padding: .24rem .4rem; border-radius: 6px; border: 1px solid var(--line); background: var(--bg);
  color: var(--fg); font-size: .86rem; max-width: 100%; }}
.card-section {{ margin: 1.15rem 0; padding-top: .85rem; border-top: 1px solid var(--line); }}
.card-section h2 {{ margin: 0 0 .55rem; font-size: .95rem; font-weight: 600; text-transform: uppercase;
  letter-spacing: .05em; color: var(--muted); display: flex; align-items: center; gap: .4rem; }}
.card-section .count {{ font-size: .78rem; padding: .05rem .42rem; border-radius: 999px; background: var(--surface);
  border: 1px solid var(--line); color: var(--fg); }}
.note-section p {{ margin: 0; padding: .55rem .8rem; background: var(--warn-bg); border: 1px solid var(--warn-line);
  border-radius: 6px; }}
.capture-gist {{ margin: .28rem 0; font-size: .9rem; color: var(--muted); }}
.diag-list {{ list-style: none; padding-left: 0; }}
.diag-list li {{ margin: 0 0 .55rem; }}
.rel-badge {{ display: inline-block; padding: .06rem .42rem; border-radius: 4px; background: var(--surface);
  border: 1px solid var(--line); font-size: .78rem; color: var(--muted); }}
.sim {{ font-size: .82rem; color: var(--muted); font-variant-numeric: tabular-nums; }}
.diag-note {{ margin: .18rem 0 0; font-size: .88rem; color: var(--muted); }}
.lens-form {{ display: flex; flex-wrap: wrap; gap: .35rem; align-items: center; margin-top: .5rem; }}
@media (max-width: 640px) {{ .card-nav {{ grid-template-columns: 1fr; }} .nav-next {{ align-items: flex-start; text-align: left; }}
  .nav-next .nav-btns {{ justify-content: flex-start; }} }}
</style><body><header><a href="/"><img src="/icon.svg" alt="" width="28" height="28" style="vertical-align: middle">
Idea Machine</a></header>{body}</body></html>"""


_LOGIN = _page("Log in", '<form method="post" action="/login"><input type="password" name="token" autofocus> '
                         '<button>Log in</button></form>')


_SKY = (files("im") / "sky.html").read_text()
_TRAILS = (files("im") / "trails.html").read_text()
_CROSSROADS = (files("im") / "crossroads.html").read_text()
_ECHOES = (files("im") / "echoes.html").read_text()
_THREADS = (files("im") / "threads.html").read_text()
_TREE = (files("im") / "tree.html").read_text()
_GRID = (files("im") / "grid.html").read_text()
_AUTHORIZE = (files("im") / "authorize.html").read_text()


class Handler(BaseHTTPRequestHandler):
    dsn = ""
    demo_only = False   # im web --demo: demo mode, and no way out of it

    def _demo(self, conn) -> bool:
        return self.demo_only or mcp.demo_on(conn)

    def _json(self, code: int, obj, headers: dict | None = None) -> None:
        self._send(code, json.dumps(obj), {"Cache-Control": "no-store"} | (headers or {}), "application/json")

    def _mcp(self) -> None:
        """MCP over Streamable HTTP (im.mcp) for tailnet clients: the web token as a bearer header."""
        given = (self.headers.get("Authorization") or "").removeprefix("Bearer ")
        if not (given and hmac.compare_digest(given.encode(), _token().encode())):
            return self._json(401, {"error": "bearer token required"}, {"WWW-Authenticate": "Bearer"})
        self._answer_mcp(writes=True)

    def _answer_mcp(self, writes: bool) -> None:
        """One MCP exchange, once the caller is authenticated: JSON-RPC, a batch or one message, answered with JSON.
        A browser page can't use it: a request with a foreign Origin is refused outright."""
        origin = self.headers.get("Origin")
        if origin and urlsplit(origin).netloc != self.headers.get("Host"):
            return self._json(403, {"error": "foreign origin"})
        n = min(int(self.headers.get("Content-Length") or 0), 65536)
        try:
            message = json.loads(self.rfile.read(n) or b"null")
        except ValueError:
            return self._json(400, mcp._error(None, -32700, "parse error"))
        connect = lambda: psycopg.connect(self.dsn, autocommit=True)  # noqa: E731
        handle = lambda m: mcp.handle(m, connect, writes, force_demo=self.demo_only)  # noqa: E731
        if isinstance(message, list):   # a batch (older protocol versions)
            answer = [a for a in map(handle, message) if a is not None]
        else:
            answer = handle(message)
        if answer is None or answer == []:   # notifications only
            return self._send(202, content_type="application/json")
        self._json(200, answer)

    def do_DELETE(self) -> None:   # MCP session teardown: there are no sessions
        self._json(405, {"error": "no sessions"})

    def _send(self, code: int, body: str = "", headers: dict | None = None,
              content_type: str = "text/html; charset=utf-8") -> None:
        data = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def _form(self, limit: int = 4096, keep_blank: bool = False) -> dict:
        """The first value of each field; every value of each is kept in `_lists`, for a form's checkboxes. Read up to
        `limit` bytes; `keep_blank` keeps empty fields, for a form where clearing one means something."""
        n = int(self.headers.get("Content-Length") or 0)
        self._lists = parse_qs(self.rfile.read(min(n, limit)).decode(), keep_blank_values=keep_blank)
        return {k: v[0] for k, v in self._lists.items()}

    def _send_static(self, path: str) -> None:
        data = files("im").joinpath("static", path.lstrip("/")).read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", STATIC[path])
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "public, max-age=86400")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        url = urlsplit(self.path)
        if url.path in STATIC:
            return self._send_static(url.path)
        if url.path == "/mcp":   # no server-initiated stream
            return self._json(405, {"error": "POST only"}, {"Allow": "POST"})
        if url.path == "/login":
            return self._send(200, _LOGIN)
        if not logged_in(self.headers.get("Cookie")):
            return self._send(303, headers={"Location": "/login"})
        q = {k: v[0] for k, v in parse_qs(url.query).items()}
        with psycopg.connect(self.dsn, autocommit=True) as conn:
            demo = self._demo(conn)
            if url.path == "/demo":
                return self._send(200, demo_page(demo, self.demo_only))
            if url.path == "/":
                page = int(q["page"]) if q.get("page", "").isdigit() else 0
                return self._send(200, feed(conn, q.get("show", "candidates"), page, demo))
            if url.path == "/sky":
                seed, by, con = q.get("seed"), q.get("lens"), q.get("constellation")
                ok = all(x is None or _UUID.match(x) for x in (seed, by, con))
                body = sky(conn, seed, demo, by, con) if ok else None
                return self._send(200, body) if body else self._send(404, _page("Not found", "Not found."))
            if url.path == "/sky/lens":   # the page lighting itself from another card, without reloading the map
                seed = q.get("seed", "")
                got = sky_lens(conn, seed, demo) if _UUID.match(seed) else None
                return self._send(200, json.dumps(got, separators=(",", ":")), content_type="application/json") \
                    if got else self._send(404, "{}", content_type="application/json")
            if url.path == "/trails":
                at = q.get("at")
                body = trails(conn, at, demo) if at is None or _UUID.match(at) else None
                return self._send(200, body) if body else self._send(404, _page("Not found", "Not found."))
            if url.path == "/echoes":
                return self._send(200, echoes(conn, q.get("kind", "all"), q.get("gap", "any"), demo))
            if url.path == "/apart":
                page = int(q["page"]) if q.get("page", "").isdigit() else 0
                return self._send(200, apart(conn, page, demo))
            if url.path == "/threads":
                return self._send(200, threads(conn, demo))
            if url.path == "/tree":
                return self._send(200, tree(conn, demo))
            if url.path == "/grid":
                return self._send(200, grid(conn, demo))
            if url.path == "/lenses":
                return self._send(200, lenses_page(conn, demo))
            if url.path in ("/board/tree", "/board/grid"):
                return self._send(200, board_page(conn, url.path.removeprefix("/board/"), demo))
            if url.path == "/crossroads":   # anywhere: a card at random
                to = anywhere(conn, demo)
                return self._send(303, headers={"Location": f"/crossroads/{to}" if to else "/"})
            edit = re.fullmatch(r"/idea/([0-9a-f-]{36})/edit", url.path)
            if edit and _UUID.match(edit[1]):
                if demo:
                    return self._send(403, _page("Demo mode", "Nothing can be changed in demo mode."))
                body = edit_page(conn, edit[1])
                return self._send(200, body) if body else self._send(404, _page("Not found", "Not found."))
            m = re.fullmatch(r"/(idea|crossroads|lens)/([0-9a-f-]{36})", url.path)
            if m and m[1] == "lens" and _UUID.match(m[2]):
                page = int(q["page"]) if q.get("page", "").isdigit() else 0
                body = lens_page(conn, m[2], page, demo)
            else:
                body = (card if m[1] == "idea" else crossroads)(conn, m[2], demo) if m and _UUID.match(m[2]) else None
        self._send(200, body) if body else self._send(404, _page("Not found", "Not found."))

    def do_POST(self) -> None:
        url = urlsplit(self.path)
        if url.path == "/mcp":
            return self._mcp()
        edit = re.fullmatch(r"/idea/([0-9a-f-]{36})/edit", url.path)
        if edit and int(self.headers.get("Content-Length") or 0) > EDIT_FORM_MAX:
            return self._send(413, _page("Too long", "That's more than a card's edit page can hold."))
        form = self._form(EDIT_FORM_MAX, keep_blank=True) if edit else self._form()
        if url.path == "/login":
            if hmac.compare_digest(form.get("token", "").encode(), _token().encode()):
                secure = "; Secure" if self.headers.get("X-Forwarded-Proto") == "https" else ""
                return self._send(303, headers={"Location": "/", "Set-Cookie": f"{COOKIE}={_session()}; Path=/; "
                                                f"Max-Age=31536000; HttpOnly; SameSite=Strict{secure}"})
            return self._send(403, _LOGIN)
        if not logged_in(self.headers.get("Cookie")):
            return self._send(403, _LOGIN)
        if url.path == "/demo":
            with psycopg.connect(self.dsn, autocommit=True) as conn:
                if form.get("action") == "on":
                    set_demo(conn, True)
                elif form.get("action") == "off" and not self.demo_only and \
                        hmac.compare_digest(form.get("token", "").encode(), _token().encode()):
                    set_demo(conn, False)
                else:
                    return self._send(403, demo_page(self._demo(conn), self.demo_only, "That token didn't work."))
            return self._send(303, headers={"Location": "/"})
        if self._lens_writes(url.path, form):
            return
        if edit and _UUID.match(edit[1]):
            with psycopg.connect(self.dsn, autocommit=True) as conn:
                if self._demo(conn):
                    return self._send(403, _page("Demo mode", "Nothing can be changed in demo mode."))
                try:
                    correct(conn, edit[1], form)
                except ValueError:
                    return self._send(400, _page("Bad request", "Bad request."))
            return self._send(303, headers={"Location": f"/idea/{edit[1]}"})
        m = re.fullmatch(r"/idea/([0-9a-f-]{36})", url.path)
        if not (m and _UUID.match(m[1]) and form.get("action") in ACTIONS):
            return self._send(400, _page("Bad request", "Bad request."))
        with psycopg.connect(self.dsn, autocommit=True) as conn:
            if self._demo(conn):
                return self._send(403, _page("Demo mode", "Nothing can be kept or dismissed in demo mode."))
            try:
                act(conn, m[1], form["action"], form.get("reason"), form.get("of"))
            except ValueError:
                return self._send(400, _page("Bad request", "Bad request."))
        self._send(303, headers={"Location": f"/idea/{m[1]}"})

    LENS_FROM_CARD = {"lens_in": "pin_in", "lens_out": "pin_out", "lens_unpin": "unpin"}

    def _lens_writes(self, path: str, form: dict) -> bool:
        """A lens made or changed, a card pinned into or out of a lens from its page, or a board's place filled: sent as
        my events, then back to the page it came from. False, having done nothing, if the request is none of these."""
        from im import lens

        idea = re.fullmatch(r"/idea/([0-9a-f-]{36})", path)
        lens_path = re.fullmatch(r"/lens/([0-9a-f-]{36})", path)
        board = re.fullmatch(r"/board/(tree|grid)", path)
        from_card = idea and form.get("action") in self.LENS_FROM_CARD
        if not (path == "/lenses" or lens_path or board or from_card):
            return False
        with psycopg.connect(self.dsn, autocommit=True) as conn:
            if self._demo(conn):
                self._send(403, _page("Demo mode", "Nothing can be changed in demo mode."))
                return True
            try:
                if path == "/lenses":
                    payload = lens_payload(form, self._lists)
                    if _name_taken(conn, payload["name"]):
                        raise ValueError("another lens has that name")
                    made = lens.add(conn, payload["name"], payload["description"], payload["query"]["kind"],
                                    payload["filters"])
                    back = f"/lens/{made}" if made else "/lenses"
                elif lens_path and _UUID.match(lens_path[1]):
                    lens_act(conn, lens_path[1], form, self._lists)
                    back = f"/lens/{lens_path[1]}"
                elif board:
                    place(conn, board[1], form.get("place", ""), form.get("lens", ""))
                    back = f"/board/{board[1]}"
                elif from_card and _UUID.match(idea[1]) and _UUID.match(form.get("lens", "")):
                    lens_act(conn, form["lens"], {"action": self.LENS_FROM_CARD[form["action"]], "idea": idea[1]}, {})
                    back = f"/idea/{idea[1]}"
                else:
                    raise ValueError("bad request")
            except ValueError as e:
                self._send(400, _page("Bad request", f"<p>{_e(e)}.</p>"))
                return True
            except (psycopg.errors.CheckViolation, psycopg.errors.ForeignKeyViolation):
                self._send(400, _page("Bad request", "<p>That isn't a lens it can keep: check the dates.</p>"))
                return True
        self._send(303, headers={"Location": back})
        return True


class PublicHandler(Handler):
    """The MCP endpoint alone, for the clients that connect from their own clouds (claude.ai and the Claude apps,
    ChatGPT), reached through a Cloudflare tunnel at IM_PUBLIC_URL, with OAuth (im.oauth) in front of it. Nothing else
    is served here: no page, no login.

    Everything but the sign-in page must come from an allowed network (oauth.mcp_allow: Anthropic's range and
    ChatGPT's), by the client address Cloudflare reports; anything else gets an empty 404. The sign-in page is opened in
    my own browser, so it's open to any address, guarded by the web token and a cap on wrong guesses. Requests are
    never logged.
    """

    def log_message(self, fmt, *args) -> None:   # no client addresses in the journal
        pass

    def _base(self) -> str:
        return oauth.public_url()

    def _allowed(self) -> bool:
        return oauth.source_ok(self.headers.get("CF-Connecting-IP"))

    def _nothing(self) -> None:
        self._send(404, content_type="text/plain")

    def _oauth_error(self, e: oauth.OAuthError) -> None:
        self._json(e.status, {"error": e.error, "error_description": e.description})

    def _sign_in(self, code: int, message: str = "", request: str = "", client: str = "", host: str = "") -> None:
        page = (_AUTHORIZE.replace("/*MESSAGE*/", _e(message)).replace("/*REQUEST*/", _e(request))
                .replace("/*CLIENT*/", _e(client)).replace("/*HOST*/", _e(host)))
        # The sign-in page can't be framed (clickjacking) and loads nothing from elsewhere.
        self._send(code, page, {"X-Frame-Options": "DENY", "Referrer-Policy": "no-referrer",
                                "Cache-Control": "no-store",
                                "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; "
                                "form-action 'self' https://claude.ai https://claude.com https://chatgpt.com; "
                                "frame-ancestors 'none'"})

    def _back_to(self, uri: str, **params) -> None:
        params = {k: v for k, v in params.items() if v is not None} | {"iss": self._base()}   # RFC 9207, always
        self._send(303, headers={"Location": uri + ("&" if "?" in uri else "?") + urlencode(params)})

    def do_GET(self) -> None:
        path, base = urlsplit(self.path).path, self._base()
        if path == "/authorize":
            return self._authorize_page()
        if not self._allowed():
            return self._nothing()
        if path in ("/.well-known/oauth-protected-resource", "/.well-known/oauth-protected-resource/mcp"):
            return self._json(200, oauth.protected_resource(base))
        if path == "/.well-known/oauth-authorization-server":
            return self._json(200, oauth.authorization_server(base))
        if path == "/mcp":   # no server-initiated stream
            return self._json(405, {"error": "POST only"}, {"Allow": "POST"})
        self._nothing()

    def do_POST(self) -> None:
        path, base = urlsplit(self.path).path, self._base()
        if path == "/authorize":
            return self._authorize_submit()
        if not self._allowed():
            return self._nothing()
        if path == "/mcp":
            header = self.headers.get("Authorization")
            grant = oauth.access(header)
            if grant is None:
                challenge = oauth.challenge_header(base, "invalid_token" if header else None)
                return self._json(401, {"error": "sign in first"}, {"WWW-Authenticate": challenge})
            return self._answer_mcp(writes=oauth.WRITE in grant["scopes"])
        n = min(int(self.headers.get("Content-Length") or 0), 8192)
        body = self.rfile.read(n)
        try:
            if path == "/register":
                meta = json.loads(body or b"{}")
                return self._json(201, oauth.register(meta if isinstance(meta, dict) else {}))
            if path == "/token":
                form = {k: v[0] for k, v in parse_qs(body.decode(errors="replace")).items()}
                return self._json(200, oauth.token(base, form), {"Pragma": "no-cache"})
        except ValueError:
            return self._oauth_error(oauth.OAuthError("invalid_client_metadata", "JSON only"))
        except oauth.OAuthError as e:
            return self._oauth_error(e)
        self._nothing()

    def do_DELETE(self) -> None:
        self._nothing()

    def _authorize_page(self) -> None:
        q = {k: v[0] for k, v in parse_qs(urlsplit(self.path).query).items()}
        try:
            client, request = oauth.check_request(self._base(), q)
        except oauth.Redirect as r:
            return self._back_to(r.uri, error=r.error, error_description=r.description, state=r.state)
        except oauth.OAuthError as e:
            return self._sign_in(400, f"Can't sign in: {e.description}")
        self._sign_in(200, "", request, client["name"], urlsplit(q["redirect_uri"]).hostname)

    def _authorize_submit(self) -> None:
        form = self._form()
        if not oauth.login_allowed():
            return self._sign_in(429, "Too many wrong tokens this hour. Try again later.")
        r = oauth.verify("request", form.get("request", ""))
        if r is None:
            return self._sign_in(400, "This sign-in page expired. Start again from the app.")
        if not hmac.compare_digest(form.get("token", "").encode(), _token().encode()):
            oauth.login_failed()
            return self._sign_in(401, "That token didn't work.", form["request"], "the app",
                                 urlsplit(r["redirect"]).hostname)
        g = oauth.grant(form["request"], allow_writes=form.get("writes") == "on")
        self._back_to(g["redirect"], code=g["code"], state=g["state"])


def serve(host: str, port: int, demo: bool = False, mcp_port: int = 8791) -> None:
    """`demo`: demo mode for every page, with no way out of it. With IM_PUBLIC_URL set, the public MCP listener
    (PublicHandler) on `mcp_port` too."""
    try:
        Handler.dsn = os.environ[DATABASE_ENV]
    except KeyError:
        raise SystemExit(f"{DATABASE_ENV} is not set (role lattice_app; see .env.example)") from None
    _token()
    Handler.demo_only = demo
    public = oauth.public_url()   # fails fast on a bad URL or allow list
    if public:
        networks = oauth.mcp_allow()
        server = ThreadingHTTPServer((host, mcp_port), PublicHandler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        print(f"public MCP listener on http://{host}:{mcp_port}/, published as {public}/mcp (OAuth), for "
              f"{len(networks)} networks", flush=True)
    print(f"serving on http://{host}:{port}/" + (" in demo mode" if demo else ""), flush=True)
    ThreadingHTTPServer((host, port), Handler).serve_forever()
