"""The idea lattice: matching captures into ideas, the pub contract, and forgetting."""

import json
import re
from types import SimpleNamespace

import numpy as np
import psycopg
import pytest

from im import citations, egress, extract, fixtures, lattice, observations, pipeline
from im.checks import run_checks
from im.config import ModelConfig

from helpers import HashEmbedder, assert_invariants, run, segment_of, table
from test_extract import FakeClaude
from test_pipeline import LORA

# Ideas I added before anything was captured: (title, statement, notes), then my two themes as (name, description,
# members), and the method of the connection a judge made between the first two.
SEEDED = [("A solitary unsatisfying grape.", "“A solitary unsatisfying grape.”",
           "Possible associations: disproportionate disappointment."),
          ("Signal may not be true, but it is…", "“Signal may not be true, but it is always correct.”",
           "Theme: truth versus correctness. Connects to #1."),
          ("The coffee order is the real meeting.", "“The coffee order is the real meeting.”",
           "Theme: office rituals.")]
SEEDED_THEMES = [("Models and signals", "Core recurring concepts: signal versus truth.", [2, 3]),
                 ("Jokes", "Compact formulations.", [1])]
JUDGED = "opus;edge-v1"


def seed(conn) -> None:
    """Three ideas I added (origin mine, kept as canonical), a judge's connection between #1 and #2, and my themes,
    pinned."""
    ids = {n: conn.execute("""INSERT INTO im.ideas (title, statement, origin, notes)
                              VALUES (%s, %s, 'mine', %s) RETURNING idea_id""",
                           (title, statement, notes)).fetchone()[0]
           for n, (title, statement, notes) in enumerate(SEEDED, 1)}
    conn.execute("INSERT INTO im.connections (a, b, method) VALUES (%s, %s, %s)", (*sorted((ids[1], ids[2])), JUDGED))
    for name, description, members in SEEDED_THEMES:
        (theme,) = conn.execute("""INSERT INTO im.themes (name, description, origin, pinned)
                                   VALUES (%s, %s, 'mine', true) RETURNING theme_id""", (name, description)).fetchone()
        for n in members:
            conn.execute("INSERT INTO im.idea_themes (idea_id, theme_id, origin) VALUES (%s, %s, 'mine')",
                         (ids[n], theme))


class FakeMatcher:
    """Matcher v2 by keyword: a 'coffee' capture makes the same point as every coffee cluster, a 'build' capture
    starts the broken-builds cluster, everything else a new one. Records every request."""

    def __init__(self):
        self.calls = []
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self.create))

    def create(self, **kw):
        self.calls.append(kw)
        text = kw["messages"][0]["content"]
        item, cands = text.split("\nCandidates\n")
        shown = re.findall(r"^(\d+)\. title(?: \(mine\))?: (.*)\n\s+statement(?: \(mine\))?: (.*)$", cands, re.M)
        coffee = "coffee" in item.lower()
        answer = {"judgments": [{"candidate": int(n), "same_point": coffee and "coffee" in f"{t} {s}".lower()}
                                for n, t, s in shown], "title": "", "statement": ""}
        if not any(j["same_point"] for j in answer["judgments"]):
            answer |= {"title": "Broken builds as signal", "statement": "A broken build is a signal about the system."} \
                if "build" in item.lower() else {"title": "Something new", "statement": "A new thing."}
        content = [SimpleNamespace(type="text", text=json.dumps(answer))]
        return SimpleNamespace(content=content, stop_reason="end_turn", model="claude-opus-5-5", _request_id="r",
                               usage=SimpleNamespace(input_tokens=300, output_tokens=50))


def items_for(text):
    """Captures: the coffee line (said by anon A), the build line (me), the LoRa idea (me)."""
    out = []
    for line in text.splitlines():
        if not line.startswith("["):
            continue
        n, rest = line[1:].split("] ", 1)
        who, said = rest.split(": ", 1)
        if "coffee" in said or "build broke" in said or "LoRa" in said:
            out.append({"said_by": who, "quote": said, "lines": [int(n)], "attributed_to": "", "reading": said,
                        "mishearings": [], "gist": said})
    return out


@pytest.fixture
def seeded(pipe):
    seed(pipe)
    return pipe


def lat_setup(conn, cfg, stream):
    """Captures extracted and matched into the seeded lattice."""
    run(conn, cfg, stream)
    extract.run_stage(conn, FakeClaude(items_for))
    lattice.match_stage(conn, FakeMatcher(), HashEmbedder())
    return conn


@pytest.fixture
def seeded_lattice(seeded, cfg, stream):
    return lat_setup(seeded, cfg, stream)


