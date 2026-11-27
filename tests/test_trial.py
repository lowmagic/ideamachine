"""Candidate extraction prompts and models: the live prompt's version never changes, and a trial reads a fixed sample
without touching the lattice, scored against a reference model."""

import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

from im import cli, config, egress, extract, jev, llm, trial

from helpers import run, system_of, table
from test_carry import DATABASE, capture
from test_extract import FakeClaude
from test_jev import fake
from test_lattice import items_for, seeded, seeded_lattice  # noqa: F401 (fixtures)


class OffSchema(FakeClaude):
    """Answers in a shape the schema doesn't allow, at $4 a request (a million tokens in, on Opus 5.5)."""

    def create(self, **kw):
        self.calls.append(kw)
        return SimpleNamespace(content=[SimpleNamespace(type="text", text='{"captures": []}')], stop_reason="end_turn",
                               model="claude-opus-5-5", _request_id="r",
                               usage=SimpleNamespace(input_tokens=1_000_000, output_tokens=0))


def test_an_answer_that_doesnt_keep_to_the_schema_is_paid_for_and_counts_toward_the_cap(pipe, cfg, stream):
    run(pipe, cfg, stream)
    claude = OffSchema()
    stats = trial.run(pipe, claude, extract.SYSTEM_V5_3, monthly_cap_usd=5)
    assert len(claude.calls) == stats["failed"] == 2 and stats["stopped"].startswith("monthly cap")
    assert egress.spent_this_month(pipe) == 8.0


def test_the_live_prompt_keeps_its_version():
    # Every extraction since the rebuild is filed under this; a different one would hold every episode for a re-read.
    assert extract.prompt_version("claude-opus-5-5", "medium") == "2fee34df8c9f5502" == \
        extract.prompt_version("claude-opus-5-5", "medium", extract.PROMPTS["v5.3"], extract.SCHEMA_V5)


V5_4_PARAGRAPH = (
    "A remark tied to the immediate occasion doesn't count unless I express a point beyond that occurrence. Don't "
    "turn banter, a passing reaction, a fact mentioned in passing, or a story of what just happened into a lasting "
    "claim. Being about a particular person, place or tool doesn't disqualify a thought: preserve an expressed "
    "characterization, preference, pattern, rule or lesson. Judge what I actually express, not what could be inferred "
    "from it.")


def test_v5_4_is_v5_3_and_one_frozen_paragraph_after_the_test_for_a_capture():
    before = extract.SYSTEM_V5_3.split("\n\n")
    after = extract.SYSTEM_V5_4.split("\n\n")
    at = next(n for n, p in enumerate(before) if p.startswith("The test for a capture: does its meaning reach"))
    assert after == before[:at + 1] + [V5_4_PARAGRAPH] + before[at + 1:]
    assert "v5.4" in extract.PROMPTS and "v5.4" not in extract.LIVE


def test_a_sample_leaves_out_others_only_episodes_and_held_out_ones_and_can_be_reordered(pipe, cfg, stream, tmp_path):
    run(pipe, cfg, stream)
    pool = [e for (e,) in table(pipe, "SELECT episode_id FROM im.episodes WHERE current AND kind <> 'others_only'")]
    assert sorted(trial.sample(pipe, 1000), key=str) == sorted(pool, key=str)
    assert sorted(trial.sample(pipe, 1000, seed=7), key=str) == sorted(pool, key=str)
    assert trial.sample(pipe, 2, seed=7) == trial.sample(pipe, 5, seed=7)[:2]
    first = trial.sample(pipe, 1)[0]
    (tmp_path / "eligibility").mkdir()
    (tmp_path / "eligibility" / "key.json").write_text(json.dumps({"items": {"1": {"episode_id": str(first)}}}))
    held, n = trial.held_out(pipe, [tmp_path / "eligibility"])
    assert held == {str(first)} and n == 1
    assert first not in trial.sample(pipe, 1000, exclude=held) and len(trial.sample(pipe, 1000, exclude=held)) == \
        len(pool) - 1
    (tmp_path / "both-ways").mkdir()   # a misses or both-ways sheet names its episodes under `pairs`
    (tmp_path / "both-ways" / "key.json").write_text(json.dumps({"pairs": {"1": {"episode": str(first)}}}))
    assert trial.held_out(pipe, [tmp_path / "both-ways"])[0] == held


