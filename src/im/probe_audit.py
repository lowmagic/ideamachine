"""Sheets that ask one question of a pair of clusters: is there an analogy, or a tension? (decision 0009)

B4 drew its pairs from neighborhoods, so every analogy I found there was between ideas already close by topic,
and the spike that ranked them by structure couldn't say anything about analogies between unrelated ideas
(2026-11-04). And two tensions in 50 pairs can't choose a model. These sheets go looking: each set is a way of
proposing pairs, and a random set beside them says how often the answer is yes by chance.

Analogy (pairs from ideas below the median similarity by topic, so unrelated):
- jev: Jev answers every analogy question (jev.QUESTIONS and jev.PROBES) about a random sample of unrelated pairs,
  and each question proposes its best-scored pairs, so a sheet grades the questions as proposers;
- facets: a model writes each idea's structure as four lines, subject matter removed, so the one an analogy rests
  on is likelier to be there; pairs whose closest lines are closest;
- random: pairs drawn at random, the base rate.
Tension (any similarity):
- apart: neighbors first said far apart in time, where a change of mind would show;
- levels: a claim beside a nearby concrete observation;
- model: a model reads every idea at once and proposes pairs that pull against each other;
- near: neighbors drawn at random, the base rate among related ideas.

The sheet is blind and shuffled like B4's, and the mark is one word: yes, no or unsure. Each pair records which
sets proposed it, so the score says which ways of looking find what, and whether they find the same things. The
sheet quotes people: keep it, and the key, in saved/.
"""

import json
import random
import re
from pathlib import Path

import numpy as np

from im import connect, edge_audit, egress, llm

FACETS = 4
FACET_BATCH = 25
FACET_MODEL, FACET_EFFORT = "claude-opus-5-5", "low"
PICK_MODEL, PICK_EFFORT = "claude-sonnet-5-5", "medium"
PICKS = 30
PER_IDEA = 2          # an idea appears at most this many times in a proposed set, so one idea can't fill it
# The sets each sheet can draw (`--only` picks one), and what it draws by default. Facets left the analogy default
# after two sheets (2 of 25 held, against Jev's 3 of 25 on analogy-2): they cost a Claude request per batch of ideas.
SETS = {"analogy": ("jev", "facets", "random"), "tension": ("apart", "levels", "model", "near")}
SIZES = {"analogy": {"jev": 30, "random": 10},
         "tension": {"apart": 10, "levels": 10, "model": 10, "near": 10}}
# The jev set. Sonnet read every idea and proposed analogies (more than Opus did), but of the up to 30 it proposed,
# 1 was between unrelated ideas (2026-11-04). Jev can't read every idea at once; it answers about pairs, cheaply,
# so it reads a sample and ranks it.
JEV_POOL = 3000       # unrelated pairs Jev is asked about
TOP = 10              # a ranking's best pairs (a Jev wording's, the facets'), which it's counted as proposing
JEV_WORKERS = 8
JEV_SAVE = 100        # answers between saves of jev-pool.json
# Asked to describe a pair, not to find an analogy, so they propose nothing: every pool pair has different
# subjects, and a high only_genre is a no.
AXES = ("different_subjects", "different_domains", "only_genre", "surprising")
MARKS = {"new": "new", "known": "known", "k": "known", "yes": "yes", "y": "yes", "no": "no", "n": "no",
         "unsure": "unsure", "?": "unsure"}
# Discovery sheets (analogy, tension) are marked for whether a pair is worth seeing, not whether I'd have made it:
# the point is a system that surfaces what my own reading of the lattice wouldn't. `yes` (from before) counts as a
# relation that holds, novel or not.
HOLDS = ("new", "known", "yes")
FOUND = ("new", "known", "yes", "no")   # the marks that answer the question

QUESTION = {
    "analogy": ("Is there an analogy between A and B: different subjects, the same underlying structure (the same "
                "move, pattern or mechanism)?",
                "For new or known, name the shared structure in a few words in `note:`."),
    "tension": ("Do A and B pull against each other: a contradiction, a change of mind, a trade-off, or two pulls "
                "that would have to be balanced?",
                "For new or known, say in a few words where they pull in `note:`."),
}

HEADER = """# {title} (blind)

Each pair is two clusters from the lattice: what was said, then Claude's summary of it. Judge by what was said;
the summary can be wrong.

{question}

After `mark:`, write one of:
- `new`: it holds, and it shows me something I hadn't connected;
- `known` (`k`): it holds, but I already had it;
- `no`: it doesn't hold, or it's noise;
- `unsure` (`?`).

{note} Judge whether it's worth seeing, not whether I'd have made it: the point is what my own reading of the
lattice wouldn't surface. Mark it yourself, without a model's help. Then run `im audit-{command} score` on this
folder. "me" is me, "someone" anyone else.
"""

