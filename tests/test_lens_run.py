"""The `jev` kind of lens (M5 slice 4): lenses imported from a lenses.json as the pilots measured them, Jev asked about
each card once per query and text, nearest the description first; a card is in at its cut; editing the query asks
again, another Jev model is held by the re-read gate."""

import json

import psycopg
import pytest

from im import jev, lens, lens_run

from helpers import HashEmbedder, table
from test_lenses import send
from test_review import as_web_role

LENSES = {"lenses": [
    {"id": "coffee", "name": "Coffee", "description": "Coffee and who orders it.", "said_by_me": False, "rule": "max",
     "cut": 0.7, "deciding": {"about": ["Is `item` about coffee?", {"true": "Coffee is its subject",
                                                                   "false": "Coffee isn't"}],
                              "drinks": ["Is `item` about drinks?", None]},
     "probes": {"orders": ["Is `item` about ordering?", None]}},
    {"id": "mine", "name": "Said by me", "description": "Anything I said.", "said_by_me": True, "rule": "mean",
     "cut": 0.5, "deciding": {"said": ["Did I say `item`?", None]}},
]}


def fake_jev(word="coffee", model=jev.MODEL):
    """Jev saying yes (0.9) to every question about a card whose text mentions `word`, and no (0.1) otherwise."""
    sent = []

    def post(body):
        sent.append(body)
        hit = word in json.dumps(body["state"]).lower()
        return {"model": model, "answers": {q: {"type": "noul", "noul": 0.9 if hit else 0.1} for q in body["questions"]},
                "usage": {"input_tokens": 100}}
    return jev.Jev(post=post, model=model), sent


@pytest.fixture
def imported(seeded_lattice, tmp_path):
    path = tmp_path / "lenses.json"
    path.write_text(json.dumps(LENSES))
    assert lens_run.import_file(seeded_lattice, path) == {"added": ["Coffee", "Said by me"], "edited": []}
    return path


def lens_id(conn, name):
    return table(conn, "SELECT lens_id::text FROM pub.lenses WHERE name = %s", name)[0][0]


def members(admin, lens_id):
    return sorted(as_web_role(admin, """SELECT c.title, m.why, m.score FROM pub.lens_members m
                                        JOIN pub.cards c USING (idea_id) WHERE m.lens_id = %s""", lens_id))


def test_a_jev_lens_holds_the_cards_its_answers_put_at_its_cut(seeded_lattice, imported, admin):
    pipe = seeded_lattice
    coffee = lens_id(pipe, "Coffee")
    assert table(pipe, "SELECT kind, filters, query->'deciding' ? 'orders' FROM pub.lenses WHERE name = 'Coffee'") \
        == [("jev", {}, False)]   # a probe decides nothing, so it isn't asked
    assert members(admin, coffee) == [] and lens.judged(pipe, {"id": coffee})[0] == 0   # nothing asked: nothing in
    client, sent = fake_jev()
    stats = lens_run.run(pipe, client, HashEmbedder(), coffee, everything=True)
    cards = table(pipe, "SELECT count(*) FROM pub.cards")[0][0]
    assert stats["asked"] == len(sent) == cards and set(sent[0]["questions"]) == {"about", "drinks"}
    assert members(admin, coffee) == [("The coffee order is the real meeting.", "query", pytest.approx(0.9))]
    assert lens.judged(pipe, {"id": coffee}) == (cards, cards)
    assert lens_run.run(pipe, client, HashEmbedder(), coffee, everything=True)["asked"] == 0   # answered: kept

    # A card's text changes: only it is asked again.
    builds = table(pipe, "SELECT idea_id FROM pub.cards WHERE title LIKE 'Broken builds%%'")[0][0]
    as_web_role(admin, "INSERT INTO pub.feedback_events (kind, idea_id, payload) VALUES ('idea_correct', %s, %s)",
                builds, json.dumps({"statement": "Broken builds are the coffee of engineering."}))
    assert lens_run.run(pipe, client, HashEmbedder(), coffee, everything=True)["asked"] == 1
    assert ("Broken builds as signal", "query", pytest.approx(0.9)) in members(admin, coffee)

    # My edit of the query is my request to ask again: everything, held by nothing.
    spec = json.loads(imported.read_text())
    spec["lenses"][0]["cut"] = 0.95
    imported.write_text(json.dumps(spec))
    assert lens_run.import_file(pipe, imported, state="pinned") == {"added": [], "edited": ["Coffee", "Said by me"]}
    assert members(admin, coffee) == [] and lens.judged(pipe, {"id": coffee})[0] == 0
    again = lens_run.run(pipe, client, HashEmbedder(), coffee, everything=True)
    assert again["asked"] == cards and "held" not in again and members(admin, coffee) == []   # nothing at 0.95
    assert table(pipe, "SELECT DISTINCT state FROM pub.lenses") == [("pinned",)]