def test_a_leftover_interests_key_is_ignored_with_a_warning(tmp_path, monkeypatch, capsys):
    path = tmp_path / "im.toml"
    path.write_text('[extract]\nprompt = "v5.3"\ninterests = "model railways"\n')
    monkeypatch.setenv("IM_CONFIG", str(path))
    assert config.load().extract == config.ExtractConfig()
    assert f"[extract] interests: retired with prompts v1 to v4 (decision 0012); delete it from {path}" in \
        capsys.readouterr().err


def both(*needles):
    return lambda text: [i for n in needles for i in capture(n)(text)]


def test_a_trial_scores_a_candidate_against_a_reference_on_the_same_sample(pipe, cfg, stream):
    run(pipe, cfg, stream)
    items_before = table(pipe, "SELECT count(*) FROM im.items")
    episodes = table(pipe, "SELECT count(*) FROM im.episodes WHERE current AND kind <> 'others_only'")[0][0]
    assert trial.sample(pipe, 2) == trial.sample(pipe, 5)[:2]   # a smaller sample is a prefix of a larger one

    v5 = extract.SYSTEM_V5_3
    opus = FakeClaude(both(DATABASE, "coffee"))
    assert trial.run(pipe, opus, v5)["read"] == episodes and all(system_of(c) == v5 for c in opus.calls)
    assert "fallbacks" in opus.calls[0]
    sonnet = FakeClaude(both(DATABASE, "lockfile"))   # finds one of Opus's two, and one of its own
    assert trial.run(pipe, sonnet, v5, model="claude-sonnet-5-5")["read"] == episodes
    assert {c["model"] for c in sonnet.calls} == {"claude-sonnet-5-5"} and not any("fallbacks" in c for c in sonnet.calls)
    assert table(pipe, "SELECT count(*) FROM im.items") == items_before   # nothing reaches the lattice

    pv = lambda model: extract.prompt_version(model, "medium", v5)  # noqa: E731
    r = trial.report(pipe, (pv("claude-sonnet-5-5"), "claude-sonnet-5-5"), (pv("claude-opus-5-5"), "claude-opus-5-5"))
    assert (r["episodes"], r["reference_captures"], r["found"], r["candidate_captures"], r["extra"]) == \
        (episodes, 2, 1, 2, 1)
    assert r["recall"] == 0.5
    assert r["missed"] == ["Who's got the coffee order?"]
    assert r["extra_examples"] == ["We should pin the cache key to the lockfile hash."]
    # The lockfile line is in the coffee's episode: the candidate missed the coffee there, but not the episode.
    assert r["episodes_reference_captured_in"] == 2 and r["of_those_candidate_captured_nothing"] == 0
    assert r["cost_vs_reference"] == 1.0   # the fake answers as Opus, at Opus's price, for both

    assert trial.run(pipe, sonnet, v5, model="claude-sonnet-5-5")["read"] == 0   # read once per payload
    again = FakeClaude(both(DATABASE))   # a replicate reads everything again, filed apart, scored against the first
    assert trial.run(pipe, again, v5, replicate=2)["read"] == episodes
    r2 = trial.report(pipe, (trial.version("claude-opus-5-5", "medium", v5, 2), "claude-opus-5-5"),
                      (pv("claude-opus-5-5"), "claude-opus-5-5"))
    assert r2["recall"] == 0.5 and trial.version("claude-opus-5-5", "medium", v5, 2) == pv("claude-opus-5-5") + "~2"
    assert trial.run(pipe, opus, extract.SYSTEM_V5_2, monthly_cap_usd=0)["stopped"]


