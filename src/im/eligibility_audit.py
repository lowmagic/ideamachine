"""Capture eligibility (ROADMAP-v2, Triage): how many of the live captures don't deserve to be a card. Measurement
only: nothing here changes prompts, extraction or quarantine. The protocol
(docs/design/eligibility-audit.md):

1. `sheet`: a seeded uniform sample of the live speech captures (on current episodes, from the live read under the
   policy requests go out under now; the ChatGPT archive's captures have no lines, so they're left out and counted),
   each shown as the transcript around it, the lines it cites, its quote and its gist. Sends nothing.
2. `draft`: a model labels every item eligible, ineligible or uncertain under the frozen rubric (RUBRIC), with a
   one-line reason, shown only the rubric and the item. Two models of different families, neither Anthropic's (Opus
   wrote the captures).
3. `review`: review.md, for me to mark blind: every item where the drafts disagree or either says uncertain, and a
   seeded AUDITED of those they agree on, shuffled together, without the models' labels or reasons.
4. `score`: (a) the models' agreement with each other over every item, (b) each model's accuracy against my marks
   and how often the agreed label held on the audited items, and (c) the ineligible rate over the sample, from my
   marks where I made them and the agreed label elsewhere. N with each.

The rubric is frozen: its version and hash are in the key, each draft and the review's key, and a folder made under
one rubric refuses another. Changing RUBRIC is a new RUBRIC_VERSION and a new sheet. The sheet quotes people: keep
it in saved/.
"""

import hashlib
import json
import math
import random
import re
from pathlib import Path

from im import egress, extract, llm
from im.capture_checks import lines_of

SIZE = 100
AUDITED = 20      # agreed items that go on review.md beside the disputed ones
BEFORE, AFTER = 8, 3   # context: lines shown before the first cited line and after the last
LABELS = ("eligible", "ineligible", "uncertain")
MARKS = {"eligible": "eligible", "e": "eligible", "ineligible": "ineligible", "i": "ineligible",
         "uncertain": "uncertain", "u": "uncertain", "?": "uncertain"}
Z95 = 1.96

RUBRIC_VERSION = 1
RUBRIC = """\
Capture eligibility, rubric 1.

A capture is a thought of mine that a model took from a transcript of my speech: the lines around it, the lines it
cites, the quote it took and its gist (a one-line restatement). "me" is me; S1, S2 and so on are other people;
"unknown" is a voice nobody was matched to. Go by the speaker labels. The question: does this capture deserve to be a
card, one of my thoughts worth keeping, that I'd recognize when I say it again? Not whether it's true, original,
important or kind, and not how well the gist is written.

eligible: in my lines I express a thought whose meaning reaches beyond the moment it was said in. Any of:
- an idea, theory, hunch, opinion, belief or preference I hold; half-formed, unfinished or obvious is fine;
- a question I'm actually wondering about;
- an observation about people, myself, social dynamics or the world that applies past this occasion, or a pattern
  in myself stated as a pattern;
- a judgment of what a particular person is like, or what they should do differently;
- a formulation, image, story or joke that carries a point of mine; exaggerated, provocative or false points count
  as said;
- something I want to build, try or look into, and what it's for; a method or rule for getting something done;
- someone else's thought I pass on as a point in the conversation (a teacher's rule, something I read), even with
  doubt or distance;
- a task or decision I explicitly mark to remember ("note to self").

ineligible: none of those is in my lines. The capture is only:
- logistics, errands, scheduling, instructions for the task at hand, or the next steps of something under way;
- small talk, a greeting, filler, or a reaction with no content ("nice", "wow", "that's crazy");
- narration of what's happening or happened, venting about one event or what someone did that one time, one
  instance of a pattern I don't state, or what's wrong with the thing in front of me, not stated as a rule;
- an insult or put-down with no point beyond it;
- someone else's point that I only agree with, say yes to or repeat back, adding nothing of my own;
- a view I voice only to mock, parody or reject it, deadpan included: asked straight, I wouldn't stand behind it,
  and the lines show I don't hold it;
- media playing or something read aloud, which I don't answer with a thought of my own;
- garbled transcription, or a fragment where you can't tell what I'm claiming even with the lines around it.

uncertain: only when what's shown can't settle it: the line that would decide is outside what's shown, or the
transcription is garbled enough that both readings stay open. Not for a close call: decide those by the tie-breaks.

Tie-breaks:
1. Judge my lines, read in their context, not the gist. A gist that says more than the lines doesn't make them
   eligible; a gist that states a real thought badly doesn't make it ineligible.
2. Small, personal, odd or unfinished counts. Doubt about whether it's worth keeping isn't a reason for
   ineligible: only the list above is.
3. Agreeing and then adding something of my own (a reason, a qualification, an example, a correction, a different
   conclusion) is eligible.
4. A joke, a tease or a story is eligible when it carries a point I'd stand behind asked straight, ineligible when
   it's only play about the moment.
5. If what the capture took from my lines includes an eligible thought, logistics or small talk around it don't
   make it ineligible.
6. Judge each capture alone: that another capture makes the same point doesn't change its label."""
DRAFT_SYSTEM = RUBRIC + """

You'll be shown one capture: the lines around it, the lines it cites, its quote and its gist. Label it eligible,
ineligible or uncertain under this rubric, and say why in one line, naming what in the rubric decides it. Don't
evaluate, fact-check or moralize the thought."""
DRAFT_SCHEMA = {
    "type": "object",
    "properties": {"label": {"type": "string", "enum": list(LABELS)}, "why": {"type": "string"}},
    "required": ["label", "why"], "additionalProperties": False,
}