FACET_SYSTEM = """For each idea from my idea lattice, captured from my conversations, write its structure four \
ways, each in one line with the subject matter removed: the general pattern, mechanism or move it rests on, in \
abstract terms (an agent, an option, a cost, a system, a signal, a group), never the specific domain, people, \
products or things. An idea usually rests on more than one: give four different ones, the most central first. Two \
ideas from different domains that make the same move should share a line. Keep each under 20 words. Don't \
evaluate the ideas."""

FACET_SCHEMA = {
    "type": "object",
    "properties": {"ideas": {"type": "array", "items": {
        "type": "object", "properties": {"idea": {"type": "integer"},
                                         "structures": {"type": "array", "items": {"type": "string"}}},
        "required": ["idea", "structures"], "additionalProperties": False}}},
    "required": ["ideas"], "additionalProperties": False,
}

PICK_SYSTEM = {
    "tension": """Below are ideas from my idea lattice, captured from my conversations, numbered. Find pairs that \
pull against each other: a contradiction, a change of mind, a trade-off, or two pulls that would have to be \
balanced. For each, say in a few words where they pull. Propose up to {n}, best first. Don't evaluate the ideas.""",
}

PICK_SCHEMA = {
    "type": "object",
    "properties": {"pairs": {"type": "array", "items": {
        "type": "object", "properties": {"a": {"type": "integer"}, "b": {"type": "integer"},
                                         "why": {"type": "string"}},
        "required": ["a", "b", "why"], "additionalProperties": False}}},
    "required": ["pairs"], "additionalProperties": False,
}


def _summary(conn, idea_id: str) -> str:
    title, statement = conn.execute("SELECT title, statement FROM im.idea_text WHERE idea_id = %s",
                                    (idea_id,)).fetchone()
    return f"{title}: {statement}"


def _ask(conn, client, model: str, effort: str, system: str, schema: dict, text: str, ideas: list[str],
         monthly_cap_usd: float) -> dict | None:
    """One request, logged in the egress log with the segments of the clusters it shows: its answer, or None when
    it was refused. A request that failed stops the sheet before it's written, so a rerun can ask again rather than
    find a smaller sheet in the folder."""
    a = egress.ask(conn, client, model, effort, system, schema, text, segment_ids=egress.segments_of(conn, ideas),
                   prompt_version=llm.version(system, schema, model, effort), monthly_cap_usd=monthly_cap_usd)
    if a.failed:
        raise SystemExit(f"{model} failed ({a.error}); no sheet written, run it again")
    return a.data


def facets(conn, client, ids: list[str], folder: Path, monthly_cap_usd: float = 20.0) -> dict[str, list[str]]:
    """Each idea's structures, kept in facets.json as they come, so a rerun picks up where one stopped."""
    path = folder / "facets.json"
    out = json.loads(path.read_text()) if path.exists() else {}
    todo = [i for i in ids if i not in out]
    for start in range(0, len(todo), FACET_BATCH):
        batch = todo[start:start + FACET_BATCH]
        text = "Ideas:\n" + "\n".join(f"{n}. {_summary(conn, i)}" for n, i in enumerate(batch, 1))
        answer = _ask(conn, client, FACET_MODEL, FACET_EFFORT, FACET_SYSTEM, FACET_SCHEMA, text, batch,
                      monthly_cap_usd)
        for row in (answer or {}).get("ideas", []):
            if 1 <= row["idea"] <= len(batch) and row["structures"]:
                out[batch[row["idea"] - 1]] = row["structures"][:FACETS]
        path.write_text(json.dumps(out, indent=2))
    return out


def picks(conn, client, question: str, ids: list[str], n: int = PICKS, monthly_cap_usd: float = 20.0) -> list[dict]:
    """A model's proposed pairs, reading every idea at once: [{"ideas": [a, b], "why": ...}], a < b."""
    text = "Ideas:\n" + "\n".join(f"{k}. {_summary(conn, i)}" for k, i in enumerate(ids, 1))
    answer = _ask(conn, client, PICK_MODEL, PICK_EFFORT, PICK_SYSTEM[question].format(n=n), PICK_SCHEMA, text, ids,
                  monthly_cap_usd)
    out, seen = [], set()
    for p in (answer or {"pairs": []})["pairs"]:
        if 1 <= p["a"] <= len(ids) and 1 <= p["b"] <= len(ids) and p["a"] != p["b"]:
            pair = tuple(sorted((ids[p["a"] - 1], ids[p["b"] - 1])))
            if pair not in seen:
                seen.add(pair)
                out.append({"ideas": list(pair), "why": p["why"]})
    return out


