"""The `jev` kind of lens query (decision 0013; docs/design/m5-lenses-and-mcp.md, Query kinds; migration 070).

A `jev` lens is a set of Nouls, a rule combining their probabilities (`max`: any one; `mean`: one question in several
wordings) and a cut. Jev is shown each card as the sensitivity tagger shows an idea and lens_audit's pilots showed it
(its title, statement, my note and first quotes: `cards`), one request per lens and card, through the egress log and
under the monthly cap, and its answer is kept in im.lens_answers under the query's version and the text's hash. A card
is in the lens when the rule gives at least the cut (pub.lens_members); one not asked yet is in no `jev` lens, and the
lens says how much of the map it has judged (pub.lens_judged).

What a run asks: the cards the lens's filters admit that it hasn't answered under its current query for their
current text, nearest its description by embedding first. `im lens run` stops at the NEAREST nearest unless `--all`;
`im run` goes on through the rest, PER_RUN a lens at a time, so a lens keeps up with new and changed cards. Editing a
query is my request to ask again: its new version asks everything. A card answered by another Jev model under the
same query and text is held by the re-read gate (im.reread), its answer standing, until `im reread lenses`.

`import_file` makes or updates lenses from a file shaped as lens_audit's `lenses.json`, so a lens judged there comes
in as it was measured: by name, as my events.
"""

import hashlib
import json
import uuid
from pathlib import Path

import numpy as np

from im import egress, jev, lens, llm, reread, sensitivity

NEAREST = 300   # what `im lens run` asks without --all: the cards nearest the lens's description
PER_RUN = 500   # what `im run` asks of each lens at most

# A card's first quotes, and my readings of those I corrected (none: the card as it was before readings could be).
CARDS = """
SELECT c.idea_id::text, c.title, c.statement, coalesce(c.my_note, ''), c.by_me > 0,
       ARRAY(SELECT left(ce.quote, 200) FROM pub.card_evidence ce WHERE ce.idea_id = c.idea_id AND ce.quote IS NOT NULL
             ORDER BY ce.said_at, ce.item_id LIMIT %(quotes)s),
       ARRAY(SELECT x.mine FROM (
               SELECT CASE WHEN ce.reading IS DISTINCT FROM ce.claude_reading THEN left(ce.reading, 400) END AS mine
               FROM pub.card_evidence ce WHERE ce.idea_id = c.idea_id AND ce.quote IS NOT NULL
               ORDER BY ce.said_at, ce.item_id LIMIT %(quotes)s) x WHERE x.mine IS NOT NULL)
FROM pub.cards c ORDER BY c.idea_id"""


def cards(conn) -> list[dict]:
    """Every card as Jev is shown it, with whether I said it and its text's hash."""
    out = []
    for idea, title, statement, note, by_me, said, mine in conn.execute(CARDS, {"quotes": sensitivity.QUOTES}):
        item = ({"title": title, "statement": statement} | ({"my_note": note} if note else {})
                | ({"said": said} if said else {}) | ({"my_readings": mine} if mine else {}))
        out.append({"id": idea, "by_me": by_me, "item": item,
                    "sha": hashlib.sha256(json.dumps(item, sort_keys=True).encode()).hexdigest()})
    return out


def nearest(conn, embedder, text: str, ids: list[str], size: int | None) -> tuple[list[str], dict[str, float]]:
    """The `size` cards among `ids` nearest `text` by embedding (all of them with None), nearest first, and every
    card's similarity. Cards with no embedding under the embedder's model are left out."""
    vecs = {i: np.array(json.loads(v)) for i, v in conn.execute(
        """SELECT idea_id::text, embedding::text FROM im.idea_embeddings
           WHERE model_version = %s AND idea_id::text = ANY(%s)""", (embedder.model_version, ids))}
    d = embedder.embed([text])[0]
    sim = {i: float(v @ d) for i, v in vecs.items()}
    ranked = sorted(sim, key=lambda i: (-sim[i], i))
    return (ranked if size is None else ranked[:size]), sim


