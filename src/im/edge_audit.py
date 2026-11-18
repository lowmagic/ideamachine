"""B4 (docs/ROADMAP-v2.md): blind sheets of pairs of clusters for me to type by hand, the labels Jev is scored on.

What the pilot (20 pairs, 2026-11-02) settled: the answers stay supports, contradicts, extends, related and none,
with the matcher's `evolves` counted as `extends`; direction is cheap to give; and contradictions and supports
don't turn up among nearest neighbors, so the sheet goes looking for them. Marks were mine, made blind: a model's
marks would score Jev against another model. Since labels 3, a pair I leave unmarked takes the chat models' mark
where they agree (see review below), and the pooled score counts those marks (`by_models`).

What the first sheet (50 pairs, 2026-11-04, ten models drafting) changed, labels 2: `extends` becomes `elaborates`,
as I mark it (an instance, a description, an application), where every model read "extends" as something narrower
and answered `related`; a later version is the matcher's `evolves`, not an elaboration. And `contradicts` becomes
`tension`, which includes contradiction: I found none, but noted two pairs pulling against each other. Old marks and
drafts read `extends` as `elaborates` and `contradicts` as `tension`.

What B4 with Jev (129 pairs, 2026-11-06) changed, labels 3 (decision 0010): `related` was "the same subject, but
none of the above", and a shared subject is what a lens gathers, not an edge. An edge says two points belong in the
same tight neighborhood, so `related` is that, with no kind to say how, and a shared subject alone is `none`. Drafts
under labels 2 answered the old question and stay in their own files.

The pairs come in five sets:
- near: nearest neighbors by embedding among candidates and ideas (SPEC §5: embeddings nominate pairs), spread
  across the range of similarity rather than only the closest;
- evolves: pairs the matcher linked as one evolving into the other;
- apart: neighbors among candidates and ideas first said far apart in time, where a change of mind would show;
- levels: a candidate or idea next to a nearby concrete observation (anyone's), where support would show;
- far: pairs below the median similarity, so `none` is represented.
The sets are shuffled together, each pair's sides in random order, with no similarity or set shown. Each side shows
what was said first, then Claude's summary, marked as Claude's. The sheet quotes people: keep it, and the key, in
saved/ (deploy.md).
"""

import json
import random
import re
from pathlib import Path

import numpy as np

from im import connect, jev

SIZE = 50
NEAR_K = 5
WIDE_K = 10    # how far the apart and levels sets look for neighbors
SHARE = {"evolves": 0.12, "apart": 0.16, "levels": 0.16, "far": 0.08}   # the rest of the sheet is near pairs
SETS = ("near", "evolves", "apart", "levels", "far")
CONCRETE = ("observation",)          # item kinds that are about a case, not a claim
CLAIMS = ("idea", "aphorism")
LABELSET = 3   # see the module docstring; drafts under a later set go to their own files
LABELS = {"supports": "supports", "sup": "supports", "tension": "tension", "ten": "tension",
          "contradicts": "tension", "con": "tension", "elaborates": "elaborates", "ela": "elaborates",
          "extends": "elaborates", "ext": "elaborates", "related": "related", "rel": "related", "none": "none",
          "same": "same", "unsure": "unsure", "?": "unsure"}
DIRECTED = ("supports", "elaborates")

HEADER = """# B4: typing edges (blind)

Each pair is two clusters from the lattice: what was said, then Claude's summary of it. Judge by what was said;
the summary can be wrong. How does A relate to B? After `mark:`, write one of:

- `supports` (`sup`): one is a reason to believe the other: evidence or an argument for it. Test: if A is true,
  is B more believable, while still saying the same thing? (A case that only illustrates it is `elaborates`.)
- `elaborates` (`ela`): one fills in the other: a specific case or example of it, a description of how it works,
  an application, a generalization, or something built on it. Test: does one fill in or carry forward the other?
- `tension` (`ten`): they pull against each other: a contradiction, a change of mind, a trade-off, or two pulls
  that would have to be balanced. Test: does holding one make the other harder to hold as stated?
- `related` (`rel`): they belong together, in the same tight neighbourhood of ideas, but none of the above says
  how. Test: is there more between them than their subject?
- `none`: no real connection. Being about the same subject, or a broad area (relationships, emotions, work), isn't
  one: a lens gathers those.
- `same`: the same idea said twice (a merge the matcher missed).
- `unsure` (`?`).

A later version of an idea is `elaborates` if it refines it, `tension` if it reverses it, and `same` if it only
says it again.

For `supports` and `elaborates`, add `A>B` if A supports or elaborates B, or `B>A` the other way; leave it off if it
runs both ways or you can't say. Use `note:` for anything worth saying, such as a summary that misstates the quotes.
Mark it yourself, without a model's help: these marks are what Jev is scored against.
Then run `im audit-edges score` on this folder. "me" is me, "someone" anyone else.
"""


