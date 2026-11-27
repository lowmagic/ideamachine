"""B1: could Jev make the matcher's decisions? Opus's logged decisions, replayed through Jev (ROADMAP-v2 B1, M4).

The matcher (lattice.match_stage) shows Opus each new capture and the ideas nearest it. Matcher v1 answered new, the
same as one, or develops one; v2 answers, of each candidate, whether the capture makes the same point, and code joins
the nearest that does. Each request is logged with exactly what was sent. This reads them back, in either form, and
asks Jev about the capture against each candidate: is it the same idea, and does it develop it
(jev.QUESTIONS["match"], two wordings each). Scoring decides as matcher v1 would, at a grid of cut-offs: the same as
the likeliest candidate if that's likely enough, else developing the likeliest, else new. Agreement with Opus is B1's
first measure. Its bar also needs the `same_as` decisions checked by me, since agreeing with Opus only shows a switch
is safe where Opus was right.

A decision is replayed only if its request is still logged in full (forgetting blanks a payload). Which candidate
Opus chose is found by text: the idea it joined (or developed) as its text reads now, or as Claude first wrote it.
If neither reads like any candidate shown, as after a later correction, the decision counts for its kind only.

Requests go out under policy B, names said as said, each logged with the segments of the capture and of every
candidate it could be placed with. Jev's answers are kept in the folder (jev-answers.json), so a rerun picks up where
one stopped and scoring sends nothing.
"""

import json
import random
import re
from pathlib import Path

from im import egress, lattice, llm, probe_audit
from im.config import ModelConfig

ITEM = re.compile(r"^Item \((?P<kind>[^,]+), said by (?P<who>[^)]+)\): “(?P<quote>.*)” — (?P<gist>.*)$", re.S)
ITEM_V2 = re.compile(r"^New capture\ngist: (?P<gist>.*)\nreading: .*\nrelayed from: (?P<relayed>.*)\n"
                     r"quote \(evidence only\): (?P<quote>.*)$", re.S)
LINE = re.compile(r"^(\d+)\. (.*)$")
TITLE_V2 = re.compile(r"^(\d+)\. title(?: \(mine\))?: (.*)$")
FIELD_V2 = re.compile(r"^   (statement|my note)(?: \(mine\))?: (.*)$")
NOTE = " (my note: "
OWN = "nobody (my own)"
SAME, DEVELOPS = ("same", "same_v2"), ("develops", "develops_v2")
CUTS = (0.4, 0.5, 0.6, 0.7, 0.8)
PRECISION = 0.95   # B1's bar for `same_as`; the best cut-offs are the most agreement at or above it


def parse(payload: str) -> tuple[dict, list[dict]] | None:
    """The capture and the candidates as the matcher showed them (lattice._text, v1 or v2), or None if it can't be
    read. A candidate's text is "title: statement", as im.idea_text has it."""
    if payload.startswith("New capture\n"):
        return _parse_v2(payload)
    head, sep, rest = payload.partition("\n\nCandidates:\n")
    m = ITEM.match(head)
    if not sep or not m:
        return None
    candidates: list[dict] = []
    for line in rest.splitlines():
        n = LINE.match(line)
        if n and int(n.group(1)) == len(candidates) + 1:
            candidates.append({"text": n.group(2)})
        elif candidates:   # a statement that ran onto another line
            candidates[-1]["text"] += "\n" + line
    for c in candidates:
        if c["text"].endswith(")") and NOTE in c["text"]:
            c["text"], note = c["text"].rsplit(NOTE, 1)
            c["note"] = note[:-1]
    item = {"kind": m["kind"], "said_by": m["who"], "quote": m["quote"], "summary": m["gist"]}
    return item, candidates


def _parse_v2(payload: str) -> tuple[dict, list[dict]] | None:
    head, sep, rest = payload.partition("\n\nCandidates\n")
    m = ITEM_V2.match(head)
    if not sep or not m:
        return None
    candidates: list[dict] = []
    for line in rest.splitlines():
        if (t := TITLE_V2.match(line)) and int(t.group(1)) == len(candidates) + 1:
            candidates.append({"title": t.group(2)})
        elif candidates and (f := FIELD_V2.match(line)):
            candidates[-1]["statement" if f.group(1) == "statement" else "note"] = f.group(2)
    shown = [{"text": f"{c['title']}: {c.get('statement', '')}"} | ({"note": c["note"]} if "note" in c else {})
             for c in candidates]
    item = {"quote": m["quote"], "summary": m["gist"]} | ({} if m["relayed"] == OWN else {"relayed_from": m["relayed"]})
    return item, shown


