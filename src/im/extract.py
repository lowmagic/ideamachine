"""Idea extraction with Claude (docs/decisions/0004-claude-extraction.md).

Each current episode goes out once, and again only if what would be sent changed. Read under another model, prompt
or policy with the same payload, it's held by the re-read gate (im.reread), its captures staying live, until
`im reread extract` reads it again. A payload already read on another episode isn't sent again: its items are reused.
Claude returns items anchored to transcript line numbers; they're mapped back to segment IDs and to who said them
locally. Every request is logged in im.egress_log, and the monthly cap stops the stage once the month's spend reaches
it (egress.send).
"""

import dataclasses
import hashlib
import json
import uuid
from importlib import resources

from im import capture_checks, egress, feedback, llm, reread
from im.config import ExtractConfig

SCHEMA_VERSION = 1
STAGE_VERSION = "extract/1"
ITEM_NS = uuid.UUID("3c1e8a52-7d0f-4b6e-9f21-5a8c4e7d1b90")

# v1 to v4 captured everyone's speech, with a capture kind and a confidence; they were retired at the cutover
# (2026-11-11, decision 0012), and their captures stay in the archived database, im_v1.

# v5 (2026-11-08, decision 0012): only my speech (0011); a reading, the attribution of a thought I relay and the
# mishearings the reading fixed; a gist that says only what the cited lines say. No capture kind, no confidence (nothing
# read them) and no interests slot (it favored my usual subjects). The text was reviewed and merged in the prompt
# review; it lives in prompts/extract_v5.txt. For trials; v5.3 is the revision that runs.
SYSTEM_V5 = resources.files("im.prompts").joinpath("extract_v5.txt").read_text().rstrip("\n")
# v5.1 (trial, 2026-11-10): v5 with the goldens' decisions written in. A point made through a joke, a story or an
# example is stated as a claim at the scope it was told, not retold; my sentence interrupted by someone else's line is
# still one sentence; explaining the fault in front of me isn't a thought unless I state it as a rule; judgments about
# a person (and what they should do differently) count; reworking something I've built is next steps, a new idea for
# it isn't; a second illustration shows a story's point.
SYSTEM_V5_1 = resources.files("im.prompts").joinpath("extract_v5_1.txt").read_text().rstrip("\n")
# v5.2 (trial, 2026-11-10): v5.1, against what its first trial showed (2026-11-10, 20 episodes: 41 captures where v5
# made 34 twice). A judgment says what someone is like or should do; a complaint about what they did once is venting.
# A personal reaction stays personal, not what anyone owes or should do.
SYSTEM_V5_2 = resources.files("im.prompts").joinpath("extract_v5_2.txt").read_text().rstrip("\n")
# v5.3 (2026-11-10, the rebuild's prompt and live since the cutover, decision 0012 amended): v5.2, and the reading
# also spells out what "that kind of …", "this approach" or "the same way" points back to: all three earlier versions
# left "that kind of overview" (Astra's review) unresolved.
SYSTEM_V5_3 = resources.files("im.prompts").joinpath("extract_v5_3.txt").read_text().rstrip("\n")
# v5.4 (trial, 2026-11-13, the moment-bound-remarks experiment; not live): v5.3 and one frozen paragraph after "The
# test for a capture", against remarks tied to the occasion (banter, a passing reaction, a fact in passing, a story of
# what just happened) without losing a characterization, preference, pattern, rule or lesson. Tried on held-out
# episodes against v5.3's live captures (`im audit-misses sheet --live`), never read by `im run`.
SYSTEM_V5_4 = resources.files("im.prompts").joinpath("extract_v5_4.txt").read_text().rstrip("\n")

# Every prompt here captures only my speech (decision 0011), so an episode where I don't speak isn't sent (_pending).
PROMPTS = {"v5": SYSTEM_V5, "v5.1": SYSTEM_V5_1, "v5.2": SYSTEM_V5_2, "v5.3": SYSTEM_V5_3, "v5.4": SYSTEM_V5_4}
LIVE = ("v5.3",)   # what `im run` may read with; v5 to v5.2 and v5.4 are for `im trial` only
REVIEW_SYSTEM = resources.files("im.prompts").joinpath("extract_review_v1.txt").read_text().rstrip("\n")

