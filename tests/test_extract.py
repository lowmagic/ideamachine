"""Idea extraction with Claude, against a fake client: what goes out, what comes back, and what's logged."""

import json
import time
from dataclasses import replace
from types import SimpleNamespace

import anthropic
import httpx2
import pytest

from im import egress, extract, feedback, importer
from im.fixtures import iso

from helpers import HashEmbedder, assert_invariants, episodes_with_text, run, script, table
from test_pipeline import COFFEE, CORRECTIONS, SECRET, V3_IDENTITY

class FakeClaude:
    """Answers with the items `respond(payload_text)` returns, and records every request."""

    def __init__(self, respond=lambda text: [], stop_reason="end_turn", error=None):
        self.calls, self.respond, self.stop_reason, self.error = [], respond, stop_reason, error
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self.create))

    def create(self, **kw):
        self.calls.append(kw)
        if self.error:
            raise self.error
        text = kw["messages"][0]["content"]
        content = [SimpleNamespace(type="text", text=json.dumps({"items": self.respond(text)}))]
        return SimpleNamespace(content=content, stop_reason=self.stop_reason, model="claude-opus-5-5",
                               _request_id="req_1", usage=SimpleNamespace(input_tokens=1000, output_tokens=200))


def coffee_item(text):
    """An item for the line where S2 (anon A in the fixtures) asks about coffee, said_by naming its speaker."""
    for line in text.splitlines():
        if "coffee" in line:
            n, rest = line[1:].split("] ", 1)
            return [{"said_by": rest.split(":")[0], "quote": "coffee order", "lines": [int(n), 999], "attributed_to": "",
                     "reading": "coffee order", "mishearings": [], "gist": "who orders the coffee"}]
    return []


def test_payload_pseudonymizes_speakers_and_numbers_lines(pipe, cfg, stream):
    stream.conversations["c-conv"].taps = [iso(20)]
    stream.write(stream.dir)
    run(pipe, cfg, stream)
    (eid,) = episodes_with_text(pipe, COFFEE)
    p = egress.build(pipe, eid)
    assert "Alice" not in p.text and "anon A" not in p.text
    assert "[1] me: Did you see the build broke again?" in p.text
    assert "[2] S1: Yeah, the cache key changed." in p.text and "[3] S2: Who's got the coffee order?" in p.text
    assert "Speakers: me, S1, S2." in p.text and "I tapped the pendant at" in p.text
    assert p.speakers == {"me": "me", "S1": "Alice", "S2": "anon A"} and len(p.segment_ids) == 6


def test_extraction_maps_items_back_and_logs_what_went_out(pipe, cfg, stream):
    run(pipe, cfg, stream)
    def respond(text):   # the coffee capture, citing lines 0 and 999 too; and one citing only line 0
        return [dict(c, lines=[0, *c["lines"]]) for c in coffee_item(text)] + \
            [dict(c, quote="nothing on line 0", lines=[0]) for c in coffee_item(text)]
    claude = FakeClaude(respond)
    stats = extract.run_stage(pipe, claude)
    assert stats["read"] == 8 and stats["items"] == 1 and len(claude.calls) == 8   # citing no real line: dropped
    (said_by, segs), = table(pipe, "SELECT said_by, source_segment_ids FROM im.items")
    assert said_by == "anon A" and len(segs) == 1  # lines 0 and 999 don't exist: dropped
    assert table(pipe, "SELECT text FROM im.source_segments WHERE segment_id = %s", segs[0]) == [(COFFEE,)]
    logged = table(pipe, "SELECT count(*), sum(cost_usd)::float, bool_and(payload IS NOT NULL) FROM im.egress_log")
    assert logged == [(8, pytest.approx(8 * (1000 * 4 + 200 * 20) / 1e6), True)]
    call = claude.calls[0]
    assert call["model"] == "claude-opus-5-5" and call["fallbacks"] == [{"model": "claude-opus-4-8"}]
    assert call["output_config"]["format"]["type"] == "json_schema"
    assert "Alice" not in json.dumps(claude.calls)  # names never leave, whatever the episode

    assert extract.run_stage(pipe, claude)["read"] == 0  # nothing changed: nothing sent again


