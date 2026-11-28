"""Synthesis blind trial (docs/design/synthesis.md): sheet, draft, review, score."""

import json

from im import cli, extract, llm, synth_trial

from helpers import run
from test_carry import DATABASE
from test_extract import FakeClaude
from test_trial import both


def _seed_neighborhood(pipe, cfg, stream):
    """Create 3 live cards across 2 conversations connected in a triangle so they form one qualifying neighborhood."""
    run(pipe, cfg, stream)
    extract.run_stage(pipe, FakeClaude(both(DATABASE, "lockfile", "Did you see")), system=extract.SYSTEM_V5_3)
    items = pipe.execute("SELECT item_id, quote, gist, source_segment_ids FROM im.items ORDER BY item_id").fetchall()
    assert len(items) == 3
    idea_ids = []
    for item_id, quote, gist, segs in items:
        idea_id = pipe.execute(
            """INSERT INTO im.ideas (title, statement, origin, segment_ids)
               VALUES (%s, %s, 'captured', %s) RETURNING idea_id""",
            (gist[:40], gist, segs)).fetchone()[0]
        pipe.execute("INSERT INTO im.idea_evidence (idea_id, item_id, relation, judged_by) VALUES (%s, %s, 'origin', 't')",
                     (idea_id, item_id))
        pipe.execute("INSERT INTO im.item_matches (item_id, decision, idea_id, judged_by) VALUES (%s, 'new', %s, 't')",
                     (item_id, idea_id))
        idea_ids.append(idea_id)
    # Connect all 3 into a tight neighborhood (a < b).
    for i in range(len(idea_ids)):
        for j in range(i + 1, len(idea_ids)):
            a, b = sorted((idea_ids[i], idea_ids[j]))
            pipe.execute("INSERT INTO im.connections (a, b, method) VALUES (%s, %s, 'test')", (a, b))
    return idea_ids


def test_synthesis_trial_end_to_end(pipe, cfg, stream, tmp_path, monkeypatch, capsys):
    """sheet -> draft (2 models, resumable) -> review (blind shuffled options + baseline) -> score; leaves im.ideas
    and im.lineage untouched."""
    idea_ids = _seed_neighborhood(pipe, cfg, stream)
    folder = tmp_path / "synth"

    monkeypatch.setattr(cli.db, "pipeline_dsn", lambda: pipe.info.dsn + " password=" + pipe.info.password)
    cli.main(["trial-synthesis", "sheet", str(folder), "--size", "5", "--seed", "1"])
    s = json.loads(capsys.readouterr().out)
    assert s["qualifying"] == 1 and s["sampled"] == 1
    assert (folder / "key.json").exists() and (folder / "neighborhoods.md").exists()

    calls = []

    def fake_post(model_name, ans):
        def _post(body):
            calls.append((model_name, body))
            return {"id": f"s-{len(calls)}", "model": model_name,
                    "usage": {"prompt_tokens": 500, "completion_tokens": 80, "cost": 0.002},
                    "choices": [{"finish_reason": "stop", "message": {"content": json.dumps(ans), "refusal": None}}]}
        return _post

    terra_ans = {"synthesizable": True, "title": "Own the build and data stack",
                 "statement": "Pinning build inputs and running my own database keeps the stack predictable.",
                 "source_ids": [1, 2], "reason": "Two cards express the same control principle."}
    inkling_ans = {"synthesizable": False, "title": "", "statement": "", "source_ids": [],
                   "reason": "Cards are mostly unrelated topics."}

    d1 = synth_trial.draft(pipe, llm.OpenRouter(post=fake_post("openai/gpt-6.1-terra", terra_ans)),
                           folder, "openai/gpt-6.1-terra", "medium")
    assert (d1["done"], d1["synthesizable"], d1["declined"]) == (1, 1, 0)
    # Re-running skips already-drafted neighborhoods.
    d1_again = synth_trial.draft(pipe, llm.OpenRouter(post=fake_post("openai/gpt-6.1-terra", terra_ans)),
                                 folder, "openai/gpt-6.1-terra", "medium")
    assert d1_again["done"] == 0 and d1_again["skipped"] == 1

    d2 = synth_trial.draft(pipe, llm.OpenRouter(post=fake_post("thinkingmachines/inkling-2-small", inkling_ans)),
                           folder, "thinkingmachines/inkling-2-small", "medium")
    assert (d2["done"], d2["synthesizable"], d2["declined"]) == (1, 0, 1)

    # Blind review includes Terra's synthesis and the baseline card (Inkling declined so isn't shown as an option).
    cli.main(["trial-synthesis", "review", str(folder)])
    rev = json.loads(capsys.readouterr().out)
    assert rev["neighborhoods_for_review"] == 1
    rev_key = json.loads((folder / "review-key.json").read_text())
    assert set(rev_key["1"].values()) == {"openai/gpt-6.1-terra:medium", "baseline"}

    # Mark Terra's letter in review.md and score it.
    terra_letter = next(letter for letter, src in rev_key["1"].items() if src == "openai/gpt-6.1-terra:medium")
    rev_md = (folder / "review.md").read_text().replace("pick: ", f"pick: {terra_letter}")
    (folder / "review.md").write_text(rev_md)
    cli.main(["trial-synthesis", "score", str(folder)])
    sc = json.loads(capsys.readouterr().out)
    assert (sc["marked"], sc["synthesis_wins"], sc["baseline_wins"], sc["synthesis_win_rate"]) == (1, 1, 0, 1.0)
    assert sc["models"]["openai/gpt-6.1-terra:medium"]["wins"] == 1
    assert sc["models"]["thinkingmachines/inkling-2-small:medium"]["declined"] == 1

    # Live tables untouched.
    assert pipe.execute("SELECT count(*) FROM im.ideas").fetchone()[0] == len(idea_ids)
    assert pipe.execute("SELECT count(*) FROM im.lineage WHERE kind = 'synthesizes'").fetchone()[0] == 0
