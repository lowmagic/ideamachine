-- M5 slice 5 (docs/design/m5-lenses-and-mcp.md, "Themes become lenses"; decision 0013): a theme is a lens. Every
-- lens not archived is listed as a theme (pub.themes, its columns as they were), an idea's themes are the lenses it's
-- in (pub.ideas.themes), and the theme events (024, 027) apply to lenses: pin and unpin keep a lens at hand or not,
-- rename renames it, a merge moves the pins and archives the lens merged away, a split makes a lens of the ideas
-- given (pinned in there, pinned out of the source), and filing or unfiling an idea pins it in or out. Themes have no
-- proposals any more (the proposer goes), so reject applies to none. The filer, the proposer and the describer go
-- with their tables: im.themes, im.idea_themes, im.theme_checks, im.theme_rejections and im.idea_theme_blocks. Any
-- theme there is moved into a lens first (its id kept, its members pinned in), so nothing I shaped is lost.

-- A cluster with no speech, nothing typed and no archive capture (one inserted by hand, as the test fixtures do) is
-- anchored to its own id, which a merge follows and a rebuild doesn't keep.
CREATE OR REPLACE FUNCTION im.idea_anchor(idea uuid, OUT segment_ids uuid[], OUT source_ref text) LANGUAGE plpgsql
STABLE SET search_path = pg_catalog, im, pub
AS $$
DECLARE
  d im.ideas;
BEGIN
  SELECT * INTO d FROM im.ideas
  WHERE idea_id = coalesce((SELECT m.into_id FROM im.merged_into m WHERE m.idea_id = idea), idea);
  IF NOT FOUND THEN RETURN; END IF;
  segment_ids := ARRAY(SELECT DISTINCT s FROM unnest(d.segment_ids) AS s ORDER BY s);
  IF cardinality(segment_ids) > 0 THEN RETURN; END IF;
  source_ref := coalesce(
    (SELECT 'idea_add:' || f.event_id FROM pub.feedback_events f
     WHERE f.kind = 'idea_add' AND f.idea_id = d.idea_id ORDER BY f.event_id LIMIT 1),
    (SELECT o.source_ref FROM im.observations o
     WHERE o.idea_id = d.idea_id AND o.source = 'typed' ORDER BY o.first_seen, o.observation_id LIMIT 1));
  IF source_ref IS NOT NULL THEN RETURN; END IF;
  segment_ids := (SELECT ARRAY(SELECT DISTINCT s FROM unnest(i.source_segment_ids) AS s ORDER BY s)
                  FROM im.idea_evidence ev JOIN im.items i USING (item_id)
                  JOIN im.episodes e ON e.episode_id = i.episode_id AND e.current
                  WHERE ev.idea_id = d.idea_id AND cardinality(i.source_segment_ids) > 0
                  ORDER BY ev.relation = 'origin' DESC, i.created_at, i.item_id LIMIT 1);
  IF cardinality(segment_ids) > 0 THEN RETURN; END IF;
  segment_ids := '{}';
  source_ref := coalesce(
    (SELECT o.source_ref FROM im.idea_evidence ev JOIN im.items i USING (item_id)
     JOIN im.observations o ON o.observation_id = i.observation_id
     WHERE ev.idea_id = d.idea_id AND o.source_ref IS NOT NULL
     ORDER BY ev.relation = 'origin' DESC, i.item_id LIMIT 1),
    'idea:' || d.idea_id);
END $$;

CREATE OR REPLACE VIEW im.lens_pin_ideas AS
WITH RECURSIVE seg (pin_id, seg) AS (
  SELECT p.pin_id, u.seg FROM im.lens_pins p CROSS JOIN unnest(p.segment_ids) AS u(seg)
  UNION
  SELECT s.pin_id, x.new_segment_id FROM seg s JOIN im.source_supersessions x ON x.old_segment_id = s.seg
), held (pin_id, idea_id) AS (
  SELECT s.pin_id, d.idea_id FROM seg s JOIN im.ideas d ON d.segment_ids @> ARRAY[s.seg]
  UNION
  SELECT s.pin_id, ev.idea_id FROM seg s
  JOIN im.items i ON i.source_segment_ids @> ARRAY[s.seg]
  JOIN im.episodes e ON e.episode_id = i.episode_id AND e.current
  JOIN im.idea_evidence ev ON ev.item_id = i.item_id
  UNION
  SELECT p.pin_id, coalesce(o.idea_id, ev.idea_id) FROM im.lens_pins p
  JOIN im.observations o ON o.source_ref = p.source_ref
  LEFT JOIN im.items i ON i.observation_id = o.observation_id
  LEFT JOIN im.idea_evidence ev ON ev.item_id = i.item_id
  WHERE coalesce(o.idea_id, ev.idea_id) IS NOT NULL
  UNION
  SELECT p.pin_id, f.idea_id FROM im.lens_pins p
  JOIN pub.feedback_events f ON f.kind = 'idea_add' AND p.source_ref = 'idea_add:' || f.event_id
  WHERE f.idea_id IS NOT NULL
  UNION
  SELECT p.pin_id, d.idea_id FROM im.lens_pins p JOIN im.ideas d ON p.source_ref = 'idea:' || d.idea_id
)
SELECT DISTINCT p.pin_id, p.lens_id, p.pin, p.event_id, coalesce(m.into_id, h.idea_id) AS idea_id
FROM held h JOIN im.lens_pins p USING (pin_id)
LEFT JOIN im.merged_into m ON m.idea_id = h.idea_id;