# v5's schema lists the evidence first, the gist after it; what JSON Schema can't say (lines exist and are in order,
# strings aren't empty) is checked in code (capture_checks).
SCHEMA_V5 = {
    "type": "object",
    "properties": {
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "quote": {"type": "string"},
                    "lines": {"type": "array", "items": {"type": "integer"}},
                    "attributed_to": {"type": "string"},
                    "reading": {"type": "string"},
                    "mishearings": {"type": "array", "items": {
                        "type": "object",
                        "properties": {"heard": {"type": "string"}, "meant": {"type": "string"}},
                        "required": ["heard", "meant"], "additionalProperties": False}},
                    "gist": {"type": "string"},
                },
                "required": ["quote", "lines", "attributed_to", "reading", "mishearings", "gist"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["items"],
    "additionalProperties": False,
}

REVIEW_SCHEMA = {
    "type": "object",
    "properties": {
        "kept": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "ids": {"type": "array", "items": {"type": "integer"}},
                    "gist": {"type": "string"},
                },
                "required": ["ids", "gist"],
                "additionalProperties": False,
            },
        },
        "dropped": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "integer"},
                    "reason": {"type": "string"},
                },
                "required": ["id", "reason"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["kept", "dropped"],
    "additionalProperties": False,
}


def prompt_version(model: str, effort: str, system: str = SYSTEM_V5_3, schema: dict = SCHEMA_V5,
                   review_model: str | None = None, review_effort: str = ExtractConfig.review_effort,
                   review_system: str = REVIEW_SYSTEM, review_schema: dict = REVIEW_SCHEMA) -> str:
    parts: list = [system, schema, model, effort]
    if review_model:
        parts += [review_system, review_schema, review_model, review_effort]
    return hashlib.sha256(json.dumps(parts, sort_keys=True).encode()).hexdigest()[:16]


def _pending(conn, model: str, pv: str) -> tuple[list, list]:
    """What's due under this model, prompt and policy, newest first, split by the re-read gate: (to read, held).

    To read: current episodes whose payload no read has had (new, or what would be sent changed), and those whose
    payload another episode's read under this version can be reused for (nothing is sent). Held: those whose payload
    was read under another version only, as (payload, that read: its episode, model, prompt version, policy and
    egress id), the latest. Episodes where I don't speak (`others_only`) aren't due: a prompt capturing only my
    speech can't capture anything in them."""
    payloads = [egress.build(conn, eid) for (eid,) in conn.execute(
        """SELECT episode_id FROM im.episodes WHERE current AND kind <> 'others_only'
           ORDER BY started_at DESC""")]
    reads: dict[str, list[tuple]] = {}
    for sha, *read in conn.execute(
            """SELECT input_hash, episode_id, model, prompt_version, privacy_policy_version, egress_id
               FROM im.extractions WHERE input_hash = ANY(%s) ORDER BY created_at DESC, episode_id""",
            ([p.sha256 for p in payloads],)):
        reads.setdefault(sha, []).append(tuple(read))
    todo, held = [], []
    for p in payloads:
        earlier = reads.get(p.sha256, [])
        now = [r for r in earlier if r[1:4] == (model, pv, p.policy)]
        if any(r[0] == p.episode_id for r in now):
            continue
        if now or not earlier:
            todo.append(p)
        else:
            held.append((p, earlier[0]))
    return todo, held


def _version(model: str, pv: str, policy: str) -> str:
    return f"{model} {pv}" + ("" if policy == egress.POLICY_VERSION else f" {policy}")


def held_report(conn, held: list, model: str, pv: str) -> dict | None:
    """The re-read gate's report on held episodes (reread.report): a payload read on two episodes is read once."""
    last = {p.sha256: read[4] for p, read in reversed(held)}
    return reread.report("extract", [_version(*read[1:4]) for _, read in held],
                         _version(model, pv, egress.POLICY_VERSION),
                         reread.estimate(conn, last.values(), lambda _: model))