def combine(query: dict, p: dict[str, float]) -> float:
    """The lens's probability from its Nouls', by its rule."""
    ps = [p[name] for name in query["deciding"]]
    return max(ps) if query["rule"] == "max" else sum(ps) / len(ps)


def _admitted(conn, lens_id: str) -> set[str]:
    """The cards a lens's filters admit."""
    return {i for (i,) in conn.execute(
        """SELECT c.idea_id::text FROM pub.cards c JOIN im.lenses l ON l.lens_id = %s
           LEFT JOIN pub.sensitivity s ON s.kind = 'idea' AND s.id = c.idea_id
           WHERE im.lens_admits(l.filters, c.maturity, c.kept_as, c.by_me, c.first_said, c.last_seen,
                                coalesce(s.sensitive, false))""", (lens_id,))}


def _todo(conn, x: dict, model: str) -> tuple[list[dict], list[dict]]:
    """(to ask, held): the admitted cards not answered under the lens's query for their current text, and those
    answered under it for that text by another model, which the re-read gate holds."""
    kept = {i: (qv, m, sha, eid) for i, qv, m, sha, eid in conn.execute(
        """SELECT idea_id::text, query_version, model, text_sha256, egress_id FROM im.lens_answers
           WHERE lens_id = %s""", (x["lens_id"],))}
    admitted = _admitted(conn, x["lens_id"])
    todo, held = [], []
    for c in cards(conn):
        if c["id"] not in admitted:
            continue
        qv, m, sha, eid = kept.get(c["id"], (None, None, None, None))
        if qv == x["query_version"] and sha == c["sha"]:
            if m != model:
                held.append(c | {"was": m, "egress_id": eid})
            continue
        todo.append(c)
    return todo, held


def _jev_lenses(conn, lens_ids: list[str] | None = None) -> list[dict]:
    cur = conn.execute("""SELECT lens_id::text, name, description, query, query_version FROM im.lenses
                          WHERE query->>'kind' = 'jev' AND (%s::text[] IS NULL AND state <> 'archived'
                                                            OR lens_id::text = ANY(%s::text[]))
                          ORDER BY created_at, lens_id""", (lens_ids, lens_ids))
    names = [c.name for c in cur.description]
    return [dict(zip(names, r)) for r in cur.fetchall()]


def run(conn, client: jev.Jev, embedder, lens_id: str, everything: bool = False, limit: int | None = None,
        monthly_cap_usd: float = 20.0, held_only: bool = False) -> dict:
    """Ask Jev a `jev` lens's Nouls about the cards it hasn't answered under its query for their text: the NEAREST
    nearest its description unless `everything`, at most `limit`. A failed answer is asked again next time; the cap
    stops the run. With `held_only`, only what the re-read gate holds (`im reread lenses`)."""
    found = _jev_lenses(conn, [lens_id])
    if not found:
        raise ValueError("not a jev lens")
    x = found[0]
    q = x["query"]
    todo, held = _todo(conn, x, client.model)
    if held_only:
        todo, held = held, []
    by_id = {c["id"]: c for c in todo}
    ranked, _ = nearest(conn, embedder, x["description"] or x["name"], list(by_id), None if everything else NEAREST)
    order = ranked + ([i for i in sorted(by_id) if i not in set(ranked)] if everything or held_only else [])
    stats = {"lens": x["name"], "query_version": x["query_version"], "to_ask": len(order), "asked": 0, "in": 0,
             "failed": 0, "cost_usd": 0.0, "stopped": None}
    asked = {name: tuple(n) for name, n in q["deciding"].items()}
    done = set()
    for i in order[:limit]:
        c = by_id[i]
        body = {"model": client.sent_as, "state": {"item": c["item"]}, "questions": jev._nouls(asked)}
        try:
            a = egress.send(conn, lambda body=body: client.complete(body), model=client.model,
                            payload=json.dumps(body), segment_ids=egress.segments_of(conn, [i]),
                            prompt_version=f"lens:{x['query_version']}", monthly_cap_usd=monthly_cap_usd,
                            read=lambda r: {k: float(r["answers"][k]["noul"]) for k in asked})
        except llm.Stop as e:
            stats["stopped"] = str(e)[:200]
            break
        stats["asked"] += 1
        stats["cost_usd"] += a.cost_usd
        if a.failed:
            stats["failed"] += 1
            continue
        p = round(combine(q, a.data), 4)
        conn.execute(
            """INSERT INTO im.lens_answers (lens_id, idea_id, query_version, model, text_sha256, answers, p, egress_id)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
               ON CONFLICT (lens_id, idea_id) DO UPDATE SET query_version = excluded.query_version,
                 model = excluded.model, text_sha256 = excluded.text_sha256, answers = excluded.answers,
                 p = excluded.p, egress_id = excluded.egress_id, asked_at = now()""",
            (x["lens_id"], i, x["query_version"], client.model, c["sha"],
             json.dumps({k: round(v, 4) for k, v in a.data.items()}), p, a.egress_id))
        done.add(i)
        stats["in"] += p >= q["cut"]
    stats["cost_usd"] = round(stats["cost_usd"], 6)
    return reread.add(stats, _held_report(conn, [h for h in held if h["id"] not in done], x, client.model))


