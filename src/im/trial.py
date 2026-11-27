"""Trying a candidate extraction model or prompt against a reference, before switching to it (B3, decision 0008).

Every model reads the same sample of current episodes (ordered by a hash of their id, so a sample of 100 is the
first 100 of a sample of 200), through the same egress path as extraction: logged, counted toward the monthly cap,
and under policy B. What it captures is kept in im.prompt_trials, never in im.items, so it reaches nothing.

The report scores a candidate against a reference's read of the same payloads (Opus, normally): the share of the
reference's captures it also found, matched the way verdicts are carried forward (overlapping segments,
`quote_score`), and what it found that the reference didn't. My keeps aren't the target: capture is the soup, and
the lattice's relationships decide what matters. Which of two models captures better where they differ is what
the blind pairs are for.

Recall is also split by how far each reference capture's idea reaches in the live lattice: the captures on the same
lines in live extraction, and the number of conversations their cluster has been said in. Missing a capture whose
idea recurs costs something; missing a one-off mostly saves matching and noise. Captures live extraction didn't
place in a cluster are counted apart.

A replicate (`replicate=2`) is a second, independent read by the same model and prompt, filed apart. The reference
against its own replicate is the ceiling: models don't capture exactly the same things twice, so a candidate's
recall means something only next to it.

Quote matching misses the same thought quoted from a neighboring line: on v5 (2026-11-09) every model, Opus against
itself included, "missed" a capture it had made from the next line. `same_point` asks Jev, of each capture only one
of them made against each only the other made in the same episode, whether they make the same point (two wordings of
jev.QUESTIONS["match"]'s `same`, averaged); its answers are kept, and the report then counts a pair at SAME_CUT or
above as found, best first, each capture once.
"""

import hashlib
import json
import re
from pathlib import Path

from im import egress, extract, jev, llm
from im.feedback import CARRY_MIN_SCORE, quote_score

SAMPLE = 150
EXAMPLES = 15
KEEP = ("said_by", "attributed_to", "quote", "reading", "mishearings", "gist", "checks")
SAME = ("same", "same_v2")
SAME_CUT = jev.YES   # not yet set from marks: Jev's own threshold for a confident yes


def sample(conn, n: int = SAMPLE, seed: int | None = None, exclude=()) -> list:
    """The first n current episodes I speak in, in a fixed pseudo-random order: the same ones for every model.
    Extraction never reads an `others_only` episode, so there's no live read to compare with. A `seed` gives another
    order (still a prefix order); `exclude` leaves out episodes (held_out: the ones an audit sheet showed)."""
    return [eid for (eid,) in conn.execute(
        """SELECT episode_id FROM im.episodes WHERE current AND kind <> 'others_only' AND episode_id <> ALL(%s::uuid[])
           ORDER BY md5(coalesce(%s::text || ':', '') || episode_id::text) LIMIT %s""",
        ([str(e) for e in exclude], None if seed is None else str(seed), n))]


def held_out(conn, folders) -> tuple[set[str], int]:
    """The episodes any capture on these audit sheets came from (each folder's key.json: an eligibility sheet's
    `items`, a misses or both-ways sheet's `pairs`), and how many of them a trial could sample (current, and I
    speak in)."""
    out = set()
    for folder in folders:
        key = json.loads((Path(folder) / "key.json").read_text())
        entries = list((key.get("items") or key.get("pairs") or {}).values())
        if not entries:
            raise SystemExit(f"{folder}/key.json lists no items or pairs to hold out")
        out |= {str(e.get("episode_id") or e["episode"]) for e in entries}
    eligible = conn.execute("""SELECT count(*) FROM im.episodes WHERE current AND kind <> 'others_only'
                               AND episode_id = ANY(%s::uuid[])""", (sorted(out),)).fetchone()[0]
    return out, eligible