def legacy_evolves(conn):
    """An evolves edge as matcher v1 wrote them, from the broken-builds cluster to my signal idea: lineage
    the live lattice still holds, though matcher v2 writes none."""
    conn.execute("""INSERT INTO im.lineage (from_id, to_id, kind, method)
                    SELECT b.idea_id, s.idea_id, 'evolves', 'match-1:legacy' FROM im.ideas b, im.ideas s
                    WHERE b.title = 'Broken builds as signal' AND s.statement LIKE '%%always correct%%'""")
    citations.cite(conn)


def test_matching_joins_the_same_point_and_writes_no_lineage(seeded, cfg, stream):
    run(seeded, cfg, stream)
    extract.run_stage(seeded, FakeClaude(items_for))
    claude = FakeMatcher()
    stats = lattice.match_stage(seeded, claude, HashEmbedder())
    assert (stats["new"], stats["same_as"], stats["possible_duplicates"]) == (2, 1, 0)
    coffee = table(seeded, """SELECT n_evidence, title FROM pub.ideas WHERE statement LIKE '%%coffee%%'""")
    assert coffee == [(1, "The coffee order is the real meeting.")]
    assert table(seeded, "SELECT said_by FROM pub.evidence ORDER BY said_by") == [("anon A",), ("me",), ("me",)]
    assert table(seeded, "SELECT count(*) FROM im.lineage") == [(0,)]   # building on a point is the connection judge's
    sent = json.dumps([c["messages"] for c in claude.calls])
    assert "anon A" not in sent and "Alice" not in sent and "relayed from: someone else" in sent
    assert lattice.match_stage(seeded, claude, HashEmbedder())["pending"] == 0 and len(claude.calls) == 3


def test_a_capture_making_the_same_point_as_two_clusters_joins_the_nearest_and_logs_them(seeded, cfg, stream):
    seeded.execute("""INSERT INTO im.ideas (title, statement, origin, segment_ids)
                      VALUES ('Coffee runs the meeting', 'Who gets the coffee order runs the meeting.', 'captured', '{}')""")
    run(seeded, cfg, stream)
    extract.run_stage(seeded, FakeClaude(lambda text: [i for i in items_for(text) if "coffee" in i["quote"]]))
    stats = lattice.match_stage(seeded, FakeMatcher(), HashEmbedder())
    assert (stats["same_as"], stats["possible_duplicates"]) == (1, 1)
    ((ideas,),) = table(seeded, "SELECT idea_ids FROM im.merge_review WHERE kind = 'duplicate'")
    (joined,) = table(seeded, "SELECT idea_id FROM im.item_matches WHERE decision = 'same_as'")[0]
    assert len(ideas) == 2 and ideas[0] == joined   # the nearest is joined; both go to my merge review


class Scripted(FakeMatcher):
    """Answers with `answers` in turn, each a function of the request text."""

    def __init__(self, *answers):
        super().__init__()
        self.answers = list(answers)

    def create(self, **kw):
        self.calls.append(kw)
        a = self.answers.pop(0)(kw["messages"][0]["content"])
        return SimpleNamespace(content=[SimpleNamespace(type="text", text=json.dumps(a))], stop_reason="end_turn",
                               model="claude-opus-5-5", _request_id="r",
                               usage=SimpleNamespace(input_tokens=300, output_tokens=50))


def candidates(text):
    return [int(n) for n in re.findall(r"^(\d+)\. title", text, re.M)]


def test_a_relayed_thought_is_matched_only_among_relayed_clusters(seeded, cfg, stream):
    run(seeded, cfg, stream)
    lines = []

    def respond(text):   # v5: my LoRa and build lines, both relayed from my teacher
        out = []
        for row in text.splitlines():
            if row.startswith("[") and ("LoRa" in row or "build broke" in row) and row.split(": ", 1)[0].endswith(" me"):
                said = row.split(": ", 1)[1]
                out.append({"quote": said, "lines": [int(row[1:].split("]")[0])], "attributed_to": "my teacher",
                            "reading": said, "mishearings": [], "gist": said})
                lines.append(said)
        return out

    extract.run_stage(seeded, FakeClaude(respond), system=extract.SYSTEM_V5)
    claude = Scripted(lambda text: {"judgments": [{"candidate": n, "same_point": False} for n in candidates(text)],
                                    "title": "My teacher's point", "statement": "My teacher says this."})
    stats = lattice.match_stage(seeded, claude, HashEmbedder())
    # The first relayed capture is the first cluster on its side: no candidates, so nothing to ask. The second sees
    # only that one, never the clusters of my own thoughts.
    assert stats["new"] == 2 and len(claude.calls) == 1
    shown = claude.calls[0]["messages"][0]["content"]
    assert candidates(shown) == [1] and "relayed from: my teacher" in shown and "coffee" not in shown.lower()