def unrelated(rows: list[tuple]) -> list[dict]:
    """Pairs of my clusters below the median similarity by topic, as {"ideas": [a, b], "similarity": s}, a < b."""
    mine = [r for r in rows if r[2]]
    if len(mine) < 2:
        return []
    M = np.vstack([np.array(json.loads(r[1])) for r in mine])
    M = M / np.linalg.norm(M, axis=1, keepdims=True)
    S = M @ M.T
    upper = np.triu_indices(len(mine), 1)
    median = float(np.median(S[upper]))
    return [{"ideas": sorted([mine[i][0], mine[j][0]]), "similarity": round(float(S[i, j]), 3)}
            for i, j in zip(*upper) if S[i, j] < median]


def _facet_pairs(embedder, structures: dict[str, list[str]], far: set[tuple]) -> list[dict]:
    """Unrelated pairs, closest first by their closest pair of structure lines."""
    ids = sorted(structures)
    lines = [(i, s) for i in ids for s in structures[i]]
    V = embedder.embed([s for _, s in lines])
    owner = np.array([ids.index(i) for i, _ in lines])
    S = V @ V.T
    best: dict[tuple, tuple] = {}
    for x in range(len(lines)):
        for y in np.argsort(-S[x])[:50]:   # each line's nearest lines are enough to find each pair's closest
            a, b = ids[owner[x]], ids[owner[y]]
            pair = tuple(sorted((a, b)))
            if a != b and pair in far and S[x, y] > best.get(pair, (-2,))[0]:
                best[pair] = (float(S[x, y]), lines[x][1], lines[y][1])
    return [{"ideas": list(p), "score": round(s, 3), "why": f"{la} | {lb}"}
            for p, (s, la, lb) in sorted(best.items(), key=lambda kv: -kv[1][0])]


def jev_pool(conn, client, pairs: list[dict], folder: Path,
             monthly_cap_usd: float = 20.0) -> dict[str, dict]:
    """Jev's answer to every analogy question about each pair, "a b" -> {question: p}, kept in jev-pool.json as they
    come (ask_all)."""
    sides: dict = {}

    def side(i):
        if i not in sides:
            sides[i] = connect.side_data(conn, i)
        return sides[i]

    def state(p):
        return {"a": side(p["ideas"][0]), "b": side(p["ideas"][1])}

    jobs = {" ".join(p["ideas"]): (lambda p=p: state(p),
                                   lambda p=p: p.get("segments") or egress.segments_of(conn, p["ideas"]))
            for p in pairs}
    return ask_all(conn, client, "analogy", jobs, folder / "jev-pool.json", monthly_cap_usd)


def ask_all(conn, client, question: str, jobs: dict, path: Path, monthly_cap_usd: float,
            probes: bool = True) -> dict[str, dict]:
    """Jev's answer to `question` (and its probes) for every job, key -> {name: p}, JEV_WORKERS requests at a time,
    each logged by its worker as it's answered. `jobs` is key -> (the state, its segment ids), each a function, so
    a key already answered costs nothing to skip. Kept in `path` as they come, so a rerun picks up where one stopped.
    Anything that stops it (the cap, a request Jev can't answer now, Ctrl-C, the database) sends nothing more: the
    queue is dropped, and the answers to requests already in flight are kept like the rest. Only a logged answer is
    kept. An answer that can't be read stops it once the rest are in, so a rerun asks that key again rather than
    going on without it."""
    from concurrent.futures import ThreadPoolExecutor, as_completed

    got = json.loads(path.read_text()) if path.exists() else {}
    todo = [(key, state(), segs()) for key, (state, segs) in jobs.items() if key not in got]
    kept, failed = set(), []

    def keep(f):
        a = f.result()
        if a.failed:
            failed.append(a.error)
        else:
            got[futures[f]] = {k: round(v, 3) for k, v in a.data.items()}
        kept.add(f)

    workers = ThreadPoolExecutor(JEV_WORKERS)
    futures = {workers.submit(_ask_jev, conn, client, question, state, probes, segs, monthly_cap_usd): key
               for key, state, segs in todo}
    try:
        for n, f in enumerate(as_completed(futures), 1):
            keep(f)
            if n % JEV_SAVE == 0:
                path.write_text(json.dumps(got))
    finally:
        workers.shutdown(wait=False, cancel_futures=True)   # nothing queued is sent
        try:   # what was already sent: wait for it (its worker logs it), and keep what it answered
            for f in futures:
                if f not in kept and not f.cancelled() and f.exception() is None:
                    keep(f)
        finally:
            workers.shutdown(wait=True)
            path.write_text(json.dumps(got))
    if failed:
        raise SystemExit(f"{len(failed)} of Jev's answers couldn't be read (first: {failed[0]}); run it again to ask "
                         "those again")
    return got