HEADER = """# Capture eligibility: the sample (rubric {version}, {sha})

{n} live captures, drawn at random. Nothing to mark here: two models label every item under the rubric
(docs/design/eligibility-audit.md), and `im audit-eligibility review` lists, on review.md, the items I mark, blind.
"me" is me; S1, S2... are other people. This sheet quotes people: keep it in saved/.
"""
REVIEW_HEADER = """# Capture eligibility: my marks (rubric {version}, {sha})

Mark each item under the rubric (docs/design/eligibility-audit.md), on its own: after `mark:`, write `eligible`
(`e`), `ineligible` (`i`) or `uncertain` (`u`, `?`); a note is optional. Some items are here because the models
split on them, some at random from those they agree on; nothing says which. Then run `im audit-eligibility score`.
"""


def rubric() -> dict:
    """The rubric's version and the hash of its text, as recorded with everything made under it."""
    return {"version": RUBRIC_VERSION, "sha256": hashlib.sha256(RUBRIC.encode()).hexdigest()}


def _same_rubric(found: dict | None, what: str) -> None:
    now = rubric()
    if found != now:
        made = f"rubric {found['version']} ({found['sha256'][:12]})" if found else "no rubric"
        raise SystemExit(f"{what} was made under {made}, not rubric {now['version']} ({now['sha256'][:12]}): a sheet "
                         "is drafted, reviewed and scored under one rubric; make a new sheet")


def live_read(x) -> tuple[str, str]:
    """The read `im run` makes, (prompt_version, model), under the configured live prompt, model and effort."""
    if x.prompt not in extract.LIVE:
        raise SystemExit(f"extract.prompt {x.prompt} isn't live; `im run` reads with {', '.join(extract.LIVE)}")
    return extract.prompt_version(x.model, x.effort, extract.PROMPTS[x.prompt]), x.model


def captures(conn, read: tuple[str, str]) -> tuple[list[dict], int]:
    """The live speech captures under `read` (prompt_version, model) and the policy requests go out under now, on
    current episodes, with the hash of the transcript their episode's read was of (im.extractions) and their
    episode's kind; and how many archive captures there are under it, which have no lines to show."""
    pv, model = read
    rows = conn.execute(
        """SELECT i.item_id, i.episode_id, x.input_hash, i.quote, i.gist, i.source_segment_ids, e.kind
           FROM im.items i JOIN im.episodes e ON e.episode_id = i.episode_id AND e.current
           JOIN im.extractions x ON x.episode_id = i.episode_id AND x.model = i.model
             AND x.prompt_version = i.prompt_version AND x.privacy_policy_version = i.privacy_policy_version
           WHERE i.prompt_version = %s AND i.model = %s AND i.privacy_policy_version = %s
           ORDER BY i.item_id""", (pv, model, egress.POLICY_VERSION)).fetchall()
    out = [{"item_id": str(item), "episode_id": str(eid), "read_sha256": sha, "quote": quote, "gist": gist,
            "segments": [str(s) for s in segs], "kind": kind}
           for item, eid, sha, quote, gist, segs, kind in rows]
    archive = conn.execute("""SELECT count(*) FROM im.items WHERE episode_id IS NULL AND prompt_version = %s
                              AND model = %s AND privacy_policy_version = %s""",
                           (pv, model, egress.POLICY_VERSION)).fetchone()[0]
    return out, archive