def _keep_earlier_read(conn, held: list) -> int:
    """A held episode re-derived with the same payload (a segmentation change, say) takes the captures of the read
    it's held on, under that read's version, as a reuse does: the earlier captures stay live, and nothing is sent.
    Returns how many."""
    n = 0
    for p, (eid, model, pv, policy, _) in held:
        if eid == p.episode_id:
            continue
        q = dataclasses.replace(p, policy=policy)
        if reused := _reuse(conn, q, model, pv):
            _carry_matches(conn, _save(conn, q, model, pv, *reused), reused[1])
            n += 1
    return n


def _reuse(conn, p: egress.Payload, model: str, pv: str) -> tuple[int, list[dict]] | None:
    """An earlier read of this exact payload on another episode: its egress id and items, mapped onto this one.

    Naming a speaker re-derives the episode but sends the same pseudonymized text, so there's nothing new to read.
    Lines map by position and speakers through the pseudonym both payloads share. Each item remembers the one it
    was copied from (`from_item_id`), so its match can come along too. None if there's no such read."""
    row = conn.execute(
        """SELECT episode_id, egress_id FROM im.extractions
           WHERE input_hash = %s AND model = %s AND prompt_version = %s AND privacy_policy_version = %s
             AND episode_id <> %s ORDER BY created_at DESC LIMIT 1""",
        (p.sha256, model, pv, p.policy, p.episode_id)).fetchone()
    if row is None:
        return None
    old = egress.build(conn, row[0])
    if old.sha256 != p.sha256:
        return None
    line = {sid: n for n, sid in enumerate(old.segment_ids)}
    alias = {label: pseudo for pseudo, label in old.speakers.items()}
    rows = conn.execute(
        """SELECT item_id, w.said_by, quote, gist, source_segment_ids, reading, attributed_to, mishearings, checks
           FROM im.items JOIN im.item_said_by w USING (item_id)
           WHERE episode_id = %s AND model = %s AND prompt_version = %s AND privacy_policy_version = %s
           ORDER BY item_id""", (row[0], model, pv, p.policy)).fetchall()
    v5 = ("reading", "attributed_to", "mishearings", "checks")
    return row[1], [{"from_item_id": i, "said_by": p.speakers.get(alias.get(by), by), "quote": q, "gist": g,
                     "source_segment_ids": [p.segment_ids[line[s]] for s in segs]}
                    | {f: v for f, v in zip(v5, more) if v is not None}
                    for i, by, q, g, segs, *more in rows]


def _items(p: egress.Payload, raw: list[dict]) -> list[dict]:
    """The model's items, mapped back to segments and local speakers (a v5 answer names none: its captures are
    mine). Items citing no real line are dropped. Each keeps `checks`, the capture checks it fails on the transcript
    as sent."""
    sent = capture_checks.lines_of(p.text)
    out = []
    for item in raw:
        lines = sorted({n for n in item["lines"] if 1 <= n <= len(p.segment_ids)})
        if not lines:
            continue
        said_by = item.get("said_by", "me")
        out.append(item | {
            "said_by": p.speakers.get(said_by, said_by),
            "source_segment_ids": [p.segment_ids[n - 1] for n in lines],
            "checks": capture_checks.check(sent, item),
        })
    return out


def _item_id(p: egress.Payload, model: str, pv: str, i: int) -> uuid.UUID:
    return uuid.uuid5(ITEM_NS, f"{p.episode_id}|{model}|{pv}|{p.policy}|{p.sha256}|{i}")