def test_the_matcher_sends_names_said_as_said(seeded, cfg, stream):
    run(seeded, cfg, stream)

    def respond(text):   # v5: my LoRa and build lines, both relayed from Alice, by name
        return [{"quote": said, "lines": [int(row[1:].split("]")[0])], "attributed_to": "Alice", "reading": said,
                 "mishearings": [], "gist": f"Alice's rule: {said}"}
                for row in text.splitlines() for said in [row.split(": ", 1)[-1]]
                if row.startswith("[") and ("LoRa" in row or "build broke" in row)
                and row.split(": ", 1)[0].endswith(" me")]

    extract.run_stage(seeded, FakeClaude(respond), system=extract.SYSTEM_V5)
    claude = Scripted(lambda text: {"judgments": [{"candidate": n, "same_point": False} for n in candidates(text)],
                                    "title": "Alice's rules", "statement": "Alice says this."})
    lattice.match_stage(seeded, claude, HashEmbedder())
    shown = claude.calls[0]["messages"][0]["content"]
    assert "relayed from: Alice" in shown and "Alice's rule:" in shown   # the candidate's text too
    assert table(seeded, "SELECT statement FROM im.ideas WHERE title = 'Alice''s rules'") == [("Alice says this.",)]
    assert table(seeded, """SELECT privacy_policy_version FROM im.egress_log
                            WHERE prompt_version = %s""", lattice.matcher_version(ModelConfig())) == \
        [(egress.POLICY_VERSION,)]


def test_an_answer_missing_a_candidate_is_asked_again_and_a_statement_shown_as_said_falls_back(seeded, cfg, stream):
    run(seeded, cfg, stream)
    extract.run_stage(seeded, FakeClaude(lambda text: [i for i in items_for(text) if "LoRa" in i["quote"]]))
    short = lambda text: {"judgments": [], "title": "x", "statement": "y"}  # noqa: E731
    quoted = lambda text: {"judgments": [{"candidate": n, "same_point": False} for n in candidates(text)],  # noqa: E731
                           "title": "Garden sensors over LoRa", "statement": "Idea: sensors could report over LoRa."}
    stats = lattice.match_stage(seeded, Scripted(short, quoted), HashEmbedder())
    assert (stats["asked_again"], stats["new"]) == (1, 1)
    assert table(seeded, "SELECT title, statement FROM pub.ideas WHERE title LIKE 'Garden%%'") == [
        ("Garden sensors over LoRa", LORA)]   # a labeled statement falls back to the gist


def test_a_re_read_joins_its_observations_cluster_unasked(seeded, cfg, stream):
    # A new prompt's re-read, once approved (`im reread extract`), reads every episode again. What it captures again
    # folds into the same observations, and those captures join the clusters their observations are already in: no
    # matcher request, no observation split.
    lat_setup(seeded, cfg, stream)
    observations.assign_stage(seeded)
    extract.run_stage(seeded, FakeClaude(items_for), system=extract.SYSTEM_V5_2, held_only=True)
    observations.assign_stage(seeded)
    claude = FakeMatcher()
    stats = lattice.match_stage(seeded, claude, HashEmbedder())
    assert (stats["carried"], stats["pending"], claude.calls) == (3, 0, [])
    assert table(seeded, """SELECT count(DISTINCT m.idea_id) FROM im.items i JOIN im.item_matches m USING (item_id)
                            GROUP BY i.observation_id""") == [(1,)] * 3
    assert table(seeded, "SELECT relation, count(*) FROM im.idea_evidence WHERE judged_by = %s GROUP BY 1",
                 lattice.CARRIED) == [("same_as", 3)]


