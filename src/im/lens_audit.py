"""The lens pilot (docs/design/m5-lenses-and-mcp.md): before lenses are stored, try `jev` lens wordings on every
card, to see how many each lets in and where its cut falls, and reword before the blind sheets that judge them.
Measurement only: nothing here is a lens, and nothing it writes is read by the pipeline.

The wordings are mine and personal, so they're never in this repo: they're a `lenses.json` in the folder (keep it in
saved/), shaped as

    {"lenses": [{"id": "l1", "name": "...", "description": "...", "said_by_me": true, "rule": "max", "cut": 0.7,
                 "deciding": {"noul": ["the question", {"true": "...", "false": "..."}]},
                 "probes": {"noul": ["the question", null]}}]}

`rule` turns the deciding Nouls' probabilities into the lens's one: `max` (any wording) or `mean` (one question in
several wordings, as the tension pass decides). Probes are asked too, and decide nothing. A lens `said_by_me` asks
only cards I said (`pub.cards.by_me`).

1. `draft`: Jev is shown each card as the sensitivity tagger shows it (title, statement, my note, its first quotes)
   and asked every Noul of every lens whose filter admits it, in one request per card, through the egress log and
   under the monthly cap. The answers go into draft.json as they come; a rerun asks only cards not answered yet or
   whose text changed. `--limit` asks only that many, drawn in a seeded order, for a smoke run first.
2. `report`: sends nothing. Per lens: the cards asked, how many pass the cut, the spread of its probability, how
   closely its deciding wordings rank cards alike, each probe beside it, and the members it shares with each other
   lens; and near-cut.md, the cards just either side of each cut, for me to read and reword. It quotes me: saved/.
3. `sheet`: sends nothing. The blind sheet that judges the lenses named (M5 design, "The canonical lenses"), from
   the draft's answers: for each, SHEET_N of its members, SHEET_N of the embedding baseline's members it left out
   (the baseline: the cards nearest its description by embedding, as many as it has members), and SHEET_N of the
   NEAR_POOL highest-scoring cards it left out other than the baseline's, drawn by seed, shuffled together under the
   lens's name and description, nothing saying which is which. I mark each in, out or ?. Which is which is in
   sheet-key.json.
4. `score`: from my marks, per lens: precision among its members (Wilson 95%), the baseline's precision weighted by
   its two parts (members it shares with the lens, and those it alone has), the two compared where they disagree,
   and how many near misses I put in. N with each.
"""

import hashlib
import json
import random
import re
from pathlib import Path

import numpy as np

from im import egress, jev, llm, sensitivity
from im.eligibility_audit import wilson

SEED = 14
NEAR = 10       # cards each side of a cut on near-cut.md
TOP = 10        # a lens's highest cards on near-cut.md
SHEET_N = 10    # cards per stratum on a lens's sheet
NEAR_POOL = 50  # the highest-scoring cards a lens left out, that its near misses are drawn from
SHEET_SEED = 15
MARKS = {"in": "in", "i": "in", "out": "out", "o": "out", "?": "unsure", "u": "unsure"}
RULES = ("max", "mean")
_ID = re.compile(r"^[a-z][a-z0-9]{0,15}$")
_NOUL = re.compile(r"^[a-z][a-z0-9_]{0,40}$")

CARDS = """
SELECT c.idea_id::text, c.title, c.statement, coalesce(c.my_note, ''), c.by_me > 0,
       ARRAY(SELECT left(ce.quote, 200) FROM pub.card_evidence ce WHERE ce.idea_id = c.idea_id AND ce.quote IS NOT NULL
             ORDER BY ce.said_at, ce.item_id LIMIT %s)
FROM pub.cards c ORDER BY c.idea_id"""