def decisions(conn) -> list[dict]:
    """Every logged decision of the matcher that can be replayed, with which candidate Opus chose (None for new;
    "?" when it can't be told) and the ideas each candidate could be."""
    texts: dict[str, set] = {}
    for idea, title, statement, c_title, c_statement in conn.execute(
            "SELECT idea_id::text, title, statement, claude_title, claude_statement FROM im.idea_text"):
        for t in {f"{title}: {statement}", f"{c_title}: {c_statement}"}:
            texts.setdefault(t, set()).add(idea)
    out = []
    for egress_id, decision, idea, from_idea, payload, segs in conn.execute(
            """SELECT g.egress_id, m.decision, m.idea_id::text,
                      (SELECT l.to_id::text FROM im.lineage l WHERE l.from_id = m.idea_id AND l.kind = 'evolves' LIMIT 1),
                      g.payload, g.segment_ids::text[]
               FROM im.item_matches m JOIN im.egress_log g USING (egress_id)
               WHERE m.judged_by LIKE 'match-%%' AND m.decision IN ('new', 'same_as', 'evolves')
                 AND g.payload IS NOT NULL ORDER BY g.egress_id"""):
        shown = parse(payload)
        if shown is None or not shown[1]:
            continue
        item, candidates = shown
        for c in candidates:
            c["ideas"] = sorted(texts.get(c["text"], ()))
        target = {"same_as": idea, "evolves": from_idea}.get(decision)
        chosen = None if decision == "new" else next(
            (n for n, c in enumerate(candidates, 1) if target in c["ideas"]), "?")
        out.append({"egress_id": egress_id, "decision": decision, "chosen": chosen, "item": item,
                    "candidates": candidates, "segments": segs})
    return out


def run(conn, client, folder: Path, limit: int | None = None, monthly_cap_usd: float = 20.0) -> dict:
    """Ask Jev about each decision's capture against each of its candidates, "egress_id n" -> {question: p}."""
    folder.mkdir(parents=True, exist_ok=True)
    found = decisions(conn)[:limit] if limit else decisions(conn)
    (folder / "key.json").write_text(json.dumps({"benchmark": "B1", "decisions": found}, indent=1))
    jobs = {}
    for d in found:
        for n, c in enumerate(d["candidates"], 1):
            state = {"a": d["item"], "b": {"idea": c["text"], **({"my_note": c["note"]} if "note" in c else {})}}
            jobs[f"{d['egress_id']} {n}"] = (
                lambda s=state: s,
                lambda d=d, c=c: sorted(set(d["segments"]) | {str(s) for s in egress.segments_of(conn, c["ideas"])}))
    got = probe_audit.ask_all(conn, client, "match", jobs, folder / "jev-answers.json", monthly_cap_usd,
                              probes=False)
    return {"decisions": len(found), "asked": len(jobs), "answered": sum(k in got for k in jobs)}


def _mean(a: dict, names) -> float:
    return sum(a[k] for k in names) / len(names)


def decide(answers: dict[int, dict], cut_same: float, cut_develops: float) -> tuple[str, int | None]:
    """The matcher's decision from Jev's answers about each candidate n: the likeliest same if it's at the cut, else
    the likeliest development if it is, else new. Ties go to the nearer candidate, as the matcher lists them."""
    for kind, names, cut in (("same_as", SAME, cut_same), ("evolves", DEVELOPS, cut_develops)):
        p = {n: _mean(a, names) for n, a in answers.items()}
        best = max(p, key=lambda n: (p[n], -n), default=None)
        if best is not None and p[best] >= cut:
            return kind, best
    return "new", None


