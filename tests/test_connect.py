"""The connection judge: nominated pairs, screened by Jev, judged by Terra, its verdicts kept and its connections
replacing the stand-ins."""

import json
from dataclasses import replace

import numpy as np
import pytest

from im import config, connect, edge_audit, jev, lattice, llm
from im.config import Config, ConnectConfig

from helpers import HashEmbedder, assert_invariants, run, table
from test_lattice import JUDGED

TERRA = "openai/gpt-6.1-terra"


def screen(belongs=lambda state: 0.9):
    """Jev answering `belongs(state)` to the edge question, recording every request."""
    sent = []

    def post(body):
        sent.append(body)
        p = belongs(body["state"])
        return {"model": jev.MODEL, "answers": {q: {"type": "noul", "noul": p} for q in body["questions"]},
                "usage": {"input_tokens": 1000, "output_tokens": 9}}
    return jev.Jev(post=post), sent


def terra(answer=lambda text: "related"):
    """Terra through OpenRouter, answering `answer(text)`: a label, (label, direction), or None to refuse. Records the
    text of every request."""
    sent = []

    def post(body):
        text = body["messages"][1]["content"]
        sent.append(text)
        got = answer(text)
        if got is None:
            return {"id": "t", "model": TERRA, "usage": {"cost": 0.0025},
                    "choices": [{"finish_reason": "content_filter", "message": {"content": None}}]}
        label, direction = got if isinstance(got, tuple) else (got, "none")
        content = json.dumps({"label": label, "direction": direction, "analogy": "", "why": f"{label}, because"})
        return {"id": "t", "model": TERRA, "usage": {"cost": 0.0025},
                "choices": [{"finish_reason": "stop", "message": {"content": content}}]}
    return llm.OpenRouter(post=post), sent


def judge(conn, belongs=lambda state: 0.9, answer=lambda text: "related", limit=None, held_only=False):
    """One run of the judge: its stats, what Jev was sent, and what Terra was. `held_only`: `im reread connect`."""
    jev_client, jev_sent = screen(belongs)
    terra_client, terra_sent = terra(answer)
    return connect.stage(conn, terra_client, jev_client, limit=limit, held_only=held_only), jev_sent, terra_sent


def idea(conn, word: str):
    (got,) = table(conn, "SELECT idea_id FROM im.idea_text WHERE title LIKE %s", f"%{word}%")[0]
    return got


def methods(conn) -> dict:
    return dict(((a, b), m) for a, b, m in table(conn, "SELECT a, b, method FROM im.connections"))


def verdicts(conn) -> dict:
    return dict(((a, b), v) for a, b, v in table(conn, "SELECT a, b, verdict FROM im.connection_verdicts"))


@pytest.fixture
def nearby(seeded_lattice, monkeypatch):
    """The seeded lattice, with every pair of its cards nominated and standing in as embedding neighbors."""
    monkeypatch.setattr(connect, "FLOOR", -1.0)
    monkeypatch.setattr(lattice, "RELATED_MIN", -1.0)
    monkeypatch.setattr(lattice, "RELATED_K", 10)
    lattice.relate(seeded_lattice, HashEmbedder())
    return seeded_lattice


def every_pair(conn) -> int:
    (n,) = table(conn, """SELECT count(*) FROM im.idea_embeddings
                          WHERE idea_id IN (SELECT idea_id FROM pub.cards)""")[0]
    return n * (n - 1) // 2


def test_b4s_prompt_is_the_judges_and_its_version_is_unchanged():
    # Terra passed B4 under this version; edge_audit's drafts and the judge send the same prompt.
    assert llm.version(connect.SYSTEM, connect.SCHEMA, TERRA, "low") == "1f2767ae110b" == connect.prompt_version()
    assert not hasattr(edge_audit, "DRAFT_SYSTEM")


def test_the_judge_is_terra_at_low_effort_unless_configured(tmp_path, monkeypatch):
    assert Config().connect == ConnectConfig(TERRA, "low", None, 500)
    path = tmp_path / "im.toml"
    path.write_text("[match]\neffort = \"medium\"\n")
    monkeypatch.setenv("IM_CONFIG", str(path))
    assert config.load().connect == ConnectConfig(TERRA, "low")
    path.write_text("[connect]\nmax_tokens = 8000\nmax_pairs_per_run = 50\n")
    assert config.load().connect == ConnectConfig(TERRA, "low", 8000, 50)