def run(conn, client, system: str, model: str = "claude-opus-5-5", effort: str = "medium",
        monthly_cap_usd: float = 20.0, limit: int | None = None, n: int = SAMPLE,
        replicate: int = 1, schema: dict = extract.SCHEMA_V5, max_tokens: int | None = None,
        seed: int | None = None, exclude=(), review_client=None, review_model: str | None = None,
        review_effort: str = extract.ExtractConfig.review_effort) -> dict:
    """Send the sampled episodes this model hasn't read yet under this prompt and policy (or whose payload changed).
    `max_tokens`, each request's output budget, isn't part of the trial's version; nor are `seed` and `exclude` (the
    sample's order and held-out episodes: see sample)."""
    if review_model and review_client is None:
        raise ValueError("review_client is required when review_model is set")
    pv = version(model, effort, system, replicate, schema, review_model, review_effort)
    policy = egress.POLICY_VERSION
    done = {r[0]: r[1] for r in conn.execute(
        """SELECT episode_id, payload_sha256 FROM im.prompt_trials
           WHERE prompt_version = %s AND model = %s AND privacy_policy_version = %s""", (pv, model, policy))}
    todo = [p for p in (egress.build(conn, eid) for eid in sample(conn, n, seed, exclude))
            if done.get(p.episode_id) != p.sha256]
    stats: dict = {"model": model, "prompt_version": pv, "pending": len(todo), "read": 0, "failed": 0, "cost_usd": 0.0,
                   "stopped": None}
    if review_model:
        stats |= {"review_model": review_model, "reviewed": 0, "review_merges": 0, "review_drops": 0}
    for p in todo[:limit] if limit else todo:
        try:
            a = egress.ask(conn, client, model, effort, system, schema, p.text, segment_ids=p.segment_ids,
                           prompt_version=pv, monthly_cap_usd=monthly_cap_usd, policy=p.policy,
                           episode_id=p.episode_id, max_tokens=max_tokens,
                           read=lambda answer: extract._items(p, answer["items"]))
        except llm.Stop as e:
            stats["stopped"] = str(e)[:200]
            break
        stats["cost_usd"] += a.cost_usd
        if a.failed:   # an answer that doesn't keep to the schema: logged and paid for, read again next time
            stats["failed"] += 1
            continue
        items = a.data or []
        if review_model and items and not a.refused:
            try:
                r, reviewed, merges, drops = extract._review(
                    conn, review_client, p, items, pv, review_model, review_effort, monthly_cap_usd,
                    max_tokens=max_tokens, episode_id=p.episode_id)
            except llm.Stop as e:
                stats["stopped"] = str(e)[:200]
                break
            stats["cost_usd"] += r.cost_usd
            if reviewed is None:
                stats["failed"] += 1
                continue
            items = reviewed
            stats["reviewed"] += 1
            stats["review_merges"] += merges
            stats["review_drops"] += drops
        rows = [{k: it[k] for k in KEEP if k in it} | {"source_segment_ids": [str(s) for s in it["source_segment_ids"]]}
                for it in items]
        conn.execute(
            """INSERT INTO im.prompt_trials (episode_id, prompt_version, model, privacy_policy_version, payload_sha256,
                 egress_id, items) VALUES (%s, %s, %s, %s, %s, %s, %s)
               ON CONFLICT (episode_id, prompt_version, model, privacy_policy_version) DO UPDATE
               SET payload_sha256 = excluded.payload_sha256, egress_id = excluded.egress_id, items = excluded.items,
                   created_at = now()""",
            (p.episode_id, pv, model, policy, p.sha256, a.egress_id, json.dumps(rows)))
        stats["read"] += 1
    stats["cost_usd"] = round(stats["cost_usd"], 4)
    return stats


def version(model: str, effort: str, system: str, replicate: int = 1, schema: dict = extract.SCHEMA_V5,
            review_model: str | None = None, review_effort: str = extract.ExtractConfig.review_effort) -> str:
    """The prompt version a trial is filed under; a replicate's is marked `~n`."""
    pv = extract.prompt_version(model, effort, system, schema, review_model, review_effort)
    return pv if replicate == 1 else f"{pv}~{replicate}"


def _reads(conn, pv: str, model: str) -> dict:
    """episode_id -> (payload sha, items, cost) for one model's trial, under the policy requests go out under now."""
    return {eid: (sha, items, float(cost) if cost is not None else None) for eid, sha, items, cost in conn.execute(
        """SELECT t.episode_id, t.payload_sha256, t.items,
                  g.cost_usd + coalesce((
                    SELECT r.cost_usd FROM im.egress_log r
                    WHERE r.episode_id = t.episode_id AND r.prompt_version = t.prompt_version || ':review'
                      AND r.privacy_policy_version = t.privacy_policy_version AND r.error IS NULL
                    ORDER BY r.egress_id DESC LIMIT 1), 0)
           FROM im.prompt_trials t JOIN im.egress_log g USING (egress_id)
           WHERE t.prompt_version = %s AND t.model = %s AND t.privacy_policy_version = %s""",
        (pv, model, egress.POLICY_VERSION))}


