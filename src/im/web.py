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
Life turned in three dimensions, and Grid (`/grid`), a cell a lens, a neighborhood standing in for each lens until
lenses exist. They only read.

Demo mode is the app's one setting (pub.app_settings): `/demo` turns it on from any device, and off again only with
the token, so whoever I hand the phone to can't; `im demo on|off` does it from the shell, and `im web --demo` forces
it on. While it's on, every page reads the `demo` views instead of `pub`'s, so nothing sensitive is there to show:
the sky keeps a sensitive cluster's star, blank, with its edges, and the lists (the feed, a card's quotes and
connections) leave it out. Nothing can be kept or dismissed in demo mode.

It also serves the MCP server (im.mcp) at /mcp, behind the same token as a bearer header, demo mode included.

It connects as its own role (IM_WEB_DATABASE_URL, role lattice_app: read pub, append feedback), never as
im_pipeline, and logs in with its token (IM_WEB_TOKEN), exchanged once per device for a signed cookie. Loopback only:
tailscale serve publishes it to the tailnet (install/nas.sh).
"""

import hashlib
import hmac
import html
import json
import math
import os
import re
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
from urllib.parse import parse_qs, urlsplit

import psycopg

from im import mcp
from im.feedback import DismissReason

DATABASE_ENV = "IM_WEB_DATABASE_URL"
TOKEN_ENV = "IM_WEB_TOKEN"
COOKIE = "emanation_session"
PAGE = 50
SHOW = {  # which maturities the feed shows, and the tab's name
    "candidates": (("candidate", "idea"), "candidates and ideas"),
    "ideas": (("idea",), "ideas"),
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


def feed(conn, show: str = "candidates", page: int = 0, demo: bool = False) -> str:
    maturities = SHOW.get(show, SHOW["candidates"])[0]
    rows = _rows(conn, f"""SELECT * FROM {_views(demo)}cards WHERE maturity = ANY(%s) {"AND NOT redacted" if demo else ""}
                           ORDER BY last_seen DESC, idea_id LIMIT %s OFFSET %s""",
                 (list(maturities), PAGE + 1, page * PAGE))
    tabs = " ".join(f'<a href="/?show={k}"{" aria-current=page" if k == show else ""}>{name}</a>'
                    for k, (_, name) in SHOW.items()) + ' <a href="/sky">sky</a> <a href="/trails">trails</a>' \
        + ' <a href="/echoes">echoes</a> <a href="/threads">threads</a> <a href="/apart">apart</a> <a href="/crossroads">crossroads</a> <a href="/tree">tree</a> <a href="/grid">grid</a>' \
        + f' <a href="/demo">{"leave demo mode" if demo else "demo mode"}</a>'
    cards = "".join(f'<li><a href="/idea/{r["idea_id"]}">{_e(r["title"])}</a><p>{_e(r["statement"])}</p>'
                    f'<small>{_e(_facts(r))}</small></li>' for r in rows[:PAGE])
    more = f'<a href="/?show={_e(show)}&page={page + 1}">older</a>' if len(rows) > PAGE else ""
    return _page("Idea Machine", f"<nav>{tabs}</nav><ol>{cards or '<li>Nothing here.</li>'}</ol>{more}")


def card(conn, idea_id: str, demo: bool = False) -> str | None:
    v = _views(demo)
    shown = "AND NOT c.redacted" if demo else ""
    rows = _rows(conn, f"SELECT * FROM {v}cards WHERE idea_id = %s", (idea_id,))
    if not rows:
        return None
    r = rows[0]
    if demo and r["redacted"]:
        return _page("Hidden", f"<h1>Hidden in demo mode</h1><small>{_e(_facts(r))}</small>")
    quotes = _rows(conn, f"""SELECT speaker, quote, said_at FROM {v}card_evidence WHERE idea_id = %s
                             ORDER BY said_at, item_id""", (idea_id,))
    links = _rows(conn, f"""SELECT l.kind, l.kinds, c.idea_id, c.title FROM pub.card_links l
                            JOIN {v}cards c ON c.idea_id = CASE WHEN l.a = %s THEN l.b ELSE l.a END
                            WHERE %s IN (l.a, l.b) {shown} ORDER BY l.kind, c.title""", (idea_id, idea_id))
    lineage = _rows(conn, f"""SELECT CASE WHEN l.from_id = %s THEN l.kind WHEN l.kind = 'evolves' THEN 'evolved by'
                                          ELSE 'synthesized in' END AS kind,
                                     c.idea_id, c.title FROM pub.card_lineage l
                              JOIN {v}cards c ON c.idea_id = CASE WHEN l.from_id = %s THEN l.to_id ELSE l.from_id END
                              WHERE %s IN (l.from_id, l.to_id) {shown} ORDER BY 1, c.title""",
                    (idea_id, idea_id, idea_id))
    near = _rows(conn, f"""SELECT c.idea_id, c.title, n.similarity FROM pub.card_neighbours(%s) n
                           JOIN {v}cards c USING (idea_id) WHERE true {shown} ORDER BY n.similarity DESC""",
                 (idea_id,))
    buttons = "".join(f'<button name="action" value="{a}">{a}</button>' for a in ACTIONS)
    body = [f"<h1>{_e(r['title'])}</h1><p>{_e(r['statement'])}</p><small>{_e(_facts(r))}</small>",
            "" if demo else f'<form method="post" action="/idea/{idea_id}">{buttons}</form>{_why(idea_id, near)}',
            f'<p><a href="/sky?seed={idea_id}">Light the sky from here</a> · '
            f'<a href="/trails?at={idea_id}">Its trail</a> · <a href="/crossroads/{idea_id}">Its crossroads</a></p>']
    if r["my_note"]:
        body.append(f"<h2>My note</h2><p>{_e(r['my_note'])}</p>")
    body.append("<h2>Said</h2><ol>" + "".join(
        f"<li><blockquote>{_e(q['quote'])}</blockquote><small>{_e(SPEAKERS.get(q['speaker'], q['speaker']))} · "
        f"{_when(q['said_at'])}</small></li>" for q in quotes) + "</ol>" if quotes else "")
    body.append("<h2>Connections</h2><ul>" + "".join(
        f'<li>{_e(", ".join([x["kind"], *x["kinds"]]))}: <a href="/idea/{x["idea_id"]}">{_e(x["title"])}</a></li>'
        for x in links) + "</ul>" if links else "")
    body.append("<h2>Lineage</h2><ul>" + "".join(
        f'<li>{_e(x["kind"])}: <a href="/idea/{x["idea_id"]}">{_e(x["title"])}</a></li>' for x in lineage)
        + "</ul>" if lineage else "")
    body.append("<h2>Near by meaning</h2><ul>" + "".join(
        f'<li><a href="/idea/{x["idea_id"]}">{_e(x["title"])}</a> <small>{x["similarity"]:.2f}</small></li>'
        for x in near) + "</ul>" if near else "")
    return _page(r["title"], "".join(body))


def _why(idea_id: str, near: list) -> str:
    """Dismissing, saying why: folded away beside the plain dismiss, one tap a reason. A duplicate may name which card,
    from those near by meaning."""
    form = f'<form method="post" action="/idea/{idea_id}"><input type="hidden" name="action" value="dismiss">'
    reasons = "".join(f'<button name="reason" value="{x}">{REASONS[x]}</button>'
                      for x in DismissReason if x != DismissReason.DUPLICATE)
    which = "".join(f'<option value="{x["idea_id"]}">{_e(x["title"])}</option>' for x in near)
    which = (f'<select name="of" aria-label="of which card"><option value="">of which? (optional)</option>{which}'
             "</select>") if near else ""
    duplicate = f'<button name="reason" value="{DismissReason.DUPLICATE}">{REASONS[DismissReason.DUPLICATE]}</button>'
    return (f"<details><summary>dismiss, saying why</summary>{form}{reasons}</form>"
            f"{form}{duplicate}{which}</form></details>")


SIZES = {"observation": 0, "candidate": 1, "idea": 2}   # a canonical idea is one bigger than a pinned one


def sky(conn, seed: str | None = None, demo: bool = False) -> str | None:
    """Every card at its place on the one map (decision 0005, amended). With a seed, a lens lights what's relevant to
    it; without one, the encodings light everything. None if the seed isn't on the map. In demo mode a sensitive
    card keeps its star, blank. The page loads the map once: lighting it from another card fetches only that view's
    relevance (sky_lens)."""
    v = _views(demo)
    rows = _rows(conn, f"""SELECT c.*, p.x, p.y FROM {v}cards c JOIN pub.card_positions p USING (idea_id)
                           ORDER BY c.idea_id""")
    index = {str(r["idea_id"]): n for n, r in enumerate(rows)}
    if seed is not None and seed not in index:
        return None
    now = datetime.now(timezone.utc)
    stars = [[str(r["idea_id"]), round(float(r["x"]), 4), round(float(r["y"]), 4),
              SIZES[r["maturity"]] + (r["kept_as"] == "canonical"), r["conversations"],
              max((now - r["last_seen"]).days, 0) if r["last_seen"] else 365, r["title"] or "", _facts(r),
              int(r["first_said"].timestamp()) if r["first_said"] else None]   # when first said, for replay
             for r in rows]
    links = sorted({tuple(sorted((index[a], index[b]))) for a, b in conn.execute(
        """SELECT a::text, b::text FROM pub.card_links UNION ALL SELECT from_id::text, to_id::text FROM pub.card_lineage""")
        if a in index and b in index and a != b})
    rel = _relevance(conn, seed, index) if seed else None
    data = json.dumps({"stars": stars, "links": links, "rel": rel, "seed": seed}, separators=(",", ":"))
    title = (rows[index[seed]]["title"] or "Sky") if seed else "Sky"
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
    lenses it's on, for the page to gather around the sephirot and the paths. There are no lenses yet (decision
    0013), so a graph neighbourhood stands in for each: the TREE_LENSES largest (lens.neighbourhoods), the largest on
    Kether. A card is also on another lens when TREE_ALSO of its connections lead into that one's neighbourhood, so
    the page has cards on several lenses to draw. Which neighbourhood is on which lens means nothing. `named` is the
    first card said in each. In demo mode a sensitive card is on no lens, blank, with no connections."""
    from im import lens

    rows = _rows(conn, f"SELECT * FROM {_views(demo)}cards ORDER BY idea_id")
    index = {str(r["idea_id"]): n for n, r in enumerate(rows) if not (demo and r["redacted"])}
    pairs = sorted({tuple(sorted((a, b))) for a, b in conn.execute("SELECT a::text, b::text FROM pub.card_links")
                    if a in index and b in index and a != b})
    near: dict = defaultdict(set)
    for a, b in pairs:
        near[a].add(b)
        near[b].add(a)
    on, named = {}, []
    for n, group in enumerate(lens.neighbourhoods(pairs)[:TREE_LENSES]):
        on.update((i, n) for i in group)
        first = min(group, key=lambda i: (rows[index[i]]["first_said"] is None, rows[index[i]]["first_said"], i))
        named.append(rows[index[first]]["title"] or "")

    def lenses(i: str) -> list[int]:
        if i not in on:
            return []
        into = Counter(on[n] for n in near[i] if n in on and on[n] != on[i])
        return [on[i], *sorted(n for n, k in into.items() if k >= TREE_ALSO)]

    stars = [[str(r["idea_id"]), r["title"] or "", lenses(str(r["idea_id"]))] for r in rows]
    order = {str(r["idea_id"]): n for n, r in enumerate(rows)}
    data = json.dumps({"stars": stars, "links": [[order[a], order[b]] for a, b in pairs], "named": named},
                      separators=(",", ":"))
    return _TREE.replace("{data}", data.replace("</", "<\\/"))