def test_a_thought_quoted_from_the_next_line_counts_as_found_once_jev_says_its_the_same_point(pipe, cfg, stream):
    run(pipe, cfg, stream)
    v5 = extract.SYSTEM_V5_3
    trial.run(pipe, FakeClaude(both(DATABASE, "coffee")), v5)
    trial.run(pipe, FakeClaude(both(DATABASE, "lockfile")), v5, model="claude-sonnet-5-5")   # the next line
    pv = lambda model: extract.prompt_version(model, "medium", v5)  # noqa: E731
    cand, ref = (pv("claude-sonnet-5-5"), "claude-sonnet-5-5"), (pv("claude-opus-5-5"), "claude-opus-5-5")
    by_quote = trial.report(pipe, cand, ref)

    def counted(nouls):
        judge, sent = fake(nouls)
        stats = trial.same_point(pipe, judge, cand, ref, 20.0)
        return stats, sent, trial.report(pipe, cand, ref, jev.kept(pipe, jev.question_set(judge, "match")))

    stats, sent, unsure = counted({"same": 0.7, "same_v2": 0.6})   # below the cut
    assert stats["pairs"] == stats["asked"] == len(sent) == 1 and unsure["found_by_same_point"] == 0
    assert "coffee" in json.dumps(sent[0]["state"]) and "lockfile" in json.dumps(sent[0]["state"])

    pipe.execute("DELETE FROM im.jev_answers")
    stats, sent, r = counted({"same": 0.9, "same_v2": 0.8})
    assert r["found_by_same_point"] == 1 and r["found"] == by_quote["found"] + 1
    assert r["extra"] == by_quote["extra"] - 1
    assert trial.same_point(pipe, fake({})[0], cand, ref, 20.0)["asked"] == 0   # kept, not asked again


def test_a_trial_sends_names_said_as_said_and_logs_each_request(pipe, cfg, stream):
    turns = stream.conversations["c-conv"].turns
    turns[0] = replace(turns[0], text="Did you see the build broke again, Bob?")
    stream.write(stream.dir)
    run(pipe, cfg, stream)
    v5 = extract.SYSTEM_V5_3

    claude = FakeClaude(both("build broke"))
    trial.run(pipe, claude, v5)
    (sent,) = [c["messages"][0]["content"] for c in claude.calls if "build broke" in c["messages"][0]["content"]]
    assert "broke again, Bob?" in sent   # the name said, as said
    assert "S1:" in sent and "Alice" not in sent   # speakers are pseudonymized

    (items,) = [i for (i,) in table(pipe, "SELECT items FROM im.prompt_trials WHERE privacy_policy_version = %s",
                                    egress.POLICY_VERSION) if i]
    assert items[0]["quote"] == "Did you see the build broke again, Bob?"
    # One row per request: exactly what was sent, and the episode's segments, line by line.
    logged = table(pipe, """SELECT g.payload, g.segment_ids, (SELECT array_agg(segment_id ORDER BY ord)
                                                              FROM im.episode_segments WHERE episode_id = g.episode_id)
                            FROM im.egress_log g WHERE privacy_policy_version = %s ORDER BY egress_id""",
                   egress.POLICY_VERSION)
    assert [p for p, _, _ in logged] == [c["messages"][0]["content"] for c in claude.calls]
    assert all(sent_ids == lines for _, sent_ids, lines in logged)
    assert trial.run(pipe, claude, v5)["read"] == 0   # read once per payload