def _held_report(conn, held: list[dict], x: dict, model: str) -> dict | None:
    return reread.report("lenses", [f"{x['query_version']}@{h['was']}" for h in held], f"{x['query_version']}@{model}",
                         reread.estimate(conn, [h["egress_id"] for h in held if h["egress_id"]], lambda _: model))


def held(conn) -> dict | None:
    """What the re-read gate holds across every lens (reread.report), sending and writing nothing."""
    out, ids = [], []
    for x in _jev_lenses(conn):
        for h in _todo(conn, x, jev.MODEL)[1]:
            out.append(f"{x['query_version']}@{h['was']}")
            ids += [h["egress_id"]] if h["egress_id"] else []
    return reread.report("lenses", out, f"@{jev.MODEL}", reread.estimate(conn, ids, lambda _: jev.MODEL))


def stage(conn, client: jev.Jev, embedder, monthly_cap_usd: float = 20.0, per_run: int = PER_RUN,
          held_only: bool = False) -> dict:
    """`im run`'s lenses: each `jev` lens not archived, through every card it admits, at most `per_run` each;
    with `held_only`, what the re-read gate holds (`im reread lenses`). The cap stops it."""
    out: dict = {}
    for x in _jev_lenses(conn):
        got = run(conn, client, embedder, x["lens_id"], everything=True, limit=per_run,
                  monthly_cap_usd=monthly_cap_usd, held_only=held_only)
        out[x["name"]] = got
        if got["stopped"]:
            break
    return out


def import_file(conn, path: Path, state: str | None = None, source: str = "cli") -> dict:
    """Make or update the lenses in a file shaped as lens_audit's `lenses.json` (lens_audit.load checks it): each
    by name, its description, its `jev` query (deciding Nouls, rule and cut; probes, which decide nothing, are left
    behind) and `said_by_me` as a filter. A lens already there by that name is edited, and one more is made; with
    `state`, each is set to it too. As my events, applied at once."""
    from im import lens_audit

    spec = lens_audit.parse(path)
    stats = {"added": [], "edited": []}
    for x in spec["lenses"]:
        query = {"kind": "jev", "deciding": x["deciding"], "rule": x["rule"], "cut": x["cut"]}
        filters = {"said_by_me": True} if x["said_by_me"] else {}
        row = conn.execute("SELECT lens_id::text FROM pub.lenses WHERE lower(btrim(name)) = lower(btrim(%s))",
                           (x["name"],)).fetchone()
        if row:
            lens_id = row[0]
            lens.record(conn, "lens_edit", {"lens_id": lens_id, "description": x["description"], "query": query,
                                            "filters": filters}, source=source)
            stats["edited"].append(x["name"])
        else:
            lens_id = str(uuid.uuid4())
            lens.record(conn, "lens_add", {"lens_id": lens_id, "name": x["name"], "description": x["description"],
                                           "query": query, "filters": filters}, source=source)
            stats["added"].append(x["name"])
        if state:
            lens.record(conn, "lens_state", {"lens_id": lens_id, "state": state}, source=source)
    return stats