def test_only_changed_episodes_go_out_again(pipe, cfg, stream):
    run(pipe, cfg, stream)
    claude = FakeClaude()
    extract.run_stage(pipe, claude)
    CORRECTIONS["split a turn"][0](stream)
    stream.write(stream.dir)
    run(pipe, cfg, stream)
    assert extract.run_stage(pipe, claude)["read"] == 1


def test_naming_a_speaker_sends_nothing_and_renames_the_capture(pipe, cfg, stream):
    run(pipe, cfg, stream)
    claude = FakeClaude(coffee_item)
    extract.run_stage(pipe, claude)
    sent = len(claude.calls)
    (old_item,), = table(pipe, "SELECT item_id FROM im.items")
    (idea,), = table(pipe, """INSERT INTO im.ideas (title, statement, origin) VALUES ('Coffee', 'Who orders it', 'captured')
                              RETURNING idea_id""")
    pipe.execute("INSERT INTO im.item_matches (item_id, decision, idea_id, judged_by) VALUES (%s, 'new', %s, 'test')",
                 (old_item, idea))
    pipe.execute("INSERT INTO im.idea_evidence VALUES (%s, %s, 'origin', 'test')", (idea, old_item))
    stream.name_speaker("c-conv", "anon A", "Carol")
    stream.write(stream.dir)
    run(pipe, cfg, stream)
    stats = extract.run_stage(pipe, claude)
    assert len(claude.calls) == sent and stats["read"] == stats["reused"] == 0   # same episode, same payload
    assert table(pipe, "SELECT item_id FROM im.items JOIN im.episodes USING (episode_id) WHERE current") == \
        [(old_item,)]
    # the capture now says who spoke by their new name, and its match and evidence never moved
    assert table(pipe, "SELECT said_by FROM pub.evidence WHERE item_id = %s", old_item) == [("Carol",)]
    assert table(pipe, "SELECT said_by FROM im.items WHERE item_id = %s", old_item) == [("anon A",)]  # as recorded
    assert table(pipe, "SELECT decision, idea_id FROM im.item_matches WHERE item_id = %s", old_item) == \
        [("new", idea)]
    assert_invariants(pipe)


def test_merging_voices_reads_that_episode_again(pipe, cfg, stream):
    run(pipe, cfg, stream)
    claude = FakeClaude(coffee_item)
    extract.run_stage(pipe, claude)
    sent = len(claude.calls)
    stream.name_speaker("c-conv", "anon A", "Alice")   # S2 is S1: what would be sent changed
    stream.write(stream.dir)
    run(pipe, cfg, stream)
    assert extract.run_stage(pipe, claude)["read"] == 1 and len(claude.calls) == sent + 1
    assert "[3] S1: Who's got the coffee order?" in claude.calls[-1]["messages"][0]["content"]
    assert table(pipe, """SELECT w.said_by FROM im.items i JOIN im.item_said_by w USING (item_id)
                          JOIN im.episodes e USING (episode_id) WHERE e.current""") == [("Alice",)]
    assert_invariants(pipe)


def test_reuse_after_a_rekey_sends_nothing(pipe, cfg, stream, monkeypatch):
    with monkeypatch.context() as m:
        m.setattr(importer, "STAGE_VERSION", "import/stream-v3")
        m.setattr(importer, "IDENTITY", V3_IDENTITY)
        run(pipe, cfg, stream)
        claude = FakeClaude(coffee_item)
        extract.run_stage(pipe, claude)
    sent = len(claude.calls)
    run(pipe, cfg, stream)   # every segment rekeyed, so every episode re-derives
    stats = extract.run_stage(pipe, claude, monthly_cap_usd=0)
    assert len(claude.calls) == sent and stats["read"] == 0 and stats["reused"] == 8 and stats["items"] == 1
    assert table(pipe, """SELECT w.said_by FROM im.items i JOIN im.item_said_by w USING (item_id)
                          JOIN im.episodes e USING (episode_id) WHERE e.current""") == [("anon A",)]
    assert table(pipe, """SELECT i.source_segment_ids <@ ARRAY(SELECT es.segment_id FROM im.episode_segments es
                                                              WHERE es.episode_id = i.episode_id)
                          FROM im.items i JOIN im.episodes e USING (episode_id) WHERE e.current""") == [(True,)]
    assert_invariants(pipe)