def _wordings(answer: dict[str, float]) -> dict[str, float]:
    """A pool answer's ways of asking whether there's an analogy: not the axes that only describe a pair, nor what's
    computed from the answers (jev.COMBOS, and `p`)."""
    from im import jev

    return {k: v for k, v in answer.items() if k not in AXES and k not in jev.COMBOS["analogy"] and k != "p"}


def _jev_proposals(answers: dict[str, dict]) -> tuple[list[dict], dict[str, set]]:
    """The pool ranked by the mean of the wordings; and each wording's top TOP, as `jev:<wording>`, to keep scoring
    them. On analogy-2 (2026-11-06) the mean ranked my analogies above the rest as well as the best single wording
    did (AUC 0.92 against 0.955, a difference of 2 in 66), and the pairs most wordings put in their top 10 held 2 of
    5, those in 2 or fewer 1 of 14: no one wording has to be picked. Ties go by pair, so the order the answers came
    back in can't change the sheet."""
    mean = {pair: sum(w.values()) / len(w) for pair, a in answers.items() if (w := _wordings(a))}
    out = [{"ideas": pair.split(), "why": f"Jev mean {mean[pair]:.3f}"}
           for pair in sorted(mean, key=lambda pair: (-mean[pair], pair))]
    names = sorted(_wordings(next(iter(answers.values()), {})))
    top = lambda k: sorted(answers, key=lambda pair: (-answers[pair][k], pair))[:TOP]  # noqa: E731
    return out, {f"jev:{k}": {tuple(pair.split()) for pair in top(k)} for k in names}


def _limited(pairs: list[dict], n: int, taken: set) -> list[dict]:
    """The first n pairs not already taken, with no idea more than PER_IDEA times."""
    out, uses = [], {}
    for p in pairs:
        if len(out) >= n:
            break
        if tuple(p["ideas"]) in taken or any(uses.get(i, 0) >= PER_IDEA for i in p["ideas"]):
            continue
        out.append(p)
        taken.add(tuple(p["ideas"]))
        for i in p["ideas"]:
            uses[i] = uses.get(i, 0) + 1
    return out


def earlier(folders: list[Path]) -> set[tuple]:
    """The pairs already on these sheets, so a new one asks about new pairs."""
    return {tuple(p["ideas"]) for f in folders for p in json.loads((f / "key.json").read_text())["pairs"].values()}


