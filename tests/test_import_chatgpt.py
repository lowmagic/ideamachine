"""`im import-chatgpt`: my messages to the ChatGPT Idea Machine, as the curation sheet marks them, read by extraction
v5.3 a session at a time into archive captures (migration 055)."""

import json

import pytest

from im import archive_observations as ao, egress, lattice

from helpers import HashEmbedder, assert_invariants, table
from test_extract import FakeClaude
from test_lattice import FakeMatcher

# Shaped like the real export (ChatGPT Exporter): an instruction, an idea, and a late-night session that runs past
# midnight, then a message more than six hours later. ChatGPT's replies carry its numbering, which is never read.
EXPORT = """# Idea Machine

**User:** Anonymous
**Link:** [https://chatgpt.com/c/6a68b7be-0000](https://chatgpt.com/c/6a68b7be-0000)

## Prompt:
7/28/2026, 10:08:02 AM

This chat is my Idea Machine. Capture ideas, don't evaluate them.

## Response:
7/28/2026, 10:08:06 AM · gpt-5-6-thinking

Idea Machine is active.

## Prompt:
7/28/2026, 10:08:30 AM

A solitary unsatisfying grape

## Response:
7/28/2026, 10:08:32 AM · gpt-5-5

Captured. **Idea #1:** *A solitary unsatisfying grape.*

## Prompt:
8/19/2026, 9:56:14 PM

signal may not be true, but it is always correct. and the grape was alone on the plate

## Response:
8/19/2026, 9:56:14 PM · gpt-5-6

**Idea #2:** *Signal may not be true, but it is always correct.* This connects to **Idea #1**.

## Prompt:
8/20/2026, 2:37:47 AM

Michelle says the meeting
is the coffee

## Response:
8/20/2026, 2:37:47 AM · gpt-5-6

**Idea #3:** *The meeting is the coffee.*

## Prompt:
8/20/2026, 8:37:47 AM

a broken build is a signal about the team

## Response:
8/20/2026, 8:37:50 AM · gpt-5-6

**Idea #4:** *Broken builds.*

---
Powered by [ChatGPT Exporter](https://www.chatgptexporter.com)
"""

SHEET = """# The ChatGPT Idea Machine: which messages to import

- `keep`: import as written.

## 1

2026-07-28 10:08

> This chat is my Idea Machine. Capture ideas, don't evaluate them.

mark: drop
text: 
note: setup instructions to ChatGPT

## 2

2026-07-28 10:08

> A solitary unsatisfying grape

mark: keep
text: 
note: 

## 3

2026-08-19 21:56

> signal may not be true, but it is always correct. and the grape was alone on the plate

mark: edit
text: Signal may not be true,
but it is always correct.
note: the grape was a joke

## 4

2026-08-20 02:37 · follows 3

> Michelle says the meeting
> is the coffee

mark: keep
text: 
note: 

## 5

2026-08-20 08:37

> a broken build is a signal about the team

mark: keep
text: 
note: 
"""
CHATGPT = ("Idea Machine is active", "Captured", "Idea #", "Broken builds.", "This chat is my Idea Machine",
           "alone on the plate")


def every_line(text):
    """v5.3: every message is one capture, quoted whole."""
    return [{"quote": said, "lines": [int(n)], "attributed_to": "", "reading": said, "mishearings": [], "gist": said}
            for n, said in (row[1:].split("] me: ", 1) for row in text.splitlines() if row.startswith("["))]


def write(conn, claude, p):
    return ao.import_sessions(conn, claude, p, "claude-opus-5-5", "medium", 20.0)


def test_each_session_goes_out_alone_with_only_my_kept_words():
    p = ao.plan(EXPORT, SHEET)
    # A gap of six hours or more starts a session; the late-night one runs past midnight.
    assert [[m["n"] for m in s] for s in p["sessions"]] == [[2], [3, 4], [5]]
    sent = [ao.payload(s).text for s in p["sessions"]]
    assert sent[1] == (
        "Context: Typed messages I sent to a chat, not speech: each line is one whole message of mine. Sent "
        "[1] Wednesday 2026-08-19 21:56, [2] Thursday 2026-08-20 02:37.\n\nTranscript:\n"
        "[1] me: Signal may not be true, but it is always correct.\n"
        "[2] me: Michelle says the meeting is the coffee")
    assert not [w for w in CHATGPT if w in "".join(sent)]   # nothing of ChatGPT's, nor what I dropped or edited