def test_reuse_keeps_each_duplicate_captures_own_verdict(pipe, cfg, stream, monkeypatch):
    # Claude sometimes captures the same line twice; quote matching alone would carry only one of the verdicts.
    twice = lambda text: coffee_item(text) * 2  # noqa: E731
    with monkeypatch.context() as m:
        m.setattr(importer, "STAGE_VERSION", "import/stream-v3")
        m.setattr(importer, "IDENTITY", V3_IDENTITY)
        run(pipe, cfg, stream)
        extract.run_stage(pipe, FakeClaude(twice))
    a, b = (r[0] for r in table(pipe, "SELECT item_id FROM im.items ORDER BY item_id"))
    extract.decide(pipe, a, "keep")
    extract.decide(pipe, b, "discard")
    run(pipe, cfg, stream)
    extract.run_stage(pipe, FakeClaude(twice), monthly_cap_usd=0)
    feedback.carry_verdicts(pipe)
    assert sorted(table(pipe, """SELECT (SELECT v.verdict FROM im.item_verdicts v WHERE v.item_id = i.item_id
                                         ORDER BY v.decided_at DESC, v.verdict_id DESC LIMIT 1)
                                 FROM im.items i JOIN im.episodes e USING (episode_id) WHERE e.current""")) == \
        [("discard",), ("keep",)]
    assert table(pipe, "SELECT method, count(*) FROM im.verdict_carries GROUP BY 1") == [("reuse", 2)]
    assert_invariants(pipe)


def test_budget_cap_refusals_and_outages(pipe, cfg, stream):
    run(pipe, cfg, stream)
    assert extract.run_stage(pipe, FakeClaude(), monthly_cap_usd=0)["stopped"].startswith("monthly cap")
    assert table(pipe, "SELECT count(*) FROM im.egress_log") == [(0,)]

    down = FakeClaude(error=anthropic.APIConnectionError(request=httpx2.Request("POST", "https://x")))
    stats = extract.run_stage(pipe, down)
    assert stats["stopped"].startswith("API unavailable") and stats["read"] == 0
    assert table(pipe, "SELECT count(*) FROM im.extractions") == [(0,)]  # retried next run
    assert table(pipe, "SELECT error IS NOT NULL FROM im.egress_log") == [(True,)]

    stats = extract.run_stage(pipe, FakeClaude(coffee_item, stop_reason="refusal"))
    assert stats["refused"] == 8 and stats["items"] == 0
    assert table(pipe, "SELECT count(*), sum(n_items) FROM im.extractions") == [(8, 0)]


def test_forgetting_purges_items_and_payloads_but_reports_what_was_sent(pipe, cfg, stream):
    run(pipe, cfg, stream)
    extract.run_stage(pipe, FakeClaude(lambda t: [{"quote": "x", "lines": [2], "attributed_to": "", "reading": "x",
                                                    "mishearings": [], "gist": "x"}]
                                       if "Private detail" in t else []))
    (item,) = [r[0] for r in table(pipe, "SELECT item_id FROM im.items")]
    extract.decide(pipe, item, "keep")
    stream.forget(14406, 14412)
    stream.write(stream.dir)
    run(pipe, cfg, stream)
    assert table(pipe, "SELECT count(*) FROM im.items") == [(0,)]
    assert table(pipe, "SELECT count(*) FROM im.item_verdicts") == [(0,)]
    assert table(pipe, "SELECT count(*) FROM im.egress_log WHERE payload LIKE %s", f"%{SECRET}%") == [(0,)]
    (sent,) = extract.forgotten_sent(pipe)
    assert sent[3] == 1
    assert_invariants(pipe)


def test_reviewing_items(pipe, cfg, stream):
    from im import cli

    run(pipe, cfg, stream)
    extract.run_stage(pipe, FakeClaude(coffee_item))
    read, write, out = script("d", "a song lyric, not an idea", "q")
    assert cli._review_items(pipe, extract, read, write) == 1
    assert table(pipe, "SELECT verdict, quote, note FROM im.item_verdicts") == \
        [("discard", "coffee order", "a song lyric, not an idea")]
    assert extract.review_items(pipe) == []
    (d,) = extract.discards(pipe)
    assert d[1] == "coffee order" and d[4] == "a song lyric, not an idea"


