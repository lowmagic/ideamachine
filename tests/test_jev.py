"""Jev: atomic Nouls about structured state, combined in code, drafting the probe sheets."""

import json

import pytest

from im import jev, probe_audit

from test_edge_audit import lattice_with_clusters  # noqa: F401 (fixture)
from test_lattice import seeded  # noqa: F401 (fixture)
from test_probe_audit import FakeProposer, fill


def fake(nouls, broken: int = 0):
    """Jev answering each question with `nouls` (0.1 otherwise); the first `broken` answers come back without any."""
    sent = []

    def post(body):
        sent.append(body)
        answers = {} if len(sent) <= broken else {"answers": {q: {"type": "noul", "noul": nouls.get(q, 0.1)}
                                                              for q in body["questions"]}}
        return {"model": "jev-1.14.0", **answers, "usage": {"input_tokens": 1000, "output_tokens": 9}}
    return jev.Jev(post=post), sent


def ask(client, question, state, probes=False):
    """One request's answer, read, and its reply (with what it cost), outside the egress log."""
    r = client.complete(client.body(question, state, probes))
    return jev.read(question, json.loads(r.text), probes), r


def test_a_tension_is_four_wordings_of_one_question_averaged():
    client, sent = fake({"competing_pulls": 0.9, "pulls_v2": 0.7, "pulls_v3": 0.8, "pulls_v4": 0.6})
    p, r = ask(client, "tension", {"a": {"said": []}, "b": {"said": []}})
    body = sent[0]
    assert body["model"] == "typesafe/jev-1.14" and set(body["questions"]) == set(jev.QUESTIONS["tension"])
    assert all(q["type"] == "noul" for q in body["questions"].values())
    assert p["p"] == pytest.approx(0.75) and jev.answer("tension", p["p"]) == "yes"
    assert r.cost_usd == pytest.approx(1000 * 0.042 / 1e6)


def test_a_tension_has_one_cut_off_and_no_middle():
    assert jev.answer("tension", 0.45) == "yes" and jev.answer("tension", 0.449) == "no"


def test_an_analogy_needs_both_and_the_middle_is_unsure():
    client, _ = fake({"different_subjects": 0.9, "same_structure": 0.5})
    p, _ = ask(client, "analogy", {"a": {}, "b": {}})
    assert p["p"] == 0.5 and jev.answer("analogy", 0.5) == "unsure" and jev.answer("analogy", 0.2) == "no"


def test_jev_drafts_a_sheet_from_structured_state(lattice_with_clusters, tmp_path):  # noqa: F811
    # Names said go out as said: test_audit_egress, on a lattice whose clusters name someone.
    probe_audit.write(lattice_with_clusters, FakeProposer(), None, "tension", tmp_path)
    key = json.loads((tmp_path / "key.json").read_text())
    fill(tmp_path, lambda p: "no")
    client, sent = fake({q: 0.95 for q in jev.QUESTIONS["tension"]})
    d = probe_audit.draft(lattice_with_clusters, client, tmp_path, "jev-1.14.0")
    assert d["draft"].endswith("draft-jev-1.14.0-pulls4-B1.json") and d["pairs"] == len(key["pairs"]) == len(sent)
    state = sent[0]["state"]
    assert set(state) == {"a", "b"} and ({"summary", "my_summary"} & set(state["a"]))
    assert isinstance(state["a"]["said"], list)
    got = json.loads((tmp_path / "draft-jev-1.14.0-pulls4-B1.json").read_text())
    assert all(a["answer"] == "yes" and a["p"] == 0.95 for a in got.values())
    r = probe_audit.score(tmp_path)["drafts"]["jev-1.14.0-pulls4-B1"]
    assert r["precision"] == 0.0 and len(r["invented"]) == len(key["pairs"])
    logged = lattice_with_clusters.execute(
        "SELECT count(*), sum(cost_usd) FROM im.egress_log WHERE model = 'jev-1.14.0'").fetchone()
    assert logged[0] == len(sent) and float(logged[1]) == pytest.approx(len(sent) * 1000 * 0.042 / 1e6)
    payloads = [json.loads(p) for (p,) in lattice_with_clusters.execute(
        "SELECT payload FROM im.egress_log WHERE model = 'jev-1.14.0' ORDER BY egress_id")]
    assert payloads == sent   # what was logged is what was sent
    probed, _ = fake({})
    d = probe_audit.draft(lattice_with_clusters, probed, tmp_path, "jev-1.14.0", probes=True)
    assert d["draft"].endswith("draft-jev-1.14.0-pulls4-probes4-B1.json")
    first = json.loads((tmp_path / "draft-jev-1.14.0-pulls4-probes4-B1.json").read_text())["1"]
    assert "old_p" in first and "both_true" in first
    assert "auc" in probe_audit.score(tmp_path)["drafts"]["jev-1.14.0-pulls4-probes4-B1"]
    quotes, qsent = fake({})
    probe_audit.draft(lattice_with_clusters, quotes, tmp_path, "jev-1.14.0", quotes_only=True)
    assert all(not ({"summary", "my_summary"} & set(b["state"][s])) for b in qsent for s in ("a", "b"))


