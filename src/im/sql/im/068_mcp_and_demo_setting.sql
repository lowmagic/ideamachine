-- M5 slice 1 (docs/design/m5-lenses-and-mcp.md): what the MCP server's 19 tools read, and demo mode as one setting.
--
-- pub.ideas gets back the columns Lattice's tools showed, appended: my note, Claude's wording beside my corrections,
-- and whether I corrected it (from im.idea_text), and the old archive's elaboration and reference, NULL since 057.
-- Same body as 058 otherwise.
CREATE OR REPLACE VIEW pub.ideas AS
SELECT d.idea_id, x.title, x.statement, d.origin, d.created_at,
       count(ev.item_id) AS n_evidence, min(ev.said_at) AS first_said,
       coalesce((SELECT array_agg(t.name ORDER BY t.name) FROM im.idea_themes it JOIN im.themes t USING (theme_id)
                 WHERE it.idea_id = d.idea_id), '{}') AS themes,
       coalesce((SELECT f.kind = 'star' FROM pub.feedback_events f
                 WHERE f.idea_id = d.idea_id AND f.kind IN ('star', 'unstar') ORDER BY f.event_id DESC LIMIT 1),
                false) AS starred,
       x.my_note, x.claude_title, x.claude_statement,
       (x.title IS DISTINCT FROM x.claude_title OR x.statement IS DISTINCT FROM x.claude_statement) AS corrected,
       NULL::text AS notes, NULL::text AS archive_ref
FROM im.ideas d
JOIN im.idea_text x USING (idea_id)
LEFT JOIN pub.evidence ev ON ev.idea_id = d.idea_id
GROUP BY d.idea_id, x.title, x.statement, x.my_note, x.claude_title, x.claude_statement
HAVING d.origin = 'mine'
    OR count(*) FILTER (WHERE ev.item_id IS NOT NULL AND ev.verdict IS DISTINCT FROM 'discard') > 0;

-- Search by meaning, as in 026: the k shown ideas nearest a query vector (unit length, in pub.embedding_model()),
-- nearest first. The web process embeds the query with the code that embedded the ideas (lattice.SentenceEmbedder).
-- A vector of the wrong size is refused.
CREATE FUNCTION pub.ideas_near(q vector, k int DEFAULT 10) RETURNS TABLE (idea_id uuid, similarity real)
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = pg_catalog, im, pub, public
AS $$
DECLARE
  model text := pub.embedding_model();
  dims int := (SELECT vector_dims(e.embedding) FROM im.idea_embeddings e WHERE e.model_version = model LIMIT 1);
BEGIN
  IF dims IS NULL THEN RETURN; END IF;
  IF vector_dims(q) <> dims THEN
    RAISE EXCEPTION 'query vector has % dimensions; % embeddings have %', vector_dims(q), model, dims
      USING ERRCODE = 'invalid_parameter_value';
  END IF;
  RETURN QUERY
    SELECT e.idea_id, (1 - (e.embedding <=> q))::real
    FROM im.idea_embeddings e
    WHERE e.model_version = model AND e.idea_id IN (SELECT d.idea_id FROM pub.ideas d)
    ORDER BY e.embedding <=> q, e.idea_id
    LIMIT greatest(1, least(coalesce(k, 10), 50));
END $$;
REVOKE ALL ON FUNCTION pub.ideas_near(vector, int) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION pub.ideas_near(vector, int) TO lattice_app;

-- Demo mode is one setting for every page and the MCP server: my latest demo_on or demo_off, the highest event_id
-- (043), off until I first turn it on. Neither names an idea or a theme.
ALTER TABLE pub.feedback_events DROP CONSTRAINT feedback_events_kind_check;
ALTER TABLE pub.feedback_events ADD CONSTRAINT feedback_events_kind_check CHECK (kind IN (
  'star', 'unstar', 'keep', 'discard', 'link', 'unlink', 'pin_theme', 'unpin_theme', 'rename_theme', 'reject_theme',
  'item_keep', 'item_discard', 'item_star', 'idea_correct', 'idea_note', 'idea_compare', 'digest_star',
  'digest_dismiss', 'idea_add', 'merge_theme', 'split_theme', 'file_idea', 'unfile_idea', 'idea_sensitive',
  'theme_sensitive', 'vocab_add', 'vocab_remove', 'idea_keep', 'idea_unkeep', 'idea_dismiss', 'idea_distinct',
  'demo_on', 'demo_off'));
ALTER TABLE pub.feedback_events ADD CONSTRAINT feedback_events_demo_names_nothing CHECK (
  kind NOT IN ('demo_on', 'demo_off') OR (idea_id IS NULL AND theme_id IS NULL AND other_idea_id IS NULL));

-- The app's settings, one row; `demo` alone so far. Not pub.demo_*: those are the demo versions of other views.
CREATE VIEW pub.app_settings AS
SELECT coalesce((SELECT f.kind = 'demo_on' FROM pub.feedback_events f WHERE f.kind IN ('demo_on', 'demo_off')
                 ORDER BY f.event_id DESC LIMIT 1), false) AS demo;
GRANT SELECT ON pub.app_settings TO lattice_app;