def test_a_capture_on_a_replaced_episode_isnt_offered_for_review(pipe, cfg, stream):
    run(pipe, cfg, stream)
    extract.run_stage(pipe, FakeClaude(coffee_item))
    assert len(extract.review_items(pipe)) == 1
    stream.name_speaker("c-conv", "anon A", "Alice")   # S2 is S1: the coffee episode is replaced, and not read yet
    stream.write(stream.dir)
    run(pipe, cfg, stream)
    assert extract.review_items(pipe) == []


def test_im_run_goes_on_without_claude(pipe, cfg, stream, monkeypatch, capsys):
    from im import cli

    run(pipe, cfg, stream)
    monkeypatch.setattr(cli.db, "pipeline_dsn", lambda: pipe.info.dsn + " password=" + pipe.info.password)
    monkeypatch.setattr(cli.db, "stream_dir", lambda: stream.dir)
    cli.main(["run"])
    out = json.loads(capsys.readouterr().out)
    assert out["extract"]["stopped"].startswith("Claude")
    assert "lattice" in out and "weight" in out   # the stages after extraction still ran


def test_pending_reports_what_would_be_resent_and_what_the_gate_holds(pipe, cfg, stream, monkeypatch, capsys):
    from im import cli

    run(pipe, cfg, stream)
    extract.run_stage(pipe, FakeClaude())
    monkeypatch.setattr(cli.db, "pipeline_dsn", lambda: pipe.info.dsn + " password=" + pipe.info.password)
    cli.main(["pending", "--expect-none-resent"])
    out = json.loads(capsys.readouterr().out)
    assert out["already_extracted_would_resend"] == 0 and out["held"] == {}
    cli.main(["pending", "--prompt", "v5.2"])   # another prompt holds what was read, and says what it would cost
    out = json.loads(capsys.readouterr().out)
    assert out["episodes_to_extract"] == 0 and out["already_extracted_would_resend"] == 0
    assert out["held"]["extract"]["held"] == 8 and out["held"]["extract"]["estimated_usd"] > 0
    monkeypatch.setenv("TZ", "Asia/Tokyo")  # a machine in another time zone renders different payloads
    time.tzset()
    try:
        with pytest.raises(SystemExit):
            cli.main(["pending", "--expect-none-resent"])
        assert json.loads(capsys.readouterr().out)["already_extracted_would_resend"] == 8
    finally:
        monkeypatch.delenv("TZ")
        time.tzset()


class FailsOn(FakeClaude):
    """FakeClaude, except that the episode where `needle` was said comes back as `how`: a 400 (no usage), a reply
    that spent its tokens thinking and wrote no text, or JSON cut off at max_tokens (both at 1000 in, 16000 out)."""

    def __init__(self, needle, how, respond=lambda text: []):
        super().__init__(respond)
        self.needle, self.how = needle, how

    def create(self, **kw):
        if self.needle not in kw["messages"][0]["content"]:
            return super().create(**kw)
        self.calls.append(kw)
        if self.how == "400":
            raise anthropic.BadRequestError("prompt is too long", body=None,
                                            response=httpx2.Response(400, request=httpx2.Request("POST", "https://x")))
        content = [SimpleNamespace(type="thinking", thinking="hmm")] if self.how == "no text" else \
            [SimpleNamespace(type="text", text='{"items": [{"kind": "jo')]
        return SimpleNamespace(content=content, stop_reason="max_tokens", model="claude-opus-5-5", _request_id="req_x",
                               usage=SimpleNamespace(input_tokens=1000, output_tokens=16000))


