"""Sensitivity tags: Jev's yes or no per tag on each idea and lens (a theme); my own marks win, at once."""

import json

import psycopg
import pytest

from im import jev, sensitivity

from helpers import table
from test_review import as_web_role


def lens(conn, name):
    return table(conn, "SELECT lens_id FROM im.lenses WHERE name = %s", name)[0][0]


def tagger(word="coffee", broken=0):
    """Jev saying `violence` of anything whose text mentions `word`, and no to every other tag; its first `broken`
    answers come back without one."""
    sent = []

    def post(body):
        sent.append(body)
        hit = word in json.dumps(body["state"]).lower()
        answers = {q: {"type": "noul", "noul": 0.9 if hit and q == "violence" else 0.1} for q in body["questions"]}
        if len(sent) <= broken:
            answers.pop("violence")
        return {"model": "jev-1.14.0", "answers": answers, "usage": {"input_tokens": 300}}
    return jev.Jev(post=post), sent


def shown(admin):
    return {(k, str(i)): (s, c, b) for k, i, s, c, b in as_web_role(
        admin, 'SELECT kind, id, sensitive, categories, "by" FROM pub.sensitivity')}


def test_jev_tags_once_and_again_when_the_text_changes(seeded_lattice, admin):
    pipe = seeded_lattice
    client, sent = tagger()
    n = table(pipe, "SELECT (SELECT count(*) FROM pub.ideas) + (SELECT count(*) FROM im.lenses)")[0][0]
    stats = sensitivity.tag_stage(pipe, client)
    assert stats["pending"] == stats["tagged"] == len(sent) == n and stats["sensitive"] == 1
    assert set(sent[0]["questions"]) == set(sensitivity.TAGS)
    coffee = table(pipe, "SELECT idea_id FROM pub.ideas WHERE statement LIKE '%%coffee%%'")[0][0]
    assert shown(admin) == {("idea", str(coffee)): (True, ["violence"], "model")}   # untagged: not listed
    (p,) = table(pipe, "SELECT probabilities FROM im.idea_sensitivity WHERE idea_id = %s", coffee)[0]
    assert p == {t: 0.9 if t == "violence" else 0.1 for t in sensitivity.TAGS}   # kept, to set the cut from my marks
    assert sensitivity.tag_stage(pipe, client)["pending"] == 0

    as_web_role(admin, "INSERT INTO pub.feedback_events (kind, idea_id, payload) VALUES ('idea_correct', %s, %s)",
               coffee, json.dumps({"title": "Tea runs it", "statement": "The tea order is the real meeting."}))
    stats = sensitivity.tag_stage(pipe, client)   # the changed idea alone, asked again
    assert (stats["pending"], stats["tagged"]) == (1, 1) and "The tea order" in json.dumps(sent[-1]["state"])

    item = table(pipe, "SELECT item_id FROM pub.card_evidence WHERE idea_id = %s AND item_id IS NOT NULL", coffee)[0][0]
    as_web_role(admin, "INSERT INTO pub.feedback_events (kind, item_id, payload) VALUES ('item_reading', %s, %s)",
                item, json.dumps({"reading": "Who's got the tea order?", "meaning_changed": True}))
    stats = sensitivity.tag_stage(pipe, client)   # my reading of its quote: that card alone, with my reading
    assert (stats["pending"], stats["tagged"]) == (1, 1)
    assert sent[-1]["state"]["item"]["my_readings"] == ["Who's got the tea order?"]


def test_my_marks_win_at_once_either_way(seeded_lattice, admin):
    pipe = seeded_lattice
    sensitivity.tag_stage(pipe, tagger()[0])
    coffee = table(pipe, "SELECT idea_id FROM pub.ideas WHERE statement LIKE '%%coffee%%'")[0][0]
    jokes = lens(pipe, "Jokes")
    mark = "INSERT INTO pub.feedback_events (kind, idea_id, theme_id, payload) VALUES (%s, %s, %s, %s)"
    as_web_role(admin, mark, "idea_sensitive", coffee, None, json.dumps({"sensitive": False}))
    as_web_role(admin, mark, "theme_sensitive", None, jokes, json.dumps({"sensitive": True}))
    assert shown(admin) == {("idea", str(coffee)): (False, ["violence"], "owner"),
                            ("theme", str(jokes)): (True, [], "owner")}
    for kind, idea, theme, payload in [("idea_sensitive", coffee, None, {}),
                                       ("idea_sensitive", None, None, {"sensitive": True}),
                                       ("idea_sensitive", coffee, None, {"sensitive": "yes"}),
                                       ("theme_sensitive", None, None, {"sensitive": True})]:
        with pytest.raises(psycopg.errors.CheckViolation):
            as_web_role(admin, mark, kind, idea, theme, json.dumps(payload))
    other = lens(pipe, "Models and signals")
    as_web_role(admin, "INSERT INTO pub.feedback_events (kind, theme_id, payload) VALUES ('merge_theme', %s, %s)",
               jokes, json.dumps({"into": str(other)}))
    assert ("theme", str(jokes)) not in shown(admin)   # merged away, archived; its old mark stays as history


def test_my_latest_mark_is_what_demo_mode_hides(seeded_lattice, admin):
    pipe = seeded_lattice
    coffee = table(pipe, "SELECT idea_id FROM pub.ideas WHERE statement LIKE '%%coffee%%'")[0][0]
    jokes = lens(pipe, "Jokes")
    mark = "INSERT INTO pub.feedback_events (kind, idea_id, theme_id, payload) VALUES (%s, %s, %s, %s)"
    for sensitive in (True, False, True, False):   # I change my mind, and back, and again
        as_web_role(admin, mark, "idea_sensitive", coffee, None, json.dumps({"sensitive": sensitive}))
        as_web_role(admin, mark, "theme_sensitive", None, jokes, json.dumps({"sensitive": not sensitive}))
        assert shown(admin) == {("idea", str(coffee)): (sensitive, [], "owner"),
                                ("theme", str(jokes)): (not sensitive, [], "owner")}


def test_quotes_are_read_too(seeded_lattice, admin):
    pipe = seeded_lattice
    client, sent = tagger(word="lora")   # only the LoRa idea's quote says "LoRa"; its title doesn't
    sensitivity.tag_stage(pipe, client)
    lora = table(pipe, "SELECT idea_id FROM pub.ideas WHERE title = 'Something new'")[0][0]
    assert ("idea", str(lora)) in shown(admin)
    assert any("said" in b["state"]["item"] for b in sent)


def test_an_answer_that_fails_is_logged_and_asked_again_next_run(seeded_lattice):
    pipe = seeded_lattice
    client, sent = tagger(broken=2)
    stats = sensitivity.tag_stage(pipe, client)
    assert stats["failed"] == 2 and stats["tagged"] == stats["pending"] - 2   # the ones after them were tagged
    logged = table(pipe, "SELECT count(*), count(error) FROM im.egress_log WHERE prompt_version = %s",
                   f"sensitivity:{sensitivity.tagger_version(client)}")
    assert logged == [(len(sent), 2)]
    again = sensitivity.tag_stage(pipe, tagger()[0])
    assert again["pending"] == again["tagged"] == 2
