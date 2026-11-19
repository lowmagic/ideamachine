-- Correcting a capture's reading (the quote repaired and resolved, 047) from its card's edit page, as I correct a
-- card's title and statement (011), with my mark of whether the correction changed what it means.
--
-- An `item_reading` event names the capture and carries the reading, an empty one reverting to Claude's. Like every
-- event on a capture it holds the capture's segments (041), so forgetting finds it. My latest reading on any capture
-- of an occurrence counts for every capture of it (im.item_reading): a re-read that captures the same speech again
-- keeps my wording.
--
-- A correction is right, and counts everywhere at once: the judges see it as changed input and ask about the card
-- again in their queues. `meaning_changed` (true or false, on `idea_correct` and `item_reading`) is my mark of what
-- kind of error it fixed: a mishearing or a meaning turned around, or only the wording. It decides nothing; it's what
-- `im corrections` sorts the errors by.

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
ALTER TABLE pub.feedback_events ADD CONSTRAINT feedback_events_meaning_changed CHECK (
  kind NOT IN ('idea_correct', 'item_reading') OR NOT payload ? 'meaning_changed'
  OR jsonb_typeof(payload->'meaning_changed') = 'boolean');

-- Every capture's reading as it counts: my latest on any capture of its occurrence, else Claude's; with my mark of
-- whether that correction changed the meaning (NULL: unmarked) and when I made it.
CREATE VIEW im.item_reading AS
SELECT i.item_id, i.reading AS claude_reading, coalesce(nullif(btrim(m.reading), ''), i.reading) AS reading,
       nullif(btrim(m.reading), '') IS NOT NULL AS corrected, m.meaning_changed, m.at AS corrected_at
FROM im.items i
LEFT JOIN LATERAL (
  SELECT f.payload->>'reading' AS reading, (f.payload->>'meaning_changed')::boolean AS meaning_changed, f.at
  FROM pub.feedback_events f JOIN im.items s ON s.item_id = f.item_id
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

-- A connection verdict replaced by another (the pair asked again: its text changed, or `im reread connect`) is kept
-- here, so what judging a corrected card again changed can be seen (`im corrections`). It cites what the verdict
-- did, so forgetting that speech forgets it too (pipeline._purge_tombstoned).
CREATE TABLE im.connection_verdicts_replaced (
  a            uuid NOT NULL REFERENCES im.ideas(idea_id) ON DELETE CASCADE,
  b            uuid NOT NULL REFERENCES im.ideas(idea_id) ON DELETE CASCADE,
  verdict      text NOT NULL,
  direction    text NULL,
  why          text NULL,
  method       text NOT NULL,
  input_hash   text NOT NULL,
  segment_ids  uuid[] NOT NULL DEFAULT '{}',
  judged_at    timestamptz NOT NULL,
  replaced_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ON im.connection_verdicts_replaced (a, b);
CREATE INDEX ON im.connection_verdicts_replaced (b);
CREATE INDEX ON im.connection_verdicts_replaced USING gin (segment_ids);