def test_a_re_read_shows_each_observation_once_with_my_verdict(seeded, cfg, stream):
    # Evidence, review and the CLI's queue go by observation: a re-capture doesn't show twice, doesn't ask for my
    # verdict again, and doesn't bring back a cluster I discarded.
    lat_setup(seeded, cfg, stream)
    observations.assign_stage(seeded)
    v1 = dict(table(seeded, "SELECT quote, item_id FROM im.items"))
    coffee, lora = (next(i for q, i in v1.items() if word in q) for word in ("coffee", "LoRa"))
    extract.decide(seeded, coffee, "keep")
    extract.decide(seeded, lora, "discard")
    shown = """SELECT d.idea_id, d.n_evidence, count(*) FILTER (WHERE ev.verdict IN ('keep', 'star')),
                      count(*) FILTER (WHERE ev.verdict = 'discard')
               FROM pub.ideas d LEFT JOIN pub.evidence ev USING (idea_id) GROUP BY 1, 2 ORDER BY 1"""
    before = table(seeded, shown)
    extract.run_stage(seeded, FakeClaude(items_for), system=extract.SYSTEM_V5_2, held_only=True)
    observations.assign_stage(seeded)
    lattice.match_stage(seeded, FakeMatcher(), HashEmbedder())
    assert table(seeded, "SELECT count(*) FROM im.items") == [(6,)]
    assert table(seeded, shown) == before
    assert table(seeded, "SELECT item_id, verdict FROM pub.evidence WHERE verdict IS NOT NULL ORDER BY verdict") == \
        [(lora, "discard"), (coffee, "keep")]   # the capture I judged stands for its observation
    assert len(extract.review_items(seeded)) == 1   # only the build line, once


def test_two_captures_of_one_line_are_matched_once(seeded, cfg, stream):
    run(seeded, cfg, stream)
    extract.run_stage(seeded, FakeClaude(lambda text: items_for(text) * 2))
    observations.assign_stage(seeded)
    claude = FakeMatcher()
    stats = lattice.match_stage(seeded, claude, HashEmbedder())
    assert (len(claude.calls), stats["carried"]) == (3, 3)


def test_discarded_items_are_skipped(seeded, cfg, stream):
    run(seeded, cfg, stream)
    extract.run_stage(seeded, FakeClaude(items_for))
    for (item_id,) in table(seeded, "SELECT item_id FROM im.items"):
        extract.decide(seeded, item_id, "discard")
    stats = lattice.match_stage(seeded, FakeMatcher(), HashEmbedder())
    assert stats["skipped"] == 3 and table(seeded, "SELECT count(*) FROM pub.ideas") == [(3,)]


class CutsOffFirst(FakeMatcher):
    """FakeMatcher, except that its first answer is cut off mid-JSON at max_tokens."""

    def create(self, **kw):
        r = super().create(**kw)
        if len(self.calls) > 1:
            return r
        return SimpleNamespace(content=[SimpleNamespace(type="text", text='{"decision": "sa')], stop_reason="max_tokens",
                               model="claude-opus-5-5", _request_id="r", usage=SimpleNamespace(input_tokens=300,
                                                                                               output_tokens=4000))


def test_a_match_cut_off_is_logged_and_paid_for_and_the_other_captures_are_matched(seeded, cfg, stream):
    run(seeded, cfg, stream)
    extract.run_stage(seeded, FakeClaude(items_for))
    claude = CutsOffFirst()
    stats = lattice.match_stage(seeded, claude, HashEmbedder())
    assert len(claude.calls) == 3 and stats["failed"] == 1 and stats["new"] + stats["same_as"] == 2
    logged = table(seeded, """SELECT count(*), count(*) FILTER (WHERE error IS NOT NULL AND cost_usd > 0)
                              FROM im.egress_log WHERE prompt_version = %s""", lattice.matcher_version())
    assert logged == [(3, 1)]
    again = lattice.match_stage(seeded, FakeMatcher(), HashEmbedder())   # the one cut off is asked again
    assert again["pending"] == 1 and again["failed"] == 0


def test_the_matchers_version_follows_its_configured_model():
    # Every match is filed under this; the defaults must keep it.
    assert lattice.matcher_version() == lattice.matcher_version(ModelConfig()) == "match-2:812666437509"
    for other in (ModelConfig(model="claude-sonnet-5-5"), ModelConfig(effort="medium")):
        assert lattice.matcher_version(other) != lattice.matcher_version()


def test_the_web_role_reads_pub_and_only_appends_feedback(seeded, admin):
    (idea,) = table(seeded, "SELECT idea_id FROM pub.ideas LIMIT 1")[0]
    with admin.transaction():
        admin.execute("SET LOCAL ROLE lattice_app")
        assert admin.execute("SELECT count(*) FROM pub.ideas").fetchone()[0] == 3
        admin.execute("INSERT INTO pub.feedback_events (kind, idea_id) VALUES ('star', %s)", (idea,))
    for sql in ["SELECT 1 FROM im.items", "SELECT 1 FROM im.ideas",
                "UPDATE pub.feedback_events SET kind = 'unstar'", "DELETE FROM pub.feedback_events"]:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            with admin.transaction():
                admin.execute("SET LOCAL ROLE lattice_app")
                admin.execute(sql)
    assert table(seeded, "SELECT starred FROM pub.ideas WHERE idea_id = %s", idea) == [(True,)]