def live_reads(conn, pv: str, model: str) -> dict:
    """episode_id -> (payload sha, items, cost) for the live read in im.extractions and im.items."""
    args = (pv, model, egress.POLICY_VERSION)
    out = {eid: (sha, [], float(cost) if cost is not None else None) for eid, sha, cost in conn.execute(
        """SELECT x.episode_id, x.input_hash,
                  g.cost_usd + coalesce((
                    SELECT r.cost_usd FROM im.egress_log r
                    WHERE r.episode_id = x.episode_id AND r.prompt_version = x.prompt_version || ':review'
                      AND r.privacy_policy_version = x.privacy_policy_version AND r.error IS NULL
                    ORDER BY r.egress_id DESC LIMIT 1), 0)
           FROM im.extractions x
           JOIN im.episodes e ON e.episode_id = x.episode_id AND e.current
           JOIN im.egress_log g USING (egress_id)
           WHERE x.prompt_version = %s AND x.model = %s AND x.privacy_policy_version = %s""", args)}
    if not out:
        return out
    for eid, quote, gist, reading, attributed_to, mishearings, checks_val, segs in conn.execute(
            """SELECT episode_id, quote, gist, reading, attributed_to, mishearings, checks, source_segment_ids
               FROM im.items
               WHERE prompt_version = %s AND model = %s AND privacy_policy_version = %s AND episode_id = ANY(%s)
               ORDER BY item_id""", (*args, list(out))):
        item: dict = {"quote": quote, "gist": gist, "source_segment_ids": [str(s) for s in segs]}
        if reading is not None:
            item["reading"] = reading
        if attributed_to is not None:
            item["attributed_to"] = attributed_to
        if mishearings is not None:
            item["mishearings"] = mishearings
        if checks_val is not None:
            item["checks"] = list(checks_val)
        out[eid][1].append(item)
    return out


def _matched(ref: list[dict], cand: list[dict]) -> dict:
    """Reference capture index -> candidate capture index, best quote first, each used once."""
    pairs = sorted(((quote_score(a["quote"], b["quote"]), i, j)
                    for i, a in enumerate(ref) for j, b in enumerate(cand)
                    if set(a["source_segment_ids"]) & set(b["source_segment_ids"])), reverse=True)
    out, used = {}, set()
    for score, i, j in pairs:
        if score >= CARRY_MIN_SCORE and i not in out and j not in used:
            out[i] = j
            used.add(j)
    return out


def _pair_key(a: dict, b: dict) -> str:
    return hashlib.sha256(json.dumps([a["quote"], a["gist"], b["quote"], b["gist"]]).encode()).hexdigest()


def _unmatched(ref: list[dict], cand: list[dict], m: dict) -> list[tuple[int, int]]:
    used = set(m.values())
    return [(i, j) for i in range(len(ref)) if i not in m for j in range(len(cand)) if j not in used]


def _same_point(ref: list[dict], cand: list[dict], m: dict, answers: dict) -> dict:
    """`m` with the unmatched pairs Jev says make the same point added, likeliest first, each capture once."""
    scored = sorted(((sum(a[q] for q in SAME) / len(SAME), i, j) for i, j in _unmatched(ref, cand, m)
                     if (a := answers.get(_pair_key(ref[i], cand[j])))), reverse=True)
    out, used = dict(m), set(m.values())
    for p, i, j in scored:
        if p >= SAME_CUT and i not in out and j not in used:
            out[i] = j
            used.add(j)
    return out


