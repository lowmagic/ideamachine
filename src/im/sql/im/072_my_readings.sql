-- Correcting a capture's reading (the quote repaired and resolved, 047) from its card's edit page, as I correct a
-- card's title and statement (011). An `item_reading` event names the capture and carries the reading, an empty one
-- reverting to Claude's. Like every event on a capture it holds the capture's segments (041), so forgetting finds
-- it. My latest reading on any capture of an occurrence counts for every capture of it (im.item_reading): a re-read
-- that captures the same speech again keeps my wording.

ALTER TABLE pub.feedback_events DROP CONSTRAINT feedback_events_kind_check;
ALTER TABLE pub.feedback_events ADD CONSTRAINT feedback_events_kind_check CHECK (kind IN (
  'star', 'unstar', 'keep', 'discard', 'link', 'unlink', 'pin_theme', 'unpin_theme', 'rename_theme', 'reject_theme',
  'item_keep', 'item_discard', 'item_star', 'idea_correct', 'idea_note', 'idea_compare', 'digest_star',
  'digest_dismiss', 'idea_add', 'merge_theme', 'split_theme', 'file_idea', 'unfile_idea', 'idea_sensitive',
  'theme_sensitive', 'vocab_add', 'vocab_remove', 'idea_keep', 'idea_unkeep', 'idea_dismiss', 'idea_distinct',
  'demo_on', 'demo_off', 'lens_add', 'lens_edit', 'lens_state', 'lens_pin', 'lens_unpin', 'board_place',
  'board_remove', 'item_reading'));
ALTER TABLE pub.feedback_events ADD CONSTRAINT feedback_events_item_reading CHECK (
  kind <> 'item_reading' OR (
    item_id IS NOT NULL AND jsonb_typeof(payload->'reading') = 'string' AND length(payload->>'reading') <= 2000));

-- Every capture's reading as it counts: my latest on any capture of its occurrence, else Claude's.
CREATE VIEW im.item_reading AS
SELECT i.item_id, i.reading AS claude_reading, coalesce(nullif(btrim(m.reading), ''), i.reading) AS reading,
       nullif(btrim(m.reading), '') IS NOT NULL AS corrected
FROM im.items i
LEFT JOIN LATERAL (
  SELECT f.payload->>'reading' AS reading FROM pub.feedback_events f JOIN im.items s ON s.item_id = f.item_id
  WHERE f.kind = 'item_reading' AND (s.item_id = i.item_id OR s.observation_id = i.observation_id)
  ORDER BY f.event_id DESC LIMIT 1) m ON true;

-- A card's captures with their readings (at the end; the observation-only rows have none).
CREATE OR REPLACE VIEW pub.card_evidence AS
SELECT ev.idea_id, i.item_id, i.observation_id, w.speaker, i.quote, i.gist, e.started_at AS said_at,
       (SELECT min(s.conversation_id) FROM im.source_segments s WHERE s.segment_id = ANY(i.source_segment_ids))
         AS conversation_id,
       'speech'::text AS source, a.arousal, r.reading, r.claude_reading
FROM im.idea_evidence ev
JOIN im.items i USING (item_id)
JOIN im.episodes e ON e.episode_id = i.episode_id AND e.current
JOIN im.item_speaker w USING (item_id)
JOIN im.item_reading r USING (item_id)
LEFT JOIN im.observation_arousal a ON a.observation_id = i.observation_id
UNION ALL
SELECT ev.idea_id, i.item_id, i.observation_id, w.speaker, i.quote, i.gist, o.said_at, o.conversation, o.source,
       NULL::real, r.reading, r.claude_reading
FROM im.idea_evidence ev
JOIN im.items i USING (item_id)
JOIN im.observations o ON o.observation_id = i.observation_id
JOIN im.item_speaker w USING (item_id)
JOIN im.item_reading r USING (item_id)
WHERE i.episode_id IS NULL
UNION ALL
SELECT o.idea_id, NULL, o.observation_id, o.said_by, o.quote, NULL, o.said_at, o.conversation, o.source, NULL::real,
       NULL, NULL
FROM im.observations o WHERE o.source <> 'speech' AND o.idea_id IS NOT NULL;

CREATE OR REPLACE VIEW pub.demo_card_evidence AS
SELECT e.* FROM pub.card_evidence e WHERE e.idea_id NOT IN (SELECT idea_id FROM pub.demo_hidden_ideas);
