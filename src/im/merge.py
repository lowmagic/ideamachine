"""Merging two clusters that make one point (`im merge KEEP OTHER`, migration 060).

OTHER is merged into KEEP, and the merge is recorded in im.idea_merges. KEEP's title, statement and note stay as they
are; OTHER's, as shown, go into the record, and its row stays with my events on it, so nothing I wrote is lost and a
merge could be undone.

- Captures: OTHER's move to KEEP as `same_as`, judged_by MERGED, and the match decisions that named OTHER name KEEP,
  so a later read of the same speech follows its observation into KEEP. Typed observations move too.
- Connections and lineage: OTHER's are re-pointed to KEEP, except an embedding stand-in (lattice.relate rebuilds
  KEEP's), one between the two (it disappears), and one where KEEP already has a connection or a judge's verdict
  (KEEP's stands). A kind's direction follows its pair. My links and unlinks on OTHER count for KEEP
  (im.my_link_events). Every connection verdict on OTHER goes: the connection judge nominates KEEP's new neighbors
  on its own.
- Merge review: a row whose clusters are now all one is resolved by this merge (`merge_id`).
- How I kept OTHER, or my mark that it's sensitive, carries to KEEP as a feedback event (source MERGED) when KEEP had
  neither.
- OTHER's derived rows go: embedding, position, weights, sensitivity tags, an open proposal. My lens pins on it hold
  for KEEP by themselves: they're anchored to its speech and follow the merge (migration 069). KEEP's weight is
  recomputed by the next run, its sensitivity tags when its text as tagged (with its first quotes) changed, and its
  edges' citations now.

From then on OTHER has no text (im.idea_text) and isn't kept (im.idea_kept): it's no card, no matcher candidate and
not embedded, so no capture can join it.

The merge screen (`im merges`, B9m) lists the pairs that may be one point, likeliest first: the open merge review rows
(`duplicate`, `judged_same`) and the near pairs the connection judge nominates (connect.nominate), both clusters
cards, leaving out pairs I unlinked or marked distinct (`idea_distinct`, 061; `im merge --distinct`). Jev's
`same_v2` (jev.QUESTIONS["match"]) on each cluster's title and statement, as the connection judge shows them, orders
it; the group's combined `p` isn't used, since it counts `develops` too. An answer is kept (im.jev_answers) under a
hash of both texts, so a pair is asked again only when either changes. A pair answered under another question set
(its wording or Jev's model) for the same texts is held by the re-read gate (im.reread), ordered by that answer, until
`im reread merges` asks it again. The screen writes nothing else, and never merges.
"""

import hashlib
import json
import uuid

from im import citations, connect, egress, jev, llm, reread

MERGED = "merge"
SAME = "same_v2"   # the answer that orders the merge screen


def _id(conn, ref: str):
    try:
        idea = uuid.UUID(ref)
    except ValueError:
        raise ValueError(f"{ref} is not a cluster id") from None
    if not conn.execute("SELECT 1 FROM im.ideas WHERE idea_id = %s", (idea,)).fetchone():
        raise ValueError(f"no cluster {ref}")
    into = conn.execute("SELECT keep_id FROM im.idea_merges WHERE other_id = %s", (idea,)).fetchone()
    if into:
        raise ValueError(f"{ref} was already merged into {into[0]}")
    return idea


def _repoint_connections(conn, keep, other) -> int:
    """OTHER's connections, as KEEP's, with their kinds; then OTHER's are gone. Returns how many moved."""
    moved = 0
    for a, b, method, confidence, cited, decided_at in conn.execute(
            """SELECT a, b, method, confidence, cited_observation_ids, decided_at FROM im.connections
               WHERE %(o)s IN (a, b) AND %(k)s NOT IN (a, b) AND method NOT LIKE 'embedding:%%'""",
            {"o": other, "k": keep}).fetchall():
        x = b if a == other else a
        new = conn.execute(
            """INSERT INTO im.connections (a, b, method, confidence, cited_observation_ids, decided_at)
               SELECT least(%(k)s, %(x)s), greatest(%(k)s, %(x)s), %(m)s, %(c)s, %(cited)s, %(at)s
               WHERE NOT EXISTS (SELECT 1 FROM im.connection_verdicts v
                                 WHERE v.a = least(%(k)s, %(x)s) AND v.b = greatest(%(k)s, %(x)s))
               ON CONFLICT (a, b) DO NOTHING RETURNING a, b""",
            {"k": keep, "x": x, "m": method, "c": confidence, "cited": cited, "at": decided_at}).fetchone()
        if not new:
            continue
        moved += 1
        flip = (a == other) != (new[0] == keep)   # OTHER's side was a, KEEP's is b, or the other way round
        conn.execute(
            """INSERT INTO im.connection_kinds (a, b, kind, direction, method, decided_at)
               SELECT %s, %s, kind,
                      CASE WHEN %s THEN CASE direction WHEN 'a>b' THEN 'b>a' WHEN 'b>a' THEN 'a>b' ELSE direction END
                           ELSE direction END,
                      method, decided_at
               FROM im.connection_kinds WHERE a = %s AND b = %s""", (*new, flip, a, b))
    conn.execute("DELETE FROM im.connections WHERE %s IN (a, b)", (other,))
    return moved