def same_point(conn, client, candidate: tuple[str, str], reference: tuple[str, str], monthly_cap_usd: float,
               live_reference: bool = False) -> dict:
    """Ask Jev about every pair quote matching left apart in the episodes both read (see the module's docstring);
    answers already kept aren't asked again."""
    cand = _reads(conn, *candidate)
    ref = live_reads(conn, *reference) if live_reference else _reads(conn, *reference)
    stats = {"pairs": 0, "asked": 0, "failed": 0, "cost_usd": 0.0, "stopped": None}
    for eid in (e for e in cand if e in ref and cand[e][0] == ref[e][0]):
        r_items, c_items = ref[eid][1], cand[eid][1]
        for i, j in _unmatched(r_items, c_items, _matched(r_items, c_items)):
            a, b = r_items[i], c_items[j]

            def state(a=a, b=b):
                return {"a": {"quote": a["quote"], "summary": a["gist"]},
                        "b": {"quote": b["quote"], "summary": b["gist"]}}

            stats["pairs"] += 1
            try:
                got, cost, sent = jev.ask(conn, client, "match", _pair_key(a, b), state,
                                          segment_ids=a["source_segment_ids"] + b["source_segment_ids"],
                                          monthly_cap_usd=monthly_cap_usd)
            except llm.Stop as e:
                stats["stopped"] = str(e)[:200]
                break
            stats["asked"] += sent
            stats["failed"] += got is None
            stats["cost_usd"] += cost
        if stats["stopped"]:
            break
    stats["cost_usd"] = round(stats["cost_usd"], 6)
    return stats


REACHES = ("recurring", "one_off", "unplaced")


def _live_reach(conn, episode_ids: list) -> dict:
    """episode_id -> [(segment ids, quote, conversations)] for the live captures there that are in a cluster:
    the most conversations any cluster they're evidence for has been said in."""
    out: dict = {}
    for eid, segs, quote, conversations in conn.execute(
            # Conversations from each cluster's current captures' own lines, and its observations that aren't speech:
            # a capture counts whether or not the observation stage has placed it yet.
            """WITH reach AS (
                 SELECT idea_id, count(DISTINCT conversation) AS conversations FROM (
                   SELECT ev.idea_id, s.conversation_id::text AS conversation
                   FROM im.idea_evidence ev JOIN im.items i USING (item_id)
                   JOIN im.episodes e ON e.episode_id = i.episode_id AND e.current
                   JOIN im.source_segments s ON s.segment_id = ANY(i.source_segment_ids)
                   UNION ALL
                   SELECT o.idea_id, o.conversation::text FROM im.observations o WHERE o.source <> 'speech') x
                 GROUP BY idea_id)
               SELECT i.episode_id, i.source_segment_ids, i.quote, max(r.conversations)
               FROM im.items i JOIN im.idea_evidence ev USING (item_id) JOIN reach r USING (idea_id)
               WHERE i.episode_id = ANY(%s)
               GROUP BY i.item_id, i.episode_id, i.source_segment_ids, i.quote""", (episode_ids,)):
        out.setdefault(eid, []).append(({str(x) for x in segs}, quote, conversations))
    return out


def by_reach(conn, found: list) -> dict:
    """Recall of the reference's captures split by reach: `recurring` (its idea said in two or more conversations),
    `one_off` (in one), `unplaced` (no live capture on its lines in a cluster). `found` is [(episode, capture,
    found by the candidate)]."""
    live = _live_reach(conn, list({eid for eid, _, _ in found}))
    n = {r: [0, 0] for r in REACHES}
    for eid, it, hit in found:
        best = max(((quote_score(it["quote"], q), c) for segs, q, c in live.get(eid, [])
                    if segs & set(it["source_segment_ids"])), default=(0.0, None))
        reach = "unplaced" if best[0] < CARRY_MIN_SCORE else "recurring" if best[1] >= 2 else "one_off"
        n[reach][0] += 1
        n[reach][1] += hit
    return {r: {"captures": t, "recall": round(f / t, 3) if t else None} for r, (t, f) in n.items()}