@pytest.mark.parametrize("how", ["400", "no text", "cut off"])
def test_a_reply_that_fails_is_logged_and_counted_and_the_next_episodes_are_read(pipe, cfg, stream, how):
    run(pipe, cfg, stream)
    claude = FailsOn("coffee", how)
    stats = extract.run_stage(pipe, claude)
    assert (stats["read"], stats["failed"], len(claude.calls)) == (7, 1, 8)
    (failed,) = table(pipe, "SELECT cardinality(segment_ids), cost_usd, payload LIKE '%%coffee%%' FROM im.egress_log "
                            "WHERE error IS NOT NULL")
    paid = 0.0 if how == "400" else 1000 * 4.0 / 1e6 + 16000 * 20.0 / 1e6   # Opus 5.5
    assert failed[0] > 0 and float(failed[1]) == pytest.approx(paid) and failed[2]
    assert egress.spent_this_month(pipe) == pytest.approx(7 * (1000 * 4.0 + 200 * 20.0) / 1e6 + paid)
    again = extract.run_stage(pipe, claude)   # only the failed episode is asked again
    assert (again["pending"], again["failed"], len(claude.calls)) == (1, 1, 9)


def test_im_run_goes_on_past_a_reply_that_fails_to_the_stages_after_extraction(pipe, cfg, stream, monkeypatch,
                                                                               capsys):
    from im import cli, lattice

    run(pipe, cfg, stream)
    monkeypatch.setattr(cli.db, "pipeline_dsn", lambda: pipe.info.dsn + " password=" + pipe.info.password)
    monkeypatch.setattr(cli.db, "stream_dir", lambda: stream.dir)
    monkeypatch.setattr(anthropic, "Anthropic", lambda: FailsOn("coffee", "no text"))
    monkeypatch.setattr(lattice, "SentenceEmbedder", lambda model: HashEmbedder())
    cli.main(["run"])
    out = json.loads(capsys.readouterr().out)
    assert (out["extract"]["read"], out["extract"]["failed"]) == (7, 1)
    assert "lattice" in out and "digest" in out


def test_a_model_with_no_known_price_is_never_sent_to(pipe, cfg, stream):
    run(pipe, cfg, stream)
    claude = FakeClaude()
    stats = extract.run_stage(pipe, claude, model="claude-opus-9")
    assert stats["stopped"] and stats["read"] == 0 and claude.calls == []
    assert table(pipe, "SELECT count(*) FROM im.egress_log") == [(0,)]


def test_a_v5_capture_keeps_its_reading_attribution_mishearings_and_checks(pipe, cfg, stream, monkeypatch):
    from im import cli

    run(pipe, cfg, stream)

    def respond(text):   # the first line I said, read with one mishearing fixed, and once tidied
        mine = [(int(r[1:].split("]")[0]), r.split(": ", 1)[1]) for r in text.splitlines()
                if r.startswith("[") and r.split(": ", 1)[0].endswith(" me")]
        if not mine:
            return []
        n, said = mine[0]
        word = said.split()[-1]
        base = {"lines": [n], "attributed_to": "my lead", "reading": said.replace(word, "WORD"),
                "mishearings": [{"heard": word, "meant": "WORD"}], "gist": "A thought."}
        return [base | {"quote": said}, base | {"quote": said.upper()}]

    monkeypatch.setattr(anthropic, "Anthropic", lambda: FakeClaude(respond))
    stats = cli._extract(pipe, cfg)   # v5.3, the live prompt
    assert stats["read"] > 0 and stats["failed"] == 0
    rows = table(pipe, """SELECT kind, confidence, said_by, attributed_to, reading LIKE '%%WORD%%', mishearings,
                                 checks FROM im.items WHERE prompt_version = %s ORDER BY checks""",
                 stats["prompt_version"])
    first = rows[0]
    assert first[:5] == (None, None, "me", "my lead", True) and first[5][0]["meant"] == "WORD" and first[6] == []
    assert {tuple(r[6]) for r in rows} == {(), ("quote_inexact", "mishearing_not_in_quote")}   # recorded, not refused

    # Reviewed like any capture: who said it, the quote and the gist.
    read, write, out = script("k", "q")
    assert cli._review_items(pipe, extract, read, write) == 1
    assert any("said by me" in line for line in out) and not any("confidence" in line for line in out)