def _shown(p: egress.Payload, c: dict) -> tuple[str, list[str]] | None:
    """What an item shows, the same on the sheet and to a model: the lines around it and the ones it cites, as the
    extractor was sent them, its quote and gist; and the segments of the lines shown. None if it cites no line of
    the transcript."""
    lines = lines_of(p.text)
    at = {str(s): n for n, s in enumerate(p.segment_ids, 1)}
    cited = sorted(at[s] for s in c["segments"] if s in at)
    if not cited:
        return None
    around = [n for n in range(cited[0] - BEFORE, cited[-1] + AFTER + 1) if n in lines]
    show = lambda ns: "\n".join(f"[{n}] {lines[n][0]}: {lines[n][1]}" for n in ns)  # noqa: E731
    text = (f"Around it:\n{show(around)}\n\nCited lines:\n{show(cited)}\n\nQuote: \"{c['quote']}\"\n"
            f"Gist: {c['gist']}")
    return text, [str(p.segment_ids[n - 1]) for n in around]


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _spread(caps: list[dict]) -> dict:
    count = lambda xs: dict(sorted({x: xs.count(x) for x in xs}.items()))  # noqa: E731
    return {"N": len(caps), "by_kind": count([c["kind"] for c in caps])}


def build(conn, read: tuple[str, str], seed: int = 10, size: int = SIZE) -> tuple[str, dict]:
    """The sheet's text and its key: `size` live captures drawn uniformly at random (by `seed`), never by a model's
    score. A capture whose episode's transcript changed since it was read can't be shown as read, so it's passed
    over and counted; the draw goes on to the next."""
    caps, archive = captures(conn, read)
    rng = random.Random(seed)
    picked, changed, payloads = [], 0, {}
    for c in rng.sample(caps, len(caps)):
        if len(picked) == size:
            break
        if c["episode_id"] not in payloads:
            payloads[c["episode_id"]] = egress.build(conn, c["episode_id"])
        p = payloads[c["episode_id"]]
        shown = _shown(p, c) if p.sha256 == c["read_sha256"] else None
        if shown is None:
            changed += 1
            continue
        picked.append(c | {"text": shown[0], "shown_segments": shown[1]})
    r = rubric()
    lines = [HEADER.format(version=r["version"], sha=r["sha256"][:12], n=len(picked))]
    for num, c in enumerate(picked, 1):
        lines += [f"## {num}", "", c["text"], ""]
    key = {"audit": "capture eligibility", "rubric": r, "seed": seed, "size": size,
           "read": {"prompt_version": read[0], "model": read[1]}, "policy": egress.POLICY_VERSION,
           "population": _spread(caps) | {"archive_skipped": archive, "payload_changed_passed_over": changed},
           "sample": _spread(picked),
           "items": {str(num): {"item_id": c["item_id"], "episode_id": c["episode_id"],
                                "read_sha256": c["read_sha256"], "shown_sha256": _sha(c["text"]),
                                "kind": c["kind"], "segments": c["segments"], "shown_segments": c["shown_segments"]}
                     for num, c in enumerate(picked, 1)}}
    return "\n".join(lines), key


def write(conn, out: Path, read: tuple[str, str], seed: int = 10, size: int = SIZE) -> dict:
    """Write sheet.md and key.json into `out`. Sends nothing. It won't write over a sheet: drafts and marks would
    no longer match it."""
    out.mkdir(parents=True, exist_ok=True)
    sheet, key = out / "sheet.md", out / "key.json"
    if sheet.exists() or key.exists():
        raise SystemExit(f"{out} already has a sheet; draft, review or score it, or pick another folder")
    text, k = build(conn, read, seed, size)
    sheet.write_text(text)
    key.write_text(json.dumps(k, indent=2))
    return {"sheet": str(sheet), "key": str(key), "rubric": k["rubric"], "items": len(k["items"]),
            "population": k["population"], "sample": k["sample"]}