def test_a_trial_keeps_the_quote_as_said_apart_from_its_reading_and_attribution(pipe, cfg, stream):
    turns = stream.conversations["c-conv"].turns
    turns[0] = replace(turns[0], text="Did you see the build broke again, Bob?")
    stream.write(stream.dir)
    run(pipe, cfg, stream)
    v5 = extract.SYSTEM_V5_2

    def respond(text):   # the line as said, and the same line tidied: only the first is word for word
        out = []
        for line in text.splitlines():
            if line.startswith("[") and "build broke" in line:
                n, said = int(line[1:].split("]")[0]), line.split(": ", 1)[1]
                common = {"lines": [n], "reading": "Bob, the build broke again.", "mishearings": [],
                          "gist": "The build keeps breaking."}
                out += [common | {"quote": said, "attributed_to": ""},
                        common | {"quote": "Did you see that the build broke", "attributed_to": "Bob's lead"}]
        return out

    claude = FakeClaude(respond)
    trial.run(pipe, claude, v5)
    assert all(c["output_config"]["format"]["schema"] == extract.SCHEMA_V5 for c in claude.calls)
    (items,) = [i for (i,) in table(pipe, "SELECT items FROM im.prompt_trials") if i]
    assert [(it["reading"], it["attributed_to"]) for it in items] == \
        [("Bob, the build broke again.", ""), ("Bob, the build broke again.", "Bob's lead")]
    opus = (trial.version("claude-opus-5-5", "medium", v5), "claude-opus-5-5")
    assert trial.report(pipe, opus, opus)["verbatim_quotes"] == {"candidate": 0.5, "reference": 0.5}

    # v5.2 is for trials only, so the live run doesn't read under it.
    sent = table(pipe, "SELECT count(*) FROM im.egress_log")
    stats = cli._extract(pipe, replace(cfg, extract=replace(cfg.extract, prompt="v5.2")))
    assert stats["stopped"] == "extract.prompt v5.2 isn't live; `im run` reads with v5.3"
    assert table(pipe, "SELECT count(*) FROM im.egress_log") == sent


def test_v5_records_which_capture_checks_each_capture_fails(pipe, cfg, stream):
    turns = stream.conversations["c-conv"].turns
    turns[0] = replace(turns[0], text="Did you see the build broke again, Bob?")
    stream.write(stream.dir)
    run(pipe, cfg, stream)
    v5, schema = extract.SYSTEM_V5, extract.SCHEMA_V5

    def respond(text):
        rows = {int(r[1:].split("]")[0]): r.split(": ", 1) for r in text.splitlines() if r.startswith("[")}
        mine = next(n for n, (who, said) in rows.items() if who.endswith("me") and "build broke" in said)
        theirs = next(n for n, (who, _) in rows.items() if not who.endswith("me"))
        said, other = rows[mine][1], rows[theirs][1]
        base = {"lines": [mine], "attributed_to": "", "reading": "Bob, the build broke again.", "mishearings": [],
                "gist": "The build broke again."}
        return [base | {"quote": said, "attributed_to": "Bob",
                        "mishearings": [{"heard": "build", "meant": "build"}]},       # passes
                base | {"quote": said.lower()},                                           # tidied
                base | {"quote": other, "lines": [mine, theirs]},                       # someone else's words
                base | {"quote": said, "mishearings": [{"heard": "cash", "meant": "cache"}]}]

    claude = FakeClaude(respond)
    trial.run(pipe, claude, v5, schema=schema)
    assert all(c["output_config"]["format"]["schema"] == extract.SCHEMA_V5 for c in claude.calls)
    (items,) = [i for (i,) in table(pipe, "SELECT items FROM im.prompt_trials") if i]
    assert [it["checks"] for it in items] == [[], ["quote_inexact"], ["quote_not_mine"],
                                              ["mishearing_not_in_quote", "mishearing_not_in_reading"]]
    assert items[0]["attributed_to"] == "Bob" and items[0]["said_by"] == "me" and "confidence" not in items[0]
    opus = (trial.version("claude-opus-5-5", "medium", v5, schema=schema), "claude-opus-5-5")
    assert trial.report(pipe, opus, opus)["checks"]["candidate"]["pass"] == 0.25


