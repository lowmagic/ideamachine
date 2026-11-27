"""B1: the matcher's logged decisions, replayed through Jev and scored against Opus."""

import json
import re

import pytest

from im import cli, jev, llm, match_bench

from helpers import table
from test_lattice import lat_setup, seeded  # noqa: F401 (fixture)


def fake_jev(sent):
    """The same idea when both sides speak of coffee; a development when a broken build meets the signal idea."""
    def post(body):
        sent.append(body)
        a, b = json.dumps(body["state"]["a"]).lower(), json.dumps(body["state"]["b"]).lower()
        same, develops = "coffee" in a and "coffee" in b, "build" in a and "signal" in b
        p = {"same": 0.9 if same else 0.1, "same_v2": 0.8 if same else 0.2,
             "develops": 0.9 if develops else 0.1, "develops_v2": 0.6 if develops else 0.3}
        return {"model": "jev-1.14.0", "usage": {"input_tokens": 500},
                "answers": {q: {"type": "noul", "noul": p[q]} for q in body["questions"]}}
    return jev.Jev(post=post)


def test_jev_replays_the_matchers_decisions_and_is_scored_against_opus(seeded, cfg, stream, tmp_path):
    lat_setup(seeded, cfg, stream)   # Opus (a fake, matcher v2) decided: the coffee capture makes the coffee idea's
    sent = []                        # point; the build and LoRa captures are new
    out = match_bench.run(seeded, fake_jev(sent), tmp_path)
    assert out == {"decisions": 3, "asked": len(sent), "answered": len(sent)}
    assert all(set(b["questions"]) == set(jev.QUESTIONS["match"]) for b in sent)
    # Each request is logged with the capture's segments and those of the idea it was asked about.
    logged = table(seeded, """SELECT g.segment_ids FROM im.egress_log g WHERE g.model = 'jev-1.14.0'""")
    matcher = {s for (segs,) in table(seeded, """SELECT g.segment_ids FROM im.item_matches m
                                                JOIN im.egress_log g USING (egress_id)""") for s in segs}
    assert len(logged) == len(sent) and all(set(segs) & matcher for (segs,) in logged)

    r = match_bench.score(tmp_path)
    assert r["by_opus"] == {"new": 2, "same_as": 1, "evolves": 0} and r["chosen_unknown"] == 0
    assert (r["best"]["agree"], r["best"]["same_as_precision"], r["best"]["same_as_recall"]) == (1.0, 1.0, 1.0)
    assert r["auc"]["same_as:mean"] == 1.0
    # Jev rates the build capture 0.75 on "develops" against the signal idea: below that cut it's read as a
    # development, which matcher v2 never decides, so Jev and Opus disagree on 1 of 3.
    assert r["agree"]["same 0.5"].split() == ["0.67", "0.67", "0.67", "0.67", "1.00"]

    assert match_bench.run(seeded, fake_jev(sent), tmp_path)["answered"] == out["answered"]
    assert len(sent) == out["asked"]   # a rerun sends nothing it has an answer to


def test_the_matchers_request_is_read_back_as_it_was_shown():
    payload = ("Item (idea, said by me): “the build broke — again” — Builds break.\n\nCandidates:\n"
               "1. Signals: A broken build is a signal (my note: true)\n2. Coffee: The order is\nthe meeting.")
    item, candidates = match_bench.parse(payload)
    assert item == {"kind": "idea", "said_by": "me", "quote": "the build broke — again", "summary": "Builds break."}
    assert candidates == [{"text": "Signals: A broken build is a signal", "note": "true"},
                          {"text": "Coffee: The order is\nthe meeting."}]


def test_matcher_v2s_request_is_read_back_as_it_was_shown():
    payload = ("New capture\ngist: Builds break.\nreading: The build broke again.\nrelayed from: my lead\n"
               "quote (evidence only): the build broke — again\n\nCandidates\n"
               "1. title (mine): Signals\n   statement: A broken build is a signal\n   my note: true\n"
               "   observations:\n   - A gist (relayed from: Bob)\n   quote: a quote\n"
               "2. title: Coffee\n   statement (mine): The order is the meeting.")
    item, candidates = match_bench.parse(payload)
    assert item == {"quote": "the build broke — again", "summary": "Builds break.", "relayed_from": "my lead"}
    assert candidates == [{"text": "Signals: A broken build is a signal", "note": "true"},
                          {"text": "Coffee: The order is the meeting."}]