def score(folder: Path) -> dict:
    """The best pair of cut-offs in full; agreement and same_as precision at every pair, as two small tables (a row
    per cut-off on "same", a column per cut-off on "develops"); and how well each wording picks out the candidate
    Opus joined (AUC). Over the decisions Jev answered in full. Delegates to score_models for B1-v2 folders."""
    key = json.loads((folder / "key.json").read_text())
    if key.get("benchmark") == "B1-v2":
        return score_models(folder, key)
    got = json.loads((folder / "jev-answers.json").read_text())
    done = []
    for d in key["decisions"]:
        answers = {n: got.get(f"{d['egress_id']} {n}") for n in range(1, len(d["candidates"]) + 1)}
        if all(answers.values()):
            done.append((d, answers))
    grid = {(cs, cd): _agreement([(d, decide(a, cs, cd)) for d, a in done]) for cs in CUTS for cd in CUTS}
    best = max(grid, key=lambda k: ((grid[k]["same_as_precision"] or 0) >= PRECISION, grid[k]["agree"] or 0))
    table = lambda field: {"develops": "  ".join(f"{cd:4}" for cd in CUTS)} | {  # noqa: E731
        f"same {cs}": "  ".join("   -" if grid[cs, cd][field] is None else f"{grid[cs, cd][field]:.2f}" for cd in CUTS)
        for cs in CUTS}
    return {"benchmark": "B1", "decisions": len(key["decisions"]), "answered": len(done),
            "by_opus": {k: sum(d["decision"] == k for d, _ in done) for k in ("new", "same_as", "evolves")},
            "chosen_unknown": sum(d["chosen"] == "?" for d, _ in done),
            "best": {"same/develops": f"{best[0]}/{best[1]}", **grid[best]},
            "agree": table("agree"), "same_as_precision": table("same_as_precision"), "auc": _auc(done)}


def _agreement(pairs: list[tuple[dict, tuple]]) -> dict:
    """How often Jev's decision is Opus's: the kind, and the kind with the same candidate where that's known."""
    rate = lambda a, b: round(a / b, 3) if b else None  # noqa: E731
    kind = sum(d["decision"] == j[0] for d, j in pairs)
    known = [(d, j) for d, j in pairs if d["chosen"] != "?"]
    exact = sum(d["decision"] == j[0] and d["chosen"] == j[1] for d, j in known)
    jev_same = [(d, j) for d, j in known if j[0] == "same_as"]
    opus_same = [(d, j) for d, j in known if d["decision"] == "same_as"]
    hit = sum(d["decision"] == "same_as" and d["chosen"] == j[1] for d, j in jev_same)
    confusion: dict = {}
    for d, j in pairs:
        confusion.setdefault(d["decision"], {}).setdefault(j[0], 0)
        confusion[d["decision"]][j[0]] += 1
    return {"agree_kind": rate(kind, len(pairs)), "agree": rate(exact, len(known)),
            "same_as_precision": rate(hit, len(jev_same)), "same_as_recall": rate(hit, len(opus_same)),
            "jev_same_as": len(jev_same), "confusion": confusion}


def _auc(done: list[tuple[dict, dict]]) -> dict:
    """Per wording, the chance that the candidate Opus joined (or developed) scores above one it passed over, ties
    half: whether the wording picks out the right idea. Over decisions where Opus chose a known candidate."""
    out = {}
    for kind, names in (("same_as", SAME), ("evolves", DEVELOPS)):
        rows = [(d, a) for d, a in done if d["decision"] == kind and isinstance(d["chosen"], int)]
        for name in (*names, "mean"):
            p = (lambda a: _mean(a, names)) if name == "mean" else (lambda a, name=name: a[name])
            wins = total = 0
            for d, a in rows:
                yes = p(a[d["chosen"]])
                for n, other in a.items():
                    if n != d["chosen"]:
                        wins += (yes > p(other)) + 0.5 * (yes == p(other))
                        total += 1
            out[f"{kind}:{name}"] = round(wins / total, 3) if total else None
    return out


# --- Matcher v2 chat-model replay (B1-v2) ---------------------------------------------------------------------------

def _idea_texts(conn) -> dict[str, set[str]]:
    """Every known 'title: statement' mapped to the current idea_id(s) it belongs to, including merged-away ideas."""
    texts: dict[str, set[str]] = {}
    for idea, title, statement, c_title, c_statement in conn.execute(
            "SELECT idea_id::text, title, statement, claude_title, claude_statement FROM im.idea_text"):
        for t in {f"{title}: {statement}", f"{c_title}: {c_statement}"}:
            texts.setdefault(t, set()).add(idea)
    for idea, title, statement in conn.execute(
            """SELECT coalesce(mi.into_id, m.keep_id)::text, m.title, m.statement
               FROM im.idea_merges m LEFT JOIN im.merged_into mi ON mi.idea_id = m.keep_id"""):
        texts.setdefault(f"{title}: {statement}", set()).add(idea)
    return texts


