"""Invariant queries. Each returns offending rows; empty means the invariant holds."""

from im.citations import ORIGIN_OBSERVATIONS

# Every current segment is in exactly one current episode.
ORPHANS = """
SELECT c.segment_id, count(e.episode_id) AS current_episodes
FROM im.current_segments c
LEFT JOIN im.episode_segments es ON es.segment_id = c.segment_id
LEFT JOIN im.episodes e ON e.episode_id = es.episode_id AND e.current
GROUP BY c.segment_id
HAVING count(e.episode_id) <> 1
"""

# No current episode holds a segment that isn't current.
STALE = """
SELECT es.episode_id, es.segment_id
FROM im.episode_segments es JOIN im.episodes e USING (episode_id)
WHERE e.current
  AND NOT EXISTS (SELECT 1 FROM im.current_segments c WHERE c.segment_id = es.segment_id)
"""

# Nothing derived refers to a tombstoned segment: no episode, observation or connection verdict holds one, and no idea
# was made from one. Nor does a lens pin, my input anchored to speech.
TOMBSTONED = """
SELECT es.episode_id, es.segment_id
FROM im.episode_segments es JOIN im.source_tombstones t USING (segment_id)
UNION ALL
SELECT o.observation_id, t.segment_id
FROM im.observations o JOIN im.source_tombstones t ON t.segment_id = ANY(o.segment_ids)
UNION ALL
SELECT d.idea_id, t.segment_id
FROM im.ideas d JOIN im.source_tombstones t ON t.segment_id = ANY(d.segment_ids)
UNION ALL
SELECT v.a, t.segment_id
FROM im.connection_verdicts v JOIN im.source_tombstones t ON t.segment_id = ANY(v.segment_ids)
UNION ALL
SELECT p.lens_id, t.segment_id
FROM im.lens_pins p JOIN im.source_tombstones t ON t.segment_id = ANY(p.segment_ids)
"""

# Every edge cites what exists (migration 035): no observation that's gone, and nothing missing when an end has one.
# Edges are connections and lineage (migration 045).
UNCITED = f"""
WITH origin AS ({ORIGIN_OBSERVATIONS}),
edges AS (SELECT a, b, 'connection' AS kind, cited_observation_ids FROM im.connections
          UNION ALL SELECT from_id, to_id, kind, cited_observation_ids FROM im.lineage)
SELECT l.a, l.b, l.kind FROM edges l
WHERE EXISTS (SELECT 1 FROM unnest(l.cited_observation_ids) c
              WHERE NOT EXISTS (SELECT 1 FROM im.observations o WHERE o.observation_id = c))
   OR (cardinality(l.cited_observation_ids) = 0 AND EXISTS (SELECT 1 FROM origin WHERE origin.idea_id IN (l.a, l.b)))
"""

# A cluster merged away (migration 060) holds nothing a cluster has: no capture, match decision, typed observation,
# edge, connection verdict or embedding, and it's on no card and in no pub.ideas.
MERGED = """
WITH gone AS (SELECT other_id AS idea_id FROM im.idea_merges)
SELECT g.idea_id, 'capture' FROM gone g JOIN im.idea_evidence e USING (idea_id)
UNION ALL SELECT g.idea_id, 'match' FROM gone g JOIN im.item_matches m USING (idea_id)
UNION ALL SELECT g.idea_id, 'observation' FROM gone g JOIN im.observations o USING (idea_id)
UNION ALL SELECT g.idea_id, 'connection' FROM gone g JOIN im.connections c ON g.idea_id IN (c.a, c.b)
UNION ALL SELECT g.idea_id, 'lineage' FROM gone g JOIN im.lineage l ON g.idea_id IN (l.from_id, l.to_id)
UNION ALL SELECT g.idea_id, 'verdict' FROM gone g JOIN im.connection_verdicts v ON g.idea_id IN (v.a, v.b)
UNION ALL SELECT g.idea_id, 'embedding' FROM gone g JOIN im.idea_embeddings e USING (idea_id)
UNION ALL SELECT g.idea_id, 'card' FROM gone g JOIN pub.cards c USING (idea_id)
UNION ALL SELECT g.idea_id, 'pub.ideas' FROM gone g JOIN pub.ideas d USING (idea_id)
"""

CHECKS = {"orphans": ORPHANS, "stale": STALE, "tombstoned": TOMBSTONED, "uncited": UNCITED, "merged": MERGED}


def run_checks(conn) -> dict[str, list]:
    return {name: conn.execute(sql).fetchall() for name, sql in CHECKS.items()}
