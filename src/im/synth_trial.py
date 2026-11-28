"""Synthesis blind trial (docs/design/synthesis.md; ROADMAP-v2 Synthesis, decision 0010).

Draws real connected neighborhoods (`lens.neighbourhoods` over `pub.card_links`, >= 3 cards across >= 2 conversations),
drafts candidate syntheses with one or more models through `egress.ask` (leaving `im.ideas` and `im.lineage`
untouched), builds a blind review sheet shuffling each model's synthesis beside the neighborhood's best existing card,
and scores the operator's picks.
"""

import json
import random
import re
from collections import defaultdict
from importlib import resources
from pathlib import Path

from im import egress, lens, llm

MIN_CARDS = 3
MIN_CONVERSATIONS = 2
MAX_CARDS = 12
QUOTES_PER_CARD = 2

SYSTEM_V1 = resources.files("im.prompts").joinpath("synthesize_v1.txt").read_text().rstrip("\n")

SCHEMA = {
    "type": "object",
    "properties": {
        "synthesizable": {"type": "boolean"},
        "title": {"type": "string"},
        "statement": {"type": "string"},
        "source_ids": {"type": "array", "items": {"type": "integer"}},
        "reason": {"type": "string"},
    },
    "required": ["synthesizable", "title", "statement", "source_ids", "reason"],
    "additionalProperties": False,
}


def _clean(text: str) -> str:
    return " ".join((text or "").split())


def _qualifying_neighborhoods(conn) -> list[dict]:
    """Connected neighborhoods of >= MIN_CARDS non-dismissed, non-synthesis cards across >= MIN_CONVERSATIONS."""
    dismissed = {str(r[0]) for r in conn.execute(
        """SELECT idea_id FROM (
             SELECT DISTINCT ON (idea_id) idea_id, kind FROM pub.feedback_events
             WHERE kind IN ('idea_keep', 'idea_unkeep', 'idea_dismiss')
             ORDER BY idea_id, event_id DESC
           ) x WHERE kind = 'idea_dismiss'""")}
    already_synth = {str(r[0]) for r in conn.execute(
        "SELECT DISTINCT from_id FROM im.lineage WHERE kind = 'synthesizes'")}
    excluded = dismissed | already_synth

    cards: dict[str, dict] = {}
    for iid, title, stmt, convs, first_said in conn.execute(
            "SELECT idea_id::text, title, statement, conversations, first_said FROM pub.cards"):
        if iid not in excluded:
            cards[iid] = {"idea_id": iid, "title": _clean(title), "statement": _clean(stmt),
                          "conversations": convs or 0, "first_said": str(first_said or ""),
                          "quotes": [], "conv_ids": set(), "segment_ids": []}

    for iid, speaker, quote, cid in conn.execute(
            """SELECT idea_id::text, speaker, quote, conversation_id FROM pub.card_evidence
               ORDER BY speaker = 'me' DESC, said_at NULLS LAST, item_id, observation_id"""):
        c = cards.get(iid)
        if not c:
            continue
        if cid is not None:
            c["conv_ids"].add(str(cid))
        q = _clean(quote)
        if q and len(c["quotes"]) < QUOTES_PER_CARD and q not in c["quotes"]:
            c["quotes"].append(q)

    for iid, segs in conn.execute(
            """SELECT ev.idea_id::text, i.source_segment_ids
               FROM im.idea_evidence ev JOIN im.items i USING (item_id)
               JOIN im.episodes e ON e.episode_id = i.episode_id AND e.current"""):
        c = cards.get(iid)
        if c and segs:
            c["segment_ids"].extend(str(s) for s in segs)

    pairs = [(a, b) for a, b in conn.execute("SELECT a::text, b::text FROM pub.card_links")
             if a in cards and b in cards]
    kinds: dict[tuple[str, str], list[str]] = defaultdict(list)
    for a, b, k, d in conn.execute("SELECT a::text, b::text, kind, direction FROM im.connection_kinds"):
        kinds[(a, b)].append(f"{k} ({d})" if d else k)

    out = []
    for group in lens.neighbourhoods(pairs):
        if len(group) < MIN_CARDS:
            continue
        gset = set(group)
        convs = set().union(*(cards[i]["conv_ids"] for i in group))
        if len(convs) < MIN_CONVERSATIONS:
            continue
        deg: dict[str, int] = defaultdict(int)
        internal = []
        for a, b in pairs:
            if a in gset and b in gset:
                deg[a] += 1
                deg[b] += 1
                lo, hi = (a, b) if a < b else (b, a)
                internal.append((a, b, kinds.get((lo, hi), [])))
        ordered = sorted(group, key=lambda i: (-deg[i], -cards[i]["conversations"], cards[i]["first_said"], i))
        chosen = ordered[:MAX_CARDS]
        cset = set(chosen)
        idx = {iid: n for n, iid in enumerate(chosen, 1)}
        links = [{"a": idx[a], "b": idx[b], "kinds": ks} for a, b, ks in internal if a in cset and b in cset]
        seg_ids = sorted({s for iid in chosen for s in cards[iid]["segment_ids"]})
        out.append({
            "id": chosen[0][:8],
            "total_cards": len(group),
            "conversations": len(convs),
            "baseline_num": 1,   # chosen[0] has the highest internal degree in the neighborhood
            "cards": [{"num": idx[iid], "idea_id": iid, "title": cards[iid]["title"],
                       "statement": cards[iid]["statement"], "quotes": cards[iid]["quotes"],
                       "degree": deg[iid], "conversations": cards[iid]["conversations"]}
                      for iid in chosen],
            "links": links,
            "segment_ids": seg_ids,
        })
    out.sort(key=lambda n: (-n["total_cards"], -n["conversations"], n["id"]))
    return out