def v2_decisions(conn) -> list[dict]:
    """Logged matcher v2 decisions whose full payloads remain in im.egress_log, without storing raw payloads."""
    texts = _idea_texts(conn)
    out = []
    for egress_id, decision, idea, payload, segs, cost_usd, model, title, statement in conn.execute(
            """SELECT g.egress_id, m.decision, m.idea_id::text, g.payload, g.segment_ids::text[],
                      g.cost_usd::float, g.model, d.title, d.statement
               FROM im.item_matches m
               JOIN im.egress_log g USING (egress_id)
               LEFT JOIN im.ideas d ON d.idea_id = m.idea_id
               WHERE m.judged_by LIKE 'match-2:%%' AND m.decision IN ('new', 'same_as')
                 AND g.payload IS NOT NULL AND g.payload LIKE 'New capture\n%%'
               ORDER BY g.egress_id"""):
        shown = parse(payload)
        if shown is None or not shown[1]:
            continue
        item, candidates = shown
        for c in candidates:
            c["ideas"] = sorted(texts.get(c["text"], ()))
        chosen = None if decision == "new" else next(
            (n for n, c in enumerate(candidates, 1) if idea in c["ideas"]), "?")
        out.append({
            "egress_id": egress_id,
            "decision": decision,
            "chosen": chosen,
            "opus_model": model,
            "opus_cost_usd": cost_usd,
            "opus_title": title if decision == "new" else None,
            "opus_statement": statement if decision == "new" else None,
            "item": item,
            "candidates": candidates,
            "segments": segs,
        })
    return out


def select_v2(all_decisions: list[dict], limit: int | None, seed: int = 1) -> tuple[list[dict], dict]:
    """Keep every decision, or a stratified sample keeping all `same_as` decisions first plus sampled `new` ones."""
    same = [d for d in all_decisions if d["decision"] == "same_as"]
    new = [d for d in all_decisions if d["decision"] == "new"]
    avail = {"same_as": len(same), "new": len(new)}
    if limit is None or limit >= len(all_decisions):
        picked = list(all_decisions)
    else:
        rng = random.Random(seed)
        if len(same) > limit:
            picked_same = rng.sample(same, limit)
            picked_new = []
        else:
            picked_same = same
            picked_new = rng.sample(new, min(len(new), limit - len(same)))
        picked = sorted(picked_same + picked_new, key=lambda d: d["egress_id"])
    meta = {
        "seed": seed,
        "selected": {"same_as": sum(d["decision"] == "same_as" for d in picked),
                     "new": sum(d["decision"] == "new" for d in picked)},
        "available": avail,
    }
    return picked, meta


def _validate_match_answer(answer: dict) -> dict:
    if not isinstance(answer, dict) or not isinstance(answer.get("judgments"), list) \
            or not isinstance(answer.get("title"), str) or not isinstance(answer.get("statement"), str):
        raise ValueError("answer missing judgments, title, or statement")
    for j in answer["judgments"]:
        if not isinstance(j, dict) or isinstance(j.get("candidate"), bool) \
                or not isinstance(j.get("candidate"), int) or not isinstance(j.get("same_point"), bool):
            raise ValueError("judgment missing candidate int or same_point bool")
    return answer


