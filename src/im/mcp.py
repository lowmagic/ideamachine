"""The MCP server (docs/design/m5-lenses-and-mcp.md, slice 1; decision 0005): Lattice's 19 tools, served by `im web`.

Their names, argument schemas and result shapes are the contract (SPEC §2), kept from Lattice's `mcp.py` (lattice
6def765); what they read is today's `pub`, as role lattice_app, and their one write is appending feedback events with
source 'mcp'. Streamable HTTP at /mcp, each JSON-RPC request answered with plain JSON (no SSE stream, no sessions),
behind IM_WEB_TOKEN as a bearer header (web.py).

What leaves through it follows policy B: quotes go out, but speakers are pseudonymized per answer ("me", "unknown",
then S1, S2... in order of first appearance).

Ideas aren't rated (decision 0007; the rubric's scores went in 053), so the tools that showed a score take the
shapes Lattice had for nothing scored: `get_idea` has no `ranking`, `draft_material` gives `score: null`, and
`best_ideas` lists the ideas I kept as canonical, most recently said first, or says there are none.

Demo mode is the app's one setting (pub.app_settings, `im demo`): while it's on, only the nine read tools Lattice's
demo served exist, what the web view's demo mode hides (pub.demo_hidden_ideas, and themes pub.sensitivity marks
sensitive) is withheld from every answer, and swearing is blacked out.
"""

import json
import re
import uuid
from datetime import datetime, timedelta, timezone
from importlib.metadata import version
from uuid import UUID

import psycopg

PROTOCOLS = ["2025-11-25", "2025-06-18", "2025-03-26"]   # newest first; the first is offered when the client's isn't
SERVER = {"name": "ideamachine", "title": "Idea Machine", "version": version("ideamachine")}
INSTRUCTIONS = """This is the owner's Idea Machine: when they mention Idea Machine, their lattice, their ideas or what
they've been thinking or saying lately, use these tools. It holds the thoughts Idea Machine captured from their
recorded conversations (and from what they once typed into a ChatGPT Idea Machine), grouped into clusters, connected
to each other, each backed by evidence quotes. Speak to the owner as "you". In quotes, "me" is the owner; S1, S2...
are other people, renumbered in every answer. Search or open a theme before asking the owner for ids. Starring,
linking and notes are the owner's feedback: do them when they ask, not on your own initiative.

When the owner wants to add an idea from your conversation: check similar_ideas with your draft (and search_ideas
for key words), and if an existing idea says the same, offer a note or a link instead. Otherwise draft a short title
and a one-to-three-sentence statement in the owner's own terms, read them back, and call add_idea only once they
approve the wording.

Themes are the owner's to shape. You may suggest renaming, merging or splitting themes, or filing an idea
elsewhere, when you notice a theme is muddled, two overlap, or an idea sits in the wrong one; but change them
(rename_theme, merge_themes, split_theme, file_idea) only once the owner agrees to the specific change.

Only the owner makes ideas, and Idea Machine doesn't rate them. When they want to see what's emerging or review,
call proposals, read each one to them, and record their own decision with keep_idea: canonical (it carries weight),
pinned (kept and visible, no weight), or dismiss. A star is a pin: it never adds weight.

To draft something from the lattice (an outline, essay, guide, talk), call draft_material for a theme, a digest
point or a set of ideas, then ask the owner what form, audience and length they want before writing. Write in
their voice from their ideas; quote them sparingly; say where ideas pull against each other rather than smoothing
it over; don't add claims they haven't made without marking them as yours."""


class ToolError(Exception):
    """A mistake the model can fix: reported as a tool result with isError, not a protocol error."""


class UnknownTool(Exception):
    pass


def _pseudonymize(rows: list[dict], key: str = "said_by", alias: dict | None = None) -> list[dict]:
    alias = {} if alias is None else alias   # shared across calls to keep one numbering in one answer
    for row in rows:
        who = row.get(key)
        if who not in ("me", "unknown", None):
            who = alias.setdefault(who, f"S{len(alias) + 1}")
        row[key] = who
    return rows


def _uuid(value, name: str = "idea_id") -> str:
    try:
        return str(UUID(str(value)))
    except ValueError:
        raise ToolError(f"{name} must be an idea id (a UUID) from search_ideas, open_theme or whats_new") from None


# --- Reading pub ---------------------------------------------------------------------------------------------

BRIEF = "idea_id, title, statement, themes, n_evidence, starred, created_at"


def _brief(row) -> dict:
    i, title, statement, themes, n, starred, made = row
    return {"id": str(i), "title": title, "statement": statement, "themes": list(themes), "evidence": n,
            "starred": starred, "created_at": made.isoformat()}


def _evidence(conn, idea_id: str) -> list[dict]:
    rows = conn.execute("""SELECT item_id, kind, said_by, quote, gist, confidence, said_at, verdict, relation, how
                           FROM pub.evidence WHERE idea_id = %s ORDER BY said_at""", (idea_id,)).fetchall()
    return [{"item_id": str(item), "kind": k, "said_by": who, "quote": q, "gist": g, "confidence": c,
             "said_at": at.isoformat(), "verdict": v, "relation": rel, "how": how}
            for item, k, who, q, g, c, at, v, rel, how in rows]


