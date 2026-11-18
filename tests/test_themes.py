"""Themes are lenses (M5, migration 071): every lens not archived is listed as a theme, an idea's themes are the
lenses it's in, and the theme events shape lenses, applied the moment they're sent."""

import json
import uuid

import psycopg
import pytest

from im import extract, lattice

from helpers import HashEmbedder, run, table
from test_extract import FakeClaude
from test_lattice import FakeMatcher, items_for, seed
from test_review import as_web_role

EVENT = "INSERT INTO pub.feedback_events (kind, theme_id, idea_id, payload) VALUES (%s, %s, %s, %s)"


@pytest.fixture
def lat(pipe, cfg, stream):
    seed(pipe)
    run(pipe, cfg, stream)
    extract.run_stage(pipe, FakeClaude(items_for))
    lattice.match_stage(pipe, FakeMatcher(), HashEmbedder())
    return pipe


def send(admin, kind, theme, idea=None, payload=None):
    return as_web_role(admin, EVENT, kind, theme, idea, json.dumps(payload or {}))


def results(conn):
    return [r for (r,) in table(conn, """SELECT a.result FROM im.feedback_applied a
                                         JOIN pub.feedback_events f USING (event_id)
                                         WHERE f.kind LIKE '%%theme%%' OR f.kind LIKE '%%file_idea'
                                         ORDER BY a.event_id""")]


def theme(conn, name):
    return table(conn, "SELECT lens_id FROM im.lenses WHERE name = %s", name)[0][0]


def idea(conn, title_like):
    return table(conn, "SELECT idea_id FROM im.ideas WHERE title LIKE %s", title_like)[0][0]


def members(conn, lens):
    return sorted(t for (t,) in table(conn, """SELECT d.title FROM pub.lens_members m JOIN pub.ideas d USING (idea_id)
                                              WHERE m.lens_id = %s""", lens))


def themed(conn, title_like):
    return table(conn, "SELECT themes FROM pub.ideas WHERE title LIKE %s", title_like)[0][0]


def test_my_lenses_not_archived_are_my_themes(lat, admin):
    lat.execute("""INSERT INTO im.lenses (lens_id, name, query, state) VALUES
                     (gen_random_uuid(), 'Coffee', '{"kind": "jev", "deciding": {"a": ["Is `item` about coffee?", null]},
                      "rule": "max", "cut": 0.5}', 'active'),
                     (gen_random_uuid(), 'Old', '{"kind": "none"}', 'archived')""")
    assert as_web_role(admin, "SELECT name, origin, pinned, n_ideas FROM pub.themes ORDER BY name") == [
        ("Coffee", "mine", False, 0), ("Jokes", "mine", True, 1), ("Models and signals", "mine", True, 2)]
    assert themed(lat, "A solitary%") == ["Jokes"]
    assert themed(lat, "Signal may%") == ["Models and signals"]


def test_pin_unpin_and_rename_a_theme_and_reject_none(lat, admin):
    jokes = theme(lat, "Jokes")
    send(admin, "unpin_theme", jokes)
    send(admin, "rename_theme", jokes, payload={"name": "models AND signals"})   # another theme's name
    send(admin, "rename_theme", jokes, payload={"name": " Quips "})
    send(admin, "reject_theme", jokes)
    assert results(lat) == ["unpinned", "name taken", "renamed",
                            "not rejected: only proposals can be, and themes are no longer proposed"]
    assert table(lat, "SELECT name, state FROM im.lenses WHERE lens_id = %s", jokes) == [("Quips", "active")]
    send(admin, "pin_theme", jokes)
    assert table(lat, "SELECT state FROM im.lenses WHERE lens_id = %s", jokes) == [("pinned",)]
    send(admin, "pin_theme", uuid.uuid4())   # no such lens: kept, its id in the payload
    assert results(lat)[-1] == "no such theme"


def test_a_merge_moves_my_pins_and_archives_the_theme_merged_away(lat, admin):
    models, jokes = theme(lat, "Models and signals"), theme(lat, "Jokes")
    before = members(lat, models) + members(lat, jokes)
    send(admin, "merge_theme", jokes, payload={"into": str(models)})
    send(admin, "merge_theme", jokes, payload={"into": str(models)})   # a resend changes nothing
    assert results(lat) == ["merged", "merged"]
    assert members(lat, models) == sorted(before)
    assert table(lat, "SELECT state FROM im.lenses WHERE lens_id = %s", jokes) == [("archived",)]
    assert as_web_role(admin, "SELECT name, n_ideas FROM pub.themes") == [("Models and signals", 3)]
    assert themed(lat, "A solitary%") == ["Models and signals"]


def test_a_split_makes_my_theme_under_the_senders_id(lat, admin):
    models = theme(lat, "Models and signals")
    inside = members(lat, models)
    new = uuid.uuid4()
    split = {"new_theme_id": str(new), "name": "Signals proper",
             "idea_ids": [str(idea(lat, inside[0])), str(uuid.uuid4())]}
    send(admin, "split_theme", models, payload=split)
    send(admin, "split_theme", models, payload=split)   # a resend
    send(admin, "pin_theme", new)   # the sender can act on the new theme at once
    assert results(lat) == ["split: 1 moved", "repeat", "pinned"]
    assert as_web_role(admin, "SELECT name, origin, pinned, n_ideas FROM pub.themes WHERE theme_id = %s", new) == \
        [("Signals proper", "mine", True, 1)]
    assert members(lat, models) == inside[1:]   # pinned out of the source
    assert members(lat, new) == inside[:1]


def test_filing_and_unfiling_pin_an_idea_in_and_out(lat, admin):
    jokes = theme(lat, "Jokes")
    builds = idea(lat, "Broken builds%")
    send(admin, "file_idea", jokes, builds)
    assert themed(lat, "Broken builds%") == ["Jokes"]
    send(admin, "unfile_idea", jokes, builds)
    send(admin, "unfile_idea", jokes, idea(lat, "A solitary%"))   # one I filed by hand before: out as well
    assert results(lat) == ["filed", "unfiled", "unfiled"]
    assert themed(lat, "Broken builds%") == [] and members(lat, jokes) == []


def test_theme_events_are_checked(lat, admin):
    models = theme(lat, "Models and signals")
    builds = idea(lat, "Broken builds%")
    for kind, lens, i, payload in [
            ("pin_theme", None, None, {}), ("rename_theme", models, None, {"name": " "}),
            ("rename_theme", models, None, {"name": "x" * 81}), ("merge_theme", models, None, {"into": str(models)}),
            ("merge_theme", models, None, {"into": "nope"}), ("merge_theme", models, None, {}),
            ("split_theme", models, None, {"name": "A", "idea_ids": [str(builds)]}),   # no new_theme_id
            ("split_theme", models, None, {"new_theme_id": str(uuid.uuid4()), "name": "A", "idea_ids": []}),
            ("file_idea", models, None, {}), ("unfile_idea", None, builds, {})]:
        with pytest.raises(psycopg.errors.CheckViolation):
            send(admin, kind, lens, i, payload)
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        as_web_role(admin, "DELETE FROM im.lens_pins")
