"""The lens pilot: Jev answers lens wordings kept outside the repo about every card, once per card and text; the
report counts what each lens lets in and lists the cards near each cut."""

import json

import pytest

from im import egress, jev, lens_audit

from helpers import table
from test_review import as_web_role

LENSES = {"lenses": [
    {"id": "coffee", "name": "Coffee", "description": "Coffee and who orders it.", "said_by_me": False, "rule": "max",
     "cut": 0.7, "deciding": {"about": ["Is `item` about coffee?", {"true": "Coffee is its subject",
                                                                   "false": "Coffee isn't"}],
                              "drinks": ["Is `item` about drinks?", None]},
     "probes": {"orders": ["Is `item` about ordering?", None]}},
    {"id": "mine", "name": "Mine", "description": "Anything I said.", "said_by_me": True, "rule": "mean", "cut": 0.5,
     "deciding": {"said": ["Did I say `item`?", None]}},
]}


def fake_jev(word="coffee"):
    """Jev saying yes (0.9) to every question about a card whose text mentions `word`, and no (0.1) otherwise; the
    `drinks` wording a little lower, so it ranks the same cards a little differently."""
    sent = []

    def post(body):
        sent.append(body)
        hit = word in json.dumps(body["state"]).lower()
        answers = {q: {"type": "noul", "noul": (0.9 if hit else 0.1) - (0.05 if q.endswith("drinks") else 0)}
                   for q in body["questions"]}
        return {"model": "jev-1.14.0", "answers": answers, "usage": {"input_tokens": 1000}}
    return jev.Jev(post=post), sent


@pytest.fixture
def folder(tmp_path):
    (tmp_path / "lenses.json").write_text(json.dumps(LENSES))
    return tmp_path


def test_each_card_is_asked_once_with_every_lens_that_admits_it(seeded_lattice, admin, folder):
    pipe = seeded_lattice
    client, sent = fake_jev()
    cards = lens_audit.cards(pipe)
    mine = sum(c["by_me"] for c in cards)
    assert 0 < mine < len(cards)   # the fixture has cards I didn't say, which `mine` never asks about

    first = lens_audit.draft(pipe, client, folder, limit=2)
    assert first["answered"] == len(sent) == 2
    stats = lens_audit.draft(pipe, client, folder)   # the rest; the first two aren't asked again
    assert stats["answered"] == len(cards) - 2 and len(sent) == len(cards)
    asked = [set(b["questions"]) for b in sent]
    assert sum("mine__said" in q for q in asked) == mine
    assert all({"coffee__about", "coffee__drinks", "coffee__orders"} <= q for q in asked)
    # Every request is logged, carrying the segments behind the card's captures (none for an idea I added).
    logged = table(pipe, "SELECT count(*), count(*) FILTER (WHERE cardinality(segment_ids) > 0) FROM im.egress_log "
                         "WHERE prompt_version LIKE 'lens-pilot:%%'")
    assert logged == [(len(cards), sum(bool(egress.segments_of(pipe, [c["id"]])) for c in cards))]
    assert lens_audit.draft(pipe, client, folder)["asked"] == 0

    coffee = table(pipe, "SELECT idea_id FROM pub.cards WHERE statement LIKE '%%coffee%%'")[0][0]
    as_web_role(admin, "INSERT INTO pub.feedback_events (kind, idea_id, payload) VALUES ('idea_correct', %s, %s)",
                coffee, json.dumps({"title": "Tea runs it", "statement": "The tea order is the real meeting."}))
    again = lens_audit.draft(pipe, client, folder)   # the corrected card alone, asked again
    assert again["answered"] == 1 and "The tea order" in json.dumps(sent[-1]["state"])

    (folder / "lenses.json").write_text(json.dumps({"lenses": [LENSES["lenses"][1] | {"cut": 0.6}]}))
    with pytest.raises(SystemExit, match="changed wordings are a new folder"):
        lens_audit.draft(pipe, client, folder)


def test_the_report_counts_each_lens_by_its_rule_and_cut(seeded_lattice, folder):
    pipe = seeded_lattice
    client, _ = fake_jev("garden")
    lens_audit.draft(pipe, client, folder)
    cards = lens_audit.cards(pipe)
    garden = sum("garden" in json.dumps(c["item"]).lower() for c in cards)
    garden_mine = sum("garden" in json.dumps(c["item"]).lower() and c["by_me"] for c in cards)
    assert garden > 0

    out = lens_audit.report(pipe, folder)
    coffee, mine = out["lenses"]["coffee"], out["lenses"]["mine"]
    assert (coffee["asked"], coffee["members"]) == (len(cards), garden)   # max: 0.9 passes 0.7, 0.1 doesn't
    assert (mine["asked"], mine["members"]) == (sum(c["by_me"] for c in cards), garden_mine)
    assert coffee["wordings"]["about~drinks"] == 1.0 and coffee["probes"]["orders"]["at_cut"] == garden
    assert out["shared_members"]["coffee&mine"] == garden_mine
    near = (folder / "near-cut.md").read_text()
    assert "## coffee: Coffee" in near and "### Just below the cut" in near


def test_wordings_that_cant_be_asked_are_refused(tmp_path):
    bad = {"lenses": [LENSES["lenses"][0] | {"rule": "vote"}]}
    (tmp_path / "lenses.json").write_text(json.dumps(bad))
    with pytest.raises(SystemExit, match="rule must be one of"):
        lens_audit.load(tmp_path)