def _idea(conn, idea_id: str) -> dict | None:
    row = conn.execute("""SELECT title, statement, origin, archive_ref, notes, themes, starred, claude_title,
                                 claude_statement, my_note, corrected
                          FROM pub.ideas WHERE idea_id = %s""", (idea_id,)).fetchone()
    if row is None:
        return None
    title, statement, origin, ref, notes, themes, starred, claude_title, claude_statement, my_note, corrected = row
    return {"id": idea_id, "title": title, "statement": statement, "origin": origin, "ref": ref, "notes": notes,
            "themes": list(themes), "starred": starred, "evidence": _evidence(conn, idea_id), "corrected": corrected,
            "claude_title": claude_title, "claude_statement": claude_statement, "my_note": my_note}


def _digests(conn, week: str | None = None) -> list[dict]:
    rows = conn.execute(
        """SELECT digest_id, week_start, summary, points FROM pub.digests
           WHERE %(week)s::date IS NULL OR week_start = date_trunc('week', %(week)s::date)::date
           ORDER BY week_start DESC""", {"week": week}).fetchall()
    return [{"id": i, "week_start": w.isoformat(), "summary": s, "points": list(p or [])} for i, w, s, p in rows]


def _proposals(conn) -> list[dict]:
    return [{"id": str(i), "title": t, "statement": st, "weight": w, "proposed_at": m.isoformat()}
            for i, t, st, w, m in conn.execute("""SELECT idea_id, title, statement, weight, made_at FROM pub.proposals
                                                  ORDER BY weight DESC, made_at, idea_id""")]


# --- Feedback events -----------------------------------------------------------------------------------------

def record(conn, event: dict) -> int:
    """Append one feedback event as Lattice's data.record did, with source 'mcp'. ValueError for one Idea Machine
    wouldn't take."""
    kind, ref, other, theme, payload = event["kind"], None, None, None, {}
    if kind in ("rename_theme", "merge_theme", "split_theme"):
        ref = str(UUID(str(event.get("theme_id", ""))))
    else:
        ref = str(UUID(str(event.get("idea_id", ""))))
    if kind == "idea_note":   # context for the idea; empty clears it
        payload = {"note": str(event.get("note") or "").strip()[:1000]}
    elif kind == "idea_add":   # my own idea; idea_id is new, made here so a resend is the same idea
        payload = {k: str(event.get(k) or "").strip()[:limit] for k, limit in (("title", 120), ("statement", 1000))}
        if not all(payload.values()):
            raise ValueError("a new idea needs a title and a statement")
        if str(event.get("note") or "").strip():
            payload["note"] = str(event["note"]).strip()[:1000]
        links = event.get("link_to") or []
        if not isinstance(links, list) or len(links) > 10:
            raise ValueError("link_to: up to 10 idea ids")
        links = sorted({str(UUID(str(x))) for x in links} - {ref})
        if links:
            payload["link_to"] = links
    elif kind == "idea_keep":
        payload = {"as": event["as"]}
    elif kind == "rename_theme":
        payload = {"name": str(event.get("name") or "").strip()[:80]}
        if not payload["name"]:
            raise ValueError("a theme needs a name")
    elif kind == "merge_theme":   # theme_id is absorbed into `into`
        payload = {"into": str(UUID(str(event.get("into", ""))))}
        if payload["into"] == ref:
            raise ValueError("a theme can't merge into itself")
    elif kind == "split_theme":   # these ideas leave theme_id for a new theme of mine
        ideas = event.get("idea_ids")
        if not isinstance(ideas, list) or not 1 <= len(ideas) <= 500:
            raise ValueError("idea_ids: the ideas that move to the new theme")
        payload = {"name": str(event.get("name") or "").strip()[:80],
                   "idea_ids": sorted({str(UUID(str(x))) for x in ideas})}
        if not payload["name"]:
            raise ValueError("the new theme needs a name")
        payload["new_theme_id"] = str(UUID(str(event.get("new_theme_id", ""))))
    elif kind in ("file_idea", "unfile_idea"):
        theme = str(UUID(str(event.get("theme_id", ""))))
    if kind in ("link", "unlink"):   # links are undirected
        other = str(UUID(str(event.get("other_idea_id", ""))))
        if other == ref:
            raise ValueError("an idea can't link to itself")
    column = "theme_id" if kind in ("rename_theme", "merge_theme", "split_theme") else "idea_id"
    try:
        if theme:   # filing names both the idea and the theme, each in its own column
            return conn.execute("""INSERT INTO pub.feedback_events (kind, idea_id, theme_id, payload, source)
                                   VALUES (%s, %s, %s, '{}', 'mcp') RETURNING event_id""",
                                (kind, ref, theme)).fetchone()[0]
        return conn.execute(f"""INSERT INTO pub.feedback_events (kind, {column}, other_idea_id, payload, source)
                                VALUES (%s, %s, %s, %s, 'mcp') RETURNING event_id""",
                            (kind, ref, other, json.dumps(payload))).fetchone()[0]
    except (psycopg.errors.CheckViolation, psycopg.errors.ForeignKeyViolation) as e:
        raise ValueError(f"Idea Machine refused the event ({e.diag.constraint_name})") from None


# --- The tools -----------------------------------------------------------------------------------------------