-- Pin a cluster in or out of a lens, anchored to its speech (im.idea_anchor); false if it's no cluster.
CREATE FUNCTION im.pin_idea(lid uuid, idea uuid, how text, event bigint) RETURNS boolean LANGUAGE plpgsql
SET search_path = pg_catalog, im, pub
AS $$
DECLARE
  a record;
BEGIN
  SELECT * INTO a FROM im.idea_anchor(idea);
  IF coalesce(cardinality(a.segment_ids), 0) = 0 AND a.source_ref IS NULL THEN RETURN false; END IF;
  INSERT INTO im.lens_pins (lens_id, pin, segment_ids, source_ref, event_id)
  VALUES (lid, how, a.segment_ids, a.source_ref, event)
  ON CONFLICT (lens_id, segment_ids, coalesce(source_ref, ''))
  DO UPDATE SET pin = EXCLUDED.pin, event_id = EXCLUDED.event_id, pinned_at = now();
  RETURN true;
END $$;

-- Themes into lenses, ids kept: each a lens that holds only what I pin, kept at hand if it was pinned; its members
-- pinned in. A name another theme already took (case aside) is left out, and so are its members.
INSERT INTO im.lenses (lens_id, name, description, query, state, created_at)
SELECT t.theme_id, btrim(t.name), coalesce(t.description, ''), '{"kind": "none"}',
       CASE WHEN t.pinned THEN 'pinned' ELSE 'active' END, t.created_at
FROM im.themes t ORDER BY t.created_at, t.theme_id
ON CONFLICT DO NOTHING;
SELECT im.pin_idea(it.theme_id, it.idea_id, 'in', 0)
FROM im.idea_themes it JOIN im.lenses l ON l.lens_id = it.theme_id;

-- Jev's tags on themes become its tags on lenses.
CREATE TABLE im.lens_sensitivity (
  lens_id         uuid PRIMARY KEY REFERENCES im.lenses(lens_id) ON DELETE CASCADE,
  categories      text[] NOT NULL DEFAULT '{}',
  probabilities   jsonb NULL,
  input_hash      text NOT NULL,
  tagger_version  text NOT NULL,
  tagged_at       timestamptz NOT NULL DEFAULT now()
);
INSERT INTO im.lens_sensitivity (lens_id, categories, probabilities, input_hash, tagger_version, tagged_at)
SELECT s.theme_id, s.categories, s.probabilities, s.input_hash, s.tagger_version, s.tagged_at
FROM im.theme_sensitivity s JOIN im.lenses l ON l.lens_id = s.theme_id;

-- A theme event names a lens now. Events on a theme that didn't become a lens keep its id in their payload.
ALTER TABLE pub.feedback_events DROP CONSTRAINT feedback_events_theme_id_fkey;
UPDATE pub.feedback_events f SET payload = f.payload || jsonb_build_object('theme_gone', f.theme_id), theme_id = NULL
WHERE f.theme_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM im.lenses l WHERE l.lens_id = f.theme_id);
ALTER TABLE pub.feedback_events ADD CONSTRAINT feedback_events_theme_id_fkey
  FOREIGN KEY (theme_id) REFERENCES im.lenses(lens_id) ON DELETE SET NULL;