def _clusters(conn) -> list[tuple]:
    """Every cluster in pub.ideas, with its embedding (under the model most were embedded with), whether it's mine
    (a candidate or idea), when it was first said, and the kind most of its captures are."""
    return conn.execute(
        """WITH v AS (SELECT model_version FROM im.idea_embeddings GROUP BY 1 ORDER BY count(*) DESC LIMIT 1),
           said AS (SELECT ev.idea_id, e.started_at AS at, i.kind FROM im.idea_evidence ev
                    JOIN im.items i USING (item_id) JOIN im.episodes e ON e.episode_id = i.episode_id AND e.current
                    UNION ALL
                    SELECT idea_id, said_at, NULL FROM im.observations WHERE idea_id IS NOT NULL)
           SELECT p.idea_id::text, e.embedding::text, m.maturity IN ('candidate', 'idea'),
                  (SELECT min(at) FROM said WHERE said.idea_id = p.idea_id),
                  (SELECT mode() WITHIN GROUP (ORDER BY kind) FROM said WHERE said.idea_id = p.idea_id)
           FROM pub.ideas p JOIN im.idea_maturity m USING (idea_id)
           JOIN im.idea_embeddings e ON e.idea_id = p.idea_id AND e.model_version = (SELECT model_version FROM v)
           ORDER BY p.idea_id""").fetchall()


def candidates(conn) -> dict[str, list[dict]]:
    """Every pair each set could draw from, as {"ideas": [a, b], "similarity": s}, a < b."""
    rows = _clusters(conn)
    mine = [n for n, r in enumerate(rows) if r[2]]
    if len(mine) < 2:
        return {s: [] for s in SETS}
    ids = [r[0] for r in rows]
    M = np.vstack([np.array(json.loads(r[1])) for r in rows])
    M = M / np.linalg.norm(M, axis=1, keepdims=True)
    S = M @ M.T
    np.fill_diagonal(S, -np.inf)
    pair = lambda i, j: {"ideas": sorted([ids[i], ids[j]]), "similarity": round(float(S[i, j]), 3)}  # noqa: E731
    by_sim = lambda ps: sorted(ps, key=lambda p: (-p["similarity"], p["ideas"]))  # noqa: E731

    def neighbours(k: int, among: list[int]) -> set[tuple[int, int]]:
        out = set()
        for i in mine:
            for j in [among[x] for x in np.argsort(-S[i, among])[:k]]:   # S[i, i] is -inf: never among them
                if j != i:
                    out.add(tuple(sorted((i, j))))
        return out

    near = neighbours(NEAR_K, mine)
    index = {x: n for n, x in enumerate(ids)}
    evolves = [pair(index[a], index[b]) for a, b in conn.execute(
        "SELECT from_id::text, to_id::text FROM im.lineage WHERE kind = 'evolves' ORDER BY 1, 2")
        if a in index and b in index and rows[index[a]][2] and rows[index[b]][2]]
    wide = neighbours(WIDE_K, mine)
    gap = {(i, j): abs((rows[i][3] - rows[j][3]).days) for i, j in wide if rows[i][3] and rows[j][3]}
    longest = float(np.quantile(list(gap.values()), 0.75)) if gap else 0
    apart = [pair(i, j) for (i, j), d in gap.items() if d >= max(longest, 1)]
    levels = [pair(i, j) for i, j in neighbours(WIDE_K, list(range(len(ids))))
              if {rows[i][4] in CONCRETE, rows[j][4] in CONCRETE} == {True, False}
              and (rows[i][4] in CLAIMS or rows[j][4] in CLAIMS)]
    sub = S[np.ix_(mine, mine)][np.triu_indices(len(mine), 1)]
    median = float(np.median(sub))
    far = [pair(mine[a], mine[b]) for a in range(len(mine)) for b in range(a + 1, len(mine))
           if S[mine[a], mine[b]] < median and tuple(sorted((mine[a], mine[b]))) not in near]
    return {"near": by_sim(pair(i, j) for i, j in near), "evolves": evolves, "apart": by_sim(apart),
            "levels": by_sim(levels), "far": far}


def _spread(pairs: list[dict], n: int) -> list[dict]:
    """n pairs evenly across the list (sorted by similarity), so the sample covers the whole range."""
    if n >= len(pairs):
        return list(pairs)
    return [pairs[round(k * (len(pairs) - 1) / max(n - 1, 1))] for k in range(n)]


