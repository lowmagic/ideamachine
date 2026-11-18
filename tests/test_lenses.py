"""Lenses stored (M5, decision 0013): made, edited and pinned by my events, applied the moment they're sent; a cluster
is in a lens if it's pinned in, or it passes the query and the filters and isn't pinned out; a pin follows its speech
through a merge and goes when that speech is forgotten; boards place lenses in their drawing's places."""

import json
import uuid

import psycopg
import pytest

from im import checks, merge

from helpers import run, table
from test_review import as_web_role

EVENT = "INSERT INTO pub.feedback_events (kind, idea_id, payload) VALUES (%s, %s, %s)"


def send(admin, kind, payload, idea=None):
    as_web_role(admin, EVENT, kind, idea, json.dumps(payload))


def results(conn):
    return [r for (r,) in table(conn, """SELECT a.result FROM im.feedback_applied a
                                         JOIN pub.feedback_events f USING (event_id)
                                         WHERE f.kind LIKE 'lens%%' OR f.kind LIKE 'board%%' ORDER BY a.event_id""")]


def add(admin, name, kind="all", **more):
    lens = uuid.uuid4()
    send(admin, "lens_add", {"lens_id": str(lens), "name": name, "query": {"kind": kind}} | more)
    return lens


def card(conn, title_like):
    return table(conn, "SELECT idea_id FROM pub.cards WHERE title LIKE %s", title_like)[0][0]


def members(admin, lens, view="lens_members"):
    return sorted(as_web_role(admin, f"""SELECT c.title, m.why FROM pub.{view} m JOIN pub.cards c USING (idea_id)
                                          WHERE m.lens_id = %s""", lens))


def test_a_lens_is_made_edited_and_set_aside_by_my_events(seeded_lattice, admin):
    pipe = seeded_lattice
    lens = add(admin, " Signals ", description="What a signal says")
    send(admin, "lens_add", {"lens_id": str(lens), "name": "Signals", "query": {"kind": "all"}})   # a resend
    add(admin, "signals")   # another lens can't take its name, whatever the case
    send(admin, "lens_edit", {"lens_id": str(lens), "name": "Signals and noise", "query": {"kind": "none"}})
    send(admin, "lens_state", {"lens_id": str(lens), "state": "archived"})
    send(admin, "lens_state", {"lens_id": str(uuid.uuid4()), "state": "pinned"})
    assert results(pipe) == ["added", "repeat", "name taken", "edited", "archived", "no such lens"]
    assert as_web_role(admin, "SELECT name, description, kind, state FROM pub.lenses WHERE lens_id = %s", lens) == \
        [("Signals and noise", "What a signal says", "none", "archived")]

    for bad in ({"lens_id": str(uuid.uuid4()), "name": "x", "query": {"kind": "vibes"}},
                {"lens_id": str(uuid.uuid4()), "name": "x", "query": {"kind": "all"}, "filters": {"mood": "up"}},
                {"lens_id": str(uuid.uuid4()), "name": "x", "query": {"kind": "all"},
                 "filters": {"said_since": "2026-02-30"}},
                {"lens_id": "not a uuid", "name": "x", "query": {"kind": "all"}},
                {"lens_id": str(uuid.uuid4()), "query": {"kind": "all"}}):
        with pytest.raises(psycopg.errors.CheckViolation):
            send(admin, "lens_add", bad)


def test_the_filters_admit_and_my_pins_beat_them(seeded_lattice, admin):
    pipe = seeded_lattice
    everything, mine = add(admin, "Everything"), add(admin, "Kept", filters={"kept_as": ["canonical"]})
    pinned = add(admin, "By hand", kind="none")
    cards = sorted(t for (t,) in table(pipe, "SELECT title FROM pub.cards"))
    kept = sorted(t for (t,) in table(pipe, "SELECT title FROM pub.cards WHERE kept_as = 'canonical'"))
    assert 0 < len(kept) < len(cards)
    assert members(admin, everything) == [(t, "query") for t in cards]
    assert members(admin, mine) == [(t, "query") for t in kept]
    assert members(admin, pinned) == []

    builds, coffee = card(pipe, "Broken builds%"), card(pipe, "The coffee order%")
    send(admin, "lens_pin", {"lens_id": str(mine), "pin": "in"}, builds)   # not kept: in only by my pin
    send(admin, "lens_pin", {"lens_id": str(everything), "pin": "out"}, coffee)
    send(admin, "lens_pin", {"lens_id": str(pinned), "pin": "in"}, coffee)
    assert ("Broken builds as signal", "pin") in members(admin, mine)
    assert "The coffee order is the real meeting." not in [t for t, _ in members(admin, everything)]
    assert members(admin, pinned) == [("The coffee order is the real meeting.", "pin")]

    send(admin, "lens_pin", {"lens_id": str(pinned), "pin": "out"}, coffee)   # my latest decides
    send(admin, "lens_unpin", {"lens_id": str(everything)}, coffee)   # back to what the query says
    send(admin, "lens_unpin", {"lens_id": str(everything)}, coffee)
    assert results(pipe)[3:] == ["pinned in", "pinned out", "pinned in", "pinned out", "unpinned", "not pinned"]
    assert members(admin, pinned) == []
    assert ("The coffee order is the real meeting.", "query") in members(admin, everything)

    seeded = table(pipe, """SELECT d.idea_id FROM im.ideas d WHERE d.origin = 'mine'
                            AND NOT EXISTS (SELECT 1 FROM im.idea_evidence ev WHERE ev.idea_id = d.idea_id)""")[0][0]
    send(admin, "lens_pin", {"lens_id": str(pinned), "pin": "in"}, seeded)   # no speech, nothing typed, nothing read
    assert results(pipe)[-1] == "pinned in"
    assert table(pipe, "SELECT source_ref FROM im.lens_pins WHERE lens_id = %s AND segment_ids = '{}'", pinned) == \
        [(f"idea:{seeded}",)]   # anchored to its own id


