"""My verdicts on captures, arriving as pub.feedback_events (ROADMAP §3.5).

Each item_keep / item_discard / item_star event becomes a row in im.item_verdicts (the same table `im ideas
--review` writes), with the event's note. Applied events are recorded in im.feedback_applied, so each applies once.
An event that repeats the item's latest verdict and note adds nothing (a resend, e.g. from an offline queue).
Theme and lens events are applied by their triggers (migrations 069, 071); idea stars are read straight from the
events by pub.ideas.

Verdicts are carried forward too (`carry_verdicts`): when the episode an item was on is replaced, and Claude
captures the same speech again, my verdicts are copied to the new item instead of asking me again.

A dismissal may say why (`DismissReason`, migration 065), and nothing reads why except a count (`dismiss_reasons`,
`im feedback --reasons`).
"""

import difflib
import re
from enum import StrEnum

ITEM_KINDS = {"item_keep": "keep", "item_discard": "discard", "item_star": "star"}


class DismissReason(StrEnum):
    """Why I dismissed a card, when I say: an idea_dismiss's payload `reason` (065's CHECK). Optional: a dismissal
    without one means only that I dismissed it."""
    NOT_A_THOUGHT = "not_a_thought"   # not a qualifying thought
    MISREPRESENTS = "misrepresents"   # misrepresents what I said
    DUPLICATE = "duplicate"           # duplicates another card; payload `of` may name which
    NOT_FOR_ME = "not_for_me"         # valid, but I don't want it surfaced


def dismiss_reasons(conn) -> dict:
    """The cards I've dismissed, each counted once, by the latest reason I gave for it; `unknown` where I gave none
    (a later dismissal without a reason doesn't take one back). A card I never dismissed counts nowhere."""
    counts = dict(conn.execute(
        """SELECT coalesce(r.reason, 'unknown'), count(*)
           FROM (SELECT DISTINCT idea_id FROM pub.feedback_events WHERE kind = 'idea_dismiss') d
           LEFT JOIN LATERAL (SELECT f.payload->>'reason' AS reason FROM pub.feedback_events f
                              WHERE f.idea_id = d.idea_id AND f.kind = 'idea_dismiss'
                                AND f.payload->>'reason' IS NOT NULL
                              ORDER BY f.event_id DESC LIMIT 1) r ON true
           GROUP BY 1""").fetchall())
    return {"dismissed": sum(counts.values())} | {r.value: counts.get(r.value, 0) for r in DismissReason} \
        | {"unknown": counts.get("unknown", 0)}


def _how(payload: dict) -> str:
    """A keep made without reading carries {"bulk": true}, and a spot check {"audit": true} (as Lattice marked them)."""
    return "bulk" if payload.get("bulk") is True else "audit" if payload.get("audit") is True else "one"


def apply_item_feedback(conn) -> dict:
    done: dict = {}
    rows = conn.execute(
        """SELECT f.event_id, f.kind, f.item_id, f.payload FROM pub.feedback_events f
           WHERE f.kind = ANY(%s) AND NOT EXISTS (SELECT 1 FROM im.feedback_applied a WHERE a.event_id = f.event_id)
           ORDER BY f.event_id""", (list(ITEM_KINDS),)).fetchall()
    for event_id, kind, item_id, payload in rows:
        verdict = ITEM_KINDS[kind]
        note = ((payload or {}).get("note") or "").strip()[:500] or None
        how = _how(payload or {})
        with conn.transaction():
            latest = conn.execute(
                """SELECT verdict, note, how FROM im.item_verdicts WHERE item_id = %s
                   ORDER BY decided_at DESC, verdict_id DESC LIMIT 1""", (item_id,)).fetchone()
            if latest == (verdict, note, how):   # e.g. a client resent a queued event whose response was lost
                result = "repeat"
            else:
                inserted = conn.execute(
                    """INSERT INTO im.item_verdicts (item_id, segment_ids, quote, verdict, note, how)
                       SELECT item_id, source_segment_ids, quote, %s, %s, %s FROM im.items WHERE item_id = %s""",
                    (verdict, note, how, item_id)).rowcount
                result = verdict if inserted else "no such item"
            conn.execute("INSERT INTO im.feedback_applied (event_id, result) VALUES (%s, %s)", (event_id, result))
        done[result] = done.get(result, 0) + 1
    return done


# --- Carrying verdicts forward ----------------------------------------------------------------------------------

CARRY_MIN_SCORE = 0.4   # calibrated on the 2026-10-06 re-transcription: same captures scored >= 0.44, others <= 0.3
CARRY_METHOD = f"segment-descent;quote-containment>={CARRY_MIN_SCORE}"
MIN_WORDS = 4
FILLERS = {"uh", "um", "er", "ah", "eh", "hmm", "mm"}


def _words(text: str) -> list[str]:
    # Letters and digits in any script: Hearsay writes Spanish as Spanish.
    return [w for w in re.findall(r"[^\W_]+", text.lower().replace("'", "").replace("’", "")) if w not in FILLERS]