def _format_neighborhood(nb: dict) -> str:
    lines = [f"Neighborhood ({nb['total_cards']} cards across {nb['conversations']} conversations):", ""]
    for c in nb["cards"]:
        lines.append(f"{c['num']}. {c['title']}")
        lines.append(f"   Statement: {c['statement']}")
        for q in c["quotes"]:
            lines.append(f'   Quote: "{q}"')
    if nb["links"]:
        lines.append("")
        lines.append("Connections:")
        for lk in nb["links"]:
            tag = f" [{', '.join(lk['kinds'])}]" if lk["kinds"] else ""
            lines.append(f"- {lk['a']} <-> {lk['b']}{tag}")
    return "\n".join(lines)


def sheet(conn, folder: Path, size: int = 15, seed: int = 1) -> dict:
    """Sample up to `size` qualifying connected neighborhoods and write key.json + neighborhoods.md (sends nothing)."""
    folder.mkdir(parents=True, exist_ok=True)
    all_nbs = _qualifying_neighborhoods(conn)
    rng = random.Random(seed)
    picked = rng.sample(all_nbs, size) if len(all_nbs) > size else list(all_nbs)
    key = {"seed": seed, "total_qualifying": len(all_nbs), "neighborhoods": {str(i): nb for i, nb in enumerate(picked, 1)}}
    (folder / "key.json").write_text(json.dumps(key, indent=2) + "\n")
    md = [f"# Synthesis trial neighborhoods ({len(picked)} of {len(all_nbs)} qualifying, seed {seed})\n"]
    for i, nb in enumerate(picked, 1):
        md.append(f"## Neighborhood {i}\n\n{_format_neighborhood(nb)}\n")
    (folder / "neighborhoods.md").write_text("\n".join(md))
    return {"folder": str(folder), "qualifying": len(all_nbs), "sampled": len(picked), "seed": seed}