def _key(folder: Path) -> dict:
    key = json.loads((folder / "key.json").read_text())
    _same_rubric(key.get("rubric"), f"{folder}'s sheet")
    return key


def _sections(text: str) -> dict[str, str]:
    parts = re.split(r"^##[ \t]+(\d+)[ \t]*$", text, flags=re.M)
    return dict(zip(parts[1::2], parts[2::2]))


def _family(model: str) -> str:
    """Who makes a model: the provider prefix of an OpenRouter id; Anthropic for a bare Claude id."""
    return "anthropic" if model.startswith("claude") else model.split("/", 1)[0]


def _draft_path(folder: Path, model: str, effort: str) -> Path:
    return folder / f"draft-{model.replace('/', '_')}-{effort}.json"


def _as_shown(conn, item: dict) -> str | None:
    """The item's text as the sheet showed it, rebuilt from the transcript, if its episode is still current and its
    transcript and the item's text haven't changed since: a forgotten or corrected line never goes out from a sheet."""
    current = conn.execute("SELECT current FROM im.episodes WHERE episode_id = %s", (item["episode_id"],)).fetchone()
    if not (current and current[0]):
        return None
    p = egress.build(conn, item["episode_id"])
    if p.sha256 != item["read_sha256"]:
        return None
    shown = conn.execute("SELECT quote, gist, source_segment_ids FROM im.items WHERE item_id = %s",
                         (item["item_id"],)).fetchone()
    if shown is None:
        return None
    got = _shown(p, {"quote": shown[0], "gist": shown[1], "segments": [str(s) for s in shown[2]]})
    return got[0] if got and _sha(got[0]) == item["shown_sha256"] else None


def draft(conn, client, folder: Path, model: str, effort: str = "low", monthly_cap_usd: float = 20.0) -> dict:
    """`model` labels every item on the sheet under the rubric, into draft-<model>-<effort>.json, as it goes: each
    request is the rubric and one item, nothing else, so no draft sees another or my marks. Each goes through the
    egress log, under the monthly cap. A rerun asks only what's missing; an item whose episode was forgotten or
    changed since the sheet is never sent, and counted. A failed answer is logged and asked again next time."""
    key = _key(folder)
    if _family(model) == "anthropic":
        raise SystemExit(f"{model}: Opus wrote the captures, so no Anthropic model drafts their eligibility; use two "
                         "of other families, e.g. openai/gpt-6.1-terra and google/gemini-4-carbon")
    path = _draft_path(folder, model, effort)
    got = json.loads(path.read_text()) if path.exists() else {
        "rubric": rubric(), "model": model, "effort": effort, "items": {}, "changed": []}
    _same_rubric(got.get("rubric"), str(path))
    pv = llm.version(DRAFT_SYSTEM, DRAFT_SCHEMA, model, effort)
    if got.setdefault("prompt_version", pv) != pv:
        raise SystemExit(f"{path} was drafted under prompt {got['prompt_version']}, not {pv}: a draft isn't resumed "
                         "under another prompt")
    asked, failed, cost, stopped = 0, [], 0.0, None
    for num, item in sorted(key["items"].items(), key=lambda kv: int(kv[0])):
        if num in got["items"] or num in got["changed"]:
            continue
        text = _as_shown(conn, item)
        if text is None:
            got["changed"].append(num)
            path.write_text(json.dumps(got, indent=2))
            continue
        try:
            a = egress.ask(conn, client, model, effort, DRAFT_SYSTEM, DRAFT_SCHEMA, text,
                           segment_ids=item["shown_segments"], prompt_version=pv, monthly_cap_usd=monthly_cap_usd,
                           episode_id=item["episode_id"])
        except llm.Stop as e:
            stopped = str(e)[:200]
            break
        asked += 1
        cost += a.cost_usd
        if a.failed:
            failed.append({"item": int(num), "error": a.error[:300]})
            continue
        got["items"][num] = ({"label": "uncertain", "why": "(refused)", "refused": True} if a.refused
                             else {"label": a.data["label"], "why": a.data["why"]})
        path.write_text(json.dumps(got, indent=2))
    path.write_text(json.dumps(got, indent=2))
    return {"draft": str(path), "rubric": rubric(), "prompt_version": pv, "items": len(key["items"]),
            "drafted": len(got["items"]), "asked": asked, "cost_usd": round(cost, 6),
            "changed_not_sent": len(got["changed"]),
            **({"failed": failed} if failed else {}), **({"stopped": stopped} if stopped else {})}


