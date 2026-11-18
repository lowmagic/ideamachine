"""A spike, not a stage: can analogies be found by comparing ideas' structure instead of their topic?

The B4 sheet (2026-11-02) had six pairs I marked `none` with an `analogy:` note: different subjects, the same
underlying move. Embeddings place ideas by topic, so they never put such a pair near each other. This tries the
alternative on those pairs: a model writes each cluster's structure in one line with its subject matter removed,
the lines are embedded, and each analogy pair's partner is ranked among all the clusters, by structure and, as the
baseline, by topic. If partners rank high by structure and low by topic, structure is worth a design doc.

Everything goes to the egress log; the structures and the result are written into the sheet's folder, which quotes
people (saved/). Nothing in the lattice changes.
"""

import json
import random
from pathlib import Path

import numpy as np

from im import edge_audit, egress

MODEL, EFFORT = "claude-opus-5-5", "low"
BATCH = 25

STRUCTURE_SYSTEM = """For each idea from my idea lattice, captured from my conversations, write its structure in \
one line with the subject matter removed: the general pattern, mechanism or move it rests on, in abstract terms \
(an agent, an option, a cost, a system, a signal, a group), never the specific domain, people, products or \
things. Two ideas from different domains that make the same move should get nearly the same line. Keep it under \
25 words. Don't evaluate the ideas."""

STRUCTURE_SCHEMA = {
    "type": "object",
    "properties": {"structures": {"type": "array", "items": {
        "type": "object", "properties": {"idea": {"type": "integer"}, "structure": {"type": "string"}},
        "required": ["idea", "structure"], "additionalProperties": False}}},
    "required": ["structures"], "additionalProperties": False,
}


def analogy_pairs(folder: Path) -> list[dict]:
    """The pairs I marked with an `analogy:` note: their clusters and my note."""
    key = json.loads((folder / "key.json").read_text())
    marks = edge_audit.marks((folder / "sheet.md").read_text())
    return [{"pair": int(n), "ideas": key["pairs"][n]["sides"], "note": m["note"]}
            for n, m in sorted(marks.items(), key=lambda kv: int(kv[0]))
            if (m.get("note") or "").lower().startswith("analogy") and n in key["pairs"]]


def _text(conn, idea_id: str) -> str:
    title, statement = conn.execute("SELECT title, statement FROM im.idea_text WHERE idea_id = %s",
                                    (idea_id,)).fetchone()
    quotes = [q for (q,) in conn.execute(
        """SELECT quote FROM pub.card_evidence WHERE idea_id = %s ORDER BY speaker = 'me' DESC, said_at LIMIT 2""",
        (idea_id,))]
    return f"{title}: {statement}" + "".join(f' Said: "{q}"' for q in quotes)


def _rank(M: np.ndarray, i: int, j: int) -> int:
    """Where j comes among everyone but i, by similarity to i: 1 is nearest."""
    s = M @ M[i]
    s[i] = -np.inf
    return int((s > s[j]).sum()) + 1


def run(conn, client, embedder, folder: Path, others: int = 300, seed: int = 7, monthly_cap_usd: float = 20.0) -> dict:
    from im import llm

    pairs = analogy_pairs(folder)
    if not pairs:
        raise SystemExit(f"no `analogy:` notes on the sheet in {folder}")
    targets = sorted({i for p in pairs for i in p["ideas"]})
    pool = [r[0] for r in conn.execute("""SELECT c.idea_id::text FROM pub.cards c
                                          JOIN im.idea_embeddings e USING (idea_id)
                                          WHERE e.model_version = pub.embedding_model() ORDER BY 1""")]
    rest = [i for i in pool if i not in targets]
    ids = targets + random.Random(seed).sample(rest, min(others, len(rest)))

    path = folder / "structures.json"
    structures = json.loads(path.read_text()) if path.exists() else {}
    pv = llm.version(STRUCTURE_SYSTEM, STRUCTURE_SCHEMA, MODEL, EFFORT)
    todo = [i for i in ids if i not in structures]
    cost = 0.0
    for start in range(0, len(todo), BATCH):
        batch = todo[start:start + BATCH]
        text = "Ideas:\n" + "\n".join(f"{n}. {_text(conn, i)}" for n, i in enumerate(batch, 1))
        a = egress.ask(conn, client, MODEL, EFFORT, STRUCTURE_SYSTEM, STRUCTURE_SCHEMA, text, prompt_version=pv,
                       segment_ids=egress.segments_of(conn, batch), monthly_cap_usd=monthly_cap_usd)
        cost += a.cost_usd
        for s in (a.data or {"structures": []})["structures"]:   # refused or failed: asked again by a rerun
            if 1 <= s["idea"] <= len(batch):
                structures[batch[s["idea"] - 1]] = s["structure"]
        path.write_text(json.dumps(structures, indent=2))   # as it goes: a rerun picks up where this stopped

    ids = [i for i in ids if i in structures]
    index = {x: n for n, x in enumerate(ids)}
    by_structure = embedder.embed([structures[i] for i in ids])
    topic = {r[0]: np.array(json.loads(r[1])) for r in conn.execute(
        """SELECT idea_id::text, embedding::text FROM im.idea_embeddings
           WHERE model_version = pub.embedding_model() AND idea_id::text = ANY(%s)""", (ids,))}
    by_topic = np.vstack([topic[i] / np.linalg.norm(topic[i]) for i in ids])
    rows = []
    for p in pairs:
        a, b = p["ideas"]
        if a not in index or b not in index:
            continue
        i, j = index[a], index[b]
        rows.append({"pair": p["pair"], "note": p["note"],
                     "structure_a": structures[a], "structure_b": structures[b],
                     "rank_by_structure": min(_rank(by_structure, i, j), _rank(by_structure, j, i)),
                     "rank_by_topic": min(_rank(by_topic, i, j), _rank(by_topic, j, i))})
    within = lambda key, k: sum(r[key] <= k for r in rows)  # noqa: E731
    result = {"clusters": len(ids), "pairs": len(rows), "cost_usd": round(cost, 4), "prompt_version": pv,
              "median_rank": {"structure": float(np.median([r["rank_by_structure"] for r in rows])),
                              "topic": float(np.median([r["rank_by_topic"] for r in rows]))},
              "partner_in_top_10": {"structure": within("rank_by_structure", 10), "topic": within("rank_by_topic", 10)},
              "partner_in_top_25": {"structure": within("rank_by_structure", 25), "topic": within("rank_by_topic", 25)},
              "pairs_detail": rows}
    (folder / "analogy-spike.json").write_text(json.dumps(result, indent=2))
    return result