GRID_CARDS = 3   # the least a cell of the grid holds


def grid(conn, demo: bool = False) -> str:
    """A grid of lenses, the plainer Trellis board (docs/design/m6-emanation.md, Grid): one cell a lens, its cards
    drawn as a small constellation, each where the sky puts it relative to the others, with the connections among
    them. There are no lenses yet (decision 0013), so a graph neighbourhood of at least GRID_CARDS cards stands in for
    each (lens.neighbourhoods), named by the first card said in it, and nothing places a cell by hand: they come in
    the order they began. A star opens its card's crossroads; a cell's cards can be lit on the sky or the tree. In
    demo mode a sensitive card is in no cell."""
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
    cells = []
    for group in lens.neighbourhoods(pairs):
        if len(group) < GRID_CARDS:
            continue
        order, inside = sorted(group, key=when), set(group)
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
        cells.append((when(order[0]), (
            f'<section><svg viewBox="0 0 100 100" role="img" aria-label="{len(order)} connected cards">{lines}{stars}'
            f'</svg><b>{_e(cards[order[0]]["title"])}</b><small>{len(order)} cards · began '
            f'{_when(cards[order[0]]["first_said"])}</small><small><a href="/sky?lit={lit}">on the sky</a>'
            f'<a href="/tree?lit={lit}">on the tree</a></small></section>')))
    cells.sort(key=lambda c: c[0])
    body = (f'<h1>Grid</h1><p class="lede">{len(cells)} cell{"s" * (len(cells) != 1)}. There are no lenses yet, so each '
            f'cell holds a neighborhood of connected cards as a stand-in, named by the first thing said in it, in the '
            f'order they began. A star is a card, placed as the sky places it among the others.</p>'
            + (f'<div class="grid">{"".join(c for _, c in cells)}</div>' if cells else "<p>Nothing here.</p>"))
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
:root {{ --bg: #fff; --fg: #1d1d1f; --muted: #6e6e73; --line: #e5e5ea; --accent: #0a58ca; }}
@media (prefers-color-scheme: dark) {{ :root {{ --bg: #161618; --fg: #ececf0; --muted: #9a9aa2; --line: #2c2c30;
  --accent: #7aa7ff; }} }}
body {{ background: var(--bg); color: var(--fg); font: 16px/1.5 system-ui, sans-serif; max-width: 46rem;
  margin: 0 auto; padding: 1rem; }}
a {{ color: var(--accent); }} small {{ color: var(--muted); }} nav a {{ margin-right: 1rem; }}
nav a[aria-current] {{ font-weight: 600; text-decoration: none; color: var(--fg); }}
ol, ul {{ padding-left: 1.2rem; }} li {{ margin: 0 0 1rem; }} li p {{ margin: .2rem 0; }}
blockquote {{ margin: 0; padding-left: .8rem; border-left: 3px solid var(--line); }}
button {{ margin: 0 .4rem .4rem 0; font: inherit; padding: .3rem .8rem; }} select {{ font: inherit; }}
summary {{ color: var(--muted); cursor: pointer; margin-bottom: .4rem; }} details button {{ font-size: .9em; }}
header {{ display: flex; align-items: center; gap: .5rem; margin-bottom: .5rem; }}
header a {{ color: var(--fg); text-decoration: none; font-weight: 600; }}
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


class Handler(BaseHTTPRequestHandler):
    dsn = ""
    demo_only = False   # im web --demo: demo mode, and no way out of it

    def _demo(self, conn) -> bool:
        return self.demo_only or mcp.demo_on(conn)

    def _json(self, code: int, obj, headers: dict | None = None) -> None:
        self._send(code, json.dumps(obj), {"Cache-Control": "no-store"} | (headers or {}), "application/json")

    def _mcp(self) -> None:
        """MCP over Streamable HTTP (im.mcp): a bearer token, then JSON-RPC, a batch or one message, answered with
        JSON. A browser page can't use it: a request with a foreign Origin is refused outright."""
        given = (self.headers.get("Authorization") or "").removeprefix("Bearer ")
        if not (given and hmac.compare_digest(given.encode(), _token().encode())):
            return self._json(401, {"error": "bearer token required"}, {"WWW-Authenticate": "Bearer"})
        origin = self.headers.get("Origin")
        if origin and urlsplit(origin).netloc != self.headers.get("Host"):
            return self._json(403, {"error": "foreign origin"})
        n = min(int(self.headers.get("Content-Length") or 0), 65536)
        try:
            message = json.loads(self.rfile.read(n) or b"null")
        except ValueError:
            return self._json(400, mcp._error(None, -32700, "parse error"))
        connect = lambda: psycopg.connect(self.dsn, autocommit=True)  # noqa: E731
        if isinstance(message, list):   # a batch (older protocol versions)
            answer = [a for a in (mcp.handle(m, connect, force_demo=self.demo_only) for m in message) if a is not None]
        else:
            answer = mcp.handle(message, connect, force_demo=self.demo_only)
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

    def _form(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        return {k: v[0] for k, v in parse_qs(self.rfile.read(min(n, 4096)).decode()).items()}

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
                seed = q.get("seed")
                body = sky(conn, seed, demo) if seed is None or _UUID.match(seed) else None
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
            if url.path == "/crossroads":   # anywhere: a card at random
                to = anywhere(conn, demo)
                return self._send(303, headers={"Location": f"/crossroads/{to}" if to else "/"})
            m = re.fullmatch(r"/(idea|crossroads)/([0-9a-f-]{36})", url.path)
            body = (card if m[1] == "idea" else crossroads)(conn, m[2], demo) if m and _UUID.match(m[2]) else None
        self._send(200, body) if body else self._send(404, _page("Not found", "Not found."))

    def do_POST(self) -> None:
        url = urlsplit(self.path)
        if url.path == "/mcp":
            return self._mcp()
        form = self._form()
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


def serve(host: str, port: int, demo: bool = False) -> None:
    """`demo`: demo mode for every page, with no way out of it."""
    try:
        Handler.dsn = os.environ[DATABASE_ENV]
    except KeyError:
        raise SystemExit(f"{DATABASE_ENV} is not set (role lattice_app; see .env.example)") from None
    _token()
    Handler.demo_only = demo
    print(f"serving on http://{host}:{port}/" + (" in demo mode" if demo else ""), flush=True)
    ThreadingHTTPServer((host, port), Handler).serve_forever()