def test_v5_3_reads_under_policy_b_with_names_said_as_said(pipe, cfg, stream, monkeypatch, capsys):
    from im import cli

    turns = stream.conversations["c-conv"].turns
    turns[0] = replace(turns[0], text="Did you see the build broke again, Alice?")
    stream.write(stream.dir)
    run(pipe, cfg, stream)

    def respond(text):   # my line about the build, as sent
        for line in text.splitlines():
            if line.startswith("[") and "build broke" in line:
                n, said = int(line[1:].split("]")[0]), line.split(": ", 1)[1]
                return [{"lines": [n], "quote": said, "attributed_to": "", "reading": said, "mishearings": [],
                         "gist": "I asked Alice whether the build broke again."}]
        return []

    v53 = FakeClaude(respond)
    monkeypatch.setattr(anthropic, "Anthropic", lambda: v53)
    episodes = table(pipe, "SELECT count(*) FILTER (WHERE kind <> 'others_only') FROM im.episodes WHERE current")[0][0]
    # Only my speech is captured, so an episode where I don't speak isn't sent.
    assert cli._extract(pipe, replace(cfg, extract=replace(cfg.extract, prompt="v5.3")))["read"] == episodes < 9
    assert "broke again, Alice?" in json.dumps([c["messages"] for c in v53.calls])   # the name said, as said
    assert table(pipe, "SELECT quote, gist, privacy_policy_version FROM im.items") == \
        [("Did you see the build broke again, Alice?", "I asked Alice whether the build broke again.",
          egress.POLICY_VERSION)]

    # Read counts as read: nothing would go out again.
    monkeypatch.setattr(cli.db, "pipeline_dsn", lambda: pipe.info.dsn + " password=" + pipe.info.password)
    cli.main(["pending", "--prompt", "v5.3", "--expect-none-resent"])
    assert json.loads(capsys.readouterr().out)["episodes_to_extract"] == 0


def test_switching_to_zero_data_retention_reads_nothing_again(pipe, cfg, stream, monkeypatch):
    from im import cli, llm

    run(pipe, cfg, stream)
    monkeypatch.setattr(anthropic, "Anthropic", lambda: FakeClaude(coffee_item))
    assert cli._extract(pipe, cfg)["read"] == 8
    # The route isn't part of extraction's version: the same model through OpenRouter has read it all already.
    monkeypatch.setenv("OPENROUTER_API_KEY", "k")
    posted = []
    monkeypatch.setattr(llm.OpenRouter, "_post", lambda self, body: posted.append(body))
    stats = cli._extract(pipe, replace(cfg, egress=replace(cfg.egress, zdr=True)))
    assert (stats["pending"], stats["read"], posted) == (0, 0, [])


def status_error(cls, code, body=None):
    return cls("rejected", body=body, response=httpx2.Response(code, request=httpx2.Request("POST", "https://x")))


@pytest.mark.parametrize("error", [
    status_error(anthropic.NotFoundError, 404),   # a retired model id
    status_error(anthropic.APIStatusError, 402, {"type": "error", "error": {"type": "billing_error"}}),
    status_error(anthropic.AuthenticationError, 401)])
def test_an_error_every_request_would_get_stops_extraction_after_the_first(pipe, cfg, stream, error):
    run(pipe, cfg, stream)
    claude = FakeClaude(error=error)
    stats = extract.run_stage(pipe, claude)
    assert len(claude.calls) == 1 and stats["stopped"] and stats["failed"] == 0
    assert table(pipe, "SELECT count(*), count(error) FROM im.egress_log") == [(1, 1)]   # it went, so it's logged