def test_the_route_decides_the_endpoint_key_and_model_name(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "or-key")
    monkeypatch.setenv("TYPESAFE_API_KEY", "ts-key")
    via = jev.Jev()
    assert (via.route, via.url, via.key, via.sent_as) == ("openrouter", "https://openrouter.ai/api/v1/systemone",
                                                          "or-key", "typesafe/jev-1.14")
    direct = jev.Jev(route="typesafe")
    assert (direct.url, direct.key, direct.sent_as) == ("https://api.typesafe.ai/v1/systemone", "ts-key",
                                                        "jev-1.14.0")
    monkeypatch.delenv("OPENROUTER_API_KEY")
    assert jev.Jev().route == "typesafe"


def test_probes_are_asked_beside_the_deciding_questions_without_changing_p():
    pulls = dict.fromkeys(jev.QUESTIONS["tension"], 0.6)
    client, sent = fake(pulls | {"contradiction": 0.95, "same_subject": 0.25, "both_true": 0.5, "both_true_v2": 0.3})
    p, _ = ask(client, "tension", {"a": {}, "b": {}}, probes=True)
    assert set(sent[0]["questions"]) == set(jev.QUESTIONS["tension"]) | set(jev.PROBES["tension"])
    assert p["p"] == pytest.approx(0.6)                    # the probes don't decide
    assert p["old_p"] == 0.95 and p["pulls_across_subjects"] == pytest.approx(0.45)
    assert p["pulls_not_both_true"] == pytest.approx(0.36)
    plain, sent2 = fake({})
    ask(plain, "tension", {"a": {}, "b": {}})
    assert set(sent2[0]["questions"]) == set(jev.QUESTIONS["tension"])


def test_an_answer_that_cant_be_read_is_logged_paid_for_and_asked_again_next_time(lattice_with_clusters,  # noqa: F811
                                                                                   tmp_path):
    probe_audit.write(lattice_with_clusters, FakeProposer(), None, "tension", tmp_path)
    n = len(json.loads((tmp_path / "key.json").read_text())["pairs"])
    client, sent = fake({}, broken=1)
    d = probe_audit.draft(lattice_with_clusters, client, tmp_path, "jev-1.14.0")
    assert d["pairs"] == n - 1 and [f["pair"] for f in d["failed"]] == [1]
    rows = lattice_with_clusters.execute("""SELECT error IS NOT NULL, cost_usd > 0 FROM im.egress_log
                                            WHERE model = 'jev-1.14.0' ORDER BY egress_id""").fetchall()
    assert len(rows) == len(sent) == n and rows[0] == (True, True)
    again, resent = fake({})
    assert probe_audit.draft(lattice_with_clusters, again, tmp_path, "jev-1.14.0")["pairs"] == n and len(resent) == 1
