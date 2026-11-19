"""B4: the blind sheet of pairs of clusters, and tallying my marks on it."""

import json
import re
from dataclasses import replace
from pathlib import Path

import pytest

from im import connect, edge_audit, egress, jev, observations

from helpers import table
from test_lattice import lat_setup, legacy_evolves

NAMED = "Did you see the build broke again, Bob?"   # a captured line that names someone


@pytest.fixture
def lattice_with_clusters(seeded, cfg, stream):
    lat_setup(seeded, cfg, stream)   # three archive ideas, plus captured clusters, one evolving an archive idea
    legacy_evolves(seeded)           # (lineage matcher v1 wrote; the sheets still draw what the live lattice holds)
    observations.assign_stage(seeded)
    return seeded


@pytest.fixture
def named_clusters(seeded, cfg, stream):
    """lattice_with_clusters, where what was said and Claude's summary of it name Bob, who Hearsay knows as a
    speaker: the build line is NAMED, and the summary of its cluster says his name too."""
    turns = stream.conversations["c-conv"].turns
    turns[0] = replace(turns[0], text=NAMED)
    stream.write(stream.dir)
    lat_setup(seeded, cfg, stream)
    observations.assign_stage(seeded)
    (build,) = table(seeded, "SELECT idea_id FROM im.ideas WHERE title = 'Broken builds as signal'")[0]
    seeded.execute("INSERT INTO pub.feedback_events (kind, idea_id, payload) VALUES ('idea_correct', %s, %s)",
                   (build, json.dumps({"statement": "A broken build is a signal about the system, Bob says."})))
    return seeded


def fill(folder, mark_of):
    """Mark each pair on the sheet from what the key says it is."""
    key = json.loads((folder / "key.json").read_text())
    sheet = (folder / "sheet.md").read_text()
    for n, p in key["pairs"].items():
        mark, note = mark_of(p)
        sheet = re.sub(rf"(## {n}\n(?:.*\n)*?)mark: \nnote: ", rf"\1mark: {mark}\nnote: {note}", sheet, count=1)
    (folder / "sheet.md").write_text(sheet)


def test_the_sheet_is_blind_and_draws_every_set(lattice_with_clusters, tmp_path):
    out = edge_audit.write(lattice_with_clusters, tmp_path)
    assert out["by_set"]["evolves"] == 1 and out["by_set"]["near"] >= 1
    key = json.loads((tmp_path / "key.json").read_text())
    assert len({tuple(p["ideas"]) for p in key["pairs"].values()}) == out["pairs"]   # no pair twice
    body = (tmp_path / "sheet.md").read_text().split("\n## 1\n", 1)[1]
    assert "Signal may not be true" in body and "> \"" in body   # statements, and quotes of what was said
    for side in re.split(r"\n(?=[AB]\. )", body):   # on each side, what was said comes before the summary
        if "> \"" in side:
            assert side.index("> \"") < re.search(r"(Claude's|My) summary", side).start()
    assert not re.search(r"near|evolves|far|similar|0\.\d", body)   # nothing says why a pair is there
    with pytest.raises(SystemExit):   # never over my marks
        edge_audit.write(lattice_with_clusters, tmp_path)


def test_a_sheet_has_as_many_pairs_as_asked_for(lattice_with_clusters):
    # An evolves pair is usually a near pair too: drawing it twice mustn't leave the sheet short.
    pool = edge_audit.candidates(lattice_with_clusters)
    every = {tuple(p["ideas"]) for s in pool.values() for p in s}
    for size in range(1, len(every) + 1):
        assert len(edge_audit.build(lattice_with_clusters, size=size)[1]["pairs"]) == size


def test_the_same_seed_makes_the_same_sheet(lattice_with_clusters):
    assert edge_audit.build(lattice_with_clusters, seed=1) == edge_audit.build(lattice_with_clusters, seed=1)


def test_scoring_tallies_labels_direction_and_notes(lattice_with_clusters, tmp_path):
    edge_audit.write(lattice_with_clusters, tmp_path)
    assert edge_audit.score(tmp_path)["marked"] == 0
    fill(tmp_path, lambda p: ("ext b > a", "a later version, not an extension") if p["set"] == "evolves"
         else ("none", "") if p["set"] == "far" else ("`con`", ""))
    r = edge_audit.score(tmp_path)
    assert r["missing"] == [] and r["by_set"]["evolves"] == {"elaborates": 1}
    assert r["direction_given"] == "1 of 1" and [m["direction"] for m in r["marks"] if m["set"] == "evolves"] == ["B>A"]
    near = sum(r["by_set"]["near"].values())
    assert near and r["by_set"]["near"] == {"tension": near}   # `con`, from before labels 2, reads as tension
    assert [n["note"] for n in r["notes"]] == ["a later version, not an extension"]