def test_review_pass_merges_drops_logs_and_preserves_verbatim(pipe, cfg, stream):
    from im import llm

    run(pipe, cfg, stream)

    def pass1(text):
        if "LoRa" not in text:
            return []
        return [
            {"lines": [1], "quote": "the garden sensors could report soil moisture", "attributed_to": "a field guide",
             "reading": "The field sensors report soil moisture.",
             "mishearings": [{"heard": "the", "meant": "The"}, {"heard": "garden", "meant": "field"}],
             "gist": "Field sensors could report soil moisture."},
            {"lines": [2], "quote": "Battery life is the hard part, maybe a solar trickle charger.",
             "attributed_to": "", "reading": "The battery needs a PV trickle charger.",
             "mishearings": [{"heard": "the", "meant": "The"}, {"heard": "solar", "meant": "PV"}],
             "gist": "Battery life probably needs a PV trickle charger."},
            {"lines": [4], "quote": "Write that down for the weekend.", "attributed_to": "",
             "reading": "Write that down for the weekend.", "mishearings": [], "gist": "Write that down."},
        ]

    review_calls = []

    def review_post(body):
        review_calls.append(body)
        ans = {"kept": [{"ids": [2, 1], "gist": "Soil moisture sensors need a solar trickle charger."}],
               "dropped": [{"id": 3, "reason": "reminder"}]}
        return {"id": "rev-1", "model": "thinkingmachines/inkling-2-small",
                "usage": {"prompt_tokens": 600, "completion_tokens": 80, "cost": 0.0015},
                "choices": [{"finish_reason": "stop", "message": {"content": json.dumps(ans), "refusal": None}}]}

    claude = FakeClaude(pass1)
    reviewer = llm.OpenRouter(post=review_post)
    stats = extract.run_stage(pipe, claude, review_client=reviewer,
                              review_model="thinkingmachines/inkling-2-small", review_effort="medium")
    pass1_cost = 8 * (1000 * 4 + 200 * 20) / 1e6
    assert (stats["read"], stats["items"], stats["reviewed"], stats["review_merges"], stats["review_drops"]) == \
        (8, 1, 1, 1, 1)
    assert stats["review_model"] == "thinkingmachines/inkling-2-small"
    assert stats["cost_usd"] == pytest.approx(round(pass1_cost + 0.0015, 4))
    assert len(claude.calls) == 8 and len(review_calls) == 1   # empty Pass-1 episodes skipped
    assert "Bob" not in json.dumps(review_calls)

    (eid, quote, gist, reading, attr, mis, segs, said_by, checks), = table(
        pipe, """SELECT i.episode_id, i.quote, i.gist, i.reading, i.attributed_to, i.mishearings,
                        i.source_segment_ids, w.said_by, i.checks
                 FROM im.items i JOIN im.item_said_by w USING (item_id)""")
    expected_segs = [s for (s,) in table(
        pipe, "SELECT segment_id FROM im.episode_segments WHERE episode_id = %s ORDER BY ord LIMIT 2", eid)]
    assert quote == "Battery life is the hard part, maybe a solar trickle charger."
    assert gist == "Soil moisture sensors need a solar trickle charger."
    assert reading == "The field sensors report soil moisture. The battery needs a PV trickle charger."
    assert attr == "a field guide" and said_by == "me" and checks == []
    assert mis == [{"heard": "the", "meant": "The"}, {"heard": "garden", "meant": "field"},
                   {"heard": "solar", "meant": "PV"}]
    assert segs == expected_segs   # sorted in episode order even when ids were [2, 1]

    assert table(pipe, "SELECT prompt_version, count(*) FROM im.egress_log GROUP BY 1 ORDER BY 1") == sorted([
        (stats["prompt_version"], 8), (f"{stats['prompt_version']}:review", 1)])
    again = extract.run_stage(pipe, claude, review_client=reviewer,
                              review_model="thinkingmachines/inkling-2-small", review_effort="medium")
    assert again["read"] == 0 and len(claude.calls) == 8 and len(review_calls) == 1
    assert_invariants(pipe)