def test_a_chat_model_replays_matcher_v2_decisions_and_is_scored_against_opus(
        seeded, cfg, stream, tmp_path, monkeypatch, capsys):
    lat_setup(seeded, cfg, stream)
    all_dec = match_bench.v2_decisions(seeded)
    assert len(all_dec) == 3
    picked, meta = match_bench.select_v2(all_dec, limit=2, seed=1)
    assert len(picked) == 2 and meta["selected"] == {"same_as": 1, "new": 1}
    assert meta["available"] == {"same_as": 1, "new": 2}

    sent = []

    def post(body):
        sent.append(body)
        payload = body["messages"][1]["content"]
        assert payload.startswith("New capture\n") and "\nCandidates\n" in payload
        item_part, cands = payload.split("\nCandidates\n")
        shown = re.findall(r"^(\d+)\. title(?: \(mine\))?: (.*)\n\s+statement(?: \(mine\))?: (.*)$", cands, re.M)
        nums = [int(n) for n, _, _ in shown]
        if "coffee" in item_part.lower():
            if sum("coffee" in b["messages"][1]["content"].split("\nCandidates\n")[0].lower() for b in sent) == 1:
                ans = {"judgments": [], "title": "", "statement": ""}   # omits candidates -> retry
            else:
                ans = {"judgments": [{"candidate": int(n), "same_point": "coffee" in f"{t} {s}".lower()}
                                     for n, t, s in shown], "title": "", "statement": ""}
        elif "build broke" in item_part.lower():
            ans = {"judgments": [{"candidate": n, "same_point": False} for n in nums],
                   "title": "Broken builds", "statement": 'I said "build broke"'}   # triggers fallback
        else:
            ans = {"judgments": [{"candidate": n, "same_point": n == nums[0]} for n in nums],
                   "title": "", "statement": ""}   # disagrees with Opus's `new`
        return {"id": "gen-1", "model": "google/gemini-4-neon-20261118",
                "usage": {"prompt_tokens": 300, "completion_tokens": 50, "cost": 0.0005},
                "choices": [{"finish_reason": "stop",
                             "message": {"content": json.dumps(ans), "refusal": None}}]}

    neon = "google/gemini-4-neon"
    folder = tmp_path / "b1-v2"
    out = match_bench.run_model(seeded, llm.OpenRouter(post=post), folder, neon, effort="low")
    assert out["decisions"] == 3 and out["answered"] == 3 and out["asked"] == 3 and out["skipped_forgotten"] == 0
    assert len(sent) == 4   # 3 decisions + 1 retry on coffee
    assert any("observations:" in b["messages"][1]["content"] for b in sent)

    # Resuming asks nothing more.
    again = match_bench.run_model(seeded, llm.OpenRouter(post=post), folder, neon, effort="low")
    assert again["asked"] == 0 and len(sent) == 4

    r = match_bench.score(folder)
    assert r["benchmark"] == "B1-v2" and r["decisions"] == 3
    m = r["models"]["google_gemini-4-neon-low"]
    assert m["answered"] == 3 and m["missing"] == 0 and m["asked_again"] == 1
    assert m["agree_kind"] == pytest.approx(2 / 3, abs=0.002)
    assert m["agree"] == pytest.approx(2 / 3, abs=0.002)
    assert m["same_as_precision"] == 0.5 and m["same_as_recall"] == 1.0
    assert m["served_by"] == {"google/gemini-4-neon-20261118": 3}
    assert m["cost_usd"] == pytest.approx(4 * 0.0005)
    assert m["statement_quality"]["both_new"] == 1 and m["statement_quality"]["statement_fallbacks"] == 1
    assert m["statement_quality"]["fallback_rate"] == 1.0
    assert len(m["disagreements"]) == 1
    md = (folder / "compare-google_gemini-4-neon-low.md").read_text()
    assert "## Disagreements (1)" in md and "## Both new" in md and "[fallback]" in md

    # CLI wiring: run and score work with --model and --limit (stratified: 1 same_as + 1 new).
    cli_dir = tmp_path / "b1-cli"
    monkeypatch.setattr(cli.db, "pipeline_dsn", lambda: seeded.info.dsn + " password=" + seeded.info.password)
    monkeypatch.setattr(llm, "client_for", lambda model, zdr=False: llm.OpenRouter(post=post))
    cli.main(["bench-match", "run", str(cli_dir), "--model", neon, "--limit", "2"])
    run_out = json.loads(capsys.readouterr().out)
    assert run_out["decisions"] == 2 and run_out["answered"] == 2
    cli.main(["bench-match", "score", str(cli_dir)])
    score_out = json.loads(capsys.readouterr().out)
    assert score_out["benchmark"] == "B1-v2" and score_out["decisions"] == 2
    assert score_out["models"]["google_gemini-4-neon-low"]["by_opus"] == {"new": 1, "same_as": 1}

    # Monthly cap stops, and a forgotten payload (egress_log.payload IS NULL) is skipped cleanly on resume.
    part = tmp_path / "b1-v2-part"
    stopped = match_bench.run_model(seeded, llm.OpenRouter(post=post), part, neon, monthly_cap_usd=0)
    assert stopped["stopped"].startswith("monthly cap") and stopped["answered"] == 0
    seeded.execute("UPDATE im.egress_log SET payload = NULL WHERE egress_id = %s", (all_dec[0]["egress_id"],))
    resumed = match_bench.run_model(seeded, llm.OpenRouter(post=post), part, neon)
    assert resumed["skipped_forgotten"] == 1 and resumed["answered"] == 2 and resumed["asked"] == 2