def _carry_matches(conn, ids: list[uuid.UUID], items: list[dict]) -> None:
    """Reused items keep the matcher's decision and my verdicts on the items they were copied from: the same capture
    isn't judged (or paid for) twice, and can't turn into a second idea. Verdicts go item to item, so two captures
    with the same quote each keep their own; feedback.carry_verdicts, which matches by quote, sees them settled.
    `ids` are the saved items' ids, in the order of `items`."""
    for new, it in zip(ids, items, strict=True):
        copied = conn.execute(
            """INSERT INTO im.item_verdicts (item_id, segment_ids, quote, verdict, note, how, decided_at, carried_from)
               SELECT n.item_id, n.source_segment_ids, n.quote, v.verdict, v.note, v.how, v.decided_at, v.verdict_id
               FROM im.item_verdicts v JOIN im.items n ON n.item_id = %s
               WHERE v.item_id = %s AND NOT EXISTS (SELECT 1 FROM im.item_verdicts x WHERE x.item_id = n.item_id)
               ORDER BY v.decided_at, v.verdict_id""", (new, it["from_item_id"])).rowcount
        if copied:
            conn.execute("""INSERT INTO im.verdict_carries (from_item_id, to_item_id, score, carried, method)
                            VALUES (%s, %s, 1, true, 'reuse') ON CONFLICT DO NOTHING""", (it["from_item_id"], new))
        conn.execute("""INSERT INTO im.item_matches (item_id, decision, idea_id, judged_by, egress_id)
                        SELECT %s, decision, idea_id, judged_by, egress_id FROM im.item_matches WHERE item_id = %s
                        ON CONFLICT DO NOTHING""", (new, it["from_item_id"]))
        conn.execute("""INSERT INTO im.idea_evidence (idea_id, item_id, relation, judged_by)
                        SELECT idea_id, %s, relation, judged_by FROM im.idea_evidence WHERE item_id = %s
                        ON CONFLICT DO NOTHING""", (new, it["from_item_id"]))


def _save(conn, p: egress.Payload, model: str, pv: str, egress_id: int, items: list[dict]) -> list[uuid.UUID]:
    """Make `items` the episode's captures under this model, prompt and policy. Returns their ids, in order.

    A re-read of the same episode (what would be sent changed, e.g. a tap was added) keeps each earlier capture it
    makes again: one citing a line the new one cites, with a matching quote (feedback.quote_score), one to one and
    best first. The earlier item stays as it was, with all that hangs off it: the matcher's decision and the idea's
    evidence (so the capture doesn't become a second idea), my verdicts, and the feedback events on it. Only captures
    the re-read doesn't make again are replaced."""
    with conn.transaction():
        old = conn.execute("""SELECT item_id, quote, source_segment_ids FROM im.items
                              WHERE episode_id = %s AND model = %s AND prompt_version = %s
                                AND privacy_policy_version = %s""", (p.episode_id, model, pv, p.policy)).fetchall()
        scored = sorted((-feedback.quote_score(quote, it["quote"]), str(item_id), n, item_id)
                        for item_id, quote, segs in old for n, it in enumerate(items)
                        if set(segs) & set(it["source_segment_ids"]))
        kept: dict[int, uuid.UUID] = {}
        for neg_score, _, n, item_id in scored:
            if -neg_score >= feedback.CARRY_MIN_SCORE and n not in kept and item_id not in kept.values():
                kept[n] = item_id
        conn.execute("""DELETE FROM im.items WHERE episode_id = %s AND model = %s AND prompt_version = %s
                        AND privacy_policy_version = %s AND item_id <> ALL(%s::uuid[])""",
                     (p.episode_id, model, pv, p.policy, list(kept.values())))
        ids = []
        for i, it in enumerate(items):
            if i in kept:
                ids.append(kept[i])
                continue
            item_id = _item_id(p, model, pv, i)
            # What would be sent can come back to an earlier read's (a tap added, then dropped), and a capture kept
            # from that read holds the id of whatever new capture now sits at its old position: step past it.
            while item_id in kept.values():
                item_id = uuid.uuid5(ITEM_NS, f"{item_id}|taken")
            ids.append(item_id)
            # said_by_segment_id: the first cited segment the speaker said, so a later rename reaches the item. A
            # capture has no kind, confidence or tags (v5).
            conn.execute(
                """INSERT INTO im.items (item_id, episode_id, said_by, quote, gist, themes, source_segment_ids,
                     egress_id, model, prompt_version, privacy_policy_version, schema_version, stage_version,
                     input_hash, reading, attributed_to, mishearings, checks, said_by_segment_id)
                   VALUES (%s, %s, %s, %s, %s, '{}', %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                     (SELECT u.seg FROM unnest(%s::uuid[]) WITH ORDINALITY AS u(seg, n)
                      JOIN im.source_segments s ON s.segment_id = u.seg
                      WHERE s.speaker_label = %s ORDER BY u.n LIMIT 1))""",
                (ids[-1], p.episode_id, it["said_by"], it["quote"], it["gist"], it["source_segment_ids"], egress_id,
                 model, pv, p.policy, SCHEMA_VERSION, STAGE_VERSION, p.sha256, it.get("reading"),
                 it.get("attributed_to"), None if "mishearings" not in it else json.dumps(it["mishearings"]),
                 it.get("checks"), it["source_segment_ids"], it["said_by"]))
        conn.execute(
            """INSERT INTO im.extractions (episode_id, model, prompt_version, privacy_policy_version, input_hash,
                 egress_id, n_items) VALUES (%s, %s, %s, %s, %s, %s, %s)
               ON CONFLICT (episode_id, model, prompt_version, privacy_policy_version) DO UPDATE
               SET input_hash = excluded.input_hash, egress_id = excluded.egress_id, n_items = excluded.n_items,
                   created_at = now()""",
            (p.episode_id, model, pv, p.policy, p.sha256, egress_id, len(items)))
    return ids