def _read_synthesis(answer: dict, n_cards: int) -> dict:
    if not isinstance(answer, dict) or not isinstance(answer.get("synthesizable"), bool):
        raise ValueError("missing boolean synthesizable")
    reason = _clean(answer.get("reason", ""))
    if not reason:
        raise ValueError("empty reason")
    if not answer["synthesizable"]:
        return {"synthesizable": False, "title": "", "statement": "", "source_ids": [], "reason": reason}
    title = _clean(answer.get("title", ""))
    statement = _clean(answer.get("statement", ""))
    if not title or not statement:
        raise ValueError("synthesizable=true requires non-empty title and statement")
    raw_ids = answer.get("source_ids")
    if not isinstance(raw_ids, list):
        raise ValueError("missing source_ids")
    seen: list[int] = []
    for x in raw_ids:
        if isinstance(x, bool) or not isinstance(x, int) or not (1 <= x <= n_cards):
            raise ValueError(f"invalid source_id {x!r} for 1..{n_cards}")
        if x not in seen:
            seen.append(x)
    if len(seen) < 2:
        raise ValueError("synthesizable=true requires at least 2 distinct source_ids")
    return {"synthesizable": True, "title": title, "statement": statement, "source_ids": seen, "reason": reason}


def _draft_path(folder: Path, model: str, effort: str) -> Path:
    return folder / f"draft-{model.replace('/', '_')}-{effort}.json"


def draft(conn, client, folder: Path, model: str, effort: str = "medium",
          monthly_cap_usd: float = 20.0) -> dict:
    """Draft syntheses for every neighborhood in folder/key.json with `model` (resumable, logged through egress)."""
    key = json.loads((folder / "key.json").read_text())
    out_path = _draft_path(folder, model, effort)
    got = json.loads(out_path.read_text()) if out_path.exists() else {"model": model, "effort": effort, "results": {}}
    pv = llm.version(SYSTEM_V1, SCHEMA, model, effort)
    stats = {"model": model, "effort": effort, "prompt_version": pv, "done": 0, "skipped": 0,
             "synthesizable": 0, "declined": 0, "failed": 0, "cost_usd": 0.0, "stopped": None}
    for num, nb in sorted(key["neighborhoods"].items(), key=lambda kv: int(kv[0])):
        if num in got["results"]:
            stats["skipped"] += 1
            continue
        prompt = _format_neighborhood(nb)
        try:
            a = egress.ask(conn, client, model, effort, SYSTEM_V1, SCHEMA, prompt,
                           segment_ids=nb["segment_ids"], prompt_version=pv,
                           monthly_cap_usd=monthly_cap_usd, policy=egress.POLICY_VERSION,
                           read=lambda ans, n=len(nb["cards"]): _read_synthesis(ans, n))
        except llm.Stop as e:
            stats["stopped"] = str(e)[:200]
            break
        stats["cost_usd"] += a.cost_usd
        if a.failed or a.refused or a.data is None:
            stats["failed"] += 1
            continue
        got["results"][num] = a.data | {"cost_usd": round(a.cost_usd, 6)}
        out_path.write_text(json.dumps(got, indent=2) + "\n")
        stats["done"] += 1
        if a.data["synthesizable"]:
            stats["synthesizable"] += 1
        else:
            stats["declined"] += 1
    stats["cost_usd"] = round(stats["cost_usd"], 4)
    return stats


def _load_drafts(folder: Path) -> list[tuple[str, dict]]:
    drafts = []
    for p in sorted(folder.glob("draft-*.json")):
        d = json.loads(p.read_text())
        label = f"{d['model']}:{d['effort']}"
        drafts.append((label, d["results"]))
    return drafts