def build(conn, seed: int = 4, size: int = SIZE, exclude: set | None = None) -> tuple[str, dict]:
    """The sheet's text and its key. `exclude` leaves out pairs already on earlier sheets: marking one again is wasted,
    since pooled sheets count a pair once."""
    rng = random.Random(seed)
    pool = candidates(conn)
    taken, picked = set(exclude or ()), []

    def take(pairs: list[dict], n: int, kind: str) -> None:
        for p in pairs:
            if n <= 0:
                return
            if tuple(p["ideas"]) not in taken:
                taken.add(tuple(p["ideas"]))
                picked.append(p | {"set": kind})
                n -= 1

    for kind in ("evolves", "apart", "levels"):
        take(rng.sample(pool[kind], len(pool[kind])), round(SHARE[kind] * size), kind)
    take(rng.sample(pool["far"], min(len(pool["far"]), 10 * size)), round(SHARE["far"] * size), "far")
    near = [p for p in pool["near"] if tuple(p["ideas"]) not in taken]   # an evolves pair is often a near one too
    take(_spread(near, size - len(picked)), size - len(picked), "near")
    for kind in SETS:   # a small lattice: whatever is left, so the sheet still has `size` pairs if it can
        take(pool[kind], size - len(picked), kind)
    rng.shuffle(picked)
    for p in picked:
        p["sides"] = rng.sample(p["ideas"], 2)
    lines = [HEADER]
    for n, p in enumerate(picked, 1):
        lines += [f"## {n}", ""]
        for side, idea in zip("AB", p["sides"]):
            lines += [*connect.shown(connect.side_data(conn, idea), side), ""]
        lines += ["mark: ", "note: ", ""]
    key = {"benchmark": "B4", "seed": seed, "size": size, "near_k": NEAR_K, "wide_k": WIDE_K,
           "excluded": len(exclude or ()),
           "available": {k: len(v) for k, v in pool.items()},
           "pairs": {str(n): p for n, p in enumerate(picked, 1)}}
    return "\n".join(lines), key


def write(conn, out: Path, seed: int = 4, size: int = SIZE, exclude_folders: list[Path] = ()) -> dict:
    """Write sheet.md and key.json into `out`, without the pairs on the sheets in `exclude_folders`. It won't write
    over a sheet: marks would be lost."""
    from im import probe_audit

    out.mkdir(parents=True, exist_ok=True)
    sheet, key = out / "sheet.md", out / "key.json"
    if sheet.exists() or key.exists():
        raise SystemExit(f"{out} already has a sheet; score it, or pick another folder")
    text, k = build(conn, seed, size, probe_audit.earlier(list(exclude_folders)))
    sheet.write_text(text)
    key.write_text(json.dumps(k, indent=2))
    sets = [p["set"] for p in k["pairs"].values()]
    return {"sheet": str(sheet), "key": str(key), "pairs": len(sets),
            "by_set": {s: sets.count(s) for s in SETS}, "available": k["available"]}


def read_sheet(text: str) -> dict[str, tuple[str, str | None, str | None]]:
    """Pair number -> (what the sheet showed, what I wrote after `mark:`, my note), None where I wrote nothing. The
    sheet's own text ends at the pair's first `mark:` or `note:` line, so nothing I type under it reaches a draft;
    my note runs from `note:` to the next pair, however many lines it takes."""
    out = {}
    parts = re.split(r"^##[ \t]+(\d+)[ \t]*$", text, flags=re.M)
    for n, body in zip(parts[1::2], parts[2::2]):
        cut = re.search(r"^[ \t]*(?:mark|note):", body, re.M | re.I)
        shown, mine = (body[:cut.start()], body[cut.start():]) if cut else (body, "")
        mark = re.search(r"^[ \t]*mark:(.*)$", mine, re.M | re.I)
        note = re.search(r"^[ \t]*note:(.*)", mine, re.M | re.I | re.S)
        note_lines = [x.strip() for x in note.group(1).splitlines() if x.strip()] if note else []
        out[n] = (shown.strip(), (mark.group(1).strip() or None) if mark else None, "\n".join(note_lines) or None)
    return out


def marks(text: str) -> dict:
    """Pair number -> {"label", "direction", "note"} from a filled-in sheet. A blank mark is missing; one that isn't
    a label is kept as {"unreadable": what was written}, so a typo doesn't hide among the pairs I skipped."""
    out = {}
    for n, (_, mark, note) in read_sheet(text).items():
        got = {}
        if mark and (m := re.match(r"`?([a-z?]+)`?\s*([AB]\s*>\s*[AB])?", mark, re.I)):
            label = LABELS.get(m.group(1).lower())
            if label:
                arrow = re.sub(r"\s", "", m.group(2) or "").upper()
                got = {"label": label, "direction": arrow if label in DIRECTED and arrow in ("A>B", "B>A") else None}
            else:
                got["unreadable"] = mark
        if note:
            got["note"] = note
        if got:
            out[n] = got
    return out