def test_marks_read_shorthand_and_ignore_blanks():
    sheet = "## 1\n\nmark: sup A>B\nnote: \n\n## 2\n\nmark: \nnote: \n\n## 3\n\nmark: rel B>A\nnote: odd one\n"
    assert edge_audit.marks(sheet) == {"1": {"label": "supports", "direction": "A>B"},
                                       "3": {"label": "related", "direction": None, "note": "odd one"}}


class FakeTyper:
    """Answers every pair with the same label, calls one analogous, and records what it was shown."""

    def __init__(self, label="related"):
        import json as _json
        from types import SimpleNamespace
        self.calls, self.label, self._json, self._ns = [], label, _json, SimpleNamespace
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self.create))

    def create(self, **kw):
        self.calls.append(kw)
        ns = self._ns
        if "structures" in kw["output_config"]["format"]["schema"]["properties"]:   # ideas 1 and 2 make one move
            n = kw["messages"][0]["content"].count("\n")
            text = self._json.dumps({"structures": [
                {"idea": i, "structure": "an agent picks the safe option" if i <= 2 else f"pattern {i} on its own"}
                for i in range(1, n + 1)]})
        else:
            text = self._json.dumps({"label": self.label, "direction": "none",
                                     "analogy": "same move" if len(self.calls) == 1 else "",
                                     "why": f"{self.label}, because"})
        return ns(content=[ns(type="text", text=text)], stop_reason="end_turn", model="claude-opus-5-5",
                  _request_id="r", usage=ns(input_tokens=500, output_tokens=40))


def test_agreement_scores_the_core_labels_and_counts_invented_ones():
    mine = {"1": {"label": "none", "note": "analogy: x"}, "2": {"label": "related", "note": None},
            "3": {"label": "extends", "note": None}, "4": {"label": "unsure", "note": None}}
    theirs = {"1": {"label": "none", "analogy": "x"}, "2": {"label": "contradicts", "analogy": ""},
              "3": {"label": "extends", "analogy": ""}, "4": {"label": "none", "analogy": ""}}
    r = edge_audit.agreement(mine, theirs)
    assert r["pairs"] == 3 and r["agree"] == round(2 / 3, 3)   # my unsure doesn't count either way
    # Old `extends` is `elaborates` and `contradicts` is `tension`; a label I never gave scores but isn't averaged in.
    assert r["f1"] == {"none": 1.0, "related": 0.0, "elaborates": 1.0, "tension": 0.0}   # old extends: elaborates
    assert r["macro_f1_core"] == round(2 / 3, 3)
    assert r["invented_supports_or_tension"] == [2] and r["tension_precision"] == 0.0
    assert r["analogies"]["both"] == [1] and (r["analogies"]["recall"], r["analogies"]["precision"]) == (1.0, 1.0)
    # The bar is whether there's an edge: a related it calls tension is still the edge I'd make.
    assert r["connection"] == {"agree": 1.0, "precision": 1.0, "recall": 1.0, "invented": [], "missed": []}
    assert r["tension"] == {"mine": 0, "theirs": 1, "both": 0}
    wrong = edge_audit.agreement(mine, {"1": {"label": "related", "analogy": ""}, "2": {"label": "none", "analogy": ""},
                                        "3": {"label": "supports", "analogy": ""}})
    assert wrong["connection"] == {"agree": round(1 / 3, 3), "precision": 0.5, "recall": 0.5, "invented": [1],
                                   "missed": [2]}


def test_a_mark_that_isnt_a_label_is_reported_not_skipped():
    got = edge_audit.marks("## 1\nmark: evolves A>B\nnote: a later telling\n\n## 2\nmark: \nnote: \n")
    assert got == {"1": {"unreadable": "evolves A>B", "note": "a later telling"}}