def _clean_ws(text: str) -> str:
    return " ".join((text or "").split())


def _validate_review(answer: dict, n_items: int) -> dict:
    if not isinstance(answer, dict) or not isinstance(answer.get("kept"), list):
        raise ValueError("missing kept list")
    seen: set[int] = set()
    cleaned = []
    for entry in answer["kept"]:
        if not isinstance(entry, dict) or not isinstance(entry.get("ids"), list) or not entry["ids"]:
            raise ValueError("kept entry missing non-empty ids")
        ids = []
        for x in entry["ids"]:
            if isinstance(x, bool) or not isinstance(x, int) or not (1 <= x <= n_items):
                raise ValueError(f"invalid candidate id {x!r} for 1..{n_items}")
            if x in seen:
                raise ValueError(f"duplicate candidate id {x}")
            seen.add(x)
            ids.append(x)
        gist = entry["gist"].strip() if isinstance(entry.get("gist"), str) else ""
        if not gist:
            raise ValueError("empty gist")
        cleaned.append({"ids": ids, "gist": gist})
    dropped = answer.get("dropped") if isinstance(answer.get("dropped"), list) else []
    return {"kept": cleaned, "dropped": dropped}


def _format_review(transcript: str, items: list[dict]) -> str:
    lines = ["Transcript:", transcript, "", "Candidate captures:"]
    for idx, it in enumerate(items, 1):
        lines.append(f"{idx}. Gist: {_clean_ws(it.get('gist', ''))}")
        lines.append(f'   Quote: "{_clean_ws(it.get("quote", ""))}"')
        if it.get("checks"):
            lines.append(f"   Checks failed: {', '.join(it['checks'])}")
    return "\n".join(lines)


def _apply_review(src_items: list[dict], decision: dict, segment_order: list | None = None) -> list[dict]:
    """Build final captures from Pass-2 `kept` decisions while preserving Pass-1 quotes and provenance."""
    out: list[dict] = []
    order = {s: n for n, s in enumerate(segment_order)} if segment_order is not None else None
    for entry in decision["kept"]:
        if len(entry["ids"]) == 1:
            base = dict(src_items[entry["ids"][0] - 1])
            base["gist"] = entry["gist"] or base.get("gist", "")
            out.append(base)
            continue
        group = [src_items[i - 1] for i in sorted(entry["ids"])]
        primary = max(group, key=lambda it: len(it.get("quote") or ""))
        segs: list = []
        for it in group:
            for s in it.get("source_segment_ids") or ():
                if s not in segs:
                    segs.append(s)
        if order is not None:
            segs.sort(key=lambda s: order.get(s, len(order)))
        mis: list[dict] = []
        seen_mis: set[tuple[str, str]] = set()
        for it in group:
            for m in it.get("mishearings") or ():
                k = (m.get("heard", ""), m.get("meant", ""))
                if k not in seen_mis:
                    seen_mis.add(k)
                    mis.append(m)
        attr = next((it.get("attributed_to") for it in group if it.get("attributed_to")), "")
        reading = " ".join(it.get("reading", "").strip() for it in group if it.get("reading"))
        item: dict = {
            "quote": primary.get("quote", ""),
            "gist": entry["gist"],
            "reading": reading or primary.get("reading", ""),
            "attributed_to": attr,
            "mishearings": mis,
            "source_segment_ids": segs,
        }
        if "checks" in primary:
            item["checks"] = list(primary["checks"])
        if "said_by" in primary:
            item["said_by"] = primary["said_by"]
        out.append(item)
    return out