def score(folder: Path) -> dict:
    """My marks, tallied: how often each label came up, per set, how often I gave a direction, and my notes."""
    key = json.loads((folder / "key.json").read_text())
    got = marks((folder / "sheet.md").read_text())
    counts = {s: {} for s in SETS}
    missing, unreadable, notes, directed, labels = [], [], [], {"given": 0, "of": 0}, []
    for num, p in sorted(key["pairs"].items(), key=lambda kv: int(kv[0])):
        mark = got.get(num, {})
        label = mark.get("label")
        if label is None and "unreadable" in mark:
            unreadable.append({"pair": int(num), "mark": mark["unreadable"]})
        elif label is None:
            missing.append(int(num))
        else:
            counts[p["set"]][label] = counts[p["set"]].get(label, 0) + 1
            labels.append({"pair": int(num), "set": p["set"], "similarity": p["similarity"], "label": label,
                           "direction": mark.get("direction"), "a": p["sides"][0], "b": p["sides"][1]})
            if label in DIRECTED:
                directed["of"] += 1
                directed["given"] += mark.get("direction") is not None
        if mark.get("note"):
            notes.append({"pair": int(num), "label": label, "note": mark["note"]})
    total = {}
    for c in counts.values():
        for label, k in c.items():
            total[label] = total.get(label, 0) + k
    mine = {num: {"label": m.get("label"), "note": m.get("note")} for num, m in got.items()}
    drafts = {d.stem.removeprefix("draft-"): agreement(mine, json.loads(d.read_text()))
              for d in sorted(folder.glob("draft-*.json"))}
    return {"benchmark": key["benchmark"], "marked": len(labels), "missing": missing,
            **({"unreadable": unreadable} if unreadable else {}), "labels": total,
            "by_set": {s: c for s, c in counts.items() if c},
            "direction_given": f"{directed['given']} of {directed['of']}", "notes": notes,
            **({"drafts": drafts} if drafts else {}), "marks": labels}


def pooled(folders: list[Path]) -> dict:
    """Several sheets' marks scored as one benchmark (B4's bar is for 200 pairs): label counts, and each model's
    agreement over every pair it drafted. A pair of clusters drawn by more than one sheet counts once, by the
    first sheet's mark. Pairs are named `<folder>/<number>`."""
    mine, drafts, seen, dupes, unreadable = {}, {}, set(), [], []
    by_set = {s: {} for s in SETS}
    for folder in folders:
        key = json.loads((folder / "key.json").read_text())
        got = marks((folder / "sheet.md").read_text())
        theirs = {d.stem.removeprefix("draft-"): json.loads(d.read_text()) for d in sorted(folder.glob("draft-*.json"))}
        for num, p in key["pairs"].items():
            name, label = f"{folder.name}/{num}", got.get(num, {}).get("label")
            if label is None:
                if "unreadable" in got.get(num, {}):
                    unreadable.append({"pair": name, "mark": got[num]["unreadable"]})
                continue
            if tuple(p["ideas"]) in seen:
                dupes.append(name)
                continue
            seen.add(tuple(p["ideas"]))
            mine[name] = {"label": label, "note": got[num].get("note")}
            by_set[p["set"]][label] = by_set[p["set"]].get(label, 0) + 1
            for model, marked in theirs.items():
                if num in marked:
                    drafts.setdefault(model, {})[name] = marked[num]
    labels: dict = {}
    for c in by_set.values():
        for label, k in c.items():
            labels[label] = labels.get(label, 0) + k
    return {"sheets": [str(f) for f in folders], "marked": len(mine),
            "by_models": sum(DRAFTED in (m["note"] or "") and REVIEWED not in (m["note"] or "") for m in mine.values()),
            "counted_once": dupes,
            **({"unreadable": unreadable} if unreadable else {}), "labels": labels,
            "by_set": {s: c for s, c in by_set.items() if c},
            "drafts": {model: agreement({n: m for n, m in mine.items() if n in theirs}, theirs)
                       for model, theirs in sorted(drafts.items())}}


# --- A model's marks on the same sheet (B4's agreement test) ---------------------------------------------------------
#
# The prompt Terra passed with is the connection judge's (connect.SYSTEM and SCHEMA): a draft by Terra at low effort
# is logged under the judge's prompt version.