def best_ideas(conn, limit: int = 10, theme: str | None = None) -> dict:
    """Not scored (decision 0007): the ideas I kept as canonical, most recently said first."""
    if not conn.execute("SELECT 1 FROM pub.cards WHERE kept_as = 'canonical' LIMIT 1").fetchone():
        raise ToolError("Idea Machine doesn't rate ideas, and the owner hasn't kept any as canonical yet; try "
                        "proposals, whats_new or search_ideas")
    limit = max(1, min(int(limit), 50))
    rows = conn.execute(
        f"""SELECT {", ".join("d." + c for c in BRIEF.split(", "))} FROM pub.ideas d JOIN pub.cards c USING (idea_id)
            WHERE c.kept_as = 'canonical'
            {"AND lower(%s) = ANY(SELECT lower(t) FROM unnest(d.themes) t)" if theme else ""}
            ORDER BY c.last_seen DESC, d.idea_id LIMIT %s""", [*([theme.strip()] if theme else []), limit]).fetchall()
    ideas = [_brief(r) | {"score": None, "why": "kept as canonical by the owner", "generality": None,
                          "novelty": None, "compression": None, "outside_value": None} for r in rows]
    return {"theme": theme, "ideas": ideas,
            "how_scored": "Not scored: Idea Machine doesn't rate ideas. These are the ideas the owner kept as "
                          "canonical, most recently said first."}


def search_ideas(conn, query: str, limit: int = 10) -> dict:
    words = [w for w in str(query).lower().split() if w][:8]
    if not words:
        raise ToolError("query is empty")
    limit = max(1, min(int(limit), 50))
    text = "lower(concat_ws(' ', title, statement, notes, my_note, array_to_string(themes, ' ')))"
    rows = conn.execute(
        f"""SELECT {BRIEF} FROM pub.ideas WHERE {" AND ".join(f"{text} LIKE %s" for _ in words)}
            ORDER BY (SELECT count(*) FROM unnest(%s::text[]) w WHERE lower(title) LIKE '%%' || w || '%%') DESC,
                     starred DESC, n_evidence DESC, title
            LIMIT %s""", [*(f"%{w}%" for w in words), words, limit]).fetchall()
    return {"query": query, "ideas": [_brief(r) for r in rows]}


def get_idea(conn, idea_id: str) -> dict:
    found = _idea(conn, _uuid(idea_id))
    if found is None:
        raise ToolError("no such idea (it may have been forgotten)")
    for ev in found["evidence"]:
        for k in ("item_id", "verdict", "relation", "confidence"):
            ev.pop(k, None)
    _pseudonymize(found["evidence"])
    found["connections"] = [
        {"id": str(other), "title": title, "kind": "your link" if kind == "mine" else kind}
        for other, title, kind in conn.execute(
            """SELECT CASE WHEN c.a = %(i)s THEN c.b ELSE c.a END AS other, d.title, c.kind FROM pub.connections c
               JOIN pub.ideas d ON d.idea_id = CASE WHEN c.a = %(i)s THEN c.b ELSE c.a END
               WHERE %(i)s IN (c.a, c.b) ORDER BY c.kind, d.title""", {"i": found["id"]})]
    if not found["corrected"]:
        found.pop("claude_title"), found.pop("claude_statement")
    return found


def list_themes(conn) -> dict:
    return {"themes": [{"id": str(i), "name": n, "description": d, "ideas": c,
                        "status": "pinned" if p else "proposed" if o == "clustered" else "unpinned"}
                       for i, n, d, o, p, c in conn.execute(
                           """SELECT theme_id, name, description, origin, pinned, n_ideas FROM pub.themes
                              ORDER BY pinned DESC, n_ideas DESC, name""")]}


def _theme(conn, theme: str) -> tuple:
    """(id, name, description) of the theme a reference names: its id, its exact name, or else part of its name."""
    row = conn.execute("""SELECT theme_id, name, description FROM pub.themes
                          WHERE theme_id::text = %(t)s OR lower(name) = lower(%(t)s)
                          ORDER BY theme_id::text = %(t)s DESC LIMIT 1""", {"t": str(theme).strip()}).fetchone()
    if row is None:
        rows = conn.execute("SELECT theme_id, name, description FROM pub.themes WHERE name ILIKE %s ORDER BY name",
                            (f"%{str(theme).strip()}%",)).fetchall()
        if len(rows) > 1:
            raise ToolError(f"{theme!r} matches several themes: {', '.join(r[1] for r in rows)}; use the full name")
        row = rows[0] if rows else None
    if row is None:
        raise ToolError(f"no theme matches {theme!r}; list_themes shows them all")
    return str(row[0]), row[1], row[2]


def open_theme(conn, theme: str) -> dict:
    tid, name, description = _theme(conn, theme)
    ideas = conn.execute(f"""SELECT {BRIEF} FROM pub.ideas WHERE %s = ANY(themes)
                             ORDER BY starred DESC, n_evidence DESC, title""", (name,)).fetchall()
    return {"id": str(tid), "name": name, "description": description, "ideas": [_brief(r) for r in ideas]}


MAX_DRAFT_IDEAS = 40


def _reading_order(ids: list[str], links: list[dict], weight: dict) -> tuple[list[list[str]], list[str]]:
    """Group connected ideas and walk each group so neighbours stay together: largest group first, each walked
    breadth-first from its best-connected idea (ties: weight, then id, so the order is stable). Ideas with no link
    in the set come back separately."""
    adj = {i: set() for i in ids}
    for link in links:
        adj[link["a"]].add(link["b"]), adj[link["b"]].add(link["a"])
    seen, groups = set(), []
    for start in sorted(ids, key=lambda i: (-len(adj[i]), -weight[i], i)):
        if start in seen or not adj[start]:
            continue
        walk, queue = [], [start]
        seen.add(start)
        while queue:
            i = queue.pop(0)
            walk.append(i)
            for j in sorted(adj[i] - seen, key=lambda j: (-len(adj[j]), -weight[j], j)):
                seen.add(j), queue.append(j)
        groups.append(walk)
    loose = sorted((i for i in ids if not adj[i]), key=lambda i: -weight[i])
    return sorted(groups, key=len, reverse=True), loose