def build(conn, client, embedder, question: str, folder: Path, seed: int = 9, only: str | None = None,
          size: int | None = None, exclude: set | None = None, jev=None,
          pool_size: int = JEV_POOL, monthly_cap_usd: float = 20.0) -> tuple[str, dict]:
    """`only` draws every pair from one set (e.g. `model`, the one that found tensions), `size` of them; `exclude`
    leaves out pairs already asked about. `jev` (a jev.Jev) is for the analogy sheet's jev set."""
    rng = random.Random(seed)
    rows = edge_audit._clusters(conn)
    mine = [r[0] for r in rows if r[2]]
    pool = edge_audit.candidates(conn) if question == "tension" else {}
    sizes = {only: size or 30} if only else SIZES[question]
    if only and only not in SETS[question]:
        raise SystemExit(f"--only is one of {', '.join(SETS[question])}")
    wanted = lambda kind: kind in sizes  # noqa: E731
    n_picks = max(PICKS, 2 * sizes.get("model", 0) + len(exclude or ()))   # enough left after the excluded
    proposed: dict[str, list[dict]] = {}
    tops: dict[str, set] = {}
    if question == "analogy":
        apart = unrelated(rows)
        far = {tuple(p["ideas"]) for p in apart}
        if wanted("facets"):
            proposed["facets"] = _facet_pairs(embedder, facets(conn, client, mine, folder, monthly_cap_usd),
                                              far)
            tops["facets"] = {tuple(p["ideas"]) for p in proposed["facets"][:TOP]}
        if wanted("jev"):
            if jev is None:
                raise SystemExit("the jev set needs Jev (OPENROUTER_API_KEY)")
            sample = [p for p in apart if tuple(p["ideas"]) not in (exclude or set())]
            answers = jev_pool(conn, jev, rng.sample(sample, min(pool_size, len(sample))), folder,
                               monthly_cap_usd)
            proposed["jev"], by_question = _jev_proposals(answers)
            tops |= by_question
        if wanted("random"):
            proposed["random"] = rng.sample(apart, min(len(apart), 20 * sizes["random"]))
    else:
        for kind in ("apart", "levels", "near"):
            if wanted(kind):
                proposed[kind] = rng.sample(pool[kind], len(pool[kind]))
        if wanted("model"):
            proposed["model"] = picks(conn, client, "tension", mine, n_picks, monthly_cap_usd)
    taken, picked = set(exclude or ()), []
    for kind, n in sizes.items():
        picked += [p | {"set": kind} for p in _limited(proposed[kind], n, taken)]
    lists = {kind: {tuple(p["ideas"]) for p in ps} for kind, ps in proposed.items()
             if kind not in ("random", "near", "jev", "facets")} | tops   # ranked ways: only their top TOP
    for p in picked:   # which ways of looking proposed it, beyond the set it was drawn for
        p["proposed_by"] = sorted(k for k, s in lists.items() if tuple(p["ideas"]) in s)
    rng.shuffle(picked)
    for p in picked:
        p["sides"] = rng.sample(p["ideas"], 2)
    title = {"analogy": "Analogies", "tension": "Tensions"}[question]
    command = {"analogy": "analogies", "tension": "tension"}[question]
    ask, note = QUESTION[question]
    lines = [HEADER.format(title=title, question=ask, note=note, command=command)]
    for n, p in enumerate(picked, 1):
        lines += [f"## {n}", ""]
        for side, idea in zip("AB", p["sides"]):
            lines += [*connect.shown(connect.side_data(conn, idea), side), ""]
        lines += ["mark: ", "note: ", ""]
    key = {"benchmark": question, "seed": seed, "sizes": sizes, "excluded": len(exclude or ()),
           "available": {k: len(v) for k, v in proposed.items()},
           **({"jev_pool": len(answers)} if "jev" in proposed else {}),
           "pairs": {str(n): p for n, p in enumerate(picked, 1)}}
    return "\n".join(lines), key


def write(conn, client, embedder, question: str, out: Path, seed: int = 9, only: str | None = None,
          size: int | None = None, exclude_folders: list[Path] = (), jev=None,
          pool_size: int = JEV_POOL, monthly_cap_usd: float = 20.0) -> dict:
    out.mkdir(parents=True, exist_ok=True)
    sheet, key = out / "sheet.md", out / "key.json"
    if sheet.exists() or key.exists():
        raise SystemExit(f"{out} already has a sheet; score it, or pick another folder")
    text, k = build(conn, client, embedder, question, out, seed, only, size, earlier(list(exclude_folders)), jev,
                    pool_size, monthly_cap_usd)
    sheet.write_text(text)
    key.write_text(json.dumps(k, indent=2))
    sets = [p["set"] for p in k["pairs"].values()]
    return {"sheet": str(sheet), "pairs": len(sets), "by_set": {s: sets.count(s) for s in k["sizes"]},
            "available": k["available"]}


DRAFT_SYSTEM = {
    "analogy": """Two clusters from my idea lattice, captured from my conversations. Each side shows what was said, \
then a summary of it; judge by what was said, since the summary can be wrong. "me" is me, "someone" anyone else. \
Is there an analogy between them: different subjects, the same underlying structure (the same move, pattern or \
mechanism in two domains)? Similar topics aren't an analogy. Answer yes, no or unsure, and if yes, name the shared \
structure in a few words. Don't evaluate the ideas.""",
    "tension": """Two clusters from my idea lattice, captured from my conversations. Each side shows what was said, \
then a summary of it; judge by what was said, since the summary can be wrong. "me" is me, "someone" anyone else. \
Do they pull against each other: a contradiction, a change of mind, a trade-off, or two pulls that would have to be \
balanced? Being about the same subject isn't a tension. Answer yes, no or unsure, and if yes, say in a few words \
where they pull. Don't evaluate the ideas.""",
}
DRAFT_SYSTEM["summary"] = """One capture from my conversations: the transcript lines it cites, the quote it took, and a \
summary written for it. "me" is me, "someone" anyone else. Does the summary claim anything those lines don't say: \
an inference, a conclusion, a motive or an outcome that isn't there? Wording that compresses what was said is \
fine; adding to it isn't. Answer yes (it adds), no (faithful) or unsure, and if yes, say in a few words what it \
adds."""
DRAFT_SCHEMA = {
    "type": "object",
    "properties": {"answer": {"type": "string", "enum": ["yes", "no", "unsure"]}, "why": {"type": "string"}},
    "required": ["answer", "why"], "additionalProperties": False,
}