CORE = ("none", "related", "elaborates", "tension")   # the labels macro_f1_core averages, over those I've used


def _pairs_as_shown(text: str) -> dict[str, str]:
    """Pair number -> what the sheet showed for it, without my mark or note: what the model sees is what I saw."""
    return {n: shown for n, (shown, _, _) in read_sheet(text).items()}


def policy_tag() -> str:
    """What a draft's file name says of the policy its pairs went out under, e.g. `-B1`, so a draft never resumes, or
    is scored as, a draft under another. A draft named without one went out under policy C, before it was retired
    (decision 0008)."""
    from im import egress
    return "-" + egress.POLICY_VERSION.split(";")[0]


def draft(conn, client, folder: Path, model: str = "claude-opus-5-5", effort: str = "low",
          monthly_cap_usd: float = 20.0) -> dict:
    """A model marks every pair on the sheet, blind to my marks, into draft-<model>-<effort>-labels<n>-<policy>.json.
    Each request goes to the egress log, like every other call that sends what was said. A pair whose answer fails is
    logged and skipped, so a rerun asks again; the cap, or the model being unavailable, stops the draft."""
    from im import egress, llm

    key = json.loads((folder / "key.json").read_text())
    if model.startswith("jev"):
        return _draft_jev(conn, client, folder, key, monthly_cap_usd)
    shown = _pairs_as_shown((folder / "sheet.md").read_text())
    out_path = folder / f"draft-{model.replace('/', '_')}-{effort}-labels{LABELSET}{policy_tag()}.json"
    got = json.loads(out_path.read_text()) if out_path.exists() else {}
    pv = llm.version(connect.SYSTEM, connect.SCHEMA, model, effort)
    cost, failed, stopped = 0.0, [], None
    for num, p in sorted(key["pairs"].items(), key=lambda kv: int(kv[0])):
        if num in got or num not in shown:
            continue
        try:
            a = egress.ask(conn, client, model, effort, connect.SYSTEM, connect.SCHEMA, shown[num], max_tokens=4000,
                           segment_ids=egress.segments_of(conn, p["ideas"]), prompt_version=pv,
                           monthly_cap_usd=monthly_cap_usd)
        except llm.Stop as e:
            stopped = str(e)[:200]
            break
        cost += a.cost_usd
        if a.failed:
            failed.append({"pair": int(num), "error": a.error[:300]})
            continue
        if a.refused:
            got[num] = {"label": "unsure", "direction": "none", "analogy": "", "refused": True}
        else:
            got[num] = a.data
        out_path.write_text(json.dumps(got, indent=2))   # as it goes: a rerun picks up where this stopped
    return {"draft": str(out_path), "pairs": len(got), "cost_usd": round(cost, 4), "prompt_version": pv,
            **({"failed": failed} if failed else {}), **({"stopped": stopped} if stopped else {})}


# --- Reviewing where the models disagree with me ---------------------------------------------------------------------
#
# Marking every pair blind is slow. Instead the chat models draft under the current label set, each with its case in a
# sentence, and I read only the pairs where one of them disagrees with me on whether there's an edge. If its case
# convinces me, its label becomes my mark: the point is edges worth seeing, not my first guess. A pair I left unmarked
# takes the models' mark when they agree on whether there's an edge, and comes to me when they don't: on the three
# sheets re-marked under labels 3 (129 pairs, 2026-11-07), Opus missed none of my edges and Terra made none I didn't,
# so wherever the two agreed they agreed with me.

AGREE, KEEP = ("agree", "a", "yes", "y"), ("keep", "k", "no", "n")
REVIEWED = "reviewed:"   # starts the note line a review adds, so a pair is reviewed once
DRAFTED = "drafted:"     # starts the note line on a pair the models marked, agreeing, where I left it unmarked


def _linked(label: str | None) -> bool:
    return label not in (None, "none", "unsure")


def _cases(folder: Path) -> dict[str, dict]:
    """The chat models' drafts under the current label set, by model. Jev's give no case."""
    return {d.stem.removeprefix("draft-"): json.loads(d.read_text())
            for d in sorted(folder.glob(f"draft-*-labels{LABELSET}*.json")) if not d.stem.startswith("draft-jev")}


def _sections(text: str) -> dict[str, str]:
    parts = re.split(r"^##[ \t]+(\d+)[ \t]*$", text, flags=re.M)
    return dict(zip(parts[1::2], parts[2::2]))


