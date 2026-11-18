"""The re-read gate: a version change never sends again, unasked, what was already done. New work and work whose
input changed go ahead; the rest is held, its old result live, reported with what it would cost, until `im reread`."""

import json
from dataclasses import replace

import anthropic
import pytest

from im import connect, extract, fixtures, jev, lattice, llm, observations, sensitivity
from im.checks import run_checks
from im.config import ModelConfig

from helpers import HashEmbedder, run, table
from test_connect import idea, judge, nearby, screen, terra  # noqa: F401  # nearby: a fixture, used by name
from test_extract import FakeClaude
from test_lattice import FakeMatcher, items_for
from test_merge import EVENT
from test_merge_screen import screened  # noqa: F401  # a fixture, used by name
from test_pipeline import CORRECTIONS
from test_review import as_web_role
from test_sensitivity import tagger

READ = (1000 * 4 + 200 * 20) / 1e6       # one FakeClaude read, at Opus's price
SONNET = '[extract]\nmodel = "claude-sonnet-5-5"\n'


@pytest.fixture
def im(pipe, monkeypatch, capsys, tmp_path):
    """`im ARGS`, as the CLI runs it on the test database with `toml` as its im.toml: what it printed, as JSON."""
    from im import cli

    monkeypatch.setattr(cli.db, "pipeline_dsn", lambda: pipe.info.dsn + " password=" + pipe.info.password)
    path = tmp_path / "im.toml"
    path.write_text("")
    monkeypatch.setenv("IM_CONFIG", str(path))

    def im_(*args, toml=""):
        path.write_text(toml)
        cli.main(list(args))
        return json.loads(capsys.readouterr().out)
    return im_


def requests(conn) -> int:
    return table(conn, "SELECT count(*) FROM im.egress_log")[0][0]


def jev_request(conn) -> float:
    """What one of the fake Jev's requests cost, as logged (they all cost the same)."""
    ((low, high),) = table(conn, """SELECT min(cost_usd)::float, max(cost_usd)::float FROM im.egress_log
                                    WHERE model = %s""", jev.MODEL)
    assert low == high > 0
    return high


def test_a_new_extraction_model_holds_what_was_read_until_i_approve_it(pipe, cfg, stream, im, monkeypatch):
    run(pipe, cfg, stream)
    extract.run_stage(pipe, FakeClaude(items_for))
    sent = requests(pipe)
    out = im("pending", toml=SONNET)
    held = out["held"]["extract"]
    assert out["episodes_to_extract"] == 0 and requests(pipe) == sent   # `im pending` sends nothing
    assert held["held"] == 8 and held["approve"] == "im reread extract"
    assert held["from"] == {f"claude-opus-5-5 {extract.prompt_version('claude-opus-5-5', 'medium')}": 8}
    assert held["to"] == f"claude-sonnet-5-5 {extract.prompt_version('claude-sonnet-5-5', 'medium')}"
    # The logged cost of their last reads, at Sonnet's prices, half Opus's.
    assert held["estimated_usd"] == pytest.approx(8 * READ / 2) and "scaled by the price ratio" in held["estimate"]
    assert im("reread", "extract", "--limit", "0", toml=SONNET) == held and requests(pipe) == sent

    claude = FakeClaude(items_for)
    monkeypatch.setattr(anthropic, "Anthropic", lambda: claude)
    got = im("reread", "extract", "--limit", "3", toml=SONNET)
    assert (got["read"], len(claude.calls), got["held"]["held"]) == (3, 3, 5)
    assert got["held"]["estimated_usd"] == pytest.approx(5 * READ / 2)
    from im import cli

    again = cli._extract(pipe, replace(cfg, extract=replace(cfg.extract, model="claude-sonnet-5-5")))   # `im run`
    assert (again["read"], again["held"]["held"], len(claude.calls)) == (0, 5, 3)
    assert im("reread", "extract", toml=SONNET)["read"] == 5 and len(claude.calls) == 8
    assert "held" not in cli._extract(pipe, replace(cfg, extract=replace(cfg.extract, model="claude-sonnet-5-5")))