def test_pairs_are_nominated_at_the_floor_among_either_sides_nearest(monkeypatch):
    degrees = {"a": 0, "b": 10, "c": 25, "d": 90}   # d is far from the rest: below the floor from each
    M = 3 * np.array([[np.cos(np.radians(x)), np.sin(np.radians(x))] for x in degrees.values()])
    assert set(connect.neighbours(list(degrees), M)) == {("a", "b"), ("a", "c"), ("b", "c")}
    monkeypatch.setattr(connect, "K", 1)
    # b-c is c's nearest, not b's; a-c is neither's; d's nearest is c, below the floor.
    assert set(connect.neighbours(list(degrees), M)) == {("a", "b"), ("b", "c")}


def test_im_connect_limit_zero_needs_no_key_sends_nothing_and_counts_what_is_due(nearby, monkeypatch, capsys):
    from im import cli

    monkeypatch.setattr(cli.db, "pipeline_dsn", lambda: nearby.info.dsn + " password=" + nearby.info.password)
    cli.main(["connect", "--limit", "0"])   # the test has no OpenRouter or Jev key
    out = json.loads(capsys.readouterr().out)
    assert out["nominated"] == out["due"] == every_pair(nearby) > 0 and out["asked"] == 0
    assert table(nearby, "SELECT count(*) FROM im.egress_log WHERE model IN (%s, %s)", TERRA, jev.MODEL) == [(0,)]
    assert verdicts(nearby) == {}
    with pytest.raises(SystemExit, match="can't run"):   # without --limit 0, the keys are needed
        cli.main(["connect", "--limit", "1"])


def test_im_run_asks_at_most_its_limit_a_run_until_nothing_is_due(nearby, cfg, monkeypatch):
    from im import cli

    n = every_pair(nearby)
    limited = (lambda k: replace(cfg, connect=replace(cfg.connect, max_pairs_per_run=k)))
    counted = cli._connect(nearby, limited(0))   # 0: counts, needing no key, and sends nothing
    assert (counted["due"], counted["asked"], counted["stopped"]) == (n, 0, None) and verdicts(nearby) == {}
    assert cli._connect(nearby, cfg)["stopped"].startswith("the connection judge can't run")   # no key
    jev_client, jev_sent = screen()
    terra_client, terra_sent = terra()
    monkeypatch.setattr(llm, "client_for", lambda model, zdr: terra_client)
    monkeypatch.setattr(jev, "Jev", lambda: jev_client)
    for run_ in range(2):   # each run takes the next two
        out = cli._connect(nearby, limited(2))
        assert (out["due"], out["asked"]) == (n - 2 * run_, 2) and len(verdicts(nearby)) == 2 * (run_ + 1)
    rest = cli._connect(nearby, cfg)   # within the default limit
    assert rest["asked"] == n - 4 > 0 and len(verdicts(nearby)) == len(terra_sent) == n
    idle = cli._connect(nearby, cfg)
    assert idle["due"] == idle["asked"] == 0 and len(jev_sent) == len(terra_sent) == n   # nothing due, nothing sent


def test_a_pair_jev_screens_out_never_reaches_terra_and_loses_its_stand_in_for_good(nearby):
    grape = idea(nearby, "grape")
    out, jev_sent, terra_sent = judge(nearby, lambda state: 0.2 if "grape" in json.dumps(state) else 0.9)
    n = every_pair(nearby)
    screened = [p for p in verdicts(nearby) if grape in p]
    assert out["screened"] == len(screened) > 0 and out["judged"] == len(terra_sent) == n - len(screened)
    assert len(jev_sent) == n and not any("grape" in t for t in terra_sent)
    assert table(nearby, "SELECT DISTINCT verdict, belongs FROM im.connection_verdicts WHERE %s IN (a, b)",
                 grape) == [("screened", 0.2)]
    shown = (lambda: {m for p, m in methods(nearby).items() if grape in p})
    assert shown() == {JUDGED}   # the stand-ins are gone; another method's connection stays
    lattice.relate(nearby, HashEmbedder())
    assert shown() == {JUDGED}   # and the embedding neighbors don't come back
    assert out["cost_usd"] == {"jev": pytest.approx(n * 1000 * jev.PRICE_IN),
                               "terra": pytest.approx(0.0025 * out["judged"])}
    assert_invariants(nearby)