CREATE OR REPLACE FUNCTION im.theme_event_names_theme() RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, im, pub
AS $$
BEGIN
  IF NEW.theme_id IS NULL THEN
    RAISE EXCEPTION '% needs a theme_id', NEW.kind USING ERRCODE = 'check_violation';
  END IF;
  IF NOT EXISTS (SELECT 1 FROM im.lenses WHERE lens_id = NEW.theme_id) THEN
    NEW.payload := NEW.payload || jsonb_build_object('theme_gone', NEW.theme_id);
    NEW.theme_id := NULL;
  END IF;
  RETURN NEW;
END $$;

-- A theme event, applied to its lens. What no longer applies is recorded, not refused, as before (024).
CREATE OR REPLACE FUNCTION im.apply_theme_event(e pub.feedback_events) RETURNS text LANGUAGE plpgsql
SET search_path = pg_catalog, im, pub
AS $$
DECLARE
  l im.lenses;
  v_name text := btrim(coalesce(e.payload->>'name', ''));
  new_id uuid;
  moved int := 0;
  idea uuid;
BEGIN
  SELECT * INTO l FROM im.lenses WHERE lens_id = e.theme_id;
  IF NOT FOUND THEN
    -- A split resent after it applied names a source that may be gone; the new lens says it already happened.
    IF e.kind = 'split_theme' AND EXISTS (SELECT 1 FROM im.lenses WHERE lens_id = (e.payload->>'new_theme_id')::uuid)
    THEN RETURN 'repeat'; END IF;
    RETURN 'no such theme';
  END IF;
  IF e.kind = 'split_theme' AND EXISTS (SELECT 1 FROM im.lenses WHERE lens_id = (e.payload->>'new_theme_id')::uuid)
  THEN RETURN 'repeat'; END IF;
  IF e.kind IN ('rename_theme', 'split_theme') AND (v_name = '' OR EXISTS (
       SELECT 1 FROM im.lenses x WHERE lower(btrim(x.name)) = lower(v_name) AND x.lens_id <> l.lens_id
         OR e.kind = 'split_theme' AND lower(btrim(x.name)) = lower(v_name)))
  THEN
    RETURN CASE WHEN v_name = '' THEN 'no name given' ELSE 'name taken' END;
  END IF;

  IF e.kind = 'pin_theme' THEN
    UPDATE im.lenses SET state = 'pinned' WHERE lens_id = l.lens_id;
    RETURN 'pinned';
  ELSIF e.kind = 'unpin_theme' THEN
    UPDATE im.lenses SET state = 'active' WHERE lens_id = l.lens_id AND state = 'pinned';
    RETURN 'unpinned';
  ELSIF e.kind = 'rename_theme' THEN
    UPDATE im.lenses SET name = v_name WHERE lens_id = l.lens_id;
    RETURN 'renamed';
  ELSIF e.kind = 'reject_theme' THEN
    RETURN 'not rejected: only proposals can be, and themes are no longer proposed';
  ELSIF e.kind = 'merge_theme' THEN
    IF NOT EXISTS (SELECT 1 FROM im.lenses WHERE lens_id = (e.payload->>'into')::uuid) THEN
      RETURN 'no such theme to merge into';
    END IF;
    INSERT INTO im.lens_pins (lens_id, pin, segment_ids, source_ref, event_id)
    SELECT (e.payload->>'into')::uuid, p.pin, p.segment_ids, p.source_ref, e.event_id
    FROM im.lens_pins p WHERE p.lens_id = l.lens_id
    ON CONFLICT (lens_id, segment_ids, coalesce(source_ref, '')) DO NOTHING;   -- its own pin wins
    UPDATE im.lenses SET state = 'archived' WHERE lens_id = l.lens_id;
    RETURN 'merged';
  ELSIF e.kind = 'split_theme' THEN
    new_id := (e.payload->>'new_theme_id')::uuid;
    INSERT INTO im.lenses (lens_id, name, query, state) VALUES (new_id, v_name, '{"kind": "none"}', 'pinned');
    FOR idea IN
      SELECT m.idea_id FROM pub.lens_members m WHERE m.lens_id = l.lens_id
        AND m.idea_id::text IN (SELECT jsonb_array_elements_text(e.payload->'idea_ids'))
    LOOP
      PERFORM im.pin_idea(new_id, idea, 'in', e.event_id);
      PERFORM im.pin_idea(l.lens_id, idea, 'out', e.event_id);
      moved := moved + 1;
    END LOOP;
    RETURN 'split: ' || moved || ' moved';
  ELSIF e.kind IN ('file_idea', 'unfile_idea') THEN
    IF NOT im.pin_idea(l.lens_id, e.idea_id, CASE WHEN e.kind = 'file_idea' THEN 'in' ELSE 'out' END, e.event_id) THEN
      RETURN 'no such idea';
    END IF;
    RETURN CASE WHEN e.kind = 'file_idea' THEN 'filed' ELSE 'unfiled' END;
  END IF;
  RETURN 'not a theme event';
