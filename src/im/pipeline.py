"""`im run`: import Hearsay's stream, then (re)segment the affected conversations.

Everything is one REPEATABLE READ transaction, so a run sees one consistent
snapshot of im.* and leaves nothing half-done if it fails.
"""

import json
from pathlib import Path

import psycopg
from psycopg.rows import class_row

from im import citations
from im.config import Config
from im.importer import import_stream
from im.segment import Seg, segment

SCHEMA_VERSION = 1
SEG_SQL = """SELECT segment_id, conversation_id AS session_id, 'hearsay' AS source, started_at, ended_at,
                    speaker_label, speaker_kind, is_self, speaker_conf
             FROM im.current_segments WHERE conversation_id = ANY(%s)"""


def _start_run(conn, command: str) -> int:
    return conn.execute("INSERT INTO im.runs (command) VALUES (%s) RETURNING run_id", (command,)).fetchone()[0]


def _finish_run(conn, run_id: int, stats: dict) -> None:
    conn.execute("UPDATE im.runs SET finished_at = now(), stats = %s WHERE run_id = %s",
                 (json.dumps(stats), run_id))


def _purge_tombstoned(conn) -> set[str]:
    """Delete everything that holds a forgotten segment (ROADMAP §3.4): every episode (current or retired), idea,
    digest, verdict, observation and feedback event made from it; and take it out of the attention queue. Returns
    the conversations whose episodes went."""
    # An idea created from forgotten speech goes, with its feedback events (they cascade), found by the speech it
    # holds even once its capture is gone (migration 041), and so does every cluster merged into it (060), whose
    # captures had moved there. Ideas that merely had forgotten evidence keep their other evidence; that evidence row
    # goes with its item.
    conn.execute("""WITH RECURSIVE gone (idea_id) AS (
                      SELECT d.idea_id FROM im.ideas d WHERE EXISTS (
                        SELECT 1 FROM im.source_tombstones t WHERE t.segment_id = ANY(d.segment_ids))
                      UNION
                      SELECT m.other_id FROM im.idea_merges m JOIN gone g ON g.idea_id = m.keep_id)
                    DELETE FROM im.ideas d USING gone g WHERE d.idea_id = g.idea_id""")
    # The feedback events on a capture of forgotten speech, whether or not the capture is still there.
    conn.execute("""DELETE FROM pub.feedback_events f WHERE EXISTS (
                      SELECT 1 FROM im.source_tombstones t WHERE t.segment_id = ANY(f.item_segment_ids))""")
    # A digest may paraphrase forgotten speech anywhere in its text, so the whole digest goes.
    conn.execute("""DELETE FROM im.digests g WHERE EXISTS (
                      SELECT 1 FROM im.source_tombstones t WHERE t.segment_id = ANY(g.segment_ids))""")
    conn.execute("""DELETE FROM im.item_verdicts v WHERE EXISTS (
                      SELECT 1 FROM im.source_tombstones t WHERE t.segment_id = ANY(v.segment_ids))""")
    conn.execute("""DELETE FROM im.observations o WHERE EXISTS (
                      SELECT 1 FROM im.source_tombstones t WHERE t.segment_id = ANY(o.segment_ids))""")
    # A connection verdict's case (`why`, `analogy`) may paraphrase forgotten speech: it goes, and the pair is asked
    # again. The connection it wrote stays until then. So do the verdicts it replaced (migration 072).
    for table in ("connection_verdicts", "connection_verdicts_replaced"):
        conn.execute(f"""DELETE FROM im.{table} v WHERE EXISTS (
                           SELECT 1 FROM im.source_tombstones t WHERE t.segment_id = ANY(v.segment_ids))""")
    # A lens pin is my input, anchored to the speech it pins: forgetting that speech forgets the pin (migration 069).
    conn.execute("""DELETE FROM im.lens_pins p WHERE EXISTS (
                      SELECT 1 FROM im.source_tombstones t WHERE t.segment_id = ANY(p.segment_ids))""")
    # An attention item is my request, not derived from what was said: one whose own "note to self" is forgotten goes,
    # and every other loses only the forgotten lines it marked (migration 066).
    conn.execute("""DELETE FROM im.attention_items a WHERE EXISTS (
                      SELECT 1 FROM im.source_tombstones t WHERE t.segment_id = a.cue_segment_id)""")
    conn.execute("""UPDATE im.attention_items a SET segment_ids = ARRAY(
                      SELECT u.seg FROM unnest(a.segment_ids) WITH ORDINALITY AS u(seg, n)
                      WHERE NOT EXISTS (SELECT 1 FROM im.source_tombstones t WHERE t.segment_id = u.seg) ORDER BY u.n)
                    WHERE EXISTS (SELECT 1 FROM im.source_tombstones t WHERE t.segment_id = ANY(a.segment_ids))""")
    citations.uncite_missing(conn)   # an edge loses the forgotten observations it cited
    # What was sent can't be recalled, but the local copy of the text goes; the segment IDs stay so
    # `im forgotten --sent` can say what went out.
    conn.execute("""UPDATE im.egress_log g SET payload = NULL WHERE payload IS NOT NULL AND EXISTS (
                      SELECT 1 FROM im.source_tombstones t WHERE t.segment_id = ANY(g.segment_ids))""")
    rows = conn.execute(
        """DELETE FROM im.episodes e
           WHERE EXISTS (SELECT 1 FROM im.episode_segments es
                         JOIN im.source_tombstones t USING (segment_id)
                         WHERE es.episode_id = e.episode_id)
           RETURNING session_id""").fetchall()
    return {r[0] for r in rows}


