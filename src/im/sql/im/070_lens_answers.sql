-- M5 slice 4 (docs/design/m5-lenses-and-mcp.md, Query kinds; decision 0013): the `jev` kind of lens query, and the
-- answers a run keeps.
--
-- A `jev` query is a set of Nouls (each a question, with its criteria or none), a rule combining their
-- probabilities (`max`: any one; `mean`: one question in several wordings) and its cut: a card is in when the rule
-- gives at least the cut. It's the shape lens_audit's pilots measured, so a lens judged there is imported as it was.
CREATE OR REPLACE FUNCTION im.lens_query_ok(q jsonb) RETURNS boolean LANGUAGE sql IMMUTABLE SECURITY DEFINER
SET search_path = pg_catalog, im
AS $$
SELECT CASE
  WHEN q IS NULL OR jsonb_typeof(q) <> 'object' THEN false
  WHEN coalesce(q->>'kind', '') IN ('all', 'none') THEN NOT EXISTS (SELECT 1 FROM jsonb_object_keys(q) k WHERE k <> 'kind')
  WHEN q->>'kind' <> 'jev' THEN false
  WHEN EXISTS (SELECT 1 FROM jsonb_object_keys(q) k WHERE k NOT IN ('kind', 'deciding', 'rule', 'cut')) THEN false
  WHEN coalesce(q->>'rule', '') NOT IN ('max', 'mean') THEN false
  WHEN coalesce(jsonb_typeof(q->'cut'), '') <> 'number' THEN false
  WHEN NOT ((q->>'cut')::numeric > 0 AND (q->>'cut')::numeric < 1) THEN false
  WHEN coalesce(jsonb_typeof(q->'deciding'), '') <> 'object' THEN false
  ELSE (SELECT count(*) BETWEEN 1 AND 40 AND bool_and(
          d.key ~ '^[a-z][a-z0-9_]{0,40}$'
          AND CASE WHEN jsonb_typeof(d.value) <> 'array' OR jsonb_array_length(d.value) <> 2 THEN false
                   WHEN coalesce(jsonb_typeof(d.value->0), '') <> 'string' THEN false
                   WHEN length(d.value->>0) NOT BETWEEN 1 AND 500 THEN false
                   WHEN jsonb_typeof(d.value->1) = 'null' THEN true
                   WHEN jsonb_typeof(d.value->1) <> 'object' THEN false
                   ELSE (SELECT array_agg(k ORDER BY k) FROM jsonb_object_keys(d.value->1) k) = ARRAY['false', 'true']
                        AND jsonb_typeof(d.value->1->'true') = 'string' AND jsonb_typeof(d.value->1->'false') = 'string'
              END)
        FROM jsonb_each(q->'deciding') d)
END
$$;

-- Jev's answer about a card for a lens: under which query (its version) and model, about which text (its hash, as
-- lens_run shows the card), each Noul's probability and the rule's, and the request it came from. One per lens and
-- card, the latest; derived, so it goes with the cluster and is asked again when the text or the query changes.
CREATE TABLE im.lens_answers (
  lens_id        uuid NOT NULL REFERENCES im.lenses(lens_id) ON DELETE CASCADE,
  idea_id        uuid NOT NULL REFERENCES im.ideas(idea_id) ON DELETE CASCADE,
  query_version  text NOT NULL,
  model          text NOT NULL,
  text_sha256    text NOT NULL,
  answers        jsonb NOT NULL,
  p              real NOT NULL CHECK (p >= 0 AND p <= 1),
  egress_id      bigint NULL REFERENCES im.egress_log(egress_id) ON DELETE SET NULL,
  asked_at       timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (lens_id, idea_id)
);
CREATE INDEX ON im.lens_answers (idea_id);

-- 069's members, and a `jev` lens's: the cards its answers under its current query put at or above its cut, with
-- that probability as the score. A card not asked yet is in no `jev` lens: it isn't a no, and the lens's judged share
-- says how much it has asked.
CREATE OR REPLACE VIEW pub.lens_members AS
SELECT p.lens_id, p.idea_id, 'pin'::text AS why, NULL::real AS score
FROM im.lens_pinned p JOIN pub.cards c USING (idea_id)
WHERE p.pin = 'in'
UNION ALL
SELECT l.lens_id, c.idea_id, 'query', a.p
FROM im.lenses l
JOIN pub.cards c ON l.query->>'kind' IN ('all', 'jev')
LEFT JOIN im.lens_answers a ON l.query->>'kind' = 'jev' AND a.lens_id = l.lens_id AND a.idea_id = c.idea_id
                               AND a.query_version = l.query_version
LEFT JOIN pub.sensitivity s ON s.kind = 'idea' AND s.id = c.idea_id
WHERE (l.query->>'kind' = 'all' OR a.p >= (l.query->>'cut')::real)
  AND im.lens_admits(l.filters, c.maturity, c.kept_as, c.by_me, c.first_said, c.last_seen, coalesce(s.sensitive, false))
  AND NOT EXISTS (SELECT 1 FROM im.lens_pinned p WHERE p.lens_id = l.lens_id AND p.idea_id = c.idea_id);

-- How much of the map each lens's query has judged: the cards answered under its current query. `all` and `none`
-- ask nothing, so they've judged every card.
CREATE VIEW pub.lens_judged AS
SELECT l.lens_id,
       CASE WHEN l.query->>'kind' = 'jev'
            THEN (SELECT count(*) FROM im.lens_answers a JOIN pub.cards c USING (idea_id)
                  WHERE a.lens_id = l.lens_id AND a.query_version = l.query_version)
            ELSE (SELECT count(*) FROM pub.cards) END AS asked,
       (SELECT count(*) FROM pub.cards) AS cards
FROM im.lenses l;
GRANT SELECT ON pub.lens_judged TO lattice_app;