def load(folder: Path) -> dict:
    """lenses.json, checked: every lens with an id, a name, a description, a rule, a cut and at least one deciding
    Noul, each Noul a question and its criteria (or null). Fails closed on anything else."""
    path = folder / "lenses.json"
    if not path.exists():
        raise SystemExit(f"{path} is missing: the wordings go there (keep them in saved/, never in the repo)")
    spec = json.loads(path.read_text())
    lenses = spec.get("lenses") or []
    if not lenses:
        raise SystemExit(f"{path}: no lenses")
    seen = set()
    for lens in lenses:
        lid = lens.get("id", "")
        if not _ID.match(lid) or lid in seen:
            raise SystemExit(f"{path}: lens id {lid!r} must be unique, a-z then a-z0-9, at most 16 characters")
        seen.add(lid)
        for field in ("name", "description"):
            if not isinstance(lens.get(field), str) or not lens[field].strip():
                raise SystemExit(f"{path}: lens {lid} needs a {field}")
        if lens.get("rule") not in RULES:
            raise SystemExit(f"{path}: lens {lid}'s rule must be one of {', '.join(RULES)}")
        if not isinstance(lens.get("cut"), (int, float)) or not 0 < lens["cut"] < 1:
            raise SystemExit(f"{path}: lens {lid}'s cut must be between 0 and 1")
        if not isinstance(lens.get("said_by_me"), bool):
            raise SystemExit(f"{path}: lens {lid} must say said_by_me, true or false")
        if not lens.get("deciding"):
            raise SystemExit(f"{path}: lens {lid} needs at least one deciding Noul")
        for kind in ("deciding", "probes"):
            for name, asked in (lens.get(kind) or {}).items():
                ok = (_NOUL.match(name) and isinstance(asked, list) and len(asked) == 2 and isinstance(asked[0], str)
                      and (asked[1] is None or set(asked[1]) == {"true", "false"}))
                if not ok:
                    raise SystemExit(f"{path}: lens {lid}'s Noul {name!r} must be [question, {{true, false}} or null]")
    return spec


def version(spec: dict, model: str) -> str:
    """What a draft is asked under: every wording, rule and cut, and Jev's model."""
    return hashlib.sha256(json.dumps([spec["lenses"], model], sort_keys=True).encode()).hexdigest()[:16]


def _nouls(lens: dict) -> dict:
    """The lens's Nouls as asked, each name prefixed by its lens, since one request asks every lens's."""
    return {f"{lens['id']}__{name}": tuple(asked) for kind in ("deciding", "probes")
            for name, asked in (lens.get(kind) or {}).items()}


def cards(conn) -> list[dict]:
    """Every card as Jev is shown it, with whether I said it and its text's hash."""
    out = []
    for idea, title, statement, note, by_me, said in conn.execute(CARDS, (sensitivity.QUOTES,)):
        item = ({"title": title, "statement": statement} | ({"my_note": note} if note else {})
                | ({"said": said} if said else {}))
        out.append({"id": idea, "by_me": by_me, "item": item,
                    "sha": hashlib.sha256(json.dumps(item, sort_keys=True).encode()).hexdigest()})
    return out


def _admits(lens: dict, card: dict) -> bool:
    return card["by_me"] or not lens["said_by_me"]


def draft(conn, client: jev.Jev, folder: Path, limit: int | None = None, monthly_cap_usd: float = 20.0) -> dict:
    """Ask Jev every admitting lens's Nouls about each card not answered under these wordings for its text. One
    request per card; a failed one is logged and asked again next time; the cap stops the run."""
    spec = load(folder)
    v = version(spec, client.model)
    path = folder / "draft.json"
    got = json.loads(path.read_text()) if path.exists() else {"version": v, "model": client.model, "cards": {}}
    if got["version"] != v:
        raise SystemExit(f"{path} was drafted under {got['version']}, not {v}: changed wordings are a new folder")
    todo = [c for c in cards(conn) if got["cards"].get(c["id"], {}).get("sha") != c["sha"]]
    random.Random(SEED).shuffle(todo)
    stats = {"version": v, "to_ask": len(todo), "asked": 0, "answered": 0, "failed": 0, "not_admitted": 0,
             "cost_usd": 0.0, "stopped": None}
    for card in todo[:limit]:
        asked = {k: q for lens in spec["lenses"] if _admits(lens, card) for k, q in _nouls(lens).items()}
        if not asked:
            stats["not_admitted"] += 1
            continue
        body = {"model": client.sent_as, "state": {"item": card["item"]}, "questions": jev._nouls(asked)}
        try:
            a = egress.send(conn, lambda body=body: client.complete(body), model=client.model,
                            payload=json.dumps(body), segment_ids=egress.segments_of(conn, [card["id"]]),
                            prompt_version=f"lens-pilot:{v}", monthly_cap_usd=monthly_cap_usd,
                            read=lambda r, asked=asked: {k: float(r["answers"][k]["noul"]) for k in asked})
        except llm.Stop as e:
            stats["stopped"] = str(e)[:200]
            break
        stats["asked"] += 1
        stats["cost_usd"] += a.cost_usd
        if a.failed:
            stats["failed"] += 1
            continue
        got["cards"][card["id"]] = {"sha": card["sha"], "p": {k: round(p, 4) for k, p in a.data.items()}}
        stats["answered"] += 1
        path.write_text(json.dumps(got, indent=1))
    path.write_text(json.dumps(got, indent=1))
    stats["cost_usd"] = round(stats["cost_usd"], 6)
    return stats | {"drafted": len(got["cards"])}