def draft_material(conn, theme: str | None = None, digest_week: str | None = None, point: int | None = None,
                   idea_ids: list | None = None) -> dict:
    if sum(x is not None for x in (theme, point, idea_ids)) != 1:
        raise ToolError("give exactly one source: theme, a digest point (point, with digest_week for an older week), "
                        "or idea_ids")
    if theme is not None:
        t = open_theme(conn, theme)
        source = {"kind": "theme", "name": t["name"], "description": t["description"]}
        ids = [i["id"] for i in t["ideas"]]
    elif point is not None:
        d = weekly_digest(conn, digest_week)
        p = next((p for p in d["points"] if p["point"] == point), None)
        if p is None:
            raise ToolError(f"that digest has points 1 to {len(d['points'])}")
        source = {"kind": "digest point", "week_start": d["week_start"], "title": p["title"], "body": p["body"]}
        ids = [i["id"] for i in p["ideas"]]
    else:
        ids = list(dict.fromkeys(_uuid(i) for i in idea_ids))
        source = {"kind": "chosen ideas"}
    if not ids:
        raise ToolError("no ideas left in that source (they may have been forgotten or discarded)")
    alias: dict = {}
    ideas = {}
    for i in ids[:MAX_DRAFT_IDEAS]:
        found = _idea(conn, i)
        if found is None:
            continue
        quotes = [{"said_by": ev["said_by"], "said_at": ev["said_at"][:10], "quote": ev["quote"]}
                  for ev in found["evidence"] if ev["verdict"] != "discard"][:3]
        _pseudonymize(quotes, alias=alias)
        ideas[i] = {"id": i, "title": found["title"], "statement": found["statement"],
                    "origin": {"mine": "added by you", "archive": "from your archive"}.get(found["origin"], "said"),
                    "your_note": found["my_note"], "context": found["notes"], "starred": found["starred"],
                    "times_said": len(found["evidence"]), "score": None, "quotes": quotes}
    if not ideas:
        raise ToolError("none of those ideas exist (they may have been forgotten)")
    links = [{"a": str(a), "b": str(b), "kind": "your link" if k == "mine" else k}
             for a, b, k in conn.execute(
                 """SELECT a, b, kind FROM pub.connections WHERE a = ANY(%(i)s::uuid[]) AND b = ANY(%(i)s::uuid[])
                    ORDER BY kind""", {"i": list(ideas)})]
    weight = {i: 2 * x["starred"] + x["times_said"] for i, x in ideas.items()}
    groups, loose = _reading_order(list(ideas), links, weight)
    title = lambda i: ideas[i]["title"]   # noqa: E731
    return {"source": source, "ideas": list(ideas.values()),
            "connections": [{**link, "a_title": title(link["a"]), "b_title": title(link["b"])} for link in links],
            "suggested_order": {"connected_groups": [[title(i) for i in g] for g in groups],
                                "loose_ends": [title(i) for i in loose]},
            "how": "Ask the owner what to make (outline, essay, one-page guide, talk, thread), for whom, and how long, "
                   "before drafting. Build from these ideas in their words: statements and notes are the owner's "
                   "thinking; quotes are what was actually said (S1, S2 are other people, renumbered per answer). "
                   "The suggested order keeps connected ideas together; loose ends may not belong. Flag tensions "
                   "between ideas instead of resolving them silently, and mark anything you add."}


def whats_new(conn, since: str | None = None) -> dict:
    if since:
        try:
            start = datetime.fromisoformat(str(since))
        except ValueError:
            raise ToolError("since must be an ISO date or time, e.g. 2026-10-01") from None
        start = start if start.tzinfo else start.replace(tzinfo=timezone.utc)
    else:
        start = datetime.now(timezone.utc) - timedelta(days=7)
    ideas = conn.execute(f"SELECT {BRIEF} FROM pub.ideas WHERE created_at >= %s ORDER BY created_at DESC LIMIT 100",
                         (start,)).fetchall()
    themes = [n for (n,) in conn.execute(
        "SELECT name FROM pub.themes WHERE origin = 'clustered' AND NOT pinned ORDER BY name")]
    total = conn.execute("SELECT count(*) FROM pub.ideas WHERE created_at >= %s", (start,)).fetchone()[0]
    return {"since": start.isoformat(), "new_ideas_total": total, "new_ideas": [_brief(r) for r in ideas],
            "proposed_themes": themes, "proposals_waiting": len(_proposals(conn))}


def weekly_digest(conn, week: str | None = None) -> dict:
    if week:
        try:
            week = datetime.fromisoformat(str(week)).date().isoformat()
        except ValueError:
            raise ToolError("week must be an ISO date in the week you want, e.g. 2026-09-30") from None
    found = _digests(conn, week)
    if not found:
        raise ToolError("no digest for that week" if week else "Idea Machine hasn't written a weekly digest yet")
    d = found[0]
    ids = list({i for p in d["points"] for i in p["idea_ids"]})
    titles = {str(i): t for i, t in conn.execute("SELECT idea_id, title FROM pub.ideas WHERE idea_id = ANY(%s::uuid[])",
                                                 (ids,))}
    weeks = [x["week_start"] for x in _digests(conn)]
    return {"digest_id": d["id"], "week_start": d["week_start"], "summary": d["summary"],
            "points": [{"point": p["point"], "kind": p["kind"], "title": p["title"], "body": p["body"],
                        "ideas": [{"id": i, "title": titles[i]} for i in p["idea_ids"] if i in titles],
                        "owner_said": p["mine"]} for p in d["points"]],
            "other_weeks": [w for w in weeks if w != d["week_start"]]}