def _repoint_lineage(conn, keep, other) -> int:
    moved = conn.execute(
        """INSERT INTO im.lineage (from_id, to_id, kind, method, cited_observation_ids, created_at)
           SELECT CASE WHEN from_id = %(o)s THEN %(k)s ELSE from_id END,
                  CASE WHEN to_id = %(o)s THEN %(k)s ELSE to_id END, kind, method, cited_observation_ids, created_at
           FROM im.lineage WHERE %(o)s IN (from_id, to_id) AND %(k)s NOT IN (from_id, to_id)
           ON CONFLICT DO NOTHING""", {"o": other, "k": keep}).rowcount
    conn.execute("DELETE FROM im.lineage WHERE %s IN (from_id, to_id)", (other,))
    return moved


def _carry_marks(conn, keep, other) -> list[int]:
    """How I kept OTHER, and my mark that it's sensitive, as events on KEEP when KEEP has neither."""
    kept = dict(conn.execute("SELECT idea_id, kept_as FROM im.idea_kept WHERE idea_id IN (%s, %s)", (keep, other)))
    marks = dict(conn.execute(
        """SELECT DISTINCT ON (idea_id) idea_id, (payload->>'sensitive')::boolean FROM pub.feedback_events
           WHERE kind = 'idea_sensitive' AND idea_id IN (%s, %s) ORDER BY idea_id, event_id DESC""", (keep, other)))
    events = []
    if kept[other] and not kept[keep]:
        events.append(("idea_keep", {"as": kept[other]}))
    if marks.get(other) and keep not in marks:
        events.append(("idea_sensitive", {"sensitive": True}))
    return [conn.execute("""INSERT INTO pub.feedback_events (kind, idea_id, payload, source)
                            VALUES (%s, %s, %s, %s) RETURNING event_id""",
                         (kind, keep, json.dumps(payload), MERGED)).fetchone()[0] for kind, payload in events]


def merge(conn, keep_ref: str, other_ref: str) -> dict:
    """Merge cluster OTHER into KEEP. Raises ValueError, changing nothing, for the same id twice, an id that's no
    cluster, or a cluster already merged away."""
    with conn.transaction():
        keep, other = _id(conn, keep_ref), _id(conn, other_ref)
        if keep == other:
            raise ValueError("a cluster can't be merged into itself")
        title, statement, note = conn.execute(
            "SELECT title, statement, my_note FROM im.idea_text WHERE idea_id = %s", (other,)).fetchone()
        events = _carry_marks(conn, keep, other)
        evidence = [{"item_id": str(i), "relation": r, "judged_by": j} for i, r, j in conn.execute(
            "SELECT item_id, relation, judged_by FROM im.idea_evidence WHERE idea_id = %s ORDER BY item_id", (other,))]
        matched = [r[0] for r in conn.execute(
            "UPDATE im.item_matches SET idea_id = %s WHERE idea_id = %s RETURNING item_id", (keep, other))]
        observations = [r[0] for r in conn.execute(
            "UPDATE im.observations SET idea_id = %s WHERE idea_id = %s RETURNING observation_id", (keep, other))]
        (merge_id,) = conn.execute(
            """INSERT INTO im.idea_merges (other_id, keep_id, evidence, matched_item_ids, observation_ids, title,
                                           statement, my_note, event_ids)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING merge_id""",
            (other, keep, json.dumps(evidence), matched, observations, title, statement, note, events)).fetchone()
        conn.execute("""INSERT INTO im.idea_evidence (idea_id, item_id, relation, judged_by)
                        SELECT %s, item_id, 'same_as', %s FROM im.idea_evidence WHERE idea_id = %s
                        ON CONFLICT DO NOTHING""", (keep, MERGED, other))
        conn.execute("DELETE FROM im.idea_evidence WHERE idea_id = %s", (other,))
        connections = _repoint_connections(conn, keep, other)
        lineage = _repoint_lineage(conn, keep, other)
        verdicts = conn.execute("DELETE FROM im.connection_verdicts WHERE %s IN (a, b)", (other,)).rowcount
        for table in ("idea_embeddings", "idea_positions", "weights", "idea_sensitivity"):
            conn.execute(f"DELETE FROM im.{table} WHERE idea_id = %s", (other,))
        conn.execute("DELETE FROM im.proposals WHERE idea_id = %s AND outcome IS NULL", (other,))
        resolved = conn.execute(
            """UPDATE im.merge_review r SET merge_id = %s
               WHERE r.merge_id IS NULL AND %s = ANY(r.idea_ids)
                 AND (SELECT count(DISTINCT coalesce(m.into_id, u)) FROM unnest(r.idea_ids) u
                      LEFT JOIN im.merged_into m ON m.idea_id = u) = 1""", (merge_id, other)).rowcount
        citations.cite(conn)
    return {"merge_id": merge_id, "kept": str(keep), "merged_away": str(other), "captures": len(evidence),
            "observations": len(observations), "connections_moved": connections, "lineage_moved": lineage,
            "verdicts_dropped": verdicts, "reviews_resolved": resolved, "marks_carried": len(events),
            "merged_away_text": {"title": title, "statement": statement, "my_note": note}}