def test_a_run_asks_the_nearest_first_and_the_rest_with_all(seeded_lattice, imported, monkeypatch):
    pipe = seeded_lattice
    monkeypatch.setattr(lens_run, "NEAREST", 2)
    coffee = lens_id(pipe, "Coffee")
    client, sent = fake_jev()
    assert lens_run.run(pipe, client, HashEmbedder(), coffee)["asked"] == 2
    asked = {json.dumps(b["state"]) for b in sent}
    assert any("coffee" in a.lower() for a in asked)   # the nearest the description, by meaning
    rest = lens_run.run(pipe, client, HashEmbedder(), coffee, everything=True)
    assert rest["asked"] == table(pipe, "SELECT count(*) FROM pub.cards")[0][0] - 2


def test_the_filters_say_which_cards_are_asked(seeded_lattice, imported):
    pipe = seeded_lattice
    client, sent = fake_jev()
    lens_run.run(pipe, client, HashEmbedder(), lens_id(pipe, "Said by me"), everything=True)
    assert len(sent) == table(pipe, "SELECT count(*) FROM pub.cards WHERE by_me > 0")[0][0] > 0
    assert table(pipe, "SELECT filters FROM pub.lenses WHERE name = 'Said by me'") == [({"said_by_me": True},)]


def test_another_jev_model_is_held_by_the_reread_gate(seeded_lattice, imported):
    pipe = seeded_lattice
    coffee = lens_id(pipe, "Coffee")
    lens_run.run(pipe, fake_jev()[0], HashEmbedder(), coffee, everything=True)
    newer, sent = fake_jev(model="jev-9.0.0")
    held = lens_run.run(pipe, newer, HashEmbedder(), coffee, everything=True)
    cards = table(pipe, "SELECT count(*) FROM pub.cards")[0][0]
    assert held["asked"] == 0 and sent == [] and held["held"]["held"] == cards
    assert held["held"]["approve"] == "im reread lenses"
    assert members_count(pipe, coffee) == 1   # the old answers stand meanwhile
    sent_again = lens_run.stage(pipe, newer, HashEmbedder(), held_only=True)
    assert sent_again["Coffee"]["asked"] == cards and len(sent) >= cards
    assert table(pipe, "SELECT DISTINCT model FROM im.lens_answers WHERE lens_id = %s", coffee) == [("jev-9.0.0",)]


def members_count(conn, lens_id):
    return table(conn, "SELECT count(*) FROM pub.lens_members WHERE lens_id = %s", lens_id)[0][0]


def test_im_run_asks_every_jev_lens_not_archived(seeded_lattice, imported, admin):
    pipe = seeded_lattice
    send(admin, "lens_state", {"lens_id": lens_id(pipe, "Said by me"), "state": "archived"})
    client, _ = fake_jev()
    got = lens_run.stage(pipe, client, HashEmbedder())
    assert list(got) == ["Coffee"] and got["Coffee"]["asked"] > 0
    assert lens_run.stage(pipe, client, HashEmbedder())["Coffee"]["asked"] == 0


def test_a_jev_query_that_cant_be_asked_is_refused(seeded_lattice, admin):
    for query in ({"kind": "jev", "deciding": {}, "rule": "max", "cut": 0.7},
                  {"kind": "jev", "deciding": {"a": ["q", None]}, "rule": "vote", "cut": 0.7},
                  {"kind": "jev", "deciding": {"a": ["q", None]}, "rule": "max", "cut": 1.5},
                  {"kind": "jev", "deciding": {"a": ["q", {"true": "yes"}]}, "rule": "max", "cut": 0.7},
                  {"kind": "jev", "deciding": {"A b": ["q", None]}, "rule": "max", "cut": 0.7}):
        with pytest.raises(psycopg.errors.CheckViolation):
            send(admin, "lens_add", {"lens_id": "00000000-0000-0000-0000-000000000001", "name": "x", "query": query})