def _theme_result(conn, theme_id: str) -> dict | None:
    row = conn.execute("SELECT theme_id, name, pinned, n_ideas FROM pub.themes WHERE theme_id = %s",
                       (theme_id,)).fetchone()
    return {"id": str(row[0]), "name": row[1], "pinned": row[2], "ideas": row[3]} if row else None


def rename_theme(conn, theme: str, name: str) -> dict:
    tid, old, _ = _theme(conn, theme)
    record(conn, {"kind": "rename_theme", "theme_id": tid, "name": name})
    now = _theme_result(conn, tid)
    if now and now["name"] == old and old.strip().lower() != str(name).strip().lower():
        raise ToolError(f"not renamed: another theme is probably called {str(name).strip()!r}")
    return {"renamed": old, "theme": now}


def merge_themes(conn, theme: str, into: str) -> dict:
    tid, name, _ = _theme(conn, theme)
    into_id, _, _ = _theme(conn, into)
    if tid == into_id:
        raise ToolError("that's the same theme")
    record(conn, {"kind": "merge_theme", "theme_id": tid, "into": into_id})
    return {"merged": name, "into": _theme_result(conn, into_id)}


def split_theme(conn, theme: str, name: str, idea_ids: list) -> dict:
    tid, source, _ = _theme(conn, theme)
    new = str(uuid.uuid4())
    record(conn, {"kind": "split_theme", "theme_id": tid, "name": name,
                  "idea_ids": [_uuid(i) for i in idea_ids or []], "new_theme_id": new})
    made = _theme_result(conn, new)
    if made is None:
        raise ToolError(f"not split: another theme is probably called {str(name).strip()!r}")
    return {"split_from": _theme_result(conn, tid) or source, "new_theme": made}


def file_idea(conn, idea_id: str, theme: str, remove: bool = False) -> dict:
    tid, name, _ = _theme(conn, theme)
    i = _uuid(idea_id)
    record(conn, {"kind": "unfile_idea" if remove else "file_idea", "idea_id": i, "theme_id": tid})
    row = conn.execute("SELECT title, themes FROM pub.ideas WHERE idea_id = %s", (i,)).fetchone()
    if row is None:
        raise ToolError("no such idea (it may have been forgotten)")
    return {"idea": row[0], "themes": list(row[1]), ("removed_from" if remove else "filed_into"): name}


_embedder = None


def embedder():
    """The model the ideas were embedded with (`[lattice] embedding_model`), loaded on the first search by meaning."""
    global _embedder
    if _embedder is None:
        from im import config, lattice

        _embedder = lattice.SentenceEmbedder(config.load().lattice.embedding_model)
    return _embedder


def _near(conn, text: str, k: int) -> list[tuple[str, float]]:
    """The k shown ideas closest in meaning to `text`, closest first. Off, saying why, if the ideas are embedded with
    another model than the one this process embeds with."""
    if not str(text).strip():
        return []
    model, e = conn.execute("SELECT pub.embedding_model()").fetchone()[0], embedder()
    if model != e.model_version:
        raise ToolError(f"search by meaning isn't available: the ideas are embedded with {model}, not "
                        f"{e.model_version}; try search_ideas")
    vec = "[" + ",".join(f"{x:.6f}" for x in e.embed([str(text)])[0]) + "]"
    rows = conn.execute("SELECT idea_id, similarity FROM pub.ideas_near(%s::vector, %s)",
                        (vec, max(1, min(int(k), 50))))
    return [(str(i), float(s)) for i, s in rows]


def similar_ideas(conn, idea_id: str | None = None, text: str | None = None, limit: int = 8) -> dict:
    if (idea_id is None) == (text is None):
        raise ToolError("give idea_id (ideas like this one) or text (ideas close to these words), not both")
    limit = max(1, min(int(limit), 30))
    if idea_id is not None:
        i = _uuid(idea_id)
        hits = [(str(o), float(s)) for o, s in conn.execute(
            "SELECT idea_id, similarity FROM pub.card_neighbours(%s, %s)", (i, limit))]
        if not hits:   # not a card, or not embedded yet: its own text
            row = conn.execute("SELECT title, statement FROM pub.ideas WHERE idea_id = %s", (i,)).fetchone()
            if row is None:
                raise ToolError("no such idea (it may have been forgotten)")
            hits = [(o, s) for o, s in _near(conn, f"{row[0]}\n{row[1]}", limit + 1) if o != i][:limit]
    else:
        hits = _near(conn, text, limit)
    sim = dict(hits)
    rows = conn.execute(f"SELECT {BRIEF} FROM pub.ideas WHERE idea_id = ANY(%s::uuid[])", (list(sim),)).fetchall()
    ideas = sorted((_brief(r) | {"similarity": round(sim[str(r[0])], 2)} for r in rows),
                   key=lambda x: -x["similarity"])
    return {"ideas": ideas, "note": "Cosine similarity of Idea Machine's embeddings, best first. Between two ideas, "
                                    "about 0.85 and up is close to the same idea and 0.7 related. A few words score "
                                    "lower against a whole idea (0.6 to 0.75 for a good match), so judge by rank."}


