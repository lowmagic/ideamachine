-- M5 slice 3 (docs/design/m5-lenses-and-mcp.md, Part 2; decision 0013): lenses stored, and boards of placed
-- lenses. A lens is a query, filters and pins: a cluster is in it if it's pinned in, or it passes the query and the
-- filters and isn't pinned out. Query kinds here are `all` (the filters decide) and `none` (only the pins); `jev`
-- comes with its run. Lenses are my input: made, edited, kept at hand or archived, and pinned by my events, applied
-- the moment they're inserted (a trigger), as theme events are (024). A lens is never deleted.
--
-- A pin is anchored to speech, not to a cluster id, so it survives a merge, a re-read and the rebuild (0013): the
-- segments the cluster was made from (im.ideas.segment_ids), or, for a cluster with none, the source reference of
-- what I typed (idea_add:<event id>) or of its archive capture's observation. It resolves to whichever clusters hold
-- that speech now. Forgetting a pin's segments deletes the pin (pipeline._purge_tombstoned).

-- The checks below run in pub.feedback_events' CHECKs, as whoever inserts the event (lattice_app, who can't see
-- schema im): SECURITY DEFINER, so they run as their owner, and aren't inlined into the caller's query.
CREATE FUNCTION im.is_uuid(t text) RETURNS boolean LANGUAGE sql IMMUTABLE SECURITY DEFINER
SET search_path = pg_catalog, im
AS $$ SELECT coalesce(t ~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$', false) $$;

-- A date as YYYY-MM-DD that is one (2026-02-30 isn't).
CREATE FUNCTION im.is_date(t text) RETURNS boolean LANGUAGE plpgsql IMMUTABLE SECURITY DEFINER
SET search_path = pg_catalog, im
AS $$
BEGIN
  IF t IS NULL OR t !~ '^\d{4}-\d{2}-\d{2}$' THEN RETURN false; END IF;
  PERFORM t::date;
  RETURN true;
EXCEPTION WHEN others THEN
  RETURN false;
END $$;

-- What a query may be: its kind, and that kind's parameters (none, for `all` and `none`).
CREATE FUNCTION im.lens_query_ok(q jsonb) RETURNS boolean LANGUAGE sql IMMUTABLE SECURITY DEFINER
SET search_path = pg_catalog, im
AS $$
SELECT CASE WHEN q IS NULL OR jsonb_typeof(q) <> 'object' THEN false
            ELSE coalesce(q->>'kind', '') IN ('all', 'none')
                 AND NOT EXISTS (SELECT 1 FROM jsonb_object_keys(q) k WHERE k <> 'kind') END
$$;

-- What filters may be: conditions on stored facts (pub.cards, pub.sensitivity), each optional. maturity and kept_as
-- are lists of what's admitted (kept_as `unkept` for a cluster I haven't kept); said_by_me and sensitive say which
-- way they must be; said_since and said_until bound when it was said, both days included.
CREATE FUNCTION im.lens_filters_ok(f jsonb) RETURNS boolean LANGUAGE sql IMMUTABLE SECURITY DEFINER
SET search_path = pg_catalog, im
AS $$
SELECT CASE WHEN f IS NULL OR jsonb_typeof(f) <> 'object' THEN false ELSE
  NOT EXISTS (SELECT 1 FROM jsonb_object_keys(f) k
              WHERE k NOT IN ('maturity', 'kept_as', 'said_by_me', 'sensitive', 'said_since', 'said_until'))
  AND (NOT f ? 'maturity' OR (jsonb_typeof(f->'maturity') = 'array' AND jsonb_array_length(f->'maturity') > 0
                              AND f->'maturity' <@ '["observation", "candidate", "idea"]'))
  AND (NOT f ? 'kept_as' OR (jsonb_typeof(f->'kept_as') = 'array' AND jsonb_array_length(f->'kept_as') > 0
                             AND f->'kept_as' <@ '["canonical", "pinned", "unkept"]'))
  AND (NOT f ? 'said_by_me' OR jsonb_typeof(f->'said_by_me') = 'boolean')
  AND (NOT f ? 'sensitive' OR jsonb_typeof(f->'sensitive') = 'boolean')
  AND (NOT f ? 'said_since' OR (jsonb_typeof(f->'said_since') = 'string' AND im.is_date(f->>'said_since')))
  AND (NOT f ? 'said_until' OR (jsonb_typeof(f->'said_until') = 'string' AND im.is_date(f->>'said_until'))) END
$$;

-- Whether a card's facts pass a lens's filters. STABLE: a day is compared in the session's time zone.
CREATE FUNCTION im.lens_admits(f jsonb, maturity text, kept_as text, by_me bigint, first_said timestamptz,
                               last_seen timestamptz, sensitive boolean) RETURNS boolean LANGUAGE sql STABLE
AS $$
SELECT (NOT f ? 'maturity' OR f->'maturity' ? maturity)
   AND (NOT f ? 'kept_as' OR f->'kept_as' ? coalesce(kept_as, 'unkept'))
   AND (NOT f ? 'said_by_me' OR (by_me > 0) = (f->>'said_by_me')::boolean)
   AND (NOT f ? 'sensitive' OR sensitive = (f->>'sensitive')::boolean)
   AND (NOT f ? 'said_since' OR last_seen >= (f->>'said_since')::date)
   AND (NOT f ? 'said_until' OR coalesce(first_said, last_seen) < (f->>'said_until')::date + 1)
$$;

CREATE TABLE im.lenses (
  lens_id        uuid PRIMARY KEY,   -- the sender's, so it can act on the lens it just made
  name           text NOT NULL CHECK (length(btrim(name)) BETWEEN 1 AND 80),
  description    text NOT NULL DEFAULT '' CHECK (length(description) <= 2000),
  query          jsonb NOT NULL CHECK (im.lens_query_ok(query)),
  query_version  text GENERATED ALWAYS AS (left(md5(query::text), 16)) STORED,
  filters        jsonb NOT NULL DEFAULT '{}' CHECK (im.lens_filters_ok(filters)),
  state          text NOT NULL DEFAULT 'active' CHECK (state IN ('active', 'pinned', 'archived')),
  created_at     timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX lenses_name ON im.lenses (lower(btrim(name)));

-- A pin: the lens, in or out, and the speech it's anchored to; one per lens and anchor, my latest deciding.
CREATE TABLE im.lens_pins (
  pin_id       bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  lens_id      uuid NOT NULL REFERENCES im.lenses(lens_id) ON DELETE CASCADE,
  pin          text NOT NULL CHECK (pin IN ('in', 'out')),
  segment_ids  uuid[] NOT NULL DEFAULT '{}',
  source_ref   text NULL,
  event_id     bigint NOT NULL,   -- my latest event on it
  pinned_at    timestamptz NOT NULL DEFAULT now(),
  CHECK ((cardinality(segment_ids) > 0) <> (source_ref IS NOT NULL))
);
CREATE UNIQUE INDEX lens_pins_anchor ON im.lens_pins (lens_id, segment_ids, coalesce(source_ref, ''));
CREATE INDEX ON im.lens_pins USING gin (segment_ids);

-- What a pin on a cluster is anchored to: the segments it was made from, sorted; else the reference of what I typed
-- (the first idea_add, before the run gives it an observation, and after); else the segments of one of its speech
-- captures on a current episode (the first it got); else its archive capture's observation. A cluster merged away
-- is anchored as the one it's part of now. Neither, if it has nothing to anchor to.
CREATE FUNCTION im.idea_anchor(idea uuid, OUT segment_ids uuid[], OUT source_ref text) LANGUAGE plpgsql STABLE
SET search_path = pg_catalog, im, pub
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
  source_ref := (SELECT o.source_ref FROM im.idea_evidence ev JOIN im.items i USING (item_id)
                 JOIN im.observations o ON o.observation_id = i.observation_id
                 WHERE ev.idea_id = d.idea_id AND o.source_ref IS NOT NULL
                 ORDER BY ev.relation = 'origin' DESC, i.item_id LIMIT 1);
END $$;

-- Each pin with the clusters that hold its speech now: by segments (following re-transcriptions), the clusters made
-- from them and those whose current captures cite them; by reference, what I typed and the archive capture's
-- cluster. A cluster merged away counts as the one it's part of now. A pin can resolve to more than one cluster, or
-- to none.
CREATE VIEW im.lens_pin_ideas AS
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
)
SELECT DISTINCT p.pin_id, p.lens_id, p.pin, p.event_id, coalesce(m.into_id, h.idea_id) AS idea_id
FROM held h JOIN im.lens_pins p USING (pin_id)
LEFT JOIN im.merged_into m ON m.idea_id = h.idea_id;

-- My latest pin on each cluster in each lens.
CREATE VIEW im.lens_pinned AS
SELECT DISTINCT ON (t.lens_id, t.idea_id) t.lens_id, t.idea_id, t.pin
FROM im.lens_pin_ideas t
ORDER BY t.lens_id, t.idea_id, t.event_id DESC, t.pin_id DESC;

-- Boards: a named set of lenses, each in a place of its drawing. The Tree and the Grid are the first two, empty.
CREATE TABLE im.boards (
  board_id    uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  name        text NOT NULL UNIQUE CHECK (length(btrim(name)) BETWEEN 1 AND 80),
  drawing     text NOT NULL CHECK (drawing IN ('tree', 'grid')),
  created_at  timestamptz NOT NULL DEFAULT now()
);
INSERT INTO im.boards (name, drawing) VALUES ('Tree', 'tree'), ('Grid', 'grid');

CREATE TABLE im.board_lenses (
  board_id   uuid NOT NULL REFERENCES im.boards(board_id) ON DELETE CASCADE,
  place      text NOT NULL,
  lens_id    uuid NOT NULL REFERENCES im.lenses(lens_id) ON DELETE CASCADE,
  event_id   bigint NOT NULL,
  placed_at  timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (board_id, place)
);

-- A drawing's places: the Tree's ten sephirot and 22 paths (each path by its letter), the Grid's cells by number.
CREATE FUNCTION im.board_place_ok(drawing text, place text) RETURNS boolean LANGUAGE sql IMMUTABLE
AS $$
SELECT CASE drawing
  WHEN 'tree' THEN coalesce(place = ANY (ARRAY[
    'kether', 'chokmah', 'binah', 'chesed', 'geburah', 'tiphareth', 'netzach', 'hod', 'yesod', 'malkuth',
    'aleph', 'beth', 'gimel', 'daleth', 'heh', 'vav', 'zain', 'cheth', 'teth', 'yod', 'kaph', 'lamed', 'mem', 'nun',
    'samekh', 'ayin', 'peh', 'tzaddi', 'qoph', 'resh', 'shin', 'tav']), false)
  WHEN 'grid' THEN coalesce(place ~ '^[1-9][0-9]{0,2}$', false)
  ELSE false END
$$;

-- The events. Shapes:
--   every lens and board event: no theme, no other idea; lens_pin and lens_unpin name the idea, the rest none.
--   lens_add: {"lens_id": uuid the sender makes, "name": 1-80 chars, "query": {"kind": ...},
--              "description"?: up to 2000 chars, "filters"?: as im.lens_filters_ok}.
--   lens_edit: {"lens_id", and any of "name", "description", "query", "filters"}.
--   lens_state: {"lens_id", "state": active | pinned | archived}.
--   lens_pin: idea_id; {"lens_id", "pin": in | out}.   lens_unpin: idea_id; {"lens_id"}.
--   board_place: {"board_id", "place": 1-40 chars, "lens_id"}.   board_remove: {"board_id", "place"}.
ALTER TABLE pub.feedback_events DROP CONSTRAINT feedback_events_kind_check;
ALTER TABLE pub.feedback_events ADD CONSTRAINT feedback_events_kind_check CHECK (kind IN (
  'star', 'unstar', 'keep', 'discard', 'link', 'unlink', 'pin_theme', 'unpin_theme', 'rename_theme', 'reject_theme',
  'item_keep', 'item_discard', 'item_star', 'idea_correct', 'idea_note', 'idea_compare', 'digest_star',
  'digest_dismiss', 'idea_add', 'merge_theme', 'split_theme', 'file_idea', 'unfile_idea', 'idea_sensitive',
  'theme_sensitive', 'vocab_add', 'vocab_remove', 'idea_keep', 'idea_unkeep', 'idea_dismiss', 'idea_distinct',
  'demo_on', 'demo_off', 'lens_add', 'lens_edit', 'lens_state', 'lens_pin', 'lens_unpin', 'board_place',
  'board_remove'));
ALTER TABLE pub.feedback_events ADD CONSTRAINT feedback_events_lens_edits CHECK (
  kind NOT IN ('lens_add', 'lens_edit', 'lens_state', 'lens_pin', 'lens_unpin', 'board_place', 'board_remove') OR (
    theme_id IS NULL AND other_idea_id IS NULL
    AND (kind IN ('lens_pin', 'lens_unpin')) = (idea_id IS NOT NULL)
    AND (kind = 'board_remove' OR im.is_uuid(payload->>'lens_id'))
    AND (kind NOT IN ('board_place', 'board_remove') OR (
      im.is_uuid(payload->>'board_id') AND coalesce(jsonb_typeof(payload->'place'), '') = 'string'
      AND length(payload->>'place') BETWEEN 1 AND 40))
    AND (kind <> 'lens_add' OR (payload ? 'name' AND im.lens_query_ok(payload->'query')))
    AND (kind NOT IN ('lens_add', 'lens_edit') OR (
      (NOT payload ? 'name' OR (coalesce(jsonb_typeof(payload->'name'), '') = 'string'
                                AND length(btrim(payload->>'name')) BETWEEN 1 AND 80))
      AND (NOT payload ? 'description' OR (coalesce(jsonb_typeof(payload->'description'), '') = 'string'
                                           AND length(payload->>'description') <= 2000))
      AND (NOT payload ? 'query' OR im.lens_query_ok(payload->'query'))
      AND (NOT payload ? 'filters' OR im.lens_filters_ok(payload->'filters'))))
    AND (kind <> 'lens_state' OR coalesce(payload->>'state', '') IN ('active', 'pinned', 'archived'))
    AND (kind <> 'lens_pin' OR coalesce(payload->>'pin', '') IN ('in', 'out'))));

-- Applies one lens or board event and says what happened. Something that no longer applies (a lens or a board that
-- isn't there, a name another lens has, a cluster with nothing to anchor a pin to) is recorded, not refused; a resent
-- lens_add is a 'repeat'.
CREATE FUNCTION im.apply_lens_event(e pub.feedback_events) RETURNS text LANGUAGE plpgsql
SET search_path = pg_catalog, im, pub
AS $$
DECLARE
  lid uuid := CASE WHEN im.is_uuid(e.payload->>'lens_id') THEN (e.payload->>'lens_id')::uuid END;
  v_name text := btrim(coalesce(e.payload->>'name', ''));
  b im.boards;
  a record;
  idea uuid;
  n int;
BEGIN
  IF e.kind IN ('board_place', 'board_remove') THEN
    SELECT * INTO b FROM im.boards WHERE board_id = (e.payload->>'board_id')::uuid;
    IF NOT FOUND THEN RETURN 'no such board'; END IF;
    IF NOT im.board_place_ok(b.drawing, e.payload->>'place') THEN RETURN 'no such place'; END IF;
    IF e.kind = 'board_remove' THEN
      DELETE FROM im.board_lenses WHERE board_id = b.board_id AND place = e.payload->>'place';
      GET DIAGNOSTICS n = ROW_COUNT;
      RETURN CASE WHEN n > 0 THEN 'removed' ELSE 'nothing there' END;
    END IF;
  END IF;

  IF e.kind = 'lens_add' THEN
    IF EXISTS (SELECT 1 FROM im.lenses WHERE lens_id = lid) THEN RETURN 'repeat'; END IF;
    IF EXISTS (SELECT 1 FROM im.lenses x WHERE lower(btrim(x.name)) = lower(v_name)) THEN RETURN 'name taken'; END IF;
    INSERT INTO im.lenses (lens_id, name, description, query, filters)
    VALUES (lid, v_name, coalesce(e.payload->>'description', ''), e.payload->'query',
            coalesce(e.payload->'filters', '{}'));
    RETURN 'added';
  END IF;
  IF NOT EXISTS (SELECT 1 FROM im.lenses WHERE lens_id = lid) THEN RETURN 'no such lens'; END IF;

  IF e.kind = 'lens_edit' THEN
    IF e.payload ? 'name' AND EXISTS (SELECT 1 FROM im.lenses x
                                      WHERE lower(btrim(x.name)) = lower(v_name) AND x.lens_id <> lid) THEN
      RETURN 'name taken';
    END IF;
    UPDATE im.lenses SET name = CASE WHEN e.payload ? 'name' THEN v_name ELSE name END,
                         description = coalesce(e.payload->>'description', description),
                         query = coalesce(e.payload->'query', query),
                         filters = coalesce(e.payload->'filters', filters)
    WHERE lens_id = lid;
    RETURN 'edited';
  ELSIF e.kind = 'lens_state' THEN
    UPDATE im.lenses SET state = e.payload->>'state' WHERE lens_id = lid;
    RETURN e.payload->>'state';
  ELSIF e.kind = 'lens_pin' THEN
    SELECT * INTO a FROM im.idea_anchor(e.idea_id);
    IF coalesce(cardinality(a.segment_ids), 0) = 0 AND a.source_ref IS NULL THEN RETURN 'nothing to anchor to'; END IF;
    INSERT INTO im.lens_pins (lens_id, pin, segment_ids, source_ref, event_id)
    VALUES (lid, e.payload->>'pin', a.segment_ids, a.source_ref, e.event_id)
    ON CONFLICT (lens_id, segment_ids, coalesce(source_ref, ''))
    DO UPDATE SET pin = EXCLUDED.pin, event_id = EXCLUDED.event_id, pinned_at = now();
    RETURN 'pinned ' || (e.payload->>'pin');
  ELSIF e.kind = 'lens_unpin' THEN
    idea := coalesce((SELECT m.into_id FROM im.merged_into m WHERE m.idea_id = e.idea_id), e.idea_id);
    DELETE FROM im.lens_pins p WHERE p.lens_id = lid
      AND p.pin_id IN (SELECT t.pin_id FROM im.lens_pin_ideas t WHERE t.lens_id = lid AND t.idea_id = idea);
    GET DIAGNOSTICS n = ROW_COUNT;
    RETURN CASE WHEN n > 0 THEN 'unpinned' ELSE 'not pinned' END;
  ELSIF e.kind = 'board_place' THEN
    INSERT INTO im.board_lenses (board_id, place, lens_id, event_id)
    VALUES (b.board_id, e.payload->>'place', lid, e.event_id)
    ON CONFLICT (board_id, place)
    DO UPDATE SET lens_id = EXCLUDED.lens_id, event_id = EXCLUDED.event_id, placed_at = now();
    RETURN 'placed';
  END IF;
  RETURN 'not a lens event';
END $$;

-- AFTER INSERT, as theme events (024): the event's CHECKs have passed and its row exists for im.feedback_applied.
CREATE FUNCTION im.lens_event_trigger() RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, im, pub
AS $$
BEGIN
  INSERT INTO im.feedback_applied (event_id, result) VALUES (NEW.event_id, im.apply_lens_event(NEW));
  RETURN NULL;
END $$;
REVOKE ALL ON FUNCTION im.lens_event_trigger() FROM PUBLIC;

CREATE TRIGGER feedback_events_lens_edits AFTER INSERT ON pub.feedback_events
  FOR EACH ROW WHEN (NEW.kind IN ('lens_add', 'lens_edit', 'lens_state', 'lens_pin', 'lens_unpin', 'board_place',
                                  'board_remove'))
  EXECUTE FUNCTION im.lens_event_trigger();

-- What the web view and the MCP server read.
CREATE VIEW pub.lenses AS
SELECT l.lens_id, l.name, l.description, l.query->>'kind' AS kind, l.query, l.query_version, l.filters, l.state,
       l.created_at
FROM im.lenses l;

-- Each lens's members among the cards, and why each is in: my pin, or the query (with the filters). No score yet:
-- `all` and `none` judge nothing.
CREATE VIEW pub.lens_members AS
SELECT p.lens_id, p.idea_id, 'pin'::text AS why, NULL::real AS score
FROM im.lens_pinned p JOIN pub.cards c USING (idea_id)
WHERE p.pin = 'in'
UNION ALL
SELECT l.lens_id, c.idea_id, 'query', NULL
FROM im.lenses l
JOIN pub.cards c ON l.query->>'kind' = 'all'
LEFT JOIN pub.sensitivity s ON s.kind = 'idea' AND s.id = c.idea_id
WHERE im.lens_admits(l.filters, c.maturity, c.kept_as, c.by_me, c.first_said, c.last_seen, coalesce(s.sensitive, false))
  AND NOT EXISTS (SELECT 1 FROM im.lens_pinned p WHERE p.lens_id = l.lens_id AND p.idea_id = c.idea_id);

-- Demo mode leaves a hidden card out of every lens.
CREATE VIEW pub.demo_lens_members AS
SELECT m.* FROM pub.lens_members m WHERE m.idea_id NOT IN (SELECT idea_id FROM pub.demo_hidden_ideas);

-- My pins as they stand, on cards: each lens's clusters pinned in or out, for a view to undo.
CREATE VIEW pub.lens_pins AS
SELECT p.lens_id, p.idea_id, p.pin FROM im.lens_pinned p WHERE p.idea_id IN (SELECT idea_id FROM pub.cards);

CREATE VIEW pub.boards AS
SELECT b.board_id, b.name, b.drawing, b.created_at FROM im.boards b;

CREATE VIEW pub.board_lenses AS
SELECT bl.board_id, bl.place, bl.lens_id, bl.placed_at FROM im.board_lenses bl;

GRANT SELECT ON pub.lenses, pub.lens_members, pub.demo_lens_members, pub.lens_pins, pub.boards, pub.board_lenses
  TO lattice_app;