def test_an_estimate_is_the_logged_cost_or_the_old_models_when_the_new_ones_price_is_unknown(pipe, cfg, stream, im):
    run(pipe, cfg, stream)
    extract.run_stage(pipe, FakeClaude())
    same_model = im("pending", "--prompt", "v5.2")["held"]["extract"]
    assert same_model["estimated_usd"] == pytest.approx(8 * READ)
    assert same_model["estimate"] == "the logged cost of their last requests"
    unpriced = im("pending", toml='[extract]\nmodel = "openai/gpt-6.1-sol"\n')["held"]["extract"]
    assert unpriced["estimated_usd"] == pytest.approx(8 * READ)
    assert "the old model's cost (no price known for openai/gpt-6.1-sol)" in unpriced["estimate"]


def test_new_and_changed_speech_is_read_under_a_new_prompt_while_the_rest_is_held(pipe, cfg, stream):
    run(pipe, cfg, stream)
    extract.run_stage(pipe, FakeClaude(items_for))
    assert "held" not in extract.run_stage(pipe, FakeClaude())   # no version change: nothing held, nothing sent
    CORRECTIONS["split a turn"][0](stream)   # re-transcribed: one episode's payload changes
    stream.conversations["c-new"] = fixtures.Conversation([fixtures._me(20000, 20008, "A case for the LoRa gateway.")])
    stream.write(stream.dir)
    run(pipe, cfg, stream)
    claude = FakeClaude(items_for)
    stats = extract.run_stage(pipe, claude, system=extract.SYSTEM_V5_2)
    assert (stats["read"], len(claude.calls), stats["held"]["held"]) == (2, 2, 7)
    assert any("A case for the LoRa gateway." in c["messages"][0]["content"] for c in claude.calls)


def test_a_held_episode_rederived_with_the_same_payload_keeps_its_captures(pipe, cfg, stream):
    run(pipe, cfg, stream)
    extract.run_stage(pipe, FakeClaude(items_for))
    lattice.match_stage(pipe, FakeMatcher(), HashEmbedder())
    live = """SELECT count(*) FROM im.items i JOIN im.episodes e USING (episode_id) WHERE e.current"""
    captured = table(pipe, live)
    resegmented = replace(cfg, segment=replace(cfg.segment, min_monologue_s=31.0))
    run(pipe, resegmented, stream)   # every episode re-derived, each sending the same payload
    assert table(pipe, live) == [(0,)]
    claude = FakeClaude(items_for)
    stats = extract.run_stage(pipe, claude, system=extract.SYSTEM_V5_2)
    assert (stats["read"], claude.calls, stats["held"]["held"], stats["held_kept_earlier_read"]) == (0, [], 8, 8)
    assert table(pipe, live) == captured   # the earlier read's captures, on the current episodes
    observations.assign_stage(pipe)
    matcher = FakeMatcher()
    assert lattice.match_stage(pipe, matcher, HashEmbedder())["pending"] == 0 and matcher.calls == []


def test_captures_from_two_prompts_live_side_by_side(seeded_lattice):
    pipe = seeded_lattice
    observations.assign_stage(pipe)
    cards = table(pipe, "SELECT idea_id, title FROM pub.cards ORDER BY 1")
    # Approved for the six newest episodes: the oldest conversation's two keep only their first read.
    stats = extract.run_stage(pipe, FakeClaude(items_for), system=extract.SYSTEM_V5_2, limit=6, held_only=True)
    assert stats["read"] == 6 and stats["held"]["held"] == 2
    assert table(pipe, "SELECT count(DISTINCT prompt_version) FROM im.items WHERE episode_id IS NOT NULL") == [(2,)]
    observations.assign_stage(pipe)
    matcher = FakeMatcher()
    lattice.match_stage(pipe, matcher, HashEmbedder())
    assert matcher.calls == [] and table(pipe, "SELECT idea_id, title FROM pub.cards ORDER BY 1") == cards
    assert table(pipe, "SELECT count(*) FROM im.items i WHERE NOT EXISTS (SELECT 1 FROM im.item_matches m "
                       "WHERE m.item_id = i.item_id)") == [(0,)]
    assert not any(run_checks(pipe).values())   # `im check`
    assert len(extract.review_items(pipe)) == 3   # each observation once