def distinct(conn, a_ref: str, b_ref: str) -> dict:
    """My mark that two clusters make distinct points (an `idea_distinct` event): the merge screen stops listing them.
    Raises ValueError, as merge does."""
    a, b = _id(conn, a_ref), _id(conn, b_ref)
    if a == b:
        raise ValueError("a cluster can't be distinct from itself")
    conn.execute("""INSERT INTO pub.feedback_events (kind, idea_id, other_idea_id, source)
                    VALUES ('idea_distinct', %s, %s, 'cli')""", (a, b))
    return {"distinct": [str(a), str(b)]}


# --- The merge screen ------------------------------------------------------------------------------------------------

def _side(title: str, statement: str) -> str:
    return f"{title}. {statement}"   # the summary, as the connection judge shows a side (connect.side_data)


def _apart(conn) -> set:
    """(a, b), a < b, for the pairs I unlinked or marked distinct, a cluster merged away counting as the one it's part
    of now."""
    return set(conn.execute(
        """SELECT a, b FROM im.my_link_events WHERE kind = 'unlink'
           UNION
           SELECT least(x.a, x.b), greatest(x.a, x.b) FROM pub.feedback_events f
           LEFT JOIN im.merged_into ma ON ma.idea_id = f.idea_id
           LEFT JOIN im.merged_into mb ON mb.idea_id = f.other_idea_id
           CROSS JOIN LATERAL (VALUES (coalesce(ma.into_id, f.idea_id), coalesce(mb.into_id, f.other_idea_id))) x(a, b)
           WHERE f.kind = 'idea_distinct' AND x.a <> x.b""").fetchall())


def pairs(conn) -> dict[tuple, dict]:
    """The pairs of cards that may be one point: (a, b), a < b -> {"cosine": the near pair's (None if only in review),
    "review": the kinds of its open merge review rows}."""
    out = {p: {"cosine": c, "review": set()} for p, c in connect.nominate(conn).items()}
    for kind, ids in conn.execute(
            """SELECT r.kind, ARRAY(SELECT DISTINCT coalesce(m.into_id, u) FROM unnest(r.idea_ids) u
                                    LEFT JOIN im.merged_into m ON m.idea_id = u
                                    WHERE coalesce(m.into_id, u) IN (SELECT idea_id FROM pub.cards) ORDER BY 1)
               FROM im.merge_review r WHERE r.merge_id IS NULL AND r.kind IN ('duplicate', 'judged_same')"""):
        for n, a in enumerate(ids):
            for b in ids[n + 1:]:
                out.setdefault((a, b), {"cosine": None, "review": set()})["review"].add(kind)
    apart = _apart(conn)
    return {p: v for p, v in out.items() if p not in apart}