def test_my_latest_star_or_unstar_is_what_pub_ideas_shows(seeded, admin):
    (idea,) = table(seeded, "SELECT idea_id FROM pub.ideas ORDER BY idea_id LIMIT 1")[0]
    for kind, starred in [("star", True), ("unstar", False), ("star", True)]:
        with admin.transaction():
            admin.execute("SET LOCAL ROLE lattice_app")
            admin.execute("INSERT INTO pub.feedback_events (kind, idea_id) VALUES (%s, %s)", (kind, idea))
        assert table(seeded, "SELECT starred FROM pub.ideas WHERE idea_id = %s", idea) == [(starred,)]


def role_can(admin, role: str) -> dict:
    """Everything ROLE may do, from the catalog: on the relations, columns, sequences, functions and schemas the
    database has beyond Postgres's own, and the roles it could become."""
    q = lambda sql: set(admin.execute(sql, {"role": role}).fetchall())  # noqa: E731
    return {
        "relations": q("""SELECT n.nspname, c.relname, p FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace,
                            unnest('{SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER}'::text[]) p
                          WHERE n.nspname IN ('im', 'pub', 'public') AND c.relkind IN ('r', 'v', 'm', 'p', 'f')
                            AND has_table_privilege(%(role)s, c.oid, p)"""),
        "columns": q("""SELECT n.nspname, c.relname, a.attname, p FROM pg_attribute a
                        JOIN pg_class c ON c.oid = a.attrelid JOIN pg_namespace n ON n.oid = c.relnamespace,
                          unnest('{SELECT,INSERT,UPDATE,REFERENCES}'::text[]) p
                        WHERE n.nspname IN ('im', 'pub', 'public') AND a.attnum > 0 AND NOT a.attisdropped
                          AND has_column_privilege(%(role)s, c.oid, a.attnum, p)
                          AND NOT has_table_privilege(%(role)s, c.oid, p)"""),
        "sequences": q("""SELECT n.nspname, c.relname, p FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace,
                            unnest('{USAGE,SELECT,UPDATE}'::text[]) p
                          WHERE n.nspname IN ('im', 'pub', 'public') AND c.relkind = 'S'
                            AND has_sequence_privilege(%(role)s, c.oid, p)"""),
        # What it can call: EXECUTE, in a schema it can use. Public holds pgvector's functions, not ours.
        "functions": q("""SELECT n.nspname, p.proname FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
                          WHERE n.nspname IN ('im', 'pub') AND has_function_privilege(%(role)s, p.oid, 'EXECUTE')
                            AND has_schema_privilege(%(role)s, n.oid, 'USAGE')"""),
        "schemas": q("""SELECT n.nspname, p FROM pg_namespace n, unnest('{USAGE,CREATE}'::text[]) p
                        WHERE n.nspname IN ('im', 'pub') AND has_schema_privilege(%(role)s, n.oid, p)"""),
        "roles": q("""SELECT r.rolname FROM pg_auth_members m JOIN pg_roles r ON r.oid = m.roleid
                      WHERE m.member = %(role)s::regrole"""),
    }


def test_the_web_role_can_read_pub_append_feedback_and_call_its_functions_and_nothing_else(seeded, admin):
    pub = {r for (r,) in admin.execute("""SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
                                          WHERE n.nspname = 'pub' AND c.relkind IN ('r', 'v', 'm')""")}
    assert role_can(admin, "lattice_app") == {
        "relations": {("pub", r, "SELECT") for r in pub} | {("pub", "feedback_events", "INSERT")},
        "columns": set(), "sequences": set(),
        "functions": {("pub", "card_neighbours"), ("pub", "embedding_model"), ("pub", "ideas_near")},
        "schemas": {("pub", "USAGE")}, "roles": set()}
    # They run as their owner (SECURITY DEFINER), reading im: only the roles granted them may call them, not PUBLIC.
    assert admin.execute("""SELECT p.proname FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
                            WHERE n.nspname = 'pub' AND p.prosecdef
                              AND has_function_privilege('public', p.oid, 'EXECUTE')""").fetchall() == []