def _review(conn, client, p: egress.Payload, items: list[dict], pv: str, model: str, effort: str,
            monthly_cap_usd: float, max_tokens: int | None = None, segment_ids=None, episode_id=None,
            system: str = REVIEW_SYSTEM, schema: dict = REVIEW_SCHEMA) -> tuple[egress.Answer, list[dict] | None, int, int]:
    """Run Pass-2 merge/prune review on non-empty Pass-1 items, logging under `pv:review`."""
    a = egress.ask(
        conn, client, model, effort, system, schema, _format_review(p.text, items),
        segment_ids=p.segment_ids if segment_ids is None else segment_ids, prompt_version=f"{pv}:review",
        monthly_cap_usd=monthly_cap_usd, policy=p.policy, episode_id=episode_id, max_tokens=max_tokens,
        read=lambda answer: _validate_review(answer, len(items)))
    if a.failed or a.refused or a.data is None:
        return a, None, 0, 0
    final = _apply_review(items, a.data, p.segment_ids)
    kept_ids = {i for e in a.data["kept"] for i in e["ids"]}
    merges = sum(len(e["ids"]) - 1 for e in a.data["kept"] if len(e["ids"]) > 1)
    drops = len(items) - len(kept_ids)
    return a, final, merges, drops


def run_stage(conn, client, model: str = ExtractConfig.model, effort: str = ExtractConfig.effort,
              monthly_cap_usd: float = 20.0, limit: int | None = None, system: str = SYSTEM_V5_3,
              max_tokens: int | None = None, schema: dict = SCHEMA_V5, held_only: bool = False,
              review_client=None, review_model: str | None = None,
              review_effort: str = ExtractConfig.review_effort, review_system: str = REVIEW_SYSTEM,
              review_schema: dict = REVIEW_SCHEMA) -> dict:
    """Read every episode due under this model, prompt and schema (_pending), at most `limit`, and keep what it
    captures in im.items. What the re-read gate holds is reported under `held`; with `held_only`, only that is read
    (`im reread extract`)."""
    if review_model and review_client is None:
        raise ValueError("review_client is required when review_model is set")
    pv = prompt_version(model, effort, system, schema, review_model, review_effort, review_system, review_schema)
    todo, held = _pending(conn, model, pv)
    stats: dict = {"model": model, "prompt_version": pv, "pending": len(todo), "read": 0, "reused": 0, "items": 0,
                   "refused": 0, "failed": 0, "cost_usd": 0.0, "stopped": None}
    if review_model:
        stats |= {"review_model": review_model, "reviewed": 0, "review_merges": 0, "review_drops": 0}
    if held_only:
        todo = [p for p, _ in held]
        stats["pending"] = len(todo)
    elif kept := _keep_earlier_read(conn, held):
        stats["held_kept_earlier_read"] = kept
    done = set()
    for p in todo[:limit] if limit else todo:
        if reused := _reuse(conn, p, model, pv):
            _carry_matches(conn, _save(conn, p, model, pv, *reused), reused[1])
            done.add(p.episode_id)
            stats["reused"] += 1
            stats["items"] += len(reused[1])
            continue
        try:
            a = egress.ask(conn, client, model, effort, system, schema, p.text, segment_ids=p.segment_ids,
                           prompt_version=pv, monthly_cap_usd=monthly_cap_usd, policy=p.policy,
                           episode_id=p.episode_id, max_tokens=max_tokens,
                           read=lambda answer: _items(p, answer["items"]))
        except llm.Stop as e:
            stats["stopped"] = str(e)[:200]
            break
        stats["cost_usd"] += a.cost_usd
        if a.failed:   # logged and counted, not recorded as read: read again next run
            stats["failed"] += 1
            continue
        items = a.data or []
        if review_model and items and not a.refused:
            try:
                r, reviewed, merges, drops = _review(
                    conn, review_client, p, items, pv, review_model, review_effort, monthly_cap_usd,
                    max_tokens=max_tokens, episode_id=p.episode_id, system=review_system, schema=review_schema)
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
        stats["read"] += 1
        if a.refused:  # the whole chain declined: record it read, with nothing captured
            stats["refused"] += 1
        _save(conn, p, model, pv, a.egress_id, items)
        done.add(p.episode_id)
        stats["items"] += len(items)
    stats["cost_usd"] = round(stats["cost_usd"], 4)
    stats["spent_this_month_usd"] = round(egress.spent_this_month(conn), 4)
    return reread.add(stats, held_report(conn, [h for h in held if h[0].episode_id not in done], model, pv))