def test_a_draft_sees_the_sheet_without_my_marks_and_score_compares(lattice_with_clusters, tmp_path):
    edge_audit.write(lattice_with_clusters, tmp_path, size=4)
    fill(tmp_path, lambda p: ("rel", "analogy: secret note,\nthis one's a TENSION I already knew"))
    assert {m["note"] for m in edge_audit.marks((tmp_path / "sheet.md").read_text()).values()} == \
        {"analogy: secret note,\nthis one's a TENSION I already knew"}   # a note runs to the next pair
    claude = FakeTyper()
    out = edge_audit.draft(lattice_with_clusters, claude, tmp_path)
    assert out["pairs"] == 4 and len(claude.calls) == 4
    sent = "\n".join(c["messages"][0]["content"] for c in claude.calls)
    assert "secret note" not in sent and "TENSION" not in sent and "mark:" not in sent and "Claude's summary" in sent
    assert table(lattice_with_clusters, "SELECT count(*) FROM im.egress_log WHERE prompt_version = %s",
                 out["prompt_version"]) == [(4,)]
    assert edge_audit.draft(lattice_with_clusters, claude, tmp_path)["pairs"] == 4 and len(claude.calls) == 4
    d = edge_audit.score(tmp_path)["drafts"][f"claude-opus-5-5-low-labels{edge_audit.LABELSET}-B1"]
    assert d["agree"] == 1.0 and d["analogies"]["both"] == [1]


def test_review_shows_only_where_a_model_disagrees_with_me_and_my_answers_become_marks(lattice_with_clusters,
                                                                                     tmp_path):
    edge_audit.write(lattice_with_clusters, tmp_path, size=4)
    key = json.loads((tmp_path / "key.json").read_text())["pairs"]
    one, two, three, four = sorted(key, key=int)
    mine = {tuple(key[n]["ideas"]): m for n, m in ((one, "rel"), (two, "ela A>B"), (three, "rel"), (four, "none"))}
    fill(tmp_path, lambda p: (mine[tuple(p["ideas"])], ""))
    edge_audit.draft(lattice_with_clusters, FakeTyper("none"), tmp_path)   # no edge anywhere, and says why
    jev_says = {four: {"label": "related", "p": 0.9}}   # Jev gives no case, so it never asks me to review
    (tmp_path / f"draft-jev-labels{edge_audit.LABELSET}.json").write_text(json.dumps(jev_says))

    assert edge_audit.review([tmp_path])[tmp_path.name]["to_review"] == 3   # my three edges; my none agrees
    text = (tmp_path / "review.md").read_text()
    assert set(edge_audit._sections(text)) == {one, two, three} and "none, because" in text
    for num, answer in ((one, "agree"), (two, "keep"), (three, "sup")):
        text = re.sub(rf"(## {num}\n(?:.*\n)*?)answer: ", rf"\1answer: {answer}", text, count=1)
    (tmp_path / "review.md").write_text(text)

    r = edge_audit.review([tmp_path])[tmp_path.name]
    assert (r["changed"], r["kept"], r["to_review"]) == (2, 1, 0)
    got = edge_audit.marks((tmp_path / "sheet.md").read_text())
    assert [got[n]["label"] for n in (one, two, three, four)] == ["none", "elaborates", "supports", "none"]
    assert got[two]["direction"] == "A>B"   # keeping my mark keeps all of it
    assert [got[n]["note"] for n in (one, two, three)] == ["reviewed: was related", "reviewed: kept",
                                                           "reviewed: was related"]
    again = edge_audit.review([tmp_path])[tmp_path.name]   # answered once: nothing is applied or asked twice
    assert (again["changed"], again["kept"], again["to_review"]) == (0, 0, 0)