def _drafts(folder: Path, key: dict) -> dict[str, dict]:
    """The two drafts, by model: exactly two, of different families, under this rubric, each with every item labeled
    or set aside as changed."""
    drafts = {}
    for d in sorted(folder.glob("draft-*.json")):
        got = json.loads(d.read_text())
        _same_rubric(got.get("rubric"), str(d))
        drafts[got["model"]] = got
    if len(drafts) != 2 or len({_family(m) for m in drafts}) != 2:
        raise SystemExit(f"{folder} needs two drafts by models of different families (it has: "
                         f"{', '.join(drafts) or 'none'}): im audit-eligibility draft {folder} --model <model>")
    for model, d in drafts.items():
        if missing := [n for n in key["items"] if n not in d["items"] and n not in d["changed"]]:
            raise SystemExit(f"{model}'s draft lacks {len(missing)} items: run its draft again")
    return drafts


def _pairs(key: dict, drafts: dict[str, dict]) -> dict[str, tuple[str, str]]:
    """Item number -> the two models' labels (in the order of `drafts`), on the items both labeled."""
    a, b = drafts.values()
    return {n: (a["items"][n]["label"], b["items"][n]["label"]) for n in sorted(key["items"], key=int)
            if n in a["items"] and n in b["items"]}


def _disputed(labels: tuple[str, str]) -> bool:
    return labels[0] != labels[1] or "uncertain" in labels


def marks(text: str) -> dict[str, dict]:
    """Item number -> {"label", "note"} from review.md. A blank mark is missing; one that isn't a label is kept as
    {"unreadable": what was written}, so a typo doesn't pass for a blank."""
    out = {}
    for num, body in _sections(text).items():
        got = {}
        if (m := re.search(r"^[ \t]*mark:[ \t]*`?([^`\n]*?)`?[ \t]*$", body, re.M | re.I)) and m.group(1):
            word = m.group(1).strip().lower()
            got = {"label": MARKS[word]} if word in MARKS else {"unreadable": m.group(1).strip()}
        if (note := re.search(r"^[ \t]*note:[ \t]*(.+)$", body, re.M | re.I)) and note.group(1).strip():
            got["note"] = note.group(1).strip()
        if got:
            out[num] = got
    return out


def review(folder: Path, audited: int = AUDITED) -> dict:
    """Write review.md: every item the drafts disagree on or either calls uncertain, and `audited` of the items they
    agree on, drawn by the sheet's seed, shuffled together, each shown as on the sheet with nothing else: no labels,
    no reasons, no models. Which is which is kept in review-key.json, for scoring. Once review.md is there it's
    never rewritten: run again, it reads my marks back and says how many are made."""
    key = _key(folder)
    path, rkey = folder / "review.md", folder / "review-key.json"
    if path.exists():
        listed = json.loads(rkey.read_text())
        _same_rubric(listed.get("rubric"), str(rkey))
        mine = marks(path.read_text())
        items = listed["disputed"] + listed["audited"]
        return {"review": str(path), "rubric": rubric(), "listed": len(items),
                "marked": sum("label" in mine.get(n, {}) for n in items),
                "unmarked": sorted((n for n in items if not {"label", "unreadable"} & set(mine.get(n, {}))), key=int),
                "unreadable": {n: m["unreadable"] for n, m in mine.items() if "unreadable" in m}}
    drafts = _drafts(folder, key)
    pairs = _pairs(key, drafts)
    disputed = [n for n, labels in pairs.items() if _disputed(labels)]
    agreed = [n for n in pairs if n not in disputed]
    rng = random.Random(f"{key['seed']}:review")
    picked = sorted(rng.sample(agreed, min(audited, len(agreed))), key=int)
    listed = disputed + picked
    rng.shuffle(listed)
    shown = _sections((folder / "sheet.md").read_text())
    r = rubric()
    lines = [REVIEW_HEADER.format(version=r["version"], sha=r["sha256"][:12])]
    for n in listed:
        lines += [f"## {n}", "", shown[n].strip(), "", "mark: ", "note: ", ""]
    path.write_text("\n".join(lines))
    rkey.write_text(json.dumps({"rubric": r, "drafts": sorted(drafts), "disputed": disputed, "audited": picked},
                               indent=2))
    return {"review": str(path), "rubric": r, "drafted_by_both": len(pairs), "disputed": len(disputed),
            "agreed": len(agreed), "audited": len(picked), "listed": len(listed)}