def test_a_new_judge_method_holds_judged_pairs_and_asks_about_changed_ones(nearby, im, monkeypatch):  # noqa: F811
    first, _, _ = judge(nearby)
    n = first["nominated"]
    monkeypatch.setattr(connect, "SCREEN_CUT", 0.5)
    coffee = idea(nearby, "coffee")
    nearby.execute("INSERT INTO pub.feedback_events (kind, idea_id, payload) VALUES ('idea_correct', %s, %s)",
                   (coffee, json.dumps({"statement": "Whoever orders the coffee runs the meeting."})))
    changed = sum(coffee in p for p in table(nearby, "SELECT a, b FROM im.connection_verdicts"))
    out, _, terra_sent = judge(nearby)
    assert out["asked"] == len(terra_sent) == changed and out["held"]["held"] == n - changed
    sent = requests(nearby)
    held = im("pending")["held"]["connect"]
    assert held["held"] == n - changed and held["approve"] == "im reread connect" and requests(nearby) == sent
    assert held["estimated_usd"] == pytest.approx((n - changed) * 0.0025)   # Terra's; Jev's answers are kept
    old = table(nearby, "SELECT count(*) FROM im.connection_verdicts WHERE method <> %s", connect.method())
    assert old == [(n - changed,)]   # their verdicts stand

    jev_client, jev_sent = screen()
    terra_client, terra_sent = terra()
    monkeypatch.setattr(llm, "client_for", lambda model, zdr: terra_client)
    monkeypatch.setattr(jev, "Jev", lambda: jev_client)
    got = im("reread", "connect", "--limit", "2")
    assert (got["asked"], len(terra_sent), jev_sent, got["held"]["held"]) == (2, 2, [], n - changed - 2)


def test_a_new_sensitivity_cut_holds_the_tags_until_i_approve_it(seeded_lattice, admin, im, monkeypatch):
    pipe = seeded_lattice
    client, sent = tagger()
    n = sensitivity.tag_stage(pipe, client)["tagged"]
    monkeypatch.setattr(sensitivity, "CUT", 0.4)
    coffee = table(pipe, "SELECT idea_id FROM pub.ideas WHERE statement LIKE '%%coffee%%'")[0][0]
    as_web_role(admin, "INSERT INTO pub.feedback_events (kind, idea_id, payload) VALUES ('idea_correct', %s, %s)",
                coffee, json.dumps({"statement": "The tea order is the real meeting."}))
    stats = sensitivity.tag_stage(pipe, client)   # the changed idea alone
    assert (stats["tagged"], len(sent), stats["held"]["held"]) == (1, n + 1, n - 1)
    held = im("pending")["held"]["sensitivity"]
    assert held["estimated_usd"] == pytest.approx((n - 1) * jev_request(pipe)) and len(sent) == n + 1
    old = """SELECT (SELECT count(*) FROM im.idea_sensitivity WHERE tagger_version <> %s)
                  + (SELECT count(*) FROM im.lens_sensitivity WHERE tagger_version <> %s)"""
    tv = sensitivity.tagger_version(client)
    assert table(pipe, old, tv, tv) == [(n - 1,)]   # their tags stand
    monkeypatch.setattr(jev, "Jev", lambda: client)
    got = im("reread", "sensitivity", "--limit", "2")
    assert (got["tagged"], len(sent), got["held"]["held"]) == (2, n + 3, n - 3)


def test_a_new_merge_question_holds_answered_pairs_with_their_answers(screened, seeded_lattice, im,  # noqa: F811
                                                                       monkeypatch):
    merges, sent = screened
    merges()
    assert len(sent) == 10
    monkeypatch.setitem(jev.QUESTIONS, "match", {name: (f"{q} Read both closely.", c)
                                                 for name, (q, c) in jev.QUESTIONS["match"].items()})
    grape = idea(seeded_lattice, "grape")
    seeded_lattice.execute(EVENT, ("idea_correct", grape, None, json.dumps({"statement": "One grape, let down."})))
    out = merges()   # the grape's four pairs changed: asked; the other six held, ordered by their answers
    assert len(sent) == 14 and "Held by the re-read gate: 6 answered under an earlier question set" in out
    assert "im reread merges" in out and "(an earlier question set)" in out
    held = im("pending")["held"]["merges"]
    assert held["held"] == 6 and held["estimated_usd"] == pytest.approx(6 * jev_request(seeded_lattice))
    assert len(sent) == 14
    got = im("reread", "merges", "--limit", "2")
    assert (got["asked"], len(sent), got["held"]["held"]) == (2, 16, 4)