def _p(lens: dict, answers: dict) -> float | None:
    """The lens's probability for a card, by its rule over its deciding Nouls; None if the card wasn't asked them."""
    ps = [answers.get(f"{lens['id']}__{n}") for n in lens["deciding"]]
    if any(p is None for p in ps):
        return None
    return max(ps) if lens["rule"] == "max" else sum(ps) / len(ps)


def _ranks(xs: list[float]) -> list[float]:
    order = sorted(range(len(xs)), key=lambda i: xs[i])
    r = [0.0] * len(xs)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and xs[order[j + 1]] == xs[order[i]]:
            j += 1
        for k in range(i, j + 1):
            r[order[k]] = (i + j) / 2
        i = j + 1
    return r


def spearman(a: list[float], b: list[float]) -> float | None:
    """Rank correlation of two lists of the same cards; None with fewer than 3 or no spread."""
    if len(a) < 3:
        return None
    ra, rb = _ranks(a), _ranks(b)
    ma, mb = sum(ra) / len(ra), sum(rb) / len(rb)
    cov = sum((x - ma) * (y - mb) for x, y in zip(ra, rb, strict=True))
    va, vb = sum((x - ma) ** 2 for x in ra), sum((y - mb) ** 2 for y in rb)
    return round(cov / (va * vb) ** 0.5, 3) if va and vb else None


def _quantiles(xs: list[float]) -> dict[str, float]:
    s = sorted(xs)
    return {f"p{q}": round(s[min(len(s) - 1, int(q / 100 * len(s)))], 3) for q in (10, 25, 50, 75, 90)}


def report(conn, folder: Path) -> dict:
    """What the draft shows, per lens, and near-cut.md. Sends nothing. Cards whose text changed since they were
    answered, or that are gone, are left out and counted."""
    spec = load(folder)
    path = folder / "draft.json"
    if not path.exists():
        raise SystemExit(f"{path} is missing: run draft first")
    got = json.loads(path.read_text())
    now = {c["id"]: c for c in cards(conn)}
    live = {i: a["p"] for i, a in got["cards"].items() if i in now and now[i]["sha"] == a["sha"]}
    members, out, lines = {}, {"version": got["version"], "cards": len(now), "answered": len(live),
                               "stale_or_gone": len(got["cards"]) - len(live), "lenses": {}}, []
    for lens in spec["lenses"]:
        asked = {i: a for i, a in live.items() if _p(lens, a) is not None}
        p = {i: _p(lens, a) for i, a in asked.items()}
        admitted = sum(_admits(lens, c) for c in now.values())
        members[lens["id"]] = {i for i, x in p.items() if x >= lens["cut"]}
        ids = sorted(p)
        row = {"name": lens["name"], "admitted": admitted, "asked": len(p), "cut": lens["cut"],
               "members": len(members[lens["id"]]),
               "share": round(len(members[lens["id"]]) / len(p), 3) if p else None,
               "spread": _quantiles(list(p.values())) if p else None}
        deciding = list(lens["deciding"])
        if len(deciding) > 1:
            row["wordings"] = {f"{a}~{b}": spearman([asked[i][f"{lens['id']}__{a}"] for i in ids],
                                                    [asked[i][f"{lens['id']}__{b}"] for i in ids])
                               for n, a in enumerate(deciding) for b in deciding[n + 1:]}
        if lens.get("probes"):
            row["probes"] = {name: {"rho": spearman([asked[i][f"{lens['id']}__{name}"] for i in ids],
                                                    [p[i] for i in ids]),
                                    "at_cut": sum(asked[i][f"{lens['id']}__{name}"] >= lens["cut"] for i in ids)}
                             for name in lens["probes"]}
        out["lenses"][lens["id"]] = row
        ranked = sorted(p, key=lambda i: (-p[i], i))
        above = [i for i in ranked if p[i] >= lens["cut"]]
        below = [i for i in ranked if p[i] < lens["cut"]]
        lines += [f"## {lens['id']}: {lens['name']}", "",
                  f"{row['members']} of {row['asked']} asked pass {lens['cut']} ({admitted} admitted).", ""]
        for title, chosen in (("Highest", above[:TOP]), ("Just above the cut", above[TOP:][-NEAR:]),
                              ("Just below the cut", below[:NEAR])):
            lines += [f"### {title}", ""] + [
                f"- {p[i]:.2f} {now[i]['item']['title']}: {now[i]['item']['statement']}" for i in chosen] + [""]
    out["shared_members"] = {f"{a}&{b}": len(members[a] & members[b])
                             for n, a in enumerate(members) for b in list(members)[n + 1:]}
    (folder / "near-cut.md").write_text("\n".join(["# Lens pilot: near the cut", ""] + lines))
    (folder / "report.json").write_text(json.dumps(out, indent=1))
    return out