def add_idea(conn, title: str, statement: str, note: str = "", link_to: list | None = None) -> dict:
    links = [_uuid(i, "link_to") for i in (link_to or [])]
    new = str(uuid.uuid4())
    record(conn, {"kind": "idea_add", "idea_id": new, "title": title, "statement": statement, "note": note,
                  "link_to": links})
    return {"idea_id": new, "title": str(title).strip()[:120], "linked_to": links,
            "note": "Added. Idea Machine embeds, places and files it on its next hourly run."}


def star_idea(conn, idea_id: str, starred: bool = True) -> dict:
    record(conn, {"kind": "star" if starred else "unstar", "idea_id": _uuid(idea_id)})
    return {"idea_id": idea_id, "starred": bool(starred)}


def proposals(conn) -> dict:
    return {"proposals": _proposals(conn),
            "ask": "Read each to the owner and ask whether to keep it as canonical (it carries weight in the "
                   "lattice), keep it as pinned (kept and visible, no weight), or dismiss it; then call keep_idea "
                   "with their answer. Don't answer for them."}


KEEP = {"canonical": "idea_keep", "pinned": "idea_keep", "unkeep": "idea_unkeep", "dismiss": "idea_dismiss"}


def keep_idea(conn, idea_id: str, decision: str) -> dict:
    if decision not in KEEP:
        raise ToolError(f"decision must be one of {', '.join(KEEP)}")
    event = {"kind": KEEP[decision], "idea_id": _uuid(idea_id)}
    if KEEP[decision] == "idea_keep":
        event["as"] = decision
    record(conn, event)
    return {"idea_id": idea_id, "decision": decision}


def link_ideas(conn, idea_id: str, other_idea_id: str, unlink: bool = False) -> dict:
    a, b = _uuid(idea_id), _uuid(other_idea_id, "other_idea_id")
    if a == b:
        raise ToolError("an idea can't link to itself")
    record(conn, {"kind": "unlink" if unlink else "link", "idea_id": a, "other_idea_id": b})
    return {"idea_id": a, "other_idea_id": b, "linked": not unlink}


def note_idea(conn, idea_id: str, note: str) -> dict:
    record(conn, {"kind": "idea_note", "idea_id": _uuid(idea_id), "note": note})
    return {"idea_id": idea_id, "note": str(note).strip()[:1000] or None}