def test_recall_is_split_by_how_far_each_captures_idea_reaches(seeded_lattice):
    conn = seeded_lattice
    v5 = extract.SYSTEM_V5_3
    trial.run(conn, FakeClaude(items_for), v5)                          # the reference: what live captured
    trial.run(conn, FakeClaude(), v5, model="claude-haiku-5-5")          # a candidate that captures nothing
    opus, haiku = ((extract.prompt_version(m, "medium", v5), m) for m in ("claude-opus-5-5", "claude-haiku-5-5"))
    itself = trial.report(conn, opus, opus)
    reach = itself["recall_by_reach"]
    assert sum(r["captures"] for r in reach.values()) == itself["reference_captures"] == 3
    assert reach["unplaced"]["captures"] == 0   # live matched every one into a cluster
    assert all(r["recall"] in (1.0, None) for r in reach.values())
    assert all(r["recall"] in (0.0, None) for r in trial.report(conn, haiku, opus)
               ["recall_by_reach"].values())
    # The LoRa idea is new, said once: a one-off.
    lora = [it for (items,) in table(conn, "SELECT items FROM im.prompt_trials") for it in items if "LoRa" in it["quote"]]
    assert trial.by_reach(conn, [(e, lora[0], True) for (e,) in table(
        conn, "SELECT episode_id FROM im.prompt_trials WHERE items::text LIKE '%%LoRa%%' LIMIT 1")])["one_off"] == \
        {"captures": 1, "recall": 1.0}


def test_a_trial_can_be_scored_against_the_live_read_including_same_point_and_cost(
        pipe, cfg, stream, monkeypatch, capsys):
    run(pipe, cfg, stream)
    v5 = extract.SYSTEM_V5_3
    extract.run_stage(pipe, FakeClaude(both(DATABASE, "coffee")))
    episodes = table(pipe, "SELECT count(*) FROM im.episodes WHERE current AND kind <> 'others_only'")[0][0]
    assert table(pipe, "SELECT count(*) FROM im.prompt_trials WHERE model = 'claude-opus-5-5'") == [(0,)]

    def post(body):
        text = body["messages"][1]["content"]
        return {"id": "gen-1", "model": "google/gemini-4-neon-20261118",
                "usage": {"prompt_tokens": 800, "completion_tokens": 120, "cost": 0.002},
                "choices": [{"finish_reason": "stop",
                             "message": {"content": json.dumps({"items": both(DATABASE, "lockfile")(text)}),
                                         "refusal": None}}]}

    neon = "google/gemini-4-neon"
    stats = trial.run(pipe, llm.OpenRouter(post=post), v5, model=neon)
    assert stats["read"] == episodes
    cand = (trial.version(neon, "medium", v5), neon)
    ref = (extract.prompt_version("claude-opus-5-5", "medium"), "claude-opus-5-5")

    r = trial.report(pipe, cand, ref, live_reference=True)
    assert (r["episodes"], r["reference_captures"], r["found"], r["candidate_captures"], r["extra"]) == \
        (episodes, 2, 1, 2, 1)
    assert r["recall"] == 0.5
    assert r["cost_usd"] == pytest.approx(episodes * 0.002) and r["reference_cost_usd"] > 0
    assert r["cost_vs_reference"] == round(r["cost_usd"] / r["reference_cost_usd"], 3)
    assert r["checks"]["candidate"] == {"captures": 2, "pass": 0.5,
                                        "failing": {"cites_none_of_mine": 1, "quote_not_mine": 1}}
    assert r["checks"]["reference"] == {"captures": 2, "pass": 0.0,
                                        "failing": {"cites_none_of_mine": 2, "quote_not_mine": 2}}
    assert r["verbatim_quotes"] == {"candidate": 1.0, "reference": 1.0}
    assert r["quotes_not_in_lines"] == {"candidate": 0.0, "reference": 0.0}

    judge, _ = fake({"same": 0.9, "same_v2": 0.8})
    sp = trial.same_point(pipe, judge, cand, ref, 20.0, live_reference=True)
    assert sp["pairs"] == 1 and sp["asked"] == 1
    r_sp = trial.report(pipe, cand, ref, jev.kept(pipe, jev.question_set(judge, "match")), live_reference=True)
    assert r_sp["found_by_same_point"] == 1 and r_sp["found"] == 2 and r_sp["recall"] == 1.0

    monkeypatch.setattr(cli.db, "pipeline_dsn", lambda: pipe.info.dsn + " password=" + pipe.info.password)
    cli.main(["trial", "v5.3", "--model", neon, "--live", "--report"])
    out = json.loads(capsys.readouterr().out)
    assert out["report"]["recall"] == 0.5 and out["report"]["reference_captures"] == 2