def _set_mark(text: str, num: str, label: str | None, note: str) -> str:
    """The sheet with pair `num` marked `label` (None leaves the mark as it is), and `note` added as the last line of
    its note."""
    parts = re.split(r"^(##[ \t]+\d+[ \t]*)$", text, flags=re.M)
    for i in range(1, len(parts), 2):
        if parts[i].split()[1] != num:
            continue
        body = parts[i + 1] if label is None else re.sub(r"^([ \t]*mark:).*$", rf"\g<1> {label}", parts[i + 1],
                                                         count=1, flags=re.M | re.I)
        end = len(body) - len(body.rstrip("\n"))
        line = note if re.search(r"^[ \t]*note:", body, re.M | re.I) else f"note: {note}"
        parts[i + 1] = body.rstrip("\n") + "\n" + line + "\n" * max(end, 1)
    return "".join(parts)


def _apply(folder: Path) -> dict:
    """My answers on review.md, into my marks: `agree` takes the proposed label, `keep` keeps mine, a label is mine
    now. Either way the pair's note says it was reviewed, and what it was. An answer I can't read, or `keep` on a pair
    I hadn't marked, is left alone."""
    path = folder / "review.md"
    if not path.exists():
        return {"changed": 0, "kept": 0}
    sheet = (folder / "sheet.md").read_text()
    mine = marks(sheet)
    changed = kept = 0
    for num, body in _sections(path.read_text()).items():
        answer = re.search(r"^answer:(.*)$", body, re.M)
        proposed = re.search(r"^proposed: (\w+)$", body, re.M)
        word = answer.group(1).strip().strip("`").lower() if answer else ""
        was = mine.get(num, {}).get("label")
        if not word or not proposed or REVIEWED in (mine.get(num, {}).get("note") or ""):
            continue
        label = proposed.group(1) if word in AGREE else was if word in KEEP else LABELS.get(word)
        if label is None:
            continue
        changed += label != was
        kept += label == was
        sheet = _set_mark(sheet, num, None if label == was else label,
                          f"{REVIEWED} {'was ' + (was or 'unmarked') if label != was else 'kept'}")
    (folder / "sheet.md").write_text(sheet)
    return {"changed": changed, "kept": kept}


def _models_mark(verdicts: list[str]) -> str:
    """The mark the models agree on: their label if they gave one, `related` if they gave different kinds of edge."""
    return verdicts[0] if len(set(verdicts)) == 1 else "related"


def review(folders: list[Path], every: bool = False) -> dict:
    """Apply what I answered on each folder's review.md, then write a new one: every pair where a chat model
    disagrees with my mark on whether there's an edge and I haven't reviewed it yet, with each model's case. A pair I
    left unmarked is marked from the models when two or more drafted it and agree on whether there's an edge, and is
    on review.md, with every case, when they don't. With `every`, the models' marks I haven't reviewed are on it too,
    to spot-check: left blank, a mark stands. A pair two sheets share is reviewed on the first, as pooling counts it."""
    out, seen = {}, set()
    for folder in folders:
        applied = _apply(folder)
        key = json.loads((folder / "key.json").read_text())["pairs"]
        sheet = (folder / "sheet.md").read_text()
        shown, mine, cases = _pairs_as_shown(sheet), marks(sheet), _cases(folder)
        lines = [f"# B4 review: where a model disagrees with me ({folder.name})", "",
                 "After `answer:`, write `agree` to take the proposed label, `keep` to keep my mark, or a label. Where "
                 "I left the pair unmarked, the models split: `agree` takes the proposed edge, `none` says there isn't "
                 "one. Where the models agree (`mine: <label>, the models'`), a blank answer leaves their mark. Then "
                 "run `im audit-edges review` again: it applies the answers and writes what's left.", ""]
        n = drafted = spot = 0
        for num, p in sorted(key.items(), key=lambda kv: int(kv[0])):
            pair, m = tuple(p["ideas"]), mine.get(num, {})
            if pair in seen:
                continue
            seen.add(pair)
            label, note = m.get("label"), m.get("note") or ""
            said = {model: d[num] for model, d in cases.items() if num in d and d[num]["label"] != "unsure"}
            unmarked = label is None and "unreadable" not in m and len(said) >= 2
            if unmarked and len({_linked(d["label"]) for d in said.values()}) == 1:
                label, note = _models_mark([d["label"] for d in said.values()]), f"{DRAFTED} {', '.join(said)} agree"
                sheet = _set_mark(sheet, num, label, note)
                drafted += 1
                unmarked = False
            if unmarked:
                against = said
                proposed = [d["label"] for d in said.values() if _linked(d["label"])]
            elif label in (None, "unsure") or REVIEWED in note:
                continue
            elif DRAFTED in note:
                if not every:
                    continue
                against, proposed = said, [label]
                spot += 1
            else:
                against = {model: d for model, d in said.items() if _linked(d["label"]) != _linked(label)}
                proposed = [d["label"] for d in against.values()]
            if not against:
                continue
            n += DRAFTED not in note
            mark = f"{label}, the models'" if DRAFTED in note else label or "unmarked"
            lines += [f"## {num}", "", shown[num], "", f"mine: {mark}", f"proposed: {max(proposed, key=proposed.count)}"]
            lines += [f"{model}: {d['label']}. {d.get('why') or '(no case given)'}" for model, d in against.items()]
            lines += ["", "answer: ", ""]
        (folder / "sheet.md").write_text(sheet)
        (folder / "review.md").write_text("\n".join(lines))
        out[folder.name] = applied | {"drafted": drafted, "to_review": n, **({"spot_check": spot} if every else {}),
                                      "models": sorted(cases)}
    return out