# On its own line under the quotes, or straight after "A." when nothing said is on record: the label stays.
SUMMARY_LINE = re.compile(r"^(\s*(?:[AB]\.\s*)?)Claude's summary:.*$", re.M)
QUOTES_ONLY = ("Each side shows what was said, then a summary of it; judge by what was said, since the summary can "
               "be wrong.", "Each side shows what was said.")


def _state(conn, question: str, p: dict, quotes_only: bool) -> dict:
    """What Jev is asked about, as named fields: the two clusters, or a capture's lines, quote and summary."""
    if question == "summary":
        lines = conn.execute(
            """SELECT s.speaker_kind = 'owner', s.text FROM unnest(%s::uuid[]) WITH ORDINALITY u(id, n)
               JOIN im.source_segments s ON s.segment_id = u.id ORDER BY u.n""", (p["segments"],)).fetchall()
        return {"lines": [{"by": "me" if me else "someone", "words": t} for me, t in lines],
                "quote": p["quote"], "summary": p["gist"]}
    sides = {}
    for name, idea in zip("ab", p["sides"]):
        side = connect.side_data(conn, idea)
        sides[name] = {k: v for k, v in side.items() if not (quotes_only and k in ("summary", "my_summary"))}
    return sides


def _ask_jev(conn, client, question: str, state: dict, probes: bool, segment_ids,
             monthly_cap_usd: float) -> egress.Answer:
    """One Jev request by egress.send, logged with the segments of what it showed and versioned by the questions it
    asked (the deciding ones, and the probes when they went too); its data is the probabilities (jev.read)."""
    from im import jev

    body = client.body(question, state, probes)
    asked = json.dumps(body["questions"], sort_keys=True)
    return egress.send(conn, lambda: client.complete(body), model=client.model, payload=json.dumps(body),
                       segment_ids=segment_ids, prompt_version=llm.version(asked, {}, client.model, ""),
                       monthly_cap_usd=monthly_cap_usd, read=lambda r: jev.read(question, r, probes))


def _draft_jev(conn, client, folder: Path, key: dict, quotes_only: bool, probes: bool,
               monthly_cap_usd: float) -> dict:
    """Jev drafts the sheet from structured state, one request of atomic Nouls per pair, combined in code."""
    from im import jev

    question = key["benchmark"]
    asked = "".join(["-" + jev.QUESTION_SETS[question] if question in jev.QUESTION_SETS else "",
                     "-" + jev.PROBE_SET[question] if probes and question in jev.PROBE_SET else ""])
    out_path = folder / (f"draft-{client.model}{asked}{edge_audit.policy_tag()}"
                         f"{'-quotes' if quotes_only else ''}.json")
    got = json.loads(out_path.read_text()) if out_path.exists() else {}
    cost, failed, stopped = 0.0, [], None
    for num, p in sorted(key["pairs"].items(), key=lambda kv: int(kv[0])):
        if num in got:
            continue
        state = _state(conn, question, p, quotes_only)
        try:
            a = _ask_jev(conn, client, question, state, probes,
                         p.get("segments") or egress.segments_of(conn, p["ideas"]), monthly_cap_usd)
        except llm.Stop as e:
            stopped = str(e)[:200]
            break
        cost += a.cost_usd
        if a.failed:   # logged; asked again by a rerun
            failed.append({"pair": int(num), "error": a.error[:300]})
            continue
        got[num] = {"answer": jev.answer(question, a.data["p"]), "why": "",
                    **{k: round(v, 3) for k, v in a.data.items()}}
        out_path.write_text(json.dumps(got, indent=2))
    return {"draft": str(out_path), "pairs": len(got), "cost_usd": round(cost, 6),
            **({"failed": failed} if failed else {}), **({"stopped": stopped} if stopped else {})}