def test_an_unmarked_pair_takes_the_models_mark_where_they_agree_and_comes_to_me_where_they_dont(
        lattice_with_clusters, tmp_path):
    edge_audit.write(lattice_with_clusters, tmp_path, size=4)
    one, two, three, four = sorted(json.loads((tmp_path / "key.json").read_text())["pairs"], key=int)
    said = {"opus": {one: "none", two: "elaborates", three: "none", four: "related"},
            "terra": {one: "none", two: "supports", three: "tension", four: "none"}}
    for model, labels in said.items():
        (tmp_path / f"draft-{model}-low-labels{edge_audit.LABELSET}.json").write_text(
            json.dumps({n: {"label": label, "why": f"{model} says {label}"} for n, label in labels.items()}))
    (tmp_path / f"draft-jev-labels{edge_audit.LABELSET}.json").write_text(json.dumps({three: {"label": "none"}}))

    r = edge_audit.review([tmp_path])[tmp_path.name]
    assert (r["drafted"], r["to_review"]) == (2, 2)   # Jev, with no case, doesn't settle three
    got = edge_audit.marks((tmp_path / "sheet.md").read_text())
    assert [got[n]["label"] for n in (one, two)] == ["none", "related"]   # two kinds of edge: an edge, no kind
    text = (tmp_path / "review.md").read_text()
    assert set(edge_audit._sections(text)) == {three, four} and "terra says tension" in text
    for num, answer in ((three, "agree"), (four, "none")):
        text = re.sub(rf"(## {num}\n(?:.*\n)*?)answer: ", rf"\1answer: {answer}", text, count=1)
    (tmp_path / "review.md").write_text(text)

    again = edge_audit.review([tmp_path])[tmp_path.name]
    assert (again["changed"], again["drafted"], again["to_review"]) == (2, 0, 0)
    got = edge_audit.marks((tmp_path / "sheet.md").read_text())
    assert [got[n]["label"] for n in (three, four)] == ["tension", "none"]
    assert got[three]["note"] == "reviewed: was unmarked"
    assert edge_audit.pooled([tmp_path])["by_models"] == 2   # the score says how many marks aren't mine

    spot = edge_audit.review([tmp_path], every=True)[tmp_path.name]   # the models' marks, to spot-check
    assert (spot["to_review"], spot["spot_check"]) == (0, 2)
    text = (tmp_path / "review.md").read_text()
    assert set(edge_audit._sections(text)) == {one, two} and "opus says elaborates" in text
    (tmp_path / "review.md").write_text(re.sub(rf"(## {one}\n(?:.*\n)*?)answer: ", r"\1answer: rel", text, count=1))
    after = edge_audit.review([tmp_path], every=True)[tmp_path.name]
    assert (after["changed"], after["spot_check"]) == (1, 1)   # a blank answer leaves two as the models marked it
    got = edge_audit.marks((tmp_path / "sheet.md").read_text())
    assert [got[n]["label"] for n in (one, two)] == ["related", "related"]
    assert edge_audit.pooled([tmp_path])["by_models"] == 1   # one is mine now


def test_jev_drafts_a_sheet_from_each_side_as_shown_and_connection_is_scored_at_every_cut(lattice_with_clusters,
                                                                                         tmp_path):
    from test_probe_audit import fake_jev   # test_probe_audit imports this module's fixtures

    edge_audit.write(lattice_with_clusters, tmp_path, size=6)
    key = json.loads((tmp_path / "key.json").read_text())["pairs"]
    linked = {tuple(key[n]["ideas"]) for n in sorted(key, key=int)[:3]}   # I link the first three, not the rest
    fill(tmp_path, lambda p: ("rel" if tuple(p["ideas"]) in linked else "none", ""))
    client, sent = fake_jev()
    out = edge_audit.draft(lattice_with_clusters, client, tmp_path, "jev-1.14.0")
    assert out["pairs"] == 6 and len(sent) == 6
    assert all(set(b["questions"]) == set(jev.QUESTIONS["edge"]) | set(jev.PROBES["edge"]) for b in sent)
    for b, (_, p) in zip(sent, sorted(key.items(), key=lambda kv: int(kv[0])), strict=True):
        assert b["state"]["a"] == connect.side_data(lattice_with_clusters, p["sides"][0])   # A as the sheet shows
    draft = json.loads(Path(out["draft"]).read_text())
    assert all(d["label"] == edge_audit.jev_label(d) for d in draft.values())

    d = edge_audit.score(tmp_path)["drafts"][f"jev-1.14.0-{jev.QUESTION_SETS['edge']}-{jev.PROBE_SET['edge']}"
                                             f"-labels{edge_audit.LABELSET}-B1"]
    by_cut = d["connection_by_cut"]
    assert by_cut[str(edge_audit.EDGE_CUT)]["agree"] == d["connection"]["agree"]   # its labels are its cut
    mine = {n for n in key if tuple(key[n]["ideas"]) in linked}
    above = {n for n in key if draft[n]["p"] >= 0.5}
    assert by_cut["0.5"]["recall"] == round(len(mine & above) / len(mine), 3)
    assert 0.0 <= by_cut["auc"] <= 1.0


def test_the_analogy_spike_ranks_a_partner_that_makes_the_same_move_first(lattice_with_clusters, tmp_path):
    from im import analogy_spike
    from helpers import HashEmbedder

    edge_audit.write(lattice_with_clusters, tmp_path, size=2)
    marked = iter([("none", "analogy: same move"), ("none", "")])
    fill(tmp_path, lambda p: next(marked))
    claude = FakeTyper()   # the pair's two clusters come first and get the same structure; the rest, their own
    r = analogy_spike.run(lattice_with_clusters, claude, HashEmbedder(), tmp_path, others=10)
    assert r["pairs"] == 1 and r["pairs_detail"][0]["rank_by_structure"] == 1
    assert table(lattice_with_clusters, "SELECT count(*) FROM im.egress_log WHERE prompt_version = %s",
                 r["prompt_version"]) == [(len(claude.calls),)]
    asked = len(claude.calls)
    again = analogy_spike.run(lattice_with_clusters, claude, HashEmbedder(), tmp_path, others=10)
    assert again["pairs_detail"] == r["pairs_detail"] and len(claude.calls) == asked   # what it read isn't re-asked