def test_a_two_pass_trial_skips_empty_episodes_and_includes_review_cost(pipe, cfg, stream, monkeypatch, capsys):
    run(pipe, cfg, stream)
    v5 = extract.SYSTEM_V5_3
    extract.run_stage(pipe, FakeClaude(both(DATABASE, "coffee")))
    episodes = table(pipe, "SELECT count(*) FROM im.episodes WHERE current AND kind <> 'others_only'")[0][0]
    p1_calls, p2_calls = [], []

    def p1_post(body):
        p1_calls.append(body)
        text = body["messages"][1]["content"]
        items = both(DATABASE, "lockfile")(text)
        if "lockfile" in text:
            items += both("Did you see")(text)
        return {"id": f"p1-{len(p1_calls)}", "model": "openai/gpt-6.1-terra",
                "usage": {"prompt_tokens": 700, "completion_tokens": 100, "cost": 0.003},
                "choices": [{"finish_reason": "stop", "message": {"content": json.dumps({"items": items}),
                                                                  "refusal": None}}]}

    def p2_post(body):
        p2_calls.append(body)
        content = body["messages"][1]["content"]
        if "lockfile" in content:
            ans = {"kept": [{"ids": [1], "gist": "Pin the cache key to the lockfile hash."}],
                   "dropped": [{"id": 2, "reason": "small talk"}]}
        else:
            ans = {"kept": [{"ids": [1], "gist": "Move off the managed database."}], "dropped": []}
        return {"id": f"p2-{len(p2_calls)}", "model": "thinkingmachines/inkling-2-small",
                "usage": {"prompt_tokens": 800, "completion_tokens": 60, "cost": 0.001},
                "choices": [{"finish_reason": "stop", "message": {"content": json.dumps(ans), "refusal": None}}]}

    terra, inkling = "openai/gpt-6.1-terra", "thinkingmachines/inkling-2-small"
    stats = trial.run(pipe, llm.OpenRouter(post=p1_post), v5, model=terra,
                      review_client=llm.OpenRouter(post=p2_post), review_model=inkling, review_effort="medium")
    assert (stats["read"], stats["reviewed"], stats["review_drops"]) == (episodes, 2, 1)
    assert len(p1_calls) == episodes and len(p2_calls) == 2
    assert stats["cost_usd"] == pytest.approx(round(episodes * 0.003 + 2 * 0.001, 4))

    cand = (trial.version(terra, "medium", v5, review_model=inkling, review_effort="medium"), terra)
    ref = (extract.prompt_version("claude-opus-5-5", "medium"), "claude-opus-5-5")
    r = trial.report(pipe, cand, ref, live_reference=True)
    assert r["cost_usd"] == pytest.approx(round(episodes * 0.003 + 2 * 0.001, 4))

    monkeypatch.setattr(cli.db, "pipeline_dsn", lambda: pipe.info.dsn + " password=" + pipe.info.password)
    cli.main(["trial", "v5.3", "--model", terra, "--review-model", inkling, "--live", "--report"])
    cli_out = json.loads(capsys.readouterr().out)
    assert cli_out["report"]["cost_usd"] == r["cost_usd"] and cli_out["report"]["recall"] == r["recall"]

    # --review-prompt v2 hashes to a distinct version and sends REVIEW_SYSTEM_V2 to the Pass-2 reviewer
    p2_calls.clear()
    v2 = extract.REVIEW_PROMPTS["v2"]
    assert trial.version(terra, "medium", v5, review_model=inkling, review_effort="medium", review_system=v2) != cand[0]
    s2 = trial.run(pipe, llm.OpenRouter(post=p1_post), v5, model=terra,
                   review_client=llm.OpenRouter(post=p2_post), review_model=inkling, review_effort="medium",
                   review_system=v2)
    assert s2["reviewed"] == 2 and all(c["messages"][0]["content"] == v2 for c in p2_calls)
    cli.main(["trial", "v5.3", "--model", terra, "--review-model", inkling, "--review-prompt", "v2",
              "--live", "--report"])
    cli_v2 = json.loads(capsys.readouterr().out)
    assert cli_v2["report"]["episodes"] == r["episodes"]