END $$;

-- pub.sensitivity as in 058, but for the clusters there are (not only those pub.ideas shows, which now reads the
-- lenses, which read this) and the lenses not archived, as themes.
CREATE OR REPLACE VIEW pub.sensitivity AS
WITH idea_marks AS (
  SELECT DISTINCT ON (f.idea_id) f.idea_id AS id, (f.payload->>'sensitive')::boolean AS sensitive
  FROM pub.feedback_events f WHERE f.kind = 'idea_sensitive' ORDER BY f.idea_id, f.event_id DESC
), theme_marks AS (
  SELECT DISTINCT ON (f.theme_id) f.theme_id AS id, (f.payload->>'sensitive')::boolean AS sensitive
  FROM pub.feedback_events f WHERE f.kind = 'theme_sensitive' AND f.theme_id IS NOT NULL
  ORDER BY f.theme_id, f.event_id DESC
), both_kinds AS (
  SELECT 'idea' AS kind, x.idea_id AS id, m.sensitive AS mine, s.categories
  FROM im.idea_text x LEFT JOIN idea_marks m ON m.id = x.idea_id LEFT JOIN im.idea_sensitivity s USING (idea_id)
  UNION ALL
  SELECT 'theme', l.lens_id, m.sensitive, s.categories
  FROM im.lenses l LEFT JOIN theme_marks m ON m.id = l.lens_id LEFT JOIN im.lens_sensitivity s USING (lens_id)
  WHERE l.state <> 'archived'
)
SELECT kind, id, coalesce(mine, cardinality(categories) > 0) AS sensitive, coalesce(categories, '{}') AS categories,
       CASE WHEN mine IS NOT NULL THEN 'owner' ELSE 'model' END AS "by"
FROM both_kinds
WHERE mine IS NOT NULL OR cardinality(categories) > 0;

-- An idea's themes: the lenses it's in, not archived.
CREATE OR REPLACE VIEW pub.ideas AS
SELECT d.idea_id, x.title, x.statement, d.origin, d.created_at,
       count(ev.item_id) AS n_evidence, min(ev.said_at) AS first_said,
       coalesce(t.names, '{}') AS themes,
       coalesce((SELECT f.kind = 'star' FROM pub.feedback_events f
                 WHERE f.idea_id = d.idea_id AND f.kind IN ('star', 'unstar') ORDER BY f.event_id DESC LIMIT 1),
                false) AS starred,
       x.my_note, x.claude_title, x.claude_statement,
       (x.title IS DISTINCT FROM x.claude_title OR x.statement IS DISTINCT FROM x.claude_statement) AS corrected,
       NULL::text AS notes, NULL::text AS archive_ref
FROM im.ideas d
JOIN im.idea_text x USING (idea_id)
LEFT JOIN pub.evidence ev ON ev.idea_id = d.idea_id
LEFT JOIN (SELECT m.idea_id, array_agg(l.name ORDER BY l.name) AS names
           FROM pub.lens_members m JOIN im.lenses l USING (lens_id) WHERE l.state <> 'archived'
           GROUP BY m.idea_id) t ON t.idea_id = d.idea_id
GROUP BY d.idea_id, x.title, x.statement, x.my_note, x.claude_title, x.claude_statement, t.names
HAVING d.origin = 'mine'
    OR count(*) FILTER (WHERE ev.item_id IS NOT NULL AND ev.verdict IS DISTINCT FROM 'discard') > 0;

-- Every lens not archived, as a theme: mine, pinned if kept at hand, with how many shown ideas it holds.
CREATE OR REPLACE VIEW pub.themes AS
SELECT l.lens_id AS theme_id, l.name, nullif(l.description, '') AS description, 'mine'::text AS origin,
       l.state = 'pinned' AS pinned, coalesce(n.n, 0) AS n_ideas, l.created_at
FROM im.lenses l
LEFT JOIN (SELECT m.lens_id, count(*) AS n FROM pub.lens_members m
           WHERE m.idea_id IN (SELECT idea_id FROM pub.ideas) GROUP BY m.lens_id) n USING (lens_id)
WHERE l.state <> 'archived';

DROP TABLE im.idea_theme_blocks, im.idea_themes, im.theme_checks, im.theme_rejections, im.theme_sensitivity,
  im.themes;