def draft(conn, client, folder: Path, model: str, effort: str = "low", quotes_only: bool = False,
          probes: bool = False, monthly_cap_usd: float = 20.0) -> dict:
    """A model answers the sheet's question for every pair, blind to my marks, into
    draft-<model>-<effort>-<policy>.json (edge_audit.policy_tag), as it goes. Every request goes to the egress log.
    A pair whose answer fails is logged and skipped, so a rerun asks again; the cap stops the draft.
    `quotes_only` leaves out Claude's summaries (into ...-quotes.json): a summary that says more than the quotes
    can manufacture the very relation asked about."""
    key = json.loads((folder / "key.json").read_text())
    if model.startswith("jev"):
        return _draft_jev(conn, client, folder, key, quotes_only, probes, monthly_cap_usd)
    question = key["benchmark"]
    shown = edge_audit._pairs_as_shown((folder / "sheet.md").read_text())
    out_path = folder / (f"draft-{model.replace('/', '_')}-{effort}{edge_audit.policy_tag()}"
                         f"{'-quotes' if quotes_only else ''}.json")
    got = json.loads(out_path.read_text()) if out_path.exists() else {}
    system = DRAFT_SYSTEM[question].replace(*QUOTES_ONLY) if quotes_only else DRAFT_SYSTEM[question]
    if quotes_only:
        shown = {n: SUMMARY_LINE.sub(lambda m: m.group(1) + ("(nothing said on record)" if m.group(1).strip() else ""),
                                     t) for n, t in shown.items()}
    cost, failed, stopped = 0.0, [], None
    for num, p in sorted(key["pairs"].items(), key=lambda kv: int(kv[0])):
        if num in got or num not in shown:
            continue
        try:
            a = egress.ask(conn, client, model, effort, system, DRAFT_SCHEMA, shown[num], max_tokens=4000,
                           # a summary sheet's item cites its own lines
                           segment_ids=p.get("segments") or egress.segments_of(conn, p["ideas"]),
                           prompt_version=llm.version(system, DRAFT_SCHEMA, model, effort),
                           monthly_cap_usd=monthly_cap_usd)
        except llm.Stop as e:
            stopped = str(e)[:200]
            break
        cost += a.cost_usd
        if a.failed:
            failed.append({"pair": int(num), "error": a.error[:300]})
            continue
        if a.refused:
            got[num] = {"answer": "unsure", "why": "", "refused": True}
        else:
            got[num] = a.data
        out_path.write_text(json.dumps(got, indent=2))
    return {"draft": str(out_path), "pairs": len(got), "cost_usd": round(cost, 4),
            **({"failed": failed} if failed else {}), **({"stopped": stopped} if stopped else {})}


def agreement(mine: dict, theirs: dict) -> dict:
    """A model's yes against mine (new, known or yes: it holds), on the pairs we both answered. For a discovery
    sheet, also what it keeps of what was new to me: a filter that drops those is filtering out the point."""
    pairs = [n for n, m in mine.items() if m in FOUND and theirs.get(n, {}).get("answer") in ("yes", "no")]
    mine_yes = {n for n in pairs if mine[n] in HOLDS}
    mine_new = {n for n in pairs if mine[n] == "new"}
    their_yes = {n for n in pairs if theirs[n]["answer"] == "yes"}
    ratio = lambda a, b: round(a / b, 3) if b else None  # noqa: E731
    novelty = {"recall_new": ratio(len(mine_new & their_yes), len(mine_new)),
               "precision_new": ratio(len(mine_new & their_yes), len(their_yes)),
               "new_dropped": sorted(int(n) for n in mine_new - their_yes)} if mine_new else {}
    return {"pairs": len(pairs), "agree": ratio(sum((n in mine_yes) == (n in their_yes) for n in pairs), len(pairs)),
            **novelty,
            "precision": ratio(len(mine_yes & their_yes), len(their_yes)),
            "recall": ratio(len(mine_yes & their_yes), len(mine_yes)),
            "found": sorted(int(n) for n in mine_yes & their_yes),
            "invented": sorted(int(n) for n in their_yes - mine_yes),
            "missed": sorted(int(n) for n in mine_yes - their_yes)}


CUTS = (0.4, 0.45, 0.5, 0.6, 0.7)


def auc(mine: dict, theirs: dict) -> dict:
    """For a draft that kept each question's probability: how well each one (and each combination) ranks what I
    marked as holding above what I marked no: the chance that a random one of mine scores higher than a random no,
    ties half. 0.5 is chance. Over every pair I answered, so nothing is set aside."""
    pairs = [n for n, m in mine.items() if m in FOUND and n in theirs]
    yes, no = [n for n in pairs if mine[n] in HOLDS], [n for n in pairs if mine[n] == "no"]
    if not yes or not no:
        return {}
    keys = sorted({k for n in pairs for k, v in theirs[n].items() if isinstance(v, (int, float))
                   and not isinstance(v, bool)})
    out = {}
    for k in keys:
        if not all(k in theirs[n] for n in pairs):
            continue
        wins = sum((theirs[a][k] > theirs[b][k]) + 0.5 * (theirs[a][k] == theirs[b][k]) for a in yes for b in no)
        out[k] = round(wins / (len(yes) * len(no)), 3)
    return dict(sorted(out.items(), key=lambda kv: -kv[1]))