EDGE_CUT = jev.CUT["edge"]   # Jev's probability of an edge at which a draft says there is one; scored at each cut
KIND_CUT = 0.5    # a kind Jev rates at least this likely is its suggestion; otherwise, connected, it's related


def jev_label(p: dict[str, float]) -> str:
    """B4's label from Jev's answers: none under EDGE_CUT; otherwise same, or the likeliest of supports, elaborates
    and tension if it's likely enough, or else related."""
    if p["p"] < EDGE_CUT:
        return "none"
    if p.get("same", 0) >= KIND_CUT:
        return "same"
    kind = max(("supports", "elaborates", "tension"), key=lambda k: (p.get(k, 0), k))
    return kind if p.get(kind, 0) >= KIND_CUT else "related"


def _draft_jev(conn, client, folder: Path, key: dict, monthly_cap_usd: float) -> dict:
    """Jev marks every pair from structured state (each side's quotes and summary, A and B as the sheet shows them),
    one request of atomic questions per pair, into draft-<model>-<question set>-<probe set>-labels<n>-<policy>.json.
    Each pair keeps every probability beside its label, so connection can be scored at every cut."""
    from im import egress, llm, probe_audit

    out_path = folder / (f"draft-{client.model}-{jev.QUESTION_SETS['edge']}-{jev.PROBE_SET['edge']}"
                         f"-labels{LABELSET}{policy_tag()}.json")
    got = json.loads(out_path.read_text()) if out_path.exists() else {}
    cost, failed, stopped = 0.0, [], None
    for num, p in sorted(key["pairs"].items(), key=lambda kv: int(kv[0])):
        if num in got:
            continue
        a, b = p.get("sides") or p["ideas"]
        state = {"a": connect.side_data(conn, a), "b": connect.side_data(conn, b)}
        try:
            ans = probe_audit._ask_jev(conn, client, "edge", state, True, egress.segments_of(conn, p["ideas"]),
                                       monthly_cap_usd)
        except llm.Stop as e:
            stopped = str(e)[:200]
            break
        cost += ans.cost_usd
        if ans.failed:
            failed.append({"pair": int(num), "error": (ans.error or "")[:300]})
            continue
        got[num] = {k: round(v, 3) for k, v in ans.data.items()} | {"label": jev_label(ans.data), "direction": "none",
                                                                     "analogy": ""}
        out_path.write_text(json.dumps(got, indent=2))   # as it goes: a rerun picks up where this stopped
    return {"draft": str(out_path), "pairs": len(got), "cost_usd": round(cost, 6),
            **({"failed": failed} if failed else {}), **({"stopped": stopped} if stopped else {})}


def _num(name: str):
    """A pair's number on one sheet; `<folder>/<number>` when sheets are pooled."""
    return int(name) if name.isdigit() else name


