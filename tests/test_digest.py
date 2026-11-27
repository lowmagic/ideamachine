"""The weekly digest: Claude synthesizes what the lattice gained in a week, citing the ideas it rests on."""

import json
from datetime import date, timedelta
from types import SimpleNamespace

import psycopg
import pytest

from im import cli, digest, extract, lattice, llm
from im.config import ModelConfig

from helpers import HashEmbedder, run, table
from test_extract import FakeClaude
from test_lattice import FakeMatcher, items_for
from test_review import as_web_role

WEEK = date(2026, 8, 31)   # the fixture stream is said on Tuesday 2026-09-01


class FakeDigester:
    """Writes a convergence of ideas 1 and 2, a point citing only an idea it wasn't shown, and a project on 1."""

    def __init__(self):
        self.calls = []
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self.create))

    def create(self, **kw):
        self.calls.append(kw)
        out = {"summary": "A week about sensors and meetings.", "points": [
            {"kind": "convergence", "title": "Two halves of one thing", "body": "They meet.", "ideas": [1, 2, 2]},
            {"kind": "sleeper", "title": "Made up", "body": "Cites nothing it saw.", "ideas": [99]},
            {"kind": "project", "title": "Build it", "body": "Weekend project.", "ideas": [1]}]}
        content = [SimpleNamespace(type="text", text=json.dumps(out))]
        return SimpleNamespace(content=content, stop_reason="end_turn", model="claude-opus-5-5", _request_id="r",
                               usage=SimpleNamespace(input_tokens=2000, output_tokens=400))


def test_last_week_is_the_previous_monday_to_monday():
    assert digest.last_week(date(2026, 10, 13)) == date(2026, 10, 5)   # a Tuesday
    assert digest.last_week(date(2026, 10, 12)) == date(2026, 10, 5)   # a Monday: last week just ended
    assert digest.last_week(date(2026, 10, 11)) == date(2026, 9, 28)   # a Sunday: this week isn't over


def test_a_week_is_digested_once_from_ideas_not_speech(seeded_lattice, admin):
    pipe = seeded_lattice
    claude = FakeDigester()
    stats = digest.digest_stage(pipe, claude, week=WEEK)
    assert stats["written"] and stats["points"] == 2   # the point citing nothing it was shown is dropped
    text = claude.calls[0]["messages"][0]["content"]
    said = table(pipe, """SELECT DISTINCT d.title FROM pub.ideas d JOIN pub.evidence e USING (idea_id)
                          WHERE e.said_at >= '2026-08-31' AND e.said_at < '2026-09-07'""")
    assert len(said) == 3 and all(title in text for (title,) in said)   # every idea said that week
    assert "Alice" not in text and "Bob" not in text   # ideas only: no speakers, no quotes
    (start, end), = table(pipe, "SELECT period_start, period_end FROM im.digests")
    assert start.astimezone().date() == WEEK and end - start == timedelta(days=7)   # local Monday to Monday

    assert digest.digest_stage(pipe, claude, week=WEEK)["written"] is False and len(claude.calls) == 1
    (summary, points), = as_web_role(admin, "SELECT summary, points FROM pub.digests")
    assert summary == "A week about sensors and meetings."
    assert [(p["point"], p["kind"], len(p["idea_ids"]), p["mine"]) for p in points] == \
        [(1, "convergence", 2, None), (2, "project", 1, None)]
    assert table(pipe, "SELECT count(*) FROM im.egress_log WHERE prompt_version = %s", digest.prompt_version()) == [(1,)]
    assert "Build it" in digest.show(pipe)

    assert digest.digest_stage(pipe, claude, week=WEEK, again=True)["written"] and len(claude.calls) == 2
    assert table(pipe, "SELECT count(*) FROM im.digests") == [(1,)]


def test_a_quiet_week_costs_nothing(seeded_lattice):
    claude = FakeDigester()
    stats = digest.digest_stage(seeded_lattice, claude, week=date(2026, 9, 7))
    assert stats["written"] and stats["points"] == 0 and not claude.calls
    assert table(seeded_lattice, "SELECT jsonb_array_length(points) FROM pub.digests") == [(0,)]


def test_a_week_read_late_is_digested_once_it_has_been_read(seeded, cfg, stream):
    pipe = seeded
    run(pipe, cfg, stream)   # the week arrived, but extraction didn't get to it: the cap, an outage, a backlog
    claude = FakeDigester()
    assert digest.digest_stage(pipe, claude, week=WEEK)["waiting"]
    extract.run_stage(pipe, FakeClaude(items_for))
    assert digest.digest_stage(pipe, claude, week=WEEK)["waiting"]   # read, but its captures aren't matched yet
    lattice.match_stage(pipe, FakeMatcher(), HashEmbedder())
    stats = digest.digest_stage(pipe, claude, week=WEEK)
    assert stats["written"] and stats["points"] == 2 and len(claude.calls) == 1