# The contract: each tool's function, whether it only reads, its description (free to change), its arguments and
# which are required (not). Lattice's TOOLS, lattice/src/lattice/mcp.py:441-516.
ID = {"type": "string", "description": "An idea's id, from search_ideas, open_theme, whats_new or get_idea"}
THEME = {"type": "string", "description": "A theme's name (or a distinctive part of it) or id, from list_themes"}
TOOLS = {
    "search_ideas": (search_ideas, True, "Find ideas in the owner's Idea Machine (their lattice) whose title, "
                     "statement, notes or themes contain every word of the query.",
                     {"query": {"type": "string"}, "limit": {"type": "integer", "minimum": 1, "maximum": 50,
                                                             "default": 10}}, ["query"]),
    "get_idea": (get_idea, True, "One idea in full: statement, your note and corrections, themes, connections, "
                 "and the evidence quotes with who said them and when.", {"idea_id": ID}, ["idea_id"]),
    "best_ideas": (best_ideas, True, "The ideas the owner kept as canonical, most recently said first; optionally "
                   "within one theme. Idea Machine doesn't rate ideas, so nothing here is a score.",
                   {"limit": {"type": "integer", "minimum": 1, "maximum": 50, "default": 10},
                    "theme": {"type": "string", "description": "A theme's exact name"}}, []),
    "list_themes": (list_themes, True, "Every theme with its size and whether it's pinned or only proposed.", {}, []),
    "open_theme": (open_theme, True, "The ideas in one theme, starred and best-evidenced first.",
                   {"theme": {"type": "string", "description": "A theme's name (or part of it) or id"}}, ["theme"]),
    "whats_new": (whats_new, True, "What's new in the owner's Idea Machine: ideas added since a date (default: the "
                  "last 7 days), proposed themes waiting for a decision, and how many proposals wait for the owner's "
                  "call.", {"since": {"type": "string", "description": "ISO date or time"}}, []),
    "weekly_digest": (weekly_digest, True, "Idea Machine's weekly digest of the owner's lattice: a summary and "
                      "points (what kept coming up, what converges, sleepers, projects forming, what a stranger would "
                      "value), each with the ideas it rests on. The latest week, or the one holding a date. Its text "
                      "is written for the owner from idea text and may name people (it isn't pseudonymized like "
                      "quotes).", {"week": {"type": "string", "description": "An ISO date in the week wanted"}}, []),
    "draft_material": (draft_material, True, "Everything needed to draft a piece of writing from the owner's lattice: "
                       "the ideas in a theme, a weekly digest point or a chosen set, with their notes, quotes, "
                       "connections and a suggested reading order. Use when the owner wants to write, outline or "
                       "turn a cluster into something.",
                       {"theme": {"type": "string", "description": "A theme's name (or part of it) or id"},
                        "point": {"type": "integer", "minimum": 1,
                                  "description": "A point number in the weekly digest"},
                        "digest_week": {"type": "string", "description": "An ISO date in the digest's week; default "
                                                                         "the latest"},
                        "idea_ids": {"type": "array", "items": ID, "maxItems": MAX_DRAFT_IDEAS}}, []),
    "rename_theme": (rename_theme, False, "Rename one of the owner's themes. Only once they've agreed to the name.",
                     {"theme": THEME, "name": {"type": "string", "maxLength": 80}}, ["theme", "name"]),
    "merge_themes": (merge_themes, False, "Merge a theme into another: its ideas join `into`, and it goes away. "
                     "`into` keeps its name. Only once the owner has agreed; it can't be undone with one call.",
                     {"theme": THEME, "into": THEME}, ["theme", "into"]),
    "split_theme": (split_theme, False, "Split some ideas out of a theme into a new theme of the owner's (pinned; "
                    "Idea Machine writes its description on its next run). open_theme first for the idea ids. Only "
                    "once the owner has agreed to the name and the ideas.",
                    {"theme": THEME, "name": {"type": "string", "maxLength": 80},
                     "idea_ids": {"type": "array", "items": ID, "minItems": 1, "maxItems": 500}},
                    ["theme", "name", "idea_ids"]),
    "file_idea": (file_idea, False, "Put one idea into a theme, or take it out (remove=true). Idea Machine won't "
                  "undo it on its own. Only when the owner asks or agrees.",
                  {"idea_id": ID, "theme": THEME, "remove": {"type": "boolean", "default": False}},
                  ["idea_id", "theme"]),
    "similar_ideas": (similar_ideas, True, "Ideas close in meaning: like one idea (idea_id), or like some words "
                      "(text), e.g. to check whether the owner has had a thought before, or to find what an idea "
                      "connects to beyond its links.",
                      {"idea_id": ID, "text": {"type": "string", "maxLength": 1000},
                       "limit": {"type": "integer", "minimum": 1, "maximum": 30, "default": 8}}, []),
    "add_idea": (add_idea, False, "Add the owner's own idea to the lattice, worded as they approved: a short title, "
                 "a one-to-three-sentence statement, optional context, and optional ideas it connects to. Only after "
                 "they approve the wording; similar_ideas first for one it may repeat.",
                 {"title": {"type": "string", "maxLength": 120}, "statement": {"type": "string", "maxLength": 1000},
                  "note": {"type": "string", "maxLength": 1000,
                           "description": "Context: where it came from, why it matters"},
                  "link_to": {"type": "array", "items": ID, "maxItems": 10}}, ["title", "statement"]),
    "proposals": (proposals, True, "Candidates Idea Machine proposes the owner keep as ideas (up to 10 a week). Only "
                  "the owner makes ideas. Use when they want to see what's emerging or review proposals.", {}, []),
    "keep_idea": (keep_idea, False, "Record the owner's decision on an idea or proposal: keep it as canonical "
                  "(weighted), keep it as pinned (visible, no weight), unkeep it, or dismiss a proposal. Only the "
                  "owner's own decision.",
                  {"idea_id": ID, "decision": {"type": "string", "enum": list(KEEP)}}, ["idea_id", "decision"]),
    "star_idea": (star_idea, False, "Star an idea (or unstar it with starred=false). Only when the owner asks.",
                  {"idea_id": ID, "starred": {"type": "boolean", "default": True}}, ["idea_id"]),
    "link_ideas": (link_ideas, False, "Link two ideas the owner sees as connected (or remove the link with "
                   "unlink=true). Only when the owner asks.",
                   {"idea_id": ID, "other_idea_id": ID, "unlink": {"type": "boolean", "default": False}},
                   ["idea_id", "other_idea_id"]),
    "note_idea": (note_idea, False, "Set the owner's context note on an idea (replaces the old one; empty clears "
                  "it). Only when the owner asks.",
                  {"idea_id": ID, "note": {"type": "string", "maxLength": 1000}}, ["idea_id", "note"]),
}


# --- Demo mode -----------------------------------------------------------------------------------------------

DEMO_TOOLS = ("search_ideas", "get_idea", "best_ideas", "list_themes", "open_theme", "whats_new", "weekly_digest",
              "similar_ideas", "draft_material")
DEMO_INSTRUCTIONS = """This is a read-only view of the owner's Idea Machine, curated for showing it to other people:
thoughts captured from the owner's recorded conversations, grouped and connected. Some ideas and themes are
withheld and swearing is blacked out; don't speculate about or mention what might be missing. Speak to the owner as
"you". In quotes, "me" is the owner; S1, S2... are other people. Search or open a theme before asking for ids. To
draft something, call draft_material, then ask what to make before writing."""
SWEARING = re.compile(r"\b(" + "|".join([
    r"fuck\w*", r"motherfuck\w*", r"shit\w*", r"bullshit\w*", r"bitch\w*", r"cunts?", r"cocks?", r"cocksuck\w*",
    r"dick(head)?s?", r"puss(y|ies)", r"ass(hole)?s?", r"arse(hole)?s?", r"bastards?", r"piss\w*", r"sluts?",
    r"whores?", r"damn\w*", r"goddamn\w*", r"twats?", r"wank\w*", r"douche\w*", r"fag(got)?s?", r"n[i1]gg\w*",
    r"retards?", r"spics?", r"chinks?", r"kikes?"]) + r")\b", re.IGNORECASE)   # Lattice's list