def run_model(conn, client, folder: Path, model: str, effort: str = "low", limit: int | None = None, seed: int = 1,
              monthly_cap_usd: float = 20.0, max_tokens: int | None = None) -> dict:
    """Replay logged matcher v2 requests through `model` at `effort`, honoring forgotten payloads and the cap."""
    folder.mkdir(parents=True, exist_ok=True)
    key_path = folder / "key.json"
    if key_path.exists():
        key = json.loads(key_path.read_text())
        if key.get("benchmark") != "B1-v2":
            raise SystemExit(f"{folder} holds a {key.get('benchmark')} benchmark, not B1-v2; pick another folder")
    else:
        found, meta = select_v2(v2_decisions(conn), limit, seed)
        key = {"benchmark": "B1-v2", "sampling": meta, "decisions": found}
        key_path.write_text(json.dumps(key, indent=1))

    pv = f"bench-match:{lattice.matcher_version(ModelConfig(model, effort))}"
    draft_path = folder / f"draft-{model.replace('/', '_')}-{effort}.json"
    if draft_path.exists():
        got = json.loads(draft_path.read_text())
        if got.get("prompt_version") != pv:
            raise SystemExit(f"{draft_path} was drafted under prompt {got.get('prompt_version')}, not {pv}: "
                             "a draft isn't resumed under another prompt")
    else:
        got = {"model": model, "effort": effort, "prompt_version": pv, "answers": {}}

    asked = skipped_forgotten = 0
    total_cost = 0.0
    failed: list[dict] = []
    stopped: str | None = None
    for d in key["decisions"]:
        eid = str(d["egress_id"])
        if eid in got["answers"]:
            continue
        row = conn.execute("SELECT payload FROM im.egress_log WHERE egress_id = %s AND payload IS NOT NULL",
                           (d["egress_id"],)).fetchone()
        if row is None:
            skipped_forgotten += 1
            continue
        payload = row[0]
        cost = 0.0
        asked_again = False
        a = None
        try:
            for attempt in range(2):
                a = egress.ask(conn, client, model, effort, lattice.MATCH_SYSTEM, lattice.MATCH_SCHEMA, payload,
                               segment_ids=d["segments"], prompt_version=pv, monthly_cap_usd=monthly_cap_usd,
                               max_tokens=max_tokens or 4000, read=_validate_match_answer)
                cost += a.cost_usd
                total_cost += a.cost_usd
                if a.failed or lattice._answer_ok(a.data, len(d["candidates"])):
                    break
                asked_again = True
        except llm.Stop as e:
            stopped = str(e)[:200]
            break
        asked += 1
        assert a is not None
        if a.failed:
            failed.append({"egress_id": d["egress_id"], "error": (a.error or "")[:300]})
            continue
        answer = a.data
        n_cand = len(d["candidates"])
        same = sorted({j["candidate"] for j in (answer or {"judgments": []})["judgments"]
                       if j["same_point"] and 1 <= j["candidate"] <= n_cand})
        if same:
            decision, chosen = "same_as", same[0]
            title = statement = raw_statement = ""
            fallback = False
        else:
            decision, chosen = "new", None
            raw_statement = answer["statement"].strip() if answer else ""
            fallback = (not raw_statement
                        or bool(lattice.NOT_A_STATEMENT.search(raw_statement))
                        or (bool(lattice.SOMEONE_SAYS.search(raw_statement)) and not d["item"].get("relayed_from")))
            statement = d["item"]["summary"] if fallback else raw_statement
            title = (answer["title"].strip() if answer else "") or lattice._title(statement)
        got["answers"][eid] = {
            "decision": decision,
            "chosen": chosen,
            "same": same,
            "possible_duplicate": len(same) > 1,
            "title": title,
            "statement": statement,
            "raw_statement": raw_statement,
            "statement_fallback": fallback,
            "refused": a.refused,
            "asked_again": asked_again,
            "cost_usd": round(cost, 6),
            "served_by": a.reply.served_by if a.reply else None,
        }
        draft_path.write_text(json.dumps(got, indent=1))
    draft_path.write_text(json.dumps(got, indent=1))
    return {
        "draft": str(draft_path),
        "model": model,
        "effort": effort,
        "prompt_version": pv,
        "decisions": len(key["decisions"]),
        "answered": len(got["answers"]),
        "asked": asked,
        "skipped_forgotten": skipped_forgotten,
        "cost_usd": round(total_cost, 6),
        **({"failed": failed} if failed else {}),
        **({"stopped": stopped} if stopped else {}),
    }


def _candidate_text(d: dict, chosen: int | str | None) -> str | None:
    if isinstance(chosen, int) and 1 <= chosen <= len(d["candidates"]):
        return d["candidates"][chosen - 1]["text"]
    return None


def _compare_markdown(folder: Path, name: str, model: str, effort: str, done: list[tuple[dict, dict]],
                      disagreements: list[dict]) -> Path:
    """Write a readable side-by-side sheet of disagreements and new cluster statements."""
    by_eid = {d["egress_id"]: (d, a) for d, a in done}
    lines = [f"# B1-v2 matcher comparison: {model} ({effort}) vs Opus", "",
             f"Answered: {len(done)} decisions. Disagreements: {len(disagreements)}.", "",
             f"## Disagreements ({len(disagreements)})", ""]
    for dis in disagreements:
        d, a = by_eid[dis["egress_id"]]
        lines += [f"### egress {dis['egress_id']}", "",
                  f"- Capture gist: {d['item'].get('summary', '')}",
                  f"- Quote: \"{d['item'].get('quote', '')}\"",
                  f"- Opus: `{d['decision']}`" + (f" (#{d['chosen']}: {dis['opus_candidate']})"
                                                 if dis["opus_candidate"] else ""),
                  f"- {model}: `{a['decision']}`" + (f" (#{a['chosen']}: {dis['model_candidate']})"
                                                     if dis["model_candidate"] else
                                                     f" (`{a['title']}`: {a['statement']})"), ""]
    both_new = [(d, a) for d, a in done if d["decision"] == "new" and a["decision"] == "new"]
    lines += [f"## Both new (showing {min(len(both_new), 25)} of {len(both_new)})", ""]
    for d, a in both_new[:25]:
        fb = " [fallback]" if a.get("statement_fallback") else ""
        lines += [f"### egress {d['egress_id']}", "",
                  f"- Capture gist: {d['item'].get('summary', '')}",
                  f"- Quote: \"{d['item'].get('quote', '')}\"",
                  f"- Opus: **{d.get('opus_title') or ''}** — {d.get('opus_statement') or ''}",
                  f"- {model}{fb}: **{a['title']}** — {a['statement']}", ""]
    out_path = folder / f"compare-{name}.md"
    out_path.write_text("\n".join(lines))
    return out_path