def test_review_live_reviews_existing_captures_without_pass1(pipe, cfg, stream, monkeypatch, capsys):
    """--review-live feeds existing live im.items straight into Pass-2 review, reports drops/merges, leaves im.items
    untouched, and doesn't double-count the review's cost in trial.report."""
    run(pipe, cfg, stream)
    v5 = extract.SYSTEM_V5_3
    # Live Opus read: 2 captures on the lockfile episode (one real, one small talk), 1 on the database episode.
    extract.run_stage(pipe, FakeClaude(both(DATABASE, "lockfile", "Did you see")), system=v5)
    live_count = pipe.execute("SELECT count(*) FROM im.items").fetchone()[0]
    assert live_count == 3

    calls = []

    def p2_post(body):
        calls.append(body)
        content = body["messages"][1]["content"]
        if "lockfile" in content:
            ans = {"kept": [{"ids": [1], "gist": "Pin the cache key to the lockfile hash."}],
                   "dropped": [{"id": 2, "reason": "small talk"}]}
        else:
            ans = {"kept": [{"ids": [1], "gist": "Move off the managed database."}], "dropped": []}
        return {"id": f"rl-{len(calls)}", "model": "thinkingmachines/inkling-2-small",
                "usage": {"prompt_tokens": 800, "completion_tokens": 60, "cost": 0.0007},
                "choices": [{"finish_reason": "stop", "message": {"content": json.dumps(ans), "refusal": None}}]}

    opus, inkling = "claude-opus-5-5", "thinkingmachines/inkling-2-small"
    v2 = extract.REVIEW_PROMPTS["v2"]
    stats = trial.review_live(pipe, llm.OpenRouter(post=p2_post), v5, opus, "medium", inkling, "medium", v2)
    assert (stats["reviewed"], stats["items_before"], stats["items_after"], stats["review_drops"]) == (2, 3, 2, 1)
    assert stats["cost_usd"] == pytest.approx(0.0014)
    assert len(stats["changes"]) == 1 and stats["changes"][0]["dropped"][0]["reason"] == "small talk"
    assert all(c["messages"][0]["content"] == v2 for c in calls)
    assert pipe.execute("SELECT count(*) FROM im.items").fetchone()[0] == live_count   # live table untouched

    # Re-running skips already-reviewed episodes; report doesn't double-count the review cost.
    again = trial.review_live(pipe, llm.OpenRouter(post=p2_post), v5, opus, "medium", inkling, "medium", v2)
    assert again["pending"] == 0 and again["reviewed"] == 0
    monkeypatch.setattr(cli.db, "pipeline_dsn", lambda: pipe.info.dsn + " password=" + pipe.info.password)
    cli.main(["trial", "v5.3", "--review-live", "--model", opus, "--review-model", inkling,
              "--review-prompt", "v2", "--report"])
    out = json.loads(capsys.readouterr().out)
    assert out["report"]["episodes"] == 2 and out["report"]["cost_usd"] == pytest.approx(0.0014)
    assert out["report"]["reference_captures"] == 3 and out["report"]["candidate_captures"] == 2