def agreement(mine: dict, theirs: dict) -> dict:
    """How a model's marks compare with mine, on the pairs I didn't mark unsure.

    mine: pair -> {"label", "note"}; theirs: pair -> {"label", "analogy"}."""
    # Old marks and drafts in labels 2's words: extends is elaborates, contradicts is tension.
    mine = {n: m | {"label": LABELS.get(m.get("label"), m.get("label"))} for n, m in mine.items()}
    theirs = {n: t | {"label": LABELS.get(t["label"], t["label"])} for n, t in theirs.items()}
    pairs = [n for n, m in mine.items() if m.get("label") not in (None, "unsure") and n in theirs]
    confusion: dict = {}
    for n in pairs:
        row = confusion.setdefault(mine[n]["label"], {})
        row[theirs[n]["label"]] = row.get(theirs[n]["label"], 0) + 1
    f1 = {}
    for label in CORE:
        tp = sum(mine[n]["label"] == label and theirs[n]["label"] == label for n in pairs)
        fp = sum(mine[n]["label"] != label and theirs[n]["label"] == label for n in pairs)
        fn = sum(mine[n]["label"] == label and theirs[n]["label"] != label for n in pairs)
        f1[label] = round(2 * tp / (2 * tp + fp + fn), 3) if tp + fp + fn else None
    used = {mine[n]["label"] for n in pairs}
    scored = [f1[label] for label in CORE if label in used]   # a label I never gave can't be averaged in
    linked = lambda label: label not in ("none", "unsure")  # noqa: E731 (an edge exists)
    mine_linked = {n for n in pairs if linked(mine[n]["label"])}
    their_linked = {n for n in pairs if linked(theirs[n]["label"])}
    invented = [_num(n) for n in pairs if theirs[n]["label"] in ("supports", "tension")
                and mine[n]["label"] not in ("supports", "tension")]
    said_tension = [n for n in pairs if theirs[n]["label"] == "tension"]
    my_analogies = {n for n, m in mine.items() if (m.get("note") or "").lower().startswith("analogy")}
    their_analogies = {n for n in theirs if (theirs[n].get("analogy") or "").strip()}
    ratio = lambda a, b: round(a / b, 3) if b else None  # noqa: E731
    my_tensions = {n for n in pairs if mine[n]["label"] == "tension"}
    return {"pairs": len(pairs),
            **({"connection_by_cut": _connection_by_cut(pairs, mine_linked, theirs)}
               if pairs and all("p" in theirs[n] for n in pairs) else {}),
            # B4's bar (decision 0009): whether there's an edge. The kind is a suggestion; it's reported below.
            "connection": {"agree": ratio(sum((n in mine_linked) == (n in their_linked) for n in pairs), len(pairs)),
                           "precision": ratio(len(mine_linked & their_linked), len(their_linked)),
                           "recall": ratio(len(mine_linked & their_linked), len(mine_linked)),
                           "invented": sorted(map(_num, their_linked - mine_linked)),
                           "missed": sorted(map(_num, mine_linked - their_linked))},
            "tension": {"mine": len(my_tensions), "theirs": len(said_tension),
                        "both": len(my_tensions & set(said_tension))},
            "agree": round(sum(mine[n]["label"] == theirs[n]["label"] for n in pairs) / len(pairs), 3)
            if pairs else None,
            "f1": f1, "macro_f1_core": round(sum(scored) / len(scored), 3) if scored else None,
            "invented_supports_or_tension": sorted(invented),
            "tension_precision": round(sum(mine[n]["label"] == "tension" for n in said_tension) / len(said_tension), 3)
            if said_tension else None,
            "analogies": {"mine": sorted(map(_num, my_analogies)), "theirs": sorted(map(_num, their_analogies)),
                          "both": sorted(map(_num, my_analogies & their_analogies)),
                          "recall": ratio(len(my_analogies & their_analogies), len(my_analogies)),
                          "precision": ratio(len(my_analogies & their_analogies), len(their_analogies))},
            "confusion_mine_to_theirs": confusion}


EDGE_CUTS = (0.5, 0.6, 0.7, 0.75, 0.8, 0.85, 0.9)


def _connection_by_cut(pairs: list, mine_linked: set, theirs: dict) -> dict:
    """For a draft that kept its probability of an edge (Jev's): agreement, precision and recall on connection at
    each cut, so the cut can be chosen from my marks; the chance that a pair I linked scores above one I didn't (AUC,
    ties half); and that chance for each question it answered, deciding or probe, so wordings can be compared."""
    ratio = lambda a, b: round(a / b, 3) if b else None  # noqa: E731
    out = {}
    for cut in EDGE_CUTS:
        yes = {n for n in pairs if theirs[n]["p"] >= cut}
        out[str(cut)] = {"agree": ratio(sum((n in mine_linked) == (n in yes) for n in pairs), len(pairs)),
                         "precision": ratio(len(mine_linked & yes), len(yes)),
                         "recall": ratio(len(mine_linked & yes), len(mine_linked))}
    linked, unlinked = [n for n in pairs if n in mine_linked], [n for n in pairs if n not in mine_linked]

    def auc(q: str):
        wins = sum((theirs[a][q] > theirs[b][q]) + 0.5 * (theirs[a][q] == theirs[b][q])
                   for a in linked for b in unlinked)
        return ratio(wins, len(linked) * len(unlinked))

    asked = [q for q, v in theirs[pairs[0]].items() if q != "p" and isinstance(v, float)
             and all(isinstance(theirs[n].get(q), float) for n in pairs)]
    return out | {"auc": auc("p"), "auc_by_question": dict(sorted(((q, auc(q)) for q in asked),
                                                                  key=lambda kv: -(kv[1] or 0)))}