def report(conn, candidate: tuple[str, str], reference: tuple[str, str], same: dict | None = None,
           live_reference: bool = False) -> dict:
    """How a candidate's captures compare with a reference's, on the episodes both read with the same payload.
    `candidate` and `reference` are (prompt_version, model). With `same` (Jev's kept answers, jev.kept), a pair
    making the same point counts as found too, and `found_by_same_point` says how many did."""
    cand = _reads(conn, *candidate)
    ref = live_reads(conn, *reference) if live_reference else _reads(conn, *reference)
    both = [eid for eid in cand if eid in ref and cand[eid][0] == ref[eid][0]]
    n = {"reference_captures": 0, "found": 0, "candidate_captures": 0, "extra": 0,
         "episodes_reference_captured_in": 0, "of_those_candidate_captured_nothing": 0} \
        | ({"found_by_same_point": 0} if same is not None else {})
    missed, extra, found_by_reach = [], [], []
    for eid in both:
        r_items, c_items = ref[eid][1], cand[eid][1]
        m = _matched(r_items, c_items)
        if same is not None:
            quoted = len(m)
            m = _same_point(r_items, c_items, m, same)
            n["found_by_same_point"] += len(m) - quoted
        n["reference_captures"] += len(r_items)
        n["found"] += len(m)
        n["candidate_captures"] += len(c_items)
        n["extra"] += len(c_items) - len(m)
        if r_items:
            n["episodes_reference_captured_in"] += 1
            n["of_those_candidate_captured_nothing"] += not c_items
        for i, it in enumerate(r_items):
            found_by_reach.append((eid, it, i in m))
            if i not in m:
                missed.append(it["quote"])
        used = set(m.values())
        extra += [it["quote"] for j, it in enumerate(c_items) if j not in used]
    rate = lambda a, b: round(a / b, 3) if b else None  # noqa: E731
    cost = lambda reads: sum(reads[e][2] or 0.0 for e in both)  # noqa: E731
    return {"candidate": candidate[1], "reference": reference[1], "episodes": len(both),
            "candidate_only_read": len(cand) - len(both)} | n | {
        "recall": rate(n["found"], n["reference_captures"]),
        "recall_by_reach": by_reach(conn, found_by_reach),
        "cost_usd": round(cost(cand), 4), "reference_cost_usd": round(cost(ref), 4),
        "cost_vs_reference": rate(cost(cand), cost(ref)),
        "verbatim_quotes": {"candidate": verbatim(conn, [cand[e][1] for e in both]),
                            "reference": verbatim(conn, [ref[e][1] for e in both])},
        "quotes_not_in_lines": {"candidate": not_in_lines(conn, {e: cand[e][1] for e in both}),
                                "reference": not_in_lines(conn, {e: ref[e][1] for e in both})},
        "checks": {"candidate": checks([cand[e][1] for e in both]), "reference": checks([ref[e][1] for e in both])},
        "missed": missed[:EXAMPLES], "extra_examples": extra[:EXAMPLES]}


def checks(reads: list[list[dict]]) -> dict | None:
    """How many captures fail each capture check (capture_checks), and the share that pass every one. None for reads
    made before the checks were recorded."""
    items = [it for r in reads for it in r]
    if not items or any("checks" not in it for it in items):
        return None
    failing: dict = {}
    for it in items:
        for code in it["checks"]:
            failing[code] = failing.get(code, 0) + 1
    return {"captures": len(items), "pass": round(sum(not it["checks"] for it in items) / len(items), 3),
            "failing": dict(sorted(failing.items()))}


def _words(text: str) -> str:
    return " ".join(re.findall(r"\w+", text.lower()))


def verbatim(conn, reads: list[list[dict]]) -> float | None:
    """The share of captures whose quote is word for word in the lines they cite, case and punctuation aside: v4
    asks for the quote exactly as transcribed, where earlier prompts let it be tidied (or stitched with "...")."""
    items = [it for r in reads for it in r]
    if not items:
        return None
    text = dict(conn.execute("SELECT segment_id::text, text FROM im.source_segments WHERE segment_id = ANY(%s::uuid[])",
                             ([s for it in items for s in it["source_segment_ids"]],)).fetchall())
    hits = sum(f" {_words(it['quote'])} " in f" {_words(' '.join(text.get(s, '') for s in it['source_segment_ids']))} "
               for it in items)
    return round(hits / len(items), 3)


def not_in_lines(conn, reads: dict) -> float | None:
    """The share of captures whose quote isn't on its lines even once its citation grows by a line either way
    (im.quotes, as the run places live captures): made up, or cited further off. {episode: items}."""
    from im import quotes

    lines = quotes.windows(conn, list(reads))
    caps = [(str(e), it) for e, items in reads.items() for it in items]
    if not caps:
        return None
    off = sum(quotes.place(it["quote"], quotes.near(lines.get(e, []), it["source_segment_ids"]),
                           it["source_segment_ids"])[2] < quotes.FLAG for e, it in caps)
    return round(off / len(caps), 3)