SHEET_HEADER = """# Lenses: the sheet

For each card: does it belong in this lens, as its description says? Write `in`, `out` or `?` (can't tell) after
`mark:`. Nothing says why a card is here; judge each on its own.
"""


def baseline(conn, embedder, description: str, ids: list[str], size: int) -> tuple[list[str], dict[str, float]]:
    """The `size` cards among `ids` nearest the description by embedding, nearest first, and every card's
    similarity. Cards with no embedding under the embedder's model are left out."""
    vecs = {i: np.array(json.loads(v)) for i, v in conn.execute(
        """SELECT idea_id::text, embedding::text FROM im.idea_embeddings
           WHERE model_version = %s AND idea_id::text = ANY(%s)""", (embedder.model_version, ids))}
    d = embedder.embed([description])[0]
    sim = {i: float(v @ d) for i, v in vecs.items()}
    return sorted(sim, key=lambda i: (-sim[i], i))[:size], sim


def _shown(item: dict) -> str:
    lines = [f"**{item['title']}**", "", item["statement"]]
    if item.get("my_note"):
        lines += ["", f"My note: {item['my_note']}"]
    lines += [""] + [f"> {q}" for q in item.get("said", [])]
    return "\n".join(lines).rstrip()


def sheet(conn, embedder, folder: Path, lens_ids: list[str], seed: int = SHEET_SEED) -> dict:
    """Write sheet.md and sheet-key.json for the lenses named, from the draft's answers. Only cards whose text is
    still what Jev was shown are drawn. Never written over: marks would no longer match."""
    spec = load(folder)
    path, kpath = folder / "sheet.md", folder / "sheet-key.json"
    if path.exists() or kpath.exists():
        raise SystemExit(f"{folder} already has a sheet; mark it and score it, or draw one in another folder")
    lenses = {lens["id"]: lens for lens in spec["lenses"]}
    if unknown := [i for i in lens_ids if i not in lenses]:
        raise SystemExit(f"no lens {', '.join(unknown)} in {folder / 'lenses.json'}")
    got = json.loads((folder / "draft.json").read_text())
    now = {c["id"]: c for c in cards(conn)}
    live = {i: a["p"] for i, a in got["cards"].items() if i in now and now[i]["sha"] == a["sha"]}
    rng = random.Random(seed)
    lines, key = [SHEET_HEADER], {"seed": seed, "version": got["version"], "lenses": {}}
    for lid in lens_ids:
        lens = lenses[lid]
        p = {i: x for i, a in live.items() if (x := _p(lens, a)) is not None}
        members = sorted(i for i, x in p.items() if x >= lens["cut"])
        base, sim = baseline(conn, embedder, lens["description"], sorted(p), len(members))
        only = sorted(set(base) - set(members))
        # The near misses: the highest-scoring cards the lens left out, other than the baseline's own, so the three
        # strata don't overlap.
        pool = sorted((i for i in p if i not in members and i not in only), key=lambda i: (-p[i], i))[:NEAR_POOL]
        drawn = [(i, "member") for i in rng.sample(members, min(SHEET_N, len(members)))]
        drawn += [(i, "baseline_only") for i in rng.sample(only, min(SHEET_N, len(only)))]
        drawn += [(i, "near") for i in rng.sample(pool, min(SHEET_N, len(pool)))]
        rng.shuffle(drawn)
        items = {}
        lines += [f"# {lens['name']}", "", lens["description"], ""]
        for n, (i, stratum) in enumerate(drawn, 1):
            num = f"{lid}-{n}"
            items[num] = {"idea_id": i, "stratum": stratum, "p": round(p[i], 4),
                          "similarity": round(sim.get(i, 0.0), 4), "in_baseline": i in base}
            lines += [f"## {num}", "", _shown(now[i]["item"]), "", "mark: ", ""]
        key["lenses"][lid] = {"name": lens["name"], "cut": lens["cut"], "asked": len(p), "members": len(members),
                              "baseline": len(base), "overlap": len(set(base) & set(members)),
                              "baseline_only": len(only), "near_pool": len(pool), "items": items}
    path.write_text("\n".join(lines))
    kpath.write_text(json.dumps(key, indent=1))
    return {"sheet": str(path), **{lid: {k: v for k, v in s.items() if k != "items"} | {"drawn": len(s["items"])}
                                   for lid, s in key["lenses"].items()}}