def test_a_pair_is_asked_once_until_its_text_changes_or_i_approve_a_new_method(nearby, monkeypatch):
    first, _, _ = judge(nearby)
    assert first["connected"] == {"related": first["nominated"]}
    assert set(methods(nearby).values()) == {connect.method()}   # every stand-in replaced, the judge's too
    again, jev_sent, terra_sent = judge(nearby)
    assert again["already_judged"] == first["nominated"] and again["due"] == 0 and not jev_sent and not terra_sent
    coffee = idea(nearby, "coffee")
    nearby.execute("INSERT INTO pub.feedback_events (kind, idea_id, payload) VALUES ('idea_correct', %s, %s)",
                   (coffee, json.dumps({"statement": "Whoever orders the coffee runs the meeting."})))
    third, jev_sent, terra_sent = judge(nearby)
    involved = sum(coffee in p for p in verdicts(nearby))
    assert third["due"] == third["asked"] == len(jev_sent) == len(terra_sent) == involved
    assert all("Whoever orders the coffee" in t for t in terra_sent)
    monkeypatch.setattr(connect, "SCREEN_CUT", 0.5)   # a new method: every pair held, until I approve it
    held, jev_sent, terra_sent = judge(nearby)
    assert held["due"] == 0 and held["held"]["held"] == first["nominated"] and not jev_sent and not terra_sent
    fourth, jev_sent, terra_sent = judge(nearby, held_only=True)   # Jev's answers are kept
    assert fourth["due"] == len(terra_sent) == first["nominated"] and not jev_sent and "held" not in fourth
    assert set(table(nearby, "SELECT DISTINCT method FROM im.connection_verdicts")) == {(connect.method(),)}


def test_none_unsure_and_a_refusal_remove_the_connection_and_relate_doesnt_restore_it(nearby, monkeypatch):
    judge(nearby)   # Terra connects every pair
    monkeypatch.setattr(connect, "SCREEN_CUT", 0.5)
    out, _, _ = judge(nearby, answer=lambda t: None if "coffee" in t else "unsure" if "grape" in t else "none",
                      held_only=True)
    assert out["refused"] > 0 and out["unsure"] > 0 and out["none"] > 0 and out["connected"] == {}
    assert out["refused"] + out["unsure"] + out["none"] == out["judged"] == every_pair(nearby)
    assert methods(nearby) == {}
    lattice.relate(nearby, HashEmbedder())
    assert methods(nearby) == {}


def test_pairs_i_linked_or_unlinked_are_never_asked(nearby):
    a, b, c = sorted(r[0] for r in table(nearby, "SELECT idea_id FROM pub.cards"))[:3]
    event = "INSERT INTO pub.feedback_events (kind, idea_id, other_idea_id) VALUES (%s, %s, %s)"
    nearby.execute(event, ("link", b, a))
    nearby.execute(event, ("unlink", c, b))
    out, jev_sent, _ = judge(nearby)
    assert out["skipped"] == 2 and out["asked"] == len(jev_sent) == every_pair(nearby) - 2
    assert (a, b) not in verdicts(nearby) and (b, c) not in verdicts(nearby)


def test_kinds_are_written_with_their_direction_and_same_goes_to_my_merge_review(nearby, monkeypatch):
    grape, signal, coffee = idea(nearby, "grape"), idea(nearby, "Signal"), idea(nearby, "coffee")

    def answer(text):
        if "grape" in text and "Signal" in text:
            return "supports", "B>A"
        return "same" if "grape" in text and "coffee" in text else "tension"

    out, _, _ = judge(nearby, answer=answer)
    assert out["connected"] == {"supports": 1, "same": 1, "tension": every_pair(nearby) - 2}
    kinds = {(a, b): (k, d) for a, b, k, d in table(nearby, "SELECT a, b, kind, direction FROM im.connection_kinds")}
    a, b = sorted((grape, signal))
    assert kinds[(a, b)] == ("supports", "b>a")   # B, the higher id, supports A
    same = tuple(sorted((grape, coffee)))
    assert same not in kinds and methods(nearby)[same] == connect.method()   # a connection, with no kind
    assert {k for p, k in kinds.items() if p not in ((a, b), same)} == {("tension", None)}
    review = "SELECT idea_ids, item_id, method FROM im.merge_review WHERE kind = 'judged_same'"
    assert table(nearby, review) == [(list(same), None, connect.method())]
    monkeypatch.setattr(connect, "SCREEN_CUT", 0.5)
    judge(nearby, answer=answer, held_only=True)   # asked again: the kinds are the new method's, and same once
    assert set(table(nearby, "SELECT DISTINCT method FROM im.connection_kinds")) == {(connect.method(),)}
    assert len(table(nearby, review)) == 1
    assert_invariants(nearby)


def test_forgetting_what_a_verdict_carried_deletes_it(nearby, cfg, stream):
    coffee = idea(nearby, "coffee")
    judge(nearby)
    assert any(coffee in p for p in verdicts(nearby))
    stream.forget(13, 16)   # the coffee line, which joined my coffee idea: the idea stays
    stream.write(stream.dir)
    run(nearby, cfg, stream)
    assert table(nearby, "SELECT count(*) FROM im.ideas WHERE idea_id = %s", coffee) == [(1,)]
    assert verdicts(nearby) and not any(coffee in p for p in verdicts(nearby))
    assert_invariants(nearby)