def test_the_reader_role_can_read_all_of_pub_and_nothing_else(seeded, admin):
    # Granted by schema (dev/init/bootstrap-db.sql.in) before the migrations made pub's views: by default, then.
    pub = {r for (r,) in admin.execute("""SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
                                          WHERE n.nspname = 'pub' AND c.relkind IN ('r', 'v', 'm')""")}
    functions = {("pub", f) for (f,) in admin.execute("""SELECT p.proname FROM pg_proc p
                                                          JOIN pg_namespace n ON n.oid = p.pronamespace
                                                          WHERE n.nspname = 'pub'""")}
    assert role_can(admin, "im_reader") == {
        "relations": {("pub", r, "SELECT") for r in pub}, "columns": set(), "sequences": set(),
        "functions": functions, "schemas": {("pub", "USAGE")}, "roles": set()}
    # It may call every function in pub, so none may write: each is STABLE or IMMUTABLE.
    assert admin.execute("""SELECT p.proname FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
                            WHERE n.nspname = 'pub' AND p.provolatile = 'v'""").fetchall() == []
    with admin.transaction():
        admin.execute("SET LOCAL ROLE im_reader")
        assert admin.execute("SELECT count(*) FROM pub.ideas").fetchone()[0] == 3
    for sql in ["SELECT 1 FROM im.egress_log", "SELECT 1 FROM im.source_segments",
                "INSERT INTO pub.feedback_events (kind) VALUES ('star')", "DELETE FROM pub.feedback_events"]:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            with admin.transaction():
                admin.execute("SET LOCAL ROLE im_reader")
                admin.execute(sql)


def test_forgetting_removes_ideas_born_from_it_and_their_stars(seeded, cfg, stream):
    run(seeded, cfg, stream)
    extract.run_stage(seeded, FakeClaude(items_for))
    lattice.match_stage(seeded, FakeMatcher(), HashEmbedder())
    (lora,) = table(seeded, "SELECT idea_id FROM pub.ideas WHERE title = 'Something new'")[0]
    (added,) = table(seeded, "SELECT idea_id FROM pub.ideas WHERE origin = 'mine' LIMIT 1")[0]
    for idea in (lora, added):
        seeded.execute("INSERT INTO pub.feedback_events (kind, idea_id, source) VALUES ('star', %s, 'test')", (idea,))
    stream.forget(3600, 3612)   # the LoRa idea's line
    stream.write(stream.dir)
    run(seeded, cfg, stream)
    assert table(seeded, "SELECT count(*) FROM im.ideas WHERE idea_id = %s", lora) == [(0,)]
    assert table(seeded, "SELECT idea_id FROM pub.feedback_events") == [(added,)]


def _star_the_lora_capture(conn):
    (item,) = table(conn, "SELECT item_id FROM im.items WHERE quote LIKE '%%LoRa%%'")[0]
    conn.execute("INSERT INTO pub.feedback_events (kind, item_id, source) VALUES ('item_star', %s, 'test')", (item,))
    return item


def _tap_the_lora_line(conn, cfg, stream):
    """Hearsay lists a tap beside the LoRa line: its episode stays, but what would be sent changes."""
    stream.conversations["c-mono"].taps.append(fixtures.iso(3605))
    stream.write(stream.dir)
    run(conn, cfg, stream)


def test_a_re_read_in_place_keeps_the_captures_it_makes_again(seeded, cfg, stream):
    lat_setup(seeded, cfg, stream)
    observations.assign_stage(seeded)
    starred = _star_the_lora_capture(seeded)
    before = table(seeded, "SELECT idea_id, title, n_evidence FROM pub.ideas ORDER BY idea_id")
    _tap_the_lora_line(seeded, cfg, stream)
    assert extract.run_stage(seeded, FakeClaude(items_for))["read"] == 1
    observations.assign_stage(seeded)
    matcher = FakeMatcher()
    lattice.match_stage(seeded, matcher, HashEmbedder())
    assert matcher.calls == []   # nothing to match again: the capture, its idea and my star are as they were
    assert table(seeded, "SELECT idea_id, title, n_evidence FROM pub.ideas ORDER BY idea_id") == before
    assert table(seeded, "SELECT count(*) FROM im.ideas WHERE title = 'Something new'") == [(1,)]
    assert table(seeded, "SELECT item_id FROM pub.feedback_events WHERE kind = 'item_star'") == [(starred,)]


def _captures_of(*words):
    """Claude's reply: a capture of each line saying one of `words`, in the order of `words`."""
    def respond(text):
        lines = [line[1:].split("] ", 1) for line in text.splitlines() if line.startswith("[")]
        return [{"said_by": said.split(": ", 1)[0], "quote": said.split(": ", 1)[1], "lines": [int(n)],
                 "attributed_to": "", "reading": said, "mishearings": [], "gist": said}
                for word in words for n, said in lines if word in said]
    return respond