def forgotten_sent(conn) -> list[tuple]:
    """Forgotten segments that had already gone out: (segment_id, forgotten_at, first sent at, requests)."""
    return conn.execute(
        """SELECT t.segment_id, t.forgotten_at, min(g.created_at), count(*)
           FROM im.source_tombstones t JOIN im.egress_log g ON t.segment_id = ANY(g.segment_ids)
           GROUP BY t.segment_id, t.forgotten_at ORDER BY min(g.created_at)""").fetchall()


def review_items(conn) -> list[tuple]:
    """Captures on current episodes I haven't decided on yet, one per observation (a verdict, or a verdict event, on
    any of its captures decides it, migrations 037 and 072; my reading doesn't): (item, said by, quote, gist,
    segments, said at, episode), newest first."""
    return conn.execute(
        """SELECT * FROM (
             SELECT DISTINCT ON (coalesce(i.observation_id, i.item_id))
                    i.item_id, w.said_by, i.quote, i.gist, i.source_segment_ids, e.started_at, e.episode_id
             FROM im.items i JOIN im.item_said_by w USING (item_id) JOIN im.episodes e USING (episode_id)
             WHERE e.current AND NOT EXISTS (SELECT 1 FROM im.capture_verdict v WHERE v.item_id = i.item_id)
               AND NOT EXISTS (SELECT 1 FROM im.items s JOIN pub.feedback_events f ON f.item_id = s.item_id
                               WHERE (s.item_id = i.item_id OR s.observation_id = i.observation_id)
                                 AND f.kind IN ('item_keep', 'item_discard', 'item_star'))
             ORDER BY coalesce(i.observation_id, i.item_id), i.created_at, i.item_id) r
           ORDER BY started_at DESC, item_id""").fetchall()


def discards(conn) -> list[tuple]:
    """Items I discarded (latest verdict), with my note and the prompt that captured them, newest first."""
    return conn.execute(
        """SELECT w.said_by, i.quote, i.gist, v.decided_at, v.note, i.model, i.prompt_version
           FROM im.items i JOIN im.item_said_by w USING (item_id) JOIN LATERAL (
             SELECT verdict, note, decided_at FROM im.item_verdicts WHERE item_id = i.item_id
             ORDER BY decided_at DESC LIMIT 1) v ON v.verdict = 'discard'
           ORDER BY v.decided_at DESC""").fetchall()


def decide(conn, item_id, verdict: str, note: str | None = None) -> None:
    conn.execute(
        """INSERT INTO im.item_verdicts (item_id, segment_ids, quote, verdict, note)
           SELECT item_id, source_segment_ids, quote, %s, %s FROM im.items WHERE item_id = %s""",
        (verdict, note, item_id))