def test_the_cap_stops_the_digest(seeded_lattice):
    stats = digest.digest_stage(seeded_lattice, FakeDigester(), monthly_cap_usd=0, week=WEEK)
    assert stats["stopped"].startswith("monthly cap") and not stats["written"]


def test_i_can_star_or_dismiss_a_point(seeded_lattice, admin):
    pipe = seeded_lattice
    digest.digest_stage(pipe, FakeDigester(), week=WEEK)
    (did,) = table(pipe, "SELECT digest_id FROM im.digests")[0]
    event = "INSERT INTO pub.feedback_events (kind, payload) VALUES (%s, %s)"
    as_web_role(admin, event, "digest_dismiss", json.dumps({"digest_id": did, "point": 1}))
    as_web_role(admin, event, "digest_star", json.dumps({"digest_id": did, "point": 2}))
    as_web_role(admin, event, "digest_star", json.dumps({"digest_id": did, "point": 1, "note": "on reflection"}))
    (points,), = as_web_role(admin, "SELECT points FROM pub.digests")
    assert [p["mine"] for p in points] == ["star", "star"]
    for bad in [{}, {"digest_id": did}, {"digest_id": str(did), "point": 1}, {"digest_id": did, "point": 1,
                                                                             "note": "x" * 501}]:
        with pytest.raises(psycopg.errors.CheckViolation):
            as_web_role(admin, event, "digest_star", json.dumps(bad))


def test_forgetting_takes_the_digest(seeded_lattice, cfg, stream):
    pipe = seeded_lattice
    digest.digest_stage(pipe, FakeDigester(), week=WEEK)
    stream.forget(3600, 3612)   # the LoRa idea's line, one of the ideas the digest was shown
    stream.write(stream.dir)
    run(pipe, cfg, stream)
    assert table(pipe, "SELECT count(*) FROM im.digests") == [(0,)]
    assert table(pipe, "SELECT count(*) FROM im.digest_points") == [(0,)]


def test_digest_compare_runs_a_candidate_model_without_overwriting_the_stored_digest(
        seeded_lattice, monkeypatch, capsys, tmp_path):
    pipe = seeded_lattice
    digest.digest_stage(pipe, FakeDigester(), week=WEEK)
    before_digests = table(pipe, "SELECT digest_id, summary, prompt_version FROM im.digests")
    before_points = table(pipe, "SELECT digest_id, ord, kind, title, body, idea_ids FROM im.digest_points ORDER BY ord")

    def post(body):
        ans = {"summary": "Neon week summary.", "points": [
            {"kind": "project", "title": "Build LoRa", "body": "Weekend project.", "ideas": [1]},
            {"kind": "sleeper", "title": "Out of range", "body": "Cites nothing shown.", "ideas": [99]}]}
        return {"id": "gen-1", "model": "google/gemini-4-neon-20261118",
                "usage": {"prompt_tokens": 1200, "completion_tokens": 250, "cost": 0.004},
                "choices": [{"finish_reason": "stop", "message": {"content": json.dumps(ans), "refusal": None}}]}

    neon_client = llm.OpenRouter(post=post)
    r = digest.compare(pipe, neon_client, WEEK, ModelConfig("google/gemini-4-neon", "low"))
    assert table(pipe, "SELECT digest_id, summary, prompt_version FROM im.digests") == before_digests
    assert table(pipe, "SELECT digest_id, ord, kind, title, body, idea_ids FROM im.digest_points ORDER BY ord") \
        == before_points
    assert r["stored"]["summary"] == "A week about sensors and meetings." and r["stored"]["model"] == "claude-opus-5-5"
    c = r["candidate"]
    assert c["model"] == "google/gemini-4-neon" and c["served_by"] == "google/gemini-4-neon-20261118"
    assert c["cost_usd"] == 0.004 and c["points_returned"] == 2 and c["points_valid"] == 1 and c["points_dropped"] == 1
    assert table(pipe, "SELECT count(*) FROM im.egress_log WHERE prompt_version LIKE 'digest-compare:%%'") == [(1,)]

    md = digest.format_compare(r)
    assert "Stored digest" in md and "Candidate digest" in md and "Neon week summary." in md

    capped = digest.compare(pipe, neon_client, WEEK, ModelConfig("google/gemini-4-neon", "low"), monthly_cap_usd=0)
    assert capped["candidate"]["stopped"].startswith("monthly cap")

    monkeypatch.setattr(cli.db, "pipeline_dsn", lambda: pipe.info.dsn + " password=" + pipe.info.password)
    monkeypatch.setattr(llm, "client_for", lambda model, zdr=False: neon_client)
    out_dir = tmp_path / "digest-out"
    cli.main(["digest", "--compare", "--model", "google/gemini-4-neon", "--out", str(out_dir)])
    summary = json.loads(capsys.readouterr().out)
    assert summary["weeks"][0]["candidate"]["points_valid"] == 1
    assert (out_dir / f"{WEEK.isoformat()}.json").exists() and (out_dir / f"{WEEK.isoformat()}.md").exists()

    with pytest.raises(SystemExit, match="never writes"):
        cli.main(["digest", "--compare", "--write"])