def test_a_re_read_back_to_an_earlier_payload_saves_its_captures_in_any_order(seeded, cfg, stream):
    # The tap comes and goes, so the third read sends what the first did, and Claude lists a new capture first:
    # the position the kept LoRa capture had in the first read.
    run(seeded, cfg, stream)
    extract.run_stage(seeded, FakeClaude(_captures_of("LoRa")))
    (lora,) = table(seeded, "SELECT item_id FROM im.items")[0]
    _tap_the_lora_line(seeded, cfg, stream)
    extract.run_stage(seeded, FakeClaude(_captures_of("LoRa")))
    stream.conversations["c-mono"].taps.clear()
    stream.write(stream.dir)
    run(seeded, cfg, stream)
    claude = FakeClaude(_captures_of("Battery", "LoRa"))
    assert extract.run_stage(seeded, claude)["read"] == 1
    assert table(seeded, "SELECT quote LIKE '%%LoRa%%', item_id = %s FROM im.items ORDER BY 1", lora) == \
        [(False, False), (True, True)]
    assert extract.run_stage(seeded, claude)["read"] == 0 and len(claude.calls) == 1   # read once, and done



@pytest.mark.parametrize("lose_the_capture", ["re-read without it", "reset --stage extract"])
def test_forgetting_reaches_an_idea_and_events_whose_capture_is_gone(seeded, cfg, stream, lose_the_capture):
    lat_setup(seeded, cfg, stream)
    _star_the_lora_capture(seeded)
    if lose_the_capture == "reset --stage extract":
        pipeline.reset(seeded, "extract")
    else:
        _tap_the_lora_line(seeded, cfg, stream)
        extract.run_stage(seeded, FakeClaude(lambda text: [i for i in items_for(text) if "LoRa" not in i["quote"]]))
    assert not table(seeded, "SELECT 1 FROM im.items WHERE quote LIKE '%%LoRa%%'")
    assert table(seeded, "SELECT count(*) FROM pub.feedback_events WHERE kind = 'item_star'") == [(1,)]   # a record
    stream.forget(3600, 3612)   # the LoRa idea's line
    stream.write(stream.dir)
    run(seeded, cfg, stream)
    assert not table(seeded, "SELECT 1 FROM im.ideas WHERE statement = 'A new thing.'")
    assert not table(seeded, "SELECT 1 FROM pub.feedback_events WHERE kind = 'item_star'")
    assert table(seeded, "SELECT count(*) FROM im.ideas WHERE title = 'Broken builds as signal'") == [(1,)]
    assert_invariants(seeded)


def test_the_checks_find_an_idea_made_from_forgotten_speech(seeded, cfg, stream):
    lat_setup(seeded, cfg, stream)
    (idea,) = table(seeded, "SELECT idea_id FROM im.ideas WHERE title = 'Something new'")[0]
    lora = segment_of(seeded, LORA)
    seeded.execute("INSERT INTO im.source_tombstones (segment_id, forgotten_at) VALUES (%s, now())", (lora,))
    assert (idea, lora) in run_checks(seeded)["tombstoned"]   # as if forgetting had missed it


def test_projection_holds_still_as_ideas_are_added():
    rng = np.random.default_rng(0)
    M = rng.normal(size=(40, 16)) @ np.diag(np.linspace(3, 0.2, 16))
    M /= np.linalg.norm(M, axis=1, keepdims=True)
    P0 = lattice.project(M[:30])
    assert np.abs(P0).max() == pytest.approx(1.0)
    P1 = lattice.project(M, P0, np.arange(30))
    assert (P1[:30] == P0).all() and np.abs(P1).max() <= 1.25   # placed ideas don't move at all
    for i in range(30, 40):   # a new idea lands near the placed idea most like it
        nearest = np.argmax(M[:30] @ M[i])
        assert np.linalg.norm(P1[i] - P0[nearest]) < np.median(np.linalg.norm(P0 - P0[nearest], axis=1))
    flipped = P0 * [-1, 1]   # a relayout keeps whatever orientation the map had
    assert np.abs(lattice.project(M[:30], flipped, np.arange(30), relayout=True) - flipped).max() < 1e-6
    assert lattice.project(M[:1]).tolist() == [[0.0, 0.0]]