def quote_score(a: str, b: str, floor: int = MIN_WORDS) -> float:
    """The share of the shorter quote's words found, in order, in the other. A re-extraction often quotes more or
    less of the same speech, and re-transcription changes fillers and small words. Quotes count as at least `floor`
    words long, so one shared word in a two-word quote isn't a match."""
    A, B = _words(a), _words(b)
    if not A or not B:
        return 0.0
    matched = sum(m.size for m in difflib.SequenceMatcher(None, A, B, autojunk=False).get_matching_blocks())
    return matched / max(min(len(A), len(B)), floor)


def carry_verdicts(conn) -> dict:
    """Copy my verdicts from items no longer current (on no current episode, and not of the archive) to the current
    items they became.

    A retired item's verdict can go to a current item whose source segments descend from the verdict's segments
    (through supersession links) and whose quote matches (`quote_score`). Matches are one-to-one, best first. A new
    item I've already decided on keeps my verdict. Either way the old item is settled and never carried again; one
    with no match yet is tried again on later runs. It counts as `waiting` while some episode its speech went to
    hasn't been read by Claude yet, else as `not_captured_again`: Claude read that speech again and captured
    nothing like it, so only a new prompt could still carry it.
    """
    descendants = """WITH RECURSIVE d(n, seg) AS (
             SELECT * FROM unnest(%s::int[], %s::uuid[])
             UNION SELECT d.n, x.new_segment_id FROM d JOIN im.source_supersessions x ON x.old_segment_id = d.seg)"""
    rows = conn.execute(
        """SELECT v.item_id, v.verdict_id, v.segment_ids, v.quote FROM im.item_verdicts v
           WHERE NOT EXISTS (SELECT 1 FROM im.items c LEFT JOIN im.episodes e USING (episode_id)
                             WHERE c.item_id = v.item_id AND (e.current OR c.episode_id IS NULL))
             AND NOT EXISTS (SELECT 1 FROM im.verdict_carries k WHERE k.from_item_id = v.item_id)
           ORDER BY v.decided_at, v.verdict_id""").fetchall()
    # Items with the same quote and segments are one capture decided on more than once: one source, latest wins.
    groups: dict = {}
    for item_id, verdict_id, segs, quote in rows:
        g = groups.setdefault((quote, tuple(sorted(segs))), {"items": set(), "verdicts": []})
        g["items"].add(item_id)
        g["verdicts"].append(verdict_id)
    if not groups:
        return {"carried": 0, "already_decided": 0, "waiting": 0, "not_captured_again": 0}
    keys = list(groups)
    pairs = [(n, seg) for n, (_, segs) in enumerate(keys) for seg in segs]
    cands = conn.execute(
        descendants + """
           SELECT DISTINCT d.n, i.item_id, i.quote,
                  EXISTS (SELECT 1 FROM im.item_verdicts v WHERE v.item_id = i.item_id)
                    OR EXISTS (SELECT 1 FROM pub.feedback_events f
                               WHERE f.item_id = i.item_id AND f.kind IN ('item_keep', 'item_discard', 'item_star'))
           FROM d JOIN im.items i ON i.source_segment_ids @> ARRAY[d.seg]
           JOIN im.episodes e ON e.episode_id = i.episode_id AND e.current""",
        ([n for n, _ in pairs], [s for _, s in pairs])).fetchall()
    scored = []
    for n, item_id, quote, decided in cands:
        score = quote_score(keys[n][0], quote)
        if score >= CARRY_MIN_SCORE:
            scored.append((-score, str(item_id), n, item_id, decided))
    stats = {"carried": 0, "already_decided": 0}
    taken, done = set(), set()
    with conn.transaction():
        for neg_score, _, n, item_id, decided in sorted(scored):
            if n in done or item_id in taken:
                continue
            done.add(n)
            taken.add(item_id)
            g = groups[keys[n]]
            if not decided:
                conn.execute(
                    """INSERT INTO im.item_verdicts (item_id, segment_ids, quote, verdict, note, how, decided_at,
                                                    carried_from)
                       SELECT i.item_id, i.source_segment_ids, i.quote, v.verdict, v.note, v.how, v.decided_at,
                              v.verdict_id
                       FROM im.item_verdicts v, im.items i WHERE v.verdict_id = ANY(%s) AND i.item_id = %s
                       ORDER BY v.decided_at, v.verdict_id""", (g["verdicts"], item_id))
            with conn.cursor() as cur:
                cur.executemany(
                    """INSERT INTO im.verdict_carries (from_item_id, to_item_id, score, carried, method)
                       VALUES (%s, %s, %s, %s, %s)""",
                    [(old, item_id, -neg_score, not decided, CARRY_METHOD) for old in g["items"]])
            stats["already_decided" if decided else "carried"] += 1
    left = [n for n in range(len(keys)) if n not in done]
    unread = {r[0] for r in conn.execute(
        descendants + """
           SELECT DISTINCT d.n FROM d JOIN im.episode_segments es ON es.segment_id = d.seg
           JOIN im.episodes e ON e.episode_id = es.episode_id AND e.current
           WHERE d.n = ANY(%s) AND e.kind <> 'others_only'
             AND NOT EXISTS (SELECT 1 FROM im.extractions x WHERE x.episode_id = e.episode_id)""",
        ([n for n, _ in pairs], [s for _, s in pairs], left))} if left else set()
    return stats | {"waiting": len(unread), "not_captured_again": len(left) - len(unread)}