def test_review_pass_failure_and_stop_leave_episode_unread(pipe, cfg, stream):
    from im import llm

    run(pipe, cfg, stream)

    def pass1(text):
        if "LoRa" not in text:
            return []
        return [{"lines": [1], "quote": "Idea: the garden sensors could report soil moisture over LoRa.",
                 "attributed_to": "", "reading": "Garden sensors could report soil moisture over LoRa.",
                 "mishearings": [], "gist": "Garden sensors could use LoRa."}]

    def post_from(ans_or_err):
        def post(_body):
            if "error" in ans_or_err:
                return ans_or_err
            return {"id": "rev-x", "model": "thinkingmachines/inkling-2-small",
                    "usage": {"prompt_tokens": 500, "completion_tokens": 40, "cost": 0.001},
                    "choices": [{"finish_reason": "stop",
                                 "message": {"content": json.dumps(ans_or_err), "refusal": None}}]}
        return llm.OpenRouter(post=post)

    bad = post_from({"kept": [{"ids": [1, 1], "gist": "duplicate candidate id"}], "dropped": []})
    s1 = extract.run_stage(pipe, FakeClaude(pass1), review_client=bad, review_model="thinkingmachines/inkling-2-small")
    assert (s1["read"], s1["failed"]) == (7, 1)
    assert table(pipe, "SELECT count(*) FROM im.extractions") == [(7,)]
    assert table(pipe, "SELECT count(*) FROM im.egress_log WHERE prompt_version = %s AND error IS NOT NULL",
                 f"{s1['prompt_version']}:review") == [(1,)]

    stopped_rev = post_from({"error": {"code": 402, "message": "out of credit"}})
    s2 = extract.run_stage(pipe, FakeClaude(pass1), review_client=stopped_rev,
                           review_model="thinkingmachines/inkling-2-small")
    assert s2["pending"] == 1 and s2["stopped"] and s2["read"] == 0
    assert s2["cost_usd"] == pytest.approx(round((1000 * 4 + 200 * 20) / 1e6, 4))
    assert table(pipe, "SELECT count(*) FROM im.extractions") == [(7,)]

    good = post_from({"kept": [{"ids": [1], "gist": "Garden sensors could use LoRa."}], "dropped": []})
    s3 = extract.run_stage(pipe, FakeClaude(pass1), review_client=good, review_model="thinkingmachines/inkling-2-small")
    assert (s3["pending"], s3["read"], s3["reviewed"], s3["failed"]) == (1, 1, 1, 0)
    assert table(pipe, "SELECT count(*) FROM im.extractions") == [(8,)]


def test_review_model_triggers_reread_gate_and_forgetting_purges_both_passes(pipe, cfg, stream, monkeypatch):
    from im import cli, fixtures, llm

    run(pipe, cfg, stream)
    monkeypatch.setattr(anthropic, "Anthropic", lambda: FakeClaude(coffee_item))
    assert cli._extract(pipe, cfg)["read"] == 8

    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    posted = []

    def fake_post(self, body):
        posted.append(body)
        if body["model"] == "openai/gpt-6.1-terra":
            items = [{"lines": [1], "quote": "Another secret thought for forgetting test.", "attributed_to": "",
                      "reading": "Another secret thought for forgetting test.", "mishearings": [],
                      "gist": "A thought to forget later."}]
            ans = {"items": items}
        else:
            ans = {"kept": [{"ids": [1], "gist": "A thought to forget later."}], "dropped": []}
        return {"id": f"or-{len(posted)}", "model": body["model"],
                "usage": {"prompt_tokens": 400, "completion_tokens": 50, "cost": 0.001},
                "choices": [{"finish_reason": "stop", "message": {"content": json.dumps(ans), "refusal": None}}]}

    monkeypatch.setattr(llm.OpenRouter, "_post", fake_post)
    cfg2 = replace(cfg, extract=replace(cfg.extract, model="openai/gpt-6.1-terra",
                                        review_model="thinkingmachines/inkling-2-small", review_effort="medium"))
    held_stats = cli._extract(pipe, cfg2)
    assert (held_stats["read"], held_stats["held"]["held"], posted) == (0, 8, [])

    stream.conversations["c-secret"] = fixtures.Conversation(
        [fixtures._me(20000, 20006, "Another secret thought for forgetting test.")])
    stream.write(stream.dir)
    run(pipe, cfg, stream)

    new_stats = cli._extract(pipe, cfg2)
    assert (new_stats["read"], new_stats["reviewed"], new_stats["held"]["held"], len(posted)) == (1, 1, 8, 2)

    stream.forget(20000, 20006)
    stream.write(stream.dir)
    run(pipe, cfg, stream)

    assert table(pipe, "SELECT count(*) FROM im.items WHERE quote LIKE '%%Another secret thought%%'") == [(0,)]
    assert table(pipe, "SELECT count(*) FROM im.egress_log WHERE payload LIKE '%%Another secret thought%%'") == [(0,)]
    (sent,) = extract.forgotten_sent(pipe)
    assert sent[3] == 2
    assert_invariants(pipe)