def marks(text: str) -> dict[str, str]:
    """Item -> `in`, `out` or `unsure` from sheet.md; a mark that isn't one is kept as `?<what was written>`, so a
    typo doesn't pass for a blank. Unmarked items are left out."""
    out = {}
    for num, body in re.findall(r"^## (\S+)\n(.*?)(?=^#{1,2} |\Z)", text, re.M | re.S):
        m = re.search(r"^mark:[ \t]*(\S*)[ \t]*$", body, re.M)
        if m and m.group(1):
            word = m.group(1).strip("`").lower()
            out[num] = MARKS.get(word, f"?{m.group(1)}")
    return out


def _rate(xs: list[str]) -> dict:
    k, n = xs.count("in"), xs.count("in") + xs.count("out")
    return {"in": k, "of": n, "share": round(k / n, 3) if n else None, "wilson95": wilson(k, n),
            "unsure": xs.count("unsure")}


def _within(marks_: dict, drawn: dict, stratum: str, in_baseline: bool | None = None) -> list[str]:
    """My marks on the items drawn in a stratum, those the baseline has or lacks if `in_baseline` says which."""
    return [m for n, m in marks_.items() if drawn[n]["stratum"] == stratum
            and (in_baseline is None or drawn[n]["in_baseline"] == in_baseline)]


def score(folder: Path) -> dict:
    """Per lens, from my marks: precision among its members; the baseline's precision, its shared part estimated
    from the members drawn that the baseline also has (the lens's own precision when none was drawn, and said so)
    and its own part from the baseline-only cards; the two where they disagree (members the baseline lacks against
    cards only the baseline has); and the near misses I put in. Unmarked and unreadable items are listed."""
    key = json.loads((folder / "sheet-key.json").read_text())
    mine = marks((folder / "sheet.md").read_text())
    out = {"unmarked": [], "unreadable": {}, "lenses": {}}
    for lid, s in key["lenses"].items():
        got = {n: mine.get(n) for n in s["items"]}
        out["unmarked"] += [n for n, m in got.items() if m is None]
        out["unreadable"] |= {n: m[1:] for n, m in got.items() if m and m.startswith("?")}
        ok = {n: m for n, m in got.items() if m in ("in", "out", "unsure")}
        drawn = s["items"]
        members = _rate(_within(ok, drawn, "member"))
        shared = _rate(_within(ok, drawn, "member", True))
        lens_only = _rate(_within(ok, drawn, "member", False))
        base_only = _rate(_within(ok, drawn, "baseline_only"))
        near = _rate(_within(ok, drawn, "near"))
        shared_p = shared["share"] if shared["share"] is not None else members["share"]
        base_p = (None if shared_p is None or base_only["share"] is None or not s["baseline"]
                  else round((s["overlap"] * shared_p + s["baseline_only"] * base_only["share"]) / s["baseline"], 3))
        out["lenses"][lid] = {"name": s["name"], "members": s["members"], "asked": s["asked"],
                              "precision": members, "baseline_precision": base_p,
                              "baseline_shared_from_lens": shared["share"] is None,
                              "disagree": {"lens_only": lens_only, "baseline_only": base_only},
                              "near_misses_in": near}
    return out