def test_every_card_gets_a_position_the_web_role_can_read(seeded_lattice, admin):
    pipe = seeded_lattice
    cards = {r[0] for r in table(pipe, "SELECT idea_id FROM pub.cards")}
    with admin.transaction():
        admin.execute("SET LOCAL ROLE lattice_app")
        pos = admin.execute("SELECT idea_id, x, y FROM pub.card_positions").fetchall()
    assert {r[0] for r in pos} == cards
    assert all(abs(x) <= 1.25 and abs(y) <= 1.25 for _, x, y in pos)
    methods = {r[0] for r in table(pipe, "SELECT method FROM im.idea_positions")}
    assert all(m.endswith(f"embedding:{HashEmbedder().model_version}") for m in methods)
    before = {r[0]: (r[1], r[2]) for r in pos}
    assert lattice.place(pipe, HashEmbedder().model_version) == len(cards)
    after = {r[0]: (r[1], r[2]) for r in table(pipe, "SELECT idea_id, x, y FROM im.idea_positions")}
    assert all(np.allclose(after[i], before[i], atol=1e-3) for i in cards)   # same cards: the map doesn't move


def test_my_links_show_at_once_and_unlinking_removes_them(seeded, admin):
    a, b, c = sorted(r[0] for r in table(seeded, "SELECT idea_id FROM pub.ideas"))

    def as_web_role(sql, *args):
        with admin.transaction():
            admin.execute("SET LOCAL ROLE lattice_app")
            return admin.execute(sql, args or None)

    def mine():
        return as_web_role("SELECT a, b, weight, method FROM pub.connections WHERE kind = 'mine' ORDER BY a, b"
                           ).fetchall()

    def on_cards():   # the neutral view's edges: the same, links to clusters pub.ideas hides included
        return as_web_role("SELECT a, b FROM pub.card_links WHERE kind = 'mine' ORDER BY a, b").fetchall()

    link = "INSERT INTO pub.feedback_events (kind, idea_id, other_idea_id) VALUES (%s, %s, %s)"
    as_web_role(link, "link", b, a)
    as_web_role(link, "link", a, b)   # the same pair, either way round
    as_web_role(link, "link", a, c)
    assert mine() == [(a, b, 1.0, "feedback"), (a, c, 1.0, "feedback")] and on_cards() == [(a, b), (a, c)]
    as_web_role(link, "unlink", c, a)
    assert mine() == [(a, b, 1.0, "feedback")] and on_cards() == [(a, b)]
    as_web_role(link, "link", c, a)   # linked again: my latest decides
    assert on_cards() == [(a, b), (a, c)]
    for args in [("link", a, a), ("unlink", a, None)]:
        with pytest.raises(psycopg.errors.CheckViolation):
            as_web_role(link, *args)


def test_a_pair_is_one_connection_and_an_unlink_holds_until_a_later_method(seeded):
    (a, b), = table(seeded, "SELECT a, b FROM im.connections WHERE method = %s", JUDGED)

    def shown():
        return table(seeded, "SELECT kind, method FROM pub.card_links WHERE a = %s AND b = %s", a, b)

    def links():
        return table(seeded, "SELECT links FROM pub.cards WHERE idea_id = %s", a)

    assert shown() == [("related", JUDGED)] and links() == [(1,)]
    event = "INSERT INTO pub.feedback_events (kind, idea_id, other_idea_id) VALUES (%s, %s, %s)"
    seeded.execute(event, ("link", b, a))
    assert shown() == [("mine", "feedback")] and links() == [(1,)]   # mine and the judge's: one connection
    seeded.execute(event, ("unlink", a, b))
    assert shown() == [] and links() == [(0,)]   # the judge's method was known when I unlinked
    seeded.execute("UPDATE im.connections SET method = 'terra;edge-v1' WHERE a = %s AND b = %s", (a, b))
    assert shown() == [("related", "terra;edge-v1")]   # a method first used after the unlink connects it again


def test_embedding_neighbours_never_replace_another_methods_connection(seeded, monkeypatch):
    monkeypatch.setattr(lattice, "RELATED_MIN", -1.0)   # every pair of the three is a neighbor
    lattice.relate(seeded, HashEmbedder())
    methods = dict(((a, b), m) for a, b, m in table(seeded, "SELECT a, b, method FROM im.connections"))
    assert len(methods) == 3 and sorted(m.split(":")[0] for m in methods.values()) == \
        ["embedding", "embedding", JUDGED]   # one per pair; the judge's stays the judge's
    decided = next(p for p, m in methods.items() if m.startswith("embedding"))
    seeded.execute("UPDATE im.connections SET method = 'terra;edge-v1' WHERE (a, b) = (%s, %s)", decided)
    monkeypatch.setattr(lattice, "RELATED_MIN", 2.0)   # no neighbors at all now
    lattice.relate(seeded, HashEmbedder())
    assert sorted(table(seeded, "SELECT method FROM im.connections")) == [(JUDGED,), ("terra;edge-v1",)]