def test_writing_stores_each_capture_once_as_mine_with_my_edits(pipe):
    p = ao.plan(EXPORT, SHEET)
    claude = FakeClaude(every_line)
    stats = write(pipe, claude, p)
    assert (len(claude.calls), stats["read"], stats["items"]) == (3, 3, 4)
    sent = json.dumps([c["messages"] for c in claude.calls])
    assert "Michelle says" in sent and not [w for w in CHATGPT if w in sent]
    assert table(pipe, """SELECT o.quote, to_char(o.said_at AT TIME ZONE 'America/New_York', 'MM-DD HH24:MI'),
                                 o.edited, o.said_by, i.episode_id
                          FROM im.items i JOIN im.observations o USING (observation_id)
                          WHERE o.source = 'archive' ORDER BY o.said_at""") == [
        ("A solitary unsatisfying grape", "07-28 10:08", False, "me", None),
        ("Signal may not be true, but it is always correct.", "08-19 21:56", True, "me", None),
        ("Michelle says the meeting is the coffee", "08-20 02:37", False, "me", None),
        ("a broken build is a signal about the team", "08-20 08:37", False, "me", None)]
    assert table(pipe, """SELECT count(*), bool_and(episode_id IS NULL AND segment_ids = '{}'),
                                 bool_and(privacy_policy_version = %s) FROM im.egress_log""",
                 egress.POLICY_VERSION) == [(3, True, True)]

    again = FakeClaude(every_line)
    assert write(pipe, again, p)["already_read"] == 3 and again.calls == []
    assert ao.report(p, ao.already_read(pipe, p))["to_read"] == 0
    assert lattice.match_stage(pipe, FakeMatcher(), HashEmbedder())["new"] == 4
    assert table(pipe, "SELECT maturity, count(*) FROM pub.cards GROUP BY 1") == [("candidate", 4)]
    assert_invariants(pipe)


@pytest.mark.parametrize("change, error", [
    (("2026-07-28 10:08\n\n> A solitary", "2026-07-28 10:09\n\n> A solitary"), "## 2 isn't the export's message 2"),
    (("> A solitary unsatisfying grape", "> A solitary grape"), "## 2 isn't the export's message 2"),
    (("## 5", "## 6"), "the sheet has messages the export doesn't: \\[6\\]"),
    (("mark: drop", "mark: maybe"), "## 1: mark 'maybe' isn't one of keep, drop, edit"),
    (("mark: keep", "mark: edit"), "## 2: marked edit, with no text"),
])
def test_a_sheet_that_doesnt_fit_the_export_is_refused(change, error):
    with pytest.raises(ValueError, match=error):
        ao.plan(EXPORT, SHEET.replace(*change, 1))


def test_not_an_export():
    with pytest.raises(ValueError, match="not a ChatGPT export"):
        ao.parse("## Prompt:\n7/28/2026, 10:08:02 AM\n\nhello\n")


def test_import_chatgpt_supports_review_model_and_openrouter_through_cli(pipe, tmp_path, monkeypatch, capsys):
    from im import cli, llm

    export_path, sheet_path = tmp_path / "export.md", tmp_path / "sheet.md"
    export_path.write_text(EXPORT)
    sheet_path.write_text(SHEET)
    cfg_path = tmp_path / "im.toml"
    cfg_path.write_text('[extract]\nmodel = "openai/gpt-6.1-terra"\n'
                        'review_model = "thinkingmachines/inkling-2-small"\nreview_effort = "medium"\n')
    monkeypatch.setenv("IM_CONFIG", str(cfg_path))
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    monkeypatch.setattr(cli.db, "pipeline_dsn", lambda: pipe.info.dsn + " password=" + pipe.info.password)

    p1_calls, p2_calls = [], []

    def fake_post(self, body):
        content = body["messages"][1]["content"]
        if body["model"] == "openai/gpt-6.1-terra":
            p1_calls.append(body)
            return {"id": f"p1-{len(p1_calls)}", "model": body["model"],
                    "usage": {"prompt_tokens": 300, "completion_tokens": 50, "cost": 0.002},
                    "choices": [{"finish_reason": "stop",
                                 "message": {"content": json.dumps({"items": every_line(content)}), "refusal": None}}]}
        p2_calls.append(body)
        if len(p2_calls) == 2:   # session 2 has 2 items: drop item 1, keep item 2
            kept = [{"ids": [2], "gist": "Michelle says the meeting is the coffee."}]
            dropped = [{"id": 1, "reason": "fragment"}]
        else:
            kept = [{"ids": [1], "gist": "First message thought."}]
            dropped = []
        return {"id": f"p2-{len(p2_calls)}", "model": body["model"],
                "usage": {"prompt_tokens": 200, "completion_tokens": 30, "cost": 0.001},
                "choices": [{"finish_reason": "stop",
                             "message": {"content": json.dumps({"kept": kept, "dropped": dropped}),
                                         "refusal": None}}]}

    monkeypatch.setattr(llm.OpenRouter, "_post", fake_post)
    cli.main(["import-chatgpt", str(export_path), str(sheet_path), "--write"])
    out = json.loads(capsys.readouterr().out)
    assert (out["read"], out["items"], len(p1_calls), len(p2_calls)) == (3, 3, 3, 3)
    assert out["cost_usd"] == pytest.approx(0.009)
    assert table(pipe, "SELECT count(*), bool_and(segment_ids = '{}') FROM im.egress_log "
                       "WHERE prompt_version LIKE '%%:review'") == [(3, True)]
    assert_invariants(pipe)