def test_sheets_pool_into_one_benchmark_counting_a_shared_pair_once(lattice_with_clusters, tmp_path):
    one, two = tmp_path / "one", tmp_path / "two"
    edge_audit.write(lattice_with_clusters, one, seed=1)
    edge_audit.write(lattice_with_clusters, two, seed=2)
    fill(one, lambda p: ("rel", ""))
    fill(two, lambda p: ("none", ""))
    keys = [json.loads((f / "key.json").read_text())["pairs"] for f in (one, two)]
    first = {tuple(p["ideas"]) for p in keys[0].values()}
    shared = first & {tuple(p["ideas"]) for p in keys[1].values()}
    assert shared   # the fixture lattice is small: two sheets draw some of the same pairs
    (one / "draft-m-low.json").write_text(json.dumps({n: {"label": "related", "analogy": ""} for n in keys[0]}))

    r = edge_audit.pooled([one, two])
    unique = len(first | {tuple(p["ideas"]) for p in keys[1].values()})
    assert r["marked"] == unique and len(r["counted_once"]) == len(shared)
    assert all(n.startswith("two/") for n in r["counted_once"])   # the first sheet's mark stands
    expected = {"related": len(first), "none": unique - len(first)}
    assert r["labels"] == {label: k for label, k in expected.items() if k}   # the second sheet may add none
    d = r["drafts"]["m-low"]   # only over the pairs that model drafted
    assert d["pairs"] == len(first) and d["agree"] == 1.0


def test_a_sheet_can_leave_out_the_pairs_on_earlier_sheets(lattice_with_clusters, tmp_path):
    one, two = tmp_path / "one", tmp_path / "two"
    edge_audit.write(lattice_with_clusters, one, seed=1, size=4)
    out = edge_audit.write(lattice_with_clusters, two, seed=1, size=4, exclude_folders=[one])
    pairs = [{tuple(p["ideas"]) for p in json.loads((f / "key.json").read_text())["pairs"].values()}
             for f in (one, two)]
    assert out["pairs"] == 4 and not pairs[0] & pairs[1]   # the same seed, yet nothing I'd mark twice


def test_a_pair_the_model_cant_answer_is_skipped_and_asked_again_next_time(lattice_with_clusters, tmp_path):
    from im import llm

    edge_audit.write(lattice_with_clusters, tmp_path, size=3)
    good = json.dumps({"label": "related", "direction": "none", "analogy": ""})
    calls = []

    def post(body):   # the first pair's two attempts come back empty: the model spent its tokens reasoning
        calls.append(body)
        empty = len(calls) <= 2
        return {"id": "g", "model": "x/y", "usage": {"cost": 0.001}, "choices": [
            {"finish_reason": "length" if empty else "stop",
             "message": {"content": None, "reasoning": "hmm"} if empty else {"content": good}}]}

    out = edge_audit.draft(lattice_with_clusters, llm.OpenRouter(post=post), tmp_path, model="x/y")
    assert out["pairs"] == 2 and [f["pair"] for f in out["failed"]] == [1]
    shown = edge_audit._pairs_as_shown((tmp_path / "sheet.md").read_text())
    ideas = json.loads((tmp_path / "key.json").read_text())["pairs"]["1"]["ideas"]
    (failed,) = table(lattice_with_clusters, """SELECT cost_usd, segment_ids FROM im.egress_log
                                                WHERE payload = %s AND error IS NOT NULL""", shown["1"])
    assert float(failed[0]) == 0.002   # what it carried went, and both attempts were paid for
    assert sorted(failed[1]) == sorted(egress.segments_of(lattice_with_clusters, ideas))
    again = edge_audit.draft(lattice_with_clusters, llm.OpenRouter(post=post), tmp_path, model="x/y")
    assert again["pairs"] == 3 and "failed" not in again and len(calls) == 5
    (spent,) = table(lattice_with_clusters, "SELECT sum(cost_usd) FROM im.egress_log WHERE model = 'x/y'")[0]
    assert float(spent) == pytest.approx(0.001 * len(calls))   # every attempt is counted toward the cap