def kappa(pairs: list[tuple[str, str]]) -> float | None:
    """Cohen's kappa between two raters' labels: agreement beyond what their label rates give by chance."""
    n = len(pairs)
    if not n:
        return None
    seen = sum(a == b for a, b in pairs) / n
    chance = sum(sum(a == x for a, _ in pairs) * sum(b == x for _, b in pairs) for x in LABELS) / n ** 2
    return None if chance == 1 else round((seen - chance) / (1 - chance), 3)


def wilson(k: int, n: int, z: float = Z95) -> list[float] | None:
    """The Wilson score interval for k of n."""
    if not n:
        return None
    p = k / n
    mid, half = p + z * z / (2 * n), z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return [round((mid - half) / (1 + z * z / n), 3), round((mid + half) / (1 + z * z / n), 3)]


def _share(k: int, n: int) -> float | None:
    return round(k / n, 3) if n else None


def score(folder: Path) -> dict:
    """(a) The two models' agreement with each other, over every item both labeled: raw, Cohen's kappa and the
    confusion table; it never reads my marks. (b) Human validation, on the items I marked on review.md: each model's
    accuracy against my marks, and how often the label the models agreed on held on the audited agreed items. (c)
    The ineligible rate over the sample: my mark where I made one, the agreed label elsewhere; disputed items I
    haven't marked are unresolved. The rate is over the items labeled eligible or ineligible, with its Wilson 95%
    interval; uncertain ones are counted beside it."""
    key = _key(folder)
    drafts = _drafts(folder, key)
    pairs = _pairs(key, drafts)
    a, b = drafts
    agreed = sum(x == y for x, y in pairs.values())
    out = {"rubric": rubric(), "items": len(key["items"]), "models": [a, b],
           "a_model_agreement": {
               "N": len(pairs), "agreed": agreed, "raw": _share(agreed, len(pairs)),
               "kappa": kappa(list(pairs.values())),
               "confusion": {f"{a}={x}": {f"{b}={y}": sum(p == (x, y) for p in pairs.values()) for y in LABELS}
                             for x in LABELS},
               "not_drafted_changed": sorted({n for d in drafts.values() for n in d["changed"]}, key=int)}}
    rkey, path = folder / "review-key.json", folder / "review.md"
    if not (rkey.exists() and path.exists()):
        out["b_human_validation"] = out["c_ineligible_rate"] = "no review yet: im audit-eligibility review"
        return out
    listed = json.loads(rkey.read_text())
    _same_rubric(listed.get("rubric"), str(rkey))
    mine = {n: m["label"] for n, m in marks(path.read_text()).items()
            if "label" in m and n in listed["disputed"] + listed["audited"]}
    by_model = {}
    for i, model in enumerate((a, b)):
        right = sum(pairs[n][i] == m for n, m in mine.items())
        by_model[model] = {"N": len(mine), "correct": right, "accuracy": _share(right, len(mine))}
    audited = {n: m for n, m in mine.items() if n in listed["audited"]}
    held = sum(pairs[n][0] == m for n, m in audited.items())
    out["b_human_validation"] = {
        "N_listed": len(listed["disputed"]) + len(listed["audited"]), "N_marked": len(mine),
        "my_marks": {x: list(mine.values()).count(x) for x in LABELS},
        "accuracy_against_my_marks": by_model,
        "agreed_label_held": {"N_audited": len(listed["audited"]), "N_marked": len(audited), "held": held,
                              "rate": _share(held, len(audited))}}
    final = {n: mine.get(n) or (pairs[n][0] if not _disputed(pairs[n]) else None) for n in pairs}
    counts = {x: sum(f == x for f in final.values()) for x in LABELS}
    decided = counts["eligible"] + counts["ineligible"]
    out["c_ineligible_rate"] = {
        "N_items": len(pairs), "from_my_marks": len(mine), "from_agreed_labels": sum(
            n not in mine and f is not None for n, f in final.items()),
        "unresolved": sorted((n for n, f in final.items() if f is None), key=int), **counts,
        "N": decided, "rate": _share(counts["ineligible"], decided), "ci95": wilson(counts["ineligible"], decided)}
    return out