def score_models(folder: Path, key: dict) -> dict:
    """Score every chat-model replay draft in `folder` against Opus's logged matcher v2 decisions."""
    draft_paths = sorted(folder.glob("draft-*.json"))
    if not draft_paths:
        raise SystemExit(f"{folder} has no draft-*.json files yet")
    models = {}
    for d_path in draft_paths:
        got = json.loads(d_path.read_text())
        name = d_path.stem.removeprefix("draft-")
        answers = got.get("answers", {})
        done = [(d, answers[str(d["egress_id"])]) for d in key["decisions"] if str(d["egress_id"]) in answers]
        ag = _agreement([(d, (a["decision"], a["chosen"])) for d, a in done])
        ag["model_same_as"] = ag.pop("jev_same_as")
        both_new = [(d, a) for d, a in done if d["decision"] == "new" and a["decision"] == "new"]
        fallbacks = sum(bool(a.get("statement_fallback")) for _, a in both_new)
        served_by: dict[str, int] = {}
        for _, a in done:
            sb = a.get("served_by") or "unknown"
            served_by[sb] = served_by.get(sb, 0) + 1
        cost_usd = round(sum(float(a.get("cost_usd") or 0.0) for _, a in done), 6)
        ref_costs = [d["opus_cost_usd"] for d, _ in done if d.get("opus_cost_usd") is not None]
        ref_cost = round(sum(ref_costs), 6) if ref_costs else None
        disagreements = []
        for d, a in done:
            if d["decision"] != a["decision"] or (d["chosen"] != "?" and d["chosen"] != a["chosen"]):
                disagreements.append({
                    "egress_id": d["egress_id"],
                    "opus": {"decision": d["decision"], "chosen": d["chosen"]},
                    "model": {"decision": a["decision"], "chosen": a["chosen"]},
                    "item_summary": d["item"].get("summary", ""),
                    "opus_candidate": _candidate_text(d, d["chosen"]),
                    "model_candidate": _candidate_text(d, a["chosen"]),
                })
        md_path = _compare_markdown(folder, name, got.get("model", name), got.get("effort", ""), done, disagreements)
        models[name] = {
            "model": got.get("model"),
            "effort": got.get("effort"),
            "prompt_version": got.get("prompt_version"),
            "answered": len(done),
            "missing": len(key["decisions"]) - len(done),
            "by_opus": {k: sum(d["decision"] == k for d, _ in done) for k in ("new", "same_as")},
            "by_model": {k: sum(a["decision"] == k for _, a in done) for k in ("new", "same_as")},
            "chosen_unknown": sum(d["chosen"] == "?" for d, _ in done),
            **ag,
            "possible_duplicates": sum(bool(a.get("possible_duplicate")) for _, a in done),
            "refused": sum(bool(a.get("refused")) for _, a in done),
            "asked_again": sum(bool(a.get("asked_again")) for _, a in done),
            "served_by": dict(sorted(served_by.items())),
            "cost_usd": cost_usd,
            "reference_cost_usd": ref_cost,
            "cost_vs_reference": round(cost_usd / ref_cost, 3) if ref_cost else None,
            "cost_per_decision_usd": round(cost_usd / len(done), 6) if done else None,
            "statement_quality": {
                "both_new": len(both_new),
                "statement_fallbacks": fallbacks,
                "fallback_rate": round(fallbacks / len(both_new), 3) if both_new else None,
                "title_over_8_words": {
                    "candidate": sum(len((a.get("title") or "").split()) > 8 for _, a in both_new),
                    "opus": sum(len((d.get("opus_title") or "").split()) > 8 for d, _ in both_new),
                },
            },
            "disagreements": disagreements,
            "compare_md": str(md_path),
        }
    return {"benchmark": "B1-v2", "sampling": key.get("sampling"), "decisions": len(key["decisions"]), "models": models}