def review(folder: Path) -> dict:
    """Write a blind review.md shuffling each model's proposed synthesis beside the best existing card baseline.
    Never overwrites an existing review.md."""
    rev_path = folder / "review.md"
    if rev_path.exists():
        raise SystemExit(f"{rev_path} already exists; edit it and run `im trial-synthesis score {folder}`")
    key = json.loads((folder / "key.json").read_text())
    drafts = _load_drafts(folder)
    if not drafts:
        raise SystemExit(f"no draft-*.json files in {folder}; run `im trial-synthesis draft` first")
    rng = random.Random(key["seed"] + 99)
    rev_key: dict[str, dict] = {}
    lines = [
        "# Blind synthesis review",
        "",
        "For each neighborhood, read the cards and pick the best option (`A`, `B`, `C`, ...) or `none` if none",
        "deserves to be proposed. One of the options is the neighborhood's best existing card.",
        "",
    ]
    shown = 0
    for num, nb in sorted(key["neighborhoods"].items(), key=lambda kv: int(kv[0])):
        opts = []
        any_synth = False
        for label, results in drafts:
            r = results.get(num)
            if r and r.get("synthesizable"):
                any_synth = True
                opts.append({"source": label, "title": r["title"], "statement": r["statement"],
                             "source_ids": r["source_ids"]})
        if not any_synth:
            continue
        base = nb["cards"][nb["baseline_num"] - 1]
        opts.append({"source": "baseline", "title": base["title"], "statement": base["statement"],
                     "source_ids": [base["num"]]})
        rng.shuffle(opts)
        letters = {}
        shown += 1
        lines.append(f"## Neighborhood {num}")
        lines.append("")
        lines.append(_format_neighborhood(nb))
        lines.append("")
        for letter, opt in zip("ABCDEFGHIJ", opts):
            letters[letter] = opt["source"]
            lines.append(f"**Option {letter}:** {opt['title']}")
            lines.append(f"> {opt['statement']}")
            lines.append("")
        lines.append("pick: ")
        lines.append("note: ")
        lines.append("")
        rev_key[num] = letters
    rev_path.write_text("\n".join(lines))
    (folder / "review-key.json").write_text(json.dumps(rev_key, indent=2) + "\n")
    return {"folder": str(folder), "neighborhoods_for_review": shown, "models": [m for m, _ in drafts]}


def score(folder: Path) -> dict:
    """Tally blind picks from review.md against review-key.json and draft-*.json."""
    rev_key = json.loads((folder / "review-key.json").read_text())
    drafts = _load_drafts(folder)
    text = (folder / "review.md").read_text()
    picks: dict[str, str] = {}
    cur = None
    for line in text.splitlines():
        m = re.match(r"^##\s+Neighborhood\s+(\d+)\s*$", line)
        if m:
            cur = m.group(1)
            continue
        p = re.match(r"^pick:\s*(\S+)", line, re.IGNORECASE)
        if p and cur is not None:
            picks[cur] = p.group(1).strip().upper()
    wins: dict[str, int] = defaultdict(int)
    marked, unmarked, invalid = 0, [], []
    for num, letters in sorted(rev_key.items(), key=lambda kv: int(kv[0])):
        pick = picks.get(num)
        if not pick:
            unmarked.append(num)
            continue
        if pick == "NONE":
            wins["none"] += 1
            marked += 1
        elif pick in letters:
            wins[letters[pick]] += 1
            marked += 1
        else:
            invalid.append(f"{num}:{pick}")
    model_stats = {}
    for label, results in drafts:
        n_total = len(results)
        n_synth = sum(1 for r in results.values() if r.get("synthesizable"))
        cost = round(sum(r.get("cost_usd", 0.0) for r in results.values()), 4)
        model_stats[label] = {
            "drafted": n_total, "synthesizable": n_synth, "declined": n_total - n_synth,
            "wins": wins.get(label, 0),
            "win_rate_of_marked": round(wins.get(label, 0) / marked, 3) if marked else None,
            "cost_usd": cost,
        }
    synth_wins = sum(wins.get(label, 0) for label, _ in drafts)
    return {
        "marked": marked, "unmarked": unmarked, "invalid": invalid,
        "synthesis_wins": synth_wins,
        "baseline_wins": wins.get("baseline", 0),
        "none_wins": wins.get("none", 0),
        "synthesis_win_rate": round(synth_wins / marked, 3) if marked else None,
        "models": model_stats,
    }