def at_cuts(mine: dict, theirs: dict) -> dict:
    """For a draft that kept its probabilities (Jev): precision and recall over every pair I answered, reading yes
    at each cut-off, so nothing is set aside as unsure. A model that's often unsure is compared fairly this way."""
    pairs = [n for n, m in mine.items() if m in FOUND and "p" in theirs.get(n, {})]
    mine_yes = {n for n in pairs if mine[n] in HOLDS}
    mine_new = {n for n in pairs if mine[n] == "new"}
    ratio = lambda a, b: round(a / b, 3) if b else None  # noqa: E731
    out = {}
    for cut in CUTS:
        yes = {n for n in pairs if theirs[n]["p"] >= cut}
        out[str(cut)] = {"flagged": len(yes), "precision": ratio(len(yes & mine_yes), len(yes)),
                         "recall": ratio(len(yes & mine_yes), len(mine_yes)),
                         **({"recall_new": ratio(len(yes & mine_new), len(mine_new))} if mine_new else {})}
    return {"pairs": len(pairs), "cuts": out}


def marks(text: str) -> dict:
    """Pair number -> {"mark", "note"}, or {"unreadable"} for a mark that isn't yes, no or unsure. A note runs to the
    next pair (edge_audit.read_sheet)."""
    out = {}
    for n, (_, mark, note) in edge_audit.read_sheet(text).items():
        got = {}
        if mark and (m := re.match(r"`?([a-z?]+)`?", mark, re.I)):
            word = MARKS.get(m.group(1).lower())
            got["mark" if word else "unreadable"] = word or mark
        if note:
            got["note"] = note
        if got:
            out[n] = got
    return out


def score(folder: Path) -> dict:
    """Per set, how often a pair held (`hit_rate`) and how often it was new to me (`novel_rate`); per Jev question
    that proposed pairs (`by_question`), the same of the ones on the sheet; which ways of looking proposed each one
    that held; and my notes."""
    key = json.loads((folder / "key.json").read_text())
    got = marks((folder / "sheet.md").read_text())
    by_set = {s: {"new": 0, "known": 0, "yes": 0, "no": 0, "unsure": 0} for s in key["sizes"]}
    yes, missing, unreadable, by_question = [], [], [], {}
    for num, p in sorted(key["pairs"].items(), key=lambda kv: int(kv[0])):
        m = got.get(num, {})
        if "mark" not in m:
            (unreadable.append({"pair": int(num), "mark": m["unreadable"]}) if "unreadable" in m
             else missing.append(int(num)))
            continue
        by_set[p["set"]][m["mark"]] += 1
        if m["mark"] in FOUND:
            for way in p.get("proposed_by", []):
                if way.startswith("jev:"):
                    c = by_question.setdefault(way.removeprefix("jev:"), {"pairs": 0, "held": 0, "new": 0})
                    c["pairs"] += 1
                    c["held"] += m["mark"] in HOLDS
                    c["new"] += m["mark"] == "new"
        if m["mark"] in HOLDS:
            yes.append({"pair": int(num), "mark": m["mark"], "set": p["set"],
                        "also_proposed_by": p.get("proposed_by", []),
                        "note": m.get("note"), "why_proposed": p.get("why")})
    for c in by_set.values():
        answered = sum(c[k] for k in FOUND)
        c["hit_rate"] = round(sum(c[k] for k in HOLDS) / answered, 3) if answered else None
        if key["benchmark"] in QUESTION:   # discovery sheets; a summary or misses sheet asks something else
            c["novel_rate"] = round(c["new"] / answered, 3) if answered else None
        for k in ("new", "known", "yes"):   # only the marks this sheet uses
            if not c[k]:
                del c[k]
    for c in by_question.values():
        c["hit_rate"] = round(c["held"] / c["pairs"], 3)
    by_question = dict(sorted(by_question.items(), key=lambda kv: (-kv[1]["hit_rate"], -kv[1]["pairs"])))
    mine = {n: m["mark"] for n, m in got.items() if "mark" in m}
    drafts = {}
    for d in sorted(folder.glob("draft-*.json")):
        theirs = json.loads(d.read_text())
        drafts[d.stem.removeprefix("draft-")] = agreement(mine, theirs) | (
            {"over_every_pair": at_cuts(mine, theirs), "auc": auc(mine, theirs)}
            if any("p" in t for t in theirs.values()) else {})
    return {"benchmark": key["benchmark"], "marked": sum(sum(c.get(k, 0) for k in (*FOUND, "unsure"))
                                                         for c in by_set.values()),
            "missing": missing, **({"unreadable": unreadable} if unreadable else {}), "by_set": by_set,
            **({"by_question": by_question} if by_question else {}), **({"drafts": drafts} if drafts else {}),
            "yes": yes}