def test_a_pin_follows_its_speech_through_a_merge_and_a_typed_idea_by_what_i_typed(seeded_lattice, admin):
    pipe = seeded_lattice
    pinned = add(admin, "By hand", kind="none")
    builds, coffee = card(pipe, "Broken builds%"), card(pipe, "The coffee order%")
    send(admin, "lens_pin", {"lens_id": str(pinned), "pin": "in"}, builds)
    merge.merge(pipe, str(coffee), str(builds))
    assert members(admin, pinned) == [("The coffee order is the real meeting.", "pin")]   # the cluster that holds it

    typed = uuid.uuid4()
    as_web_role(admin, "INSERT INTO pub.feedback_events (kind, idea_id, payload) VALUES ('idea_add', %s, %s)",
                typed, json.dumps({"title": "Calm is a signal", "statement": "Others read your calm."}))
    send(admin, "lens_pin", {"lens_id": str(pinned), "pin": "in"}, typed)
    assert ("Calm is a signal", "pin") in members(admin, pinned)
    assert table(pipe, """SELECT source_ref LIKE 'idea_add:%%' FROM im.lens_pins
                          WHERE segment_ids = '{}' AND lens_id = %s""", pinned) == [(True,)]


def test_forgetting_the_pinned_speech_forgets_the_pin(seeded_lattice, admin, cfg, stream):
    pipe = seeded_lattice
    pinned = add(admin, "By hand", kind="none")
    lora = card(pipe, "Something new")
    send(admin, "lens_pin", {"lens_id": str(pinned), "pin": "in"}, lora)
    assert table(pipe, "SELECT count(*) FROM im.lens_pins WHERE lens_id = %s", pinned) == [(1,)]
    stream.forget(3600, 3612)   # the LoRa idea's line
    stream.write(stream.dir)
    run(pipe, cfg, stream)
    assert table(pipe, "SELECT count(*) FROM im.lens_pins WHERE lens_id = %s", pinned) == [(0,)]
    assert table(pipe, checks.TOMBSTONED) == []
    assert table(pipe, "SELECT name FROM im.lenses WHERE lens_id = %s", pinned) == [("By hand",)]   # mine, not speech's


def test_demo_mode_leaves_a_hidden_card_out_of_every_lens(seeded_lattice, admin):
    pipe = seeded_lattice
    everything = add(admin, "Everything")
    coffee = card(pipe, "The coffee order%")
    as_web_role(admin, "INSERT INTO pub.feedback_events (kind, idea_id, payload) VALUES ('idea_sensitive', %s, %s)",
                coffee, json.dumps({"sensitive": True}))
    assert "The coffee order is the real meeting." in [t for t, _ in members(admin, everything)]
    assert "The coffee order is the real meeting." not in [t for t, _ in members(admin, everything,
                                                                                  "demo_lens_members")]
    open_only = add(admin, "Not sensitive", filters={"sensitive": False})
    assert "The coffee order is the real meeting." not in [t for t, _ in members(admin, open_only)]


def test_a_board_places_lenses_in_its_drawings_places(seeded_lattice, admin):
    pipe = seeded_lattice
    boards = dict(as_web_role(admin, "SELECT drawing, board_id FROM pub.boards"))
    assert set(boards) == {"tree", "grid"}
    a, b = add(admin, "A"), add(admin, "B")
    tree, grid = str(boards["tree"]), str(boards["grid"])
    send(admin, "board_place", {"board_id": tree, "place": "kether", "lens_id": str(a)})
    send(admin, "board_place", {"board_id": tree, "place": "kether", "lens_id": str(b)})   # a place holds one lens
    send(admin, "board_place", {"board_id": tree, "place": "aleph", "lens_id": str(a)})
    send(admin, "board_place", {"board_id": tree, "place": "nowhere", "lens_id": str(a)})
    send(admin, "board_place", {"board_id": grid, "place": "3", "lens_id": str(a)})
    send(admin, "board_place", {"board_id": grid, "place": "kether", "lens_id": str(a)})
    send(admin, "board_place", {"board_id": str(uuid.uuid4()), "place": "1", "lens_id": str(a)})
    send(admin, "board_remove", {"board_id": tree, "place": "aleph"})
    send(admin, "board_remove", {"board_id": tree, "place": "aleph"})
    assert results(pipe) == ["added", "added", "placed", "placed", "placed", "no such place", "placed",
                             "no such place", "no such board", "removed", "nothing there"]
    placed = as_web_role(admin, """SELECT b.drawing, p.place, l.name FROM pub.board_lenses p
                                   JOIN pub.boards b USING (board_id) JOIN pub.lenses l USING (lens_id) ORDER BY 1, 2""")
    assert placed == [("grid", "3", "A"), ("tree", "kether", "B")]