def test_apply_review_updates_live_items_and_protects_curated_cards(pipe, cfg, stream, monkeypatch, capsys):
    """--apply deletes dropped items, updates kept/merged gists in place (preserving item_ids and matches), protects
    cards I kept/starred/edited, updates im.extractions, is idempotent, and keeps every invariant clean."""
    from im import checks

    run(pipe, cfg, stream)
    v5 = extract.SYSTEM_V5_3
    extract.run_stage(pipe, FakeClaude(both(DATABASE, "lockfile", "Did you see")), system=v5)
    # Build live ideas/cards for all 3 items so we can verify card disappearance + feedback protection.
    items = pipe.execute("SELECT item_id, quote, gist FROM im.items ORDER BY quote").fetchall()
    idea_ids = {}
    for item_id, quote, gist in items:
        idea_id = pipe.execute(
            """INSERT INTO im.ideas (title, statement, origin, segment_ids)
               SELECT left(gist, 40), gist, 'captured', source_segment_ids FROM im.items WHERE item_id = %s
               RETURNING idea_id""", (item_id,)).fetchone()[0]
        pipe.execute("INSERT INTO im.idea_evidence (idea_id, item_id, relation, judged_by) VALUES (%s, %s, 'origin', 't')",
                     (idea_id, item_id))
        pipe.execute("INSERT INTO im.item_matches (item_id, decision, idea_id, judged_by) VALUES (%s, 'new', %s, 't')",
                     (item_id, idea_id))
        idea_ids[quote] = (item_id, idea_id)
    assert pipe.execute("SELECT count(*) FROM pub.cards").fetchone()[0] == 3

    # Star the database card so even if Inkling tries to drop it, --apply protects it.
    db_item_id, db_idea_id = next(v for q, v in idea_ids.items() if DATABASE in q)
    pipe.execute("INSERT INTO pub.feedback_events (kind, idea_id, payload) VALUES ('star', %s, '{}')", (db_idea_id,))

    def p2_post(body):
        content = body["messages"][1]["content"]
        if "lockfile" in content:
            ans = {"kept": [{"ids": [1], "gist": "Updated: pin cache key to lockfile hash."}],
                   "dropped": [{"id": 2, "reason": "small talk"}]}
        else:
            ans = {"kept": [], "dropped": [{"id": 1, "reason": "test drop of starred card"}]}
        return {"id": "rl", "model": "thinkingmachines/inkling-2-small",
                "usage": {"prompt_tokens": 800, "completion_tokens": 60, "cost": 0.0007},
                "choices": [{"finish_reason": "stop", "message": {"content": json.dumps(ans), "refusal": None}}]}

    opus, inkling = "claude-opus-5-5", "thinkingmachines/inkling-2-small"
    v2 = extract.REVIEW_PROMPTS["v2"]
    trial.review_live(pipe, llm.OpenRouter(post=p2_post), v5, opus, "medium", inkling, "medium", v2)

    monkeypatch.setattr(cli.db, "pipeline_dsn", lambda: pipe.info.dsn + " password=" + pipe.info.password)
    # Dry-run previews counts without mutating.
    cli.main(["trial", "v5.3", "--apply", "--dry-run", "--model", opus, "--review-model", inkling,
              "--review-prompt", "v2"])
    dry = json.loads(capsys.readouterr().out)
    assert (dry["episodes"], dry["items_before"], dry["items_after"], dry["items_deleted"],
            dry["gists_updated"], dry["protected_by_feedback"]) == (2, 3, 2, 1, 1, 1)
    assert pipe.execute("SELECT count(*) FROM im.items").fetchone()[0] == 3

    # Real apply deletes the small-talk item, updates the kept gist in place, and protects the starred card.
    cli.main(["trial", "v5.3", "--apply", "--model", opus, "--review-model", inkling, "--review-prompt", "v2"])
    applied = json.loads(capsys.readouterr().out)
    assert (applied["items_deleted"], applied["gists_updated"], applied["protected_by_feedback"],
            applied["cards_before"], applied["cards_after"]) == (1, 1, 1, 3, 2)
    lock_item_id = next(v[0] for q, v in idea_ids.items() if "lockfile" in q)
    assert pipe.execute("SELECT gist FROM im.items WHERE item_id = %s", (lock_item_id,)).fetchone()[0] == \
        "Updated: pin cache key to lockfile hash."
    assert pipe.execute("SELECT 1 FROM im.items WHERE item_id = %s", (db_item_id,)).fetchone() is not None
    assert all(not rows for rows in checks.run_checks(pipe).values())

    # Idempotent: a second --apply is a no-op.
    cli.main(["trial", "v5.3", "--apply", "--model", opus, "--review-model", inkling, "--review-prompt", "v2"])
    again = json.loads(capsys.readouterr().out)
    assert again["episodes"] == 0 and again["items_deleted"] == 0