class _Withheld:
    """What demo mode keeps back, worked out once per call: what the web view's demo mode hides."""

    def __init__(self, conn):
        self.ideas = {str(i) for (i,) in conn.execute("SELECT idea_id FROM pub.demo_hidden_ideas")}
        self.themes = {str(i) for (i,) in conn.execute(
            "SELECT id FROM pub.sensitivity WHERE kind = 'theme' AND sensitive")}
        self.theme_names = {n for i, n in conn.execute("SELECT theme_id::text, name FROM pub.themes")
                            if i in self.themes}
        self.titles = {t for i, t in conn.execute("SELECT idea_id::text, title FROM pub.ideas") if i in self.ideas}
        self.ids = self.ideas | self.themes

    def check(self, conn, name: str, args: dict) -> None:
        """Asking for a withheld idea or theme gets the answer a missing one would."""
        if args.get("idea_id") is not None and str(args["idea_id"]) in self.ideas:
            raise ToolError("no such idea (it may have been forgotten)")
        if name in ("open_theme", "draft_material", "best_ideas") and args.get("theme") is not None:
            if _theme(conn, args["theme"])[0] in self.themes:
                raise ToolError(f"no theme matches {args['theme']!r}; list_themes shows them all")
        if any(str(i) in self.ideas for i in args.get("idea_ids") or []):
            raise ToolError("no such idea (it may have been forgotten)")

    def clean(self, x):
        if isinstance(x, dict):
            return {k: self.clean(v) for k, v in x.items() if k != "proposals_waiting"}
        if isinstance(x, list):
            out = []
            for el in x:
                if isinstance(el, dict) and any(str(el.get(k)) in self.ids for k in ("id", "idea_id", "other_id",
                                                                                     "a", "b")):
                    continue
                if isinstance(el, str) and (el in self.theme_names or el in self.titles):
                    continue
                out.append(self.clean(el))
            return out
        if isinstance(x, str):
            return SWEARING.sub(lambda m: "\u2588" * len(m.group()), x)
        return x


def _demo_digest(result: dict, held: _Withheld) -> dict:
    return {**result, "summary": "", "points": [p for p in result["points"]
                                                if not any(i["id"] in held.ideas for i in p["ideas"])]}


# --- JSON-RPC ------------------------------------------------------------------------------------------------

def tool_list(writes: bool = True, demo: bool = False) -> list[dict]:
    return [{"name": name, "description": desc,
             "inputSchema": {"type": "object", "properties": props, "required": req, "additionalProperties": False},
             "annotations": {"readOnlyHint": ro, "destructiveHint": False, "openWorldHint": False}}
            for name, (_, ro, desc, props, req) in TOOLS.items()
            if (name in DEMO_TOOLS if demo else ro or writes)]


def call(conn, name: str, args: dict, writes: bool = True, demo: bool = False) -> dict:
    if name not in TOOLS or (demo and name not in DEMO_TOOLS) or not (TOOLS[name][1] or (writes and not demo)):
        raise UnknownTool(name)   # a write tool on a read-only grant, or anything outside demo mode, doesn't exist
    fn, _, _, props, req = TOOLS[name]
    try:
        missing = [k for k in req if k not in args]
        if missing:
            raise ToolError(f"missing {', '.join(missing)}")
        held = _Withheld(conn) if demo else None
        if held:
            held.check(conn, name, args)
            if name == "draft_material" and args.get("point") is not None:   # a withheld point isn't there
                d = _demo_digest(weekly_digest(conn, args.get("digest_week")), held)
                if not any(p["point"] == args["point"] for p in d["points"]):
                    raise ToolError("that digest has no such point")
        result = fn(conn, **{k: v for k, v in args.items() if k in props})
        if held:
            if name == "weekly_digest":
                result = _demo_digest(result, held)
            result = held.clean(result)
    except (ToolError, ValueError, TypeError) as e:
        return {"content": [{"type": "text", "text": str(e)}], "isError": True}
    return {"content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False)}], "structuredContent": result}


def demo_on(conn) -> bool:
    """Whether demo mode is on: the app's one setting (pub.app_settings)."""
    return conn.execute("SELECT demo FROM pub.app_settings").fetchone()[0]


def handle(message, connect, writes: bool = True, force_demo: bool = False):
    """One JSON-RPC message in, its response out (None for a notification). `connect()` opens a connection as the
    web view's role. `writes`: whether the caller may write. Demo mode is the app's setting, or `force_demo`
    (`im web --demo`)."""
    if not isinstance(message, dict) or message.get("jsonrpc") != "2.0" or not isinstance(message.get("method"), str):
        return _error(message.get("id") if isinstance(message, dict) else None, -32600, "invalid request")
    mid, method, params = message.get("id"), message["method"], message.get("params") or {}
    if "id" not in message:   # a notification (notifications/initialized and friends): nothing to say
        return None
    if method == "ping":
        return _result(mid, {})
    with connect() as conn:
        demo = force_demo or demo_on(conn)
        if method == "initialize":
            asked = params.get("protocolVersion")
            return _result(mid, {"protocolVersion": asked if asked in PROTOCOLS else PROTOCOLS[0],
                                 "capabilities": {"tools": {"listChanged": False}},
                                 "serverInfo": {**SERVER, "title": "Idea Machine (demo)"} if demo else SERVER,
                                 "instructions": DEMO_INSTRUCTIONS if demo else INSTRUCTIONS})
        if method == "tools/list":
            return _result(mid, {"tools": tool_list(writes, demo)})
        if method == "tools/call":
            try:
                return _result(mid, call(conn, params.get("name"), params.get("arguments") or {}, writes, demo))
            except UnknownTool:
                return _error(mid, -32602, f"unknown tool {params.get('name')!r}")
    return _error(mid, -32601, f"method not found: {method}")


def _result(mid, result) -> dict:
    return {"jsonrpc": "2.0", "id": mid, "result": result}


def _error(mid, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": code, "message": message}}