def screen(conn, client: "jev.Jev | None", limit: int | None = None,
           monthly_cap_usd: float = 20.0, held_only: bool = False) -> tuple[list[dict], dict]:
    """Every pair, the likeliest duplicate first by Jev's SAME, then those not asked yet; and what asking cost. Asks
    Jev about at most `limit` pairs with no answer for their texts (None: all; none without a client), review rows
    first, then the nearest. A pair the re-read gate holds keeps its earlier answer, and is asked only `held_only`
    (`im reread merges`), when it's all that's asked; what's held is in the stats. The cap stops the asking, not the
    listing."""
    found = pairs(conn)
    shown = {i: (t, s) for i, t, s in conn.execute(
        "SELECT idea_id, title, statement FROM im.idea_text WHERE idea_id = ANY(%s)",
        (list({i for p in found for i in p}),))}
    qs = jev.question_set(client, "match")
    kept = jev.kept(conn, qs)
    rows = []
    for (a, b), v in found.items():
        key = hashlib.sha256(json.dumps([_side(*shown[a]), _side(*shown[b])]).encode()).hexdigest()
        rows.append({"a": a, "b": b, "shown": (shown[a], shown[b]), "key": key, "answer": kept.get(key), **v})
    earlier = {k: (asked, a, egress_id) for k, asked, a, egress_id in conn.execute(
        """SELECT key_sha256, question_set, answers, egress_id FROM im.jev_answers
           WHERE key_sha256 = ANY(%s) AND question_set <> %s ORDER BY created_at""",
        ([r["key"] for r in rows if r["answer"] is None], qs))}
    held = []
    for r in rows:
        if r["answer"] is None and r["key"] in earlier:
            r["held"] = earlier[r["key"]]
            r["answer"] = r["held"][1] if SAME in r["held"][1] else None
            held.append(r)
    due = held if held_only else [r for r in rows if r["answer"] is None and "held" not in r]
    due = sorted(due, key=lambda r: (not r["review"], -(r["cosine"] or 0), str(r["a"]), str(r["b"])))
    stats = {"pairs": len(rows), "due": len(due), "asked": 0, "failed": 0, "cost_usd": 0.0, "stopped": None}
    for r in due[:limit] if client else []:
        def state(r=r):
            return {side: {"summary": _side(*text)} for side, text in zip("ab", r["shown"], strict=True)}

        try:
            got, cost, sent = jev.ask(conn, client, "match", r["key"], state,
                                      segment_ids=egress.segments_of(conn, [r["a"], r["b"]]),
                                      monthly_cap_usd=monthly_cap_usd)
        except llm.Stop as e:
            stats["stopped"] = str(e)[:200]
            break
        stats["asked"] += sent
        stats["failed"] += got is None
        stats["cost_usd"] += cost
        if got is not None:
            r["answer"] = got
            r.pop("held", None)
    stats["cost_usd"] = round(stats["cost_usd"], 6)
    still = [r for r in held if "held" in r]
    model = client.model if client else jev.MODEL
    reread.add(stats, reread.report("merges", [r["held"][0] for r in still], qs, reread.estimate(
        conn, [r["held"][2] for r in still if r["held"][2] is not None], lambda _: model)))
    rows.sort(key=lambda r: (r["answer"] is None, -(r["answer"] or {}).get(SAME, 0), not r["review"],
                             -(r["cosine"] or 0), str(r["a"]), str(r["b"])))
    return rows, stats


def listing(rows: list[dict], stats: dict, top: int) -> str:
    """The first `top` pairs as `im merges` prints them, and what's left."""
    out = []
    for r in rows[:top]:
        same = f"{r['answer'][SAME]:.2f}" if r["answer"] else "not asked"
        same += " (an earlier question set)" if "held" in r else ""
        near = f"cosine {r['cosine']:.2f}" if r["cosine"] is not None else "not near"
        review = f"merge review: {', '.join(sorted(r['review']))}" if r["review"] else "not in merge review"
        out.append(f"{SAME} {same}   {near}   {review}")
        for idea, (title, statement) in zip((r["a"], r["b"]), r["shown"], strict=True):
            out += [f"  {idea}  {title}", f"  {'':36}  {statement}"]
        out.append("")
    unasked = sum(r["answer"] is None and "held" not in r for r in rows)
    out.append(f"{min(top, len(rows))} of {stats['pairs']} pairs shown; {unasked} not asked yet. Asked now: "
               f"{stats['asked']} (${stats['cost_usd']}), {stats['failed']} failed"
               + (f"; stopped: {stats['stopped']}" if stats["stopped"] else "") + ".")
    if held := stats.get("held"):
        out.append(f"Held by the re-read gate: {held['held']} answered under an earlier question set, about "
                   f"${held['estimated_usd']} to ask again ({held['estimate']}): {held['approve']}.")
    out.append("Merge one: im merge KEEP OTHER. Mark two distinct: im merge --distinct A B.")
    return "\n".join(out)