def _sessions_needing_work(conn, stage_version: str) -> set[str]:
    """Conversations whose current episodes are out of date (a segment left, or another heuristic
    version built them), or that have current segments in no current episode (e.g. after a reset)."""
    rows = conn.execute(
        """SELECT e.session_id FROM im.episodes e
           WHERE e.current AND (
             e.stage_version <> %s OR EXISTS (
               SELECT 1 FROM im.episode_segments es
               WHERE es.episode_id = e.episode_id
                 AND NOT EXISTS (SELECT 1 FROM im.current_segments c WHERE c.segment_id = es.segment_id)))
           UNION
           SELECT c.conversation_id FROM im.current_segments c
           WHERE NOT EXISTS (SELECT 1 FROM im.episode_segments es JOIN im.episodes e USING (episode_id)
                             WHERE es.segment_id = c.segment_id AND e.current)""",
        (stage_version,)).fetchall()
    return {r[0] for r in rows}


def _resegment(conn, cfg: Config, sessions: set[str]) -> dict:
    scfg = cfg.segment
    by_session: dict[str, list[Seg]] = {s: [] for s in sessions}
    with conn.cursor(row_factory=class_row(Seg)) as cur:
        cur.execute(SEG_SQL, (list(sessions),))
        for s in cur:
            by_session[s.session_id].append(s)

    created = retired = 0
    for session_id in sorted(by_session):
        desired = {e.episode_id: e for e in segment(by_session[session_id], scfg)}
        existing = {r[0] for r in conn.execute(
            "SELECT episode_id FROM im.episodes WHERE session_id = %s AND current", (session_id,))}
        gone = existing - desired.keys()
        if gone:
            conn.execute("UPDATE im.episodes SET current = false, retired_at = now() WHERE episode_id = ANY(%s)",
                         (list(gone),))
            retired += len(gone)
        for eid in desired.keys() - existing:
            e = desired[eid]
            segs = e.segments
            conn.execute(
                """INSERT INTO im.episodes (episode_id, session_id, source, started_at, ended_at, kind,
                     n_segments, n_speakers, self_segments, schema_version, stage_version, input_hash)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                   ON CONFLICT (episode_id) DO UPDATE SET current = true, retired_at = NULL""",
                (eid, session_id, segs[0].source, min(s.started_at for s in segs),
                 max(s.ended_at for s in segs), e.kind, len(segs),
                 len({s.speaker_label for s in segs}), e.self_segments,
                 SCHEMA_VERSION, scfg.stage_version, e.input_hash))
            with conn.cursor() as cur:
                cur.executemany(
                    """INSERT INTO im.episode_segments (episode_id, segment_id, ord) VALUES (%s, %s, %s)
                       ON CONFLICT DO NOTHING""",
                    [(eid, s.segment_id, i) for i, s in enumerate(segs)])
            created += 1
    return {"sessions": len(by_session), "episodes_created": created, "episodes_retired": retired}


def run(conn: psycopg.Connection, cfg: Config, stream_dir: Path) -> dict:
    conn.isolation_level = psycopg.IsolationLevel.REPEATABLE_READ
    with conn.transaction():
        conn.execute("SELECT pg_advisory_xact_lock(hashtext('im.run'))")
        run_id = _start_run(conn, "run")
        sessions, stats = import_stream(conn, stream_dir)
        purged = _purge_tombstoned(conn)
        sessions |= purged
        sessions |= _sessions_needing_work(conn, cfg.segment.stage_version)
        stats |= {"stage_version": cfg.segment.stage_version, "sessions_with_purges": len(purged)}
        stats |= _resegment(conn, cfg, sessions)
        _finish_run(conn, run_id, stats)
    return stats | {"run_id": run_id}   # im run records the later stages on the same row


STAGES = ("segment", "extract")


def reset(conn: psycopg.Connection, stage: str) -> dict:
    """Drop a stage's derived rows. The next run rebuilds them for every current segment."""
    if stage not in STAGES:
        raise ValueError(f"unknown stage {stage!r}; known: {', '.join(STAGES)}")
    with conn.transaction():
        conn.execute("SELECT pg_advisory_xact_lock(hashtext('im.run'))")
        run_id = _start_run(conn, f"reset --stage {stage}")
        if stage == "segment":
            stats = {"episodes_deleted": conn.execute("DELETE FROM im.episodes").rowcount}
        else:
            # The egress log and feedback events stay, and so do the archive's captures (no episode): the next run
            # reads the stream, not the ChatGPT export.
            stats = {"items_deleted": conn.execute("DELETE FROM im.items WHERE episode_id IS NOT NULL").rowcount,
                     "extractions_deleted": conn.execute("DELETE FROM im.extractions").rowcount}
        _finish_run(conn, run_id, stats)
    return stats
