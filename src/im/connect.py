"""The connection judge (SPEC §5, M4): which pairs of clusters are connected, decided by Terra with the prompt it
passed B4 with, behind Jev as a screen (B8).

Embeddings nominate the pairs: every two cards, in the current embedding model, at cosine FLOOR or above, where one
is among the other's K nearest. A pair I linked or unlinked is never asked: my latest decision stands. Jev asks first
whether the two belong in the same tight neighbourhood (jev.QUESTIONS["edge"], as B4's Jev drafts asked it), and
below SCREEN_CUT the pair is screened out without asking Terra. Terra types the rest as B4's sheet showed them, A
being the side with the lower id, so its `A>B` is `a>b`.

Every verdict is kept (im.connection_verdicts, migration 059), so a pair is asked again only when its text as shown
changes. A pair judged by another method (a new prompt, model, effort, screen question or cut) with the same text is
held by the re-read gate (im.reread), its verdict standing, until `im reread connect` asks it again. A connection
(supports, elaborates, tension, related or same) replaces whatever stood in for the pair, with its kind (supports,
elaborates or tension) as a suggestion; any other verdict (none, unsure, a refusal, screened) removes the pair's
stand-in and any earlier judgment's connection, and lattice.relate doesn't write the pair again. `same` is also
logged for my review (im.merge_review, `judged_same`), never merged by itself.
"""

import hashlib
import json

import numpy as np

from im import citations, egress, jev, llm, reread
from im.config import Config, ModelConfig

FLOOR, K = 0.65, 10   # nominated: cosine >= FLOOR, among either side's K nearest. Not in the method: they choose which
                      # pairs are asked, not the answer
# B8 (2026-11-12, on B4's marks): Jev's `belongs` below this screens a pair out. Chosen on sheets 1-3; held out on
# sheet 4, it missed 1 of 22 connections (a `related` Terra also rejects) and screened 45% of the near pairs.
SCREEN_CUT = 0.39
MAX_TOKENS = 4000     # as B4's drafts asked, unless [connect] sets max_tokens
CONNECTED = ("supports", "elaborates", "tension", "related", "same")
KINDS = ("supports", "elaborates", "tension")
DIRECTION = {"A>B": "a>b", "B>A": "b>a", "both": "both"}

# B4's draft prompt (edge_audit.draft), as Terra passed it: a change is a new prompt version, and B4 is run again.
SYSTEM = """You type the relationship between two clusters from my idea lattice, captured from my \
conversations. Each side shows what was said, then a summary of it; judge by what was said, since the summary can \
be wrong. "me" is me, "someone" anyone else. Choose one label:
- supports: one is a reason to believe the other: evidence or an argument for it. Test: if A is true, is B more \
believable, while still saying the same thing? A case that only illustrates it is elaborates.
- elaborates: one fills in the other: a specific case or example of it, a description of how it works, an \
application, a generalization, or something built on it. Test: does one fill in or carry forward the other?
- tension: they pull against each other: a contradiction, a change of mind, a trade-off, or two pulls that would \
have to be balanced. Test: does holding one make the other harder to hold as stated?
- related: they belong together, in the same tight neighbourhood of ideas, but none of the above says how. Test: is \
there more between them than their subject?
- none: no real connection. Being about the same subject, or the same broad area (relationships, emotions, work), \
isn't a connection.
- same: the same idea said twice.
- unsure.
A later version of an idea is elaborates if it refines it, tension if it reverses it, and same if it only says it \
again.
For supports and elaborates, give the direction: "A>B" if A supports or elaborates B, "B>A" the other way, or \
"both" if it runs both ways or you can't say; otherwise "none".
Separately, say whether the two are analogous: about different subjects, but with the same underlying structure \
(the same move, pattern or mechanism in two domains). If they are, name the shared structure in a few words; \
otherwise leave it empty. In `why`, make the case for your label in one sentence: what's between them, or why \
there's nothing beyond their subject. Don't evaluate the ideas."""

SCHEMA = {
    "type": "object",
    "properties": {"label": {"type": "string", "enum": ["supports", "elaborates", "tension", "related", "none",
                                                          "same", "unsure"]},
                   "direction": {"type": "string", "enum": ["A>B", "B>A", "both", "none"]},
                   "analogy": {"type": "string"}, "why": {"type": "string"}},
    "required": ["label", "direction", "analogy", "why"], "additionalProperties": False,
}


def prompt_version(chat: ModelConfig = Config.connect) -> str:
    """What Terra's requests are logged under: B4's draft prompt version for the same model and effort."""
    return llm.version(SYSTEM, SCHEMA, chat.model, chat.effort)


def method(chat: ModelConfig = Config.connect) -> str:
    """The judge's method, on its verdicts, connections and kinds: Terra's prompt version, model and effort, and the
    screen (Jev's question and model, and the cut). A change holds every pair judged before (the re-read gate)."""
    screen = [jev.MODEL, jev.QUESTIONS["edge"], SCREEN_CUT]
    return f"connect-1:{llm.version(prompt_version(chat), chat.model, chat.effort, screen)}"


# --- A pair as shown -------------------------------------------------------------------------------------------------

def side_data(conn, idea_id) -> dict:
    """A cluster as shown: up to two things said, mine first, with who and when; Claude's summary; my note."""
    title, statement, note = conn.execute(
        "SELECT title, statement, my_note FROM im.idea_text WHERE idea_id = %s", (idea_id,)).fetchone()
    quotes = conn.execute(
        """SELECT quote, said_on, me FROM (
             SELECT DISTINCT ON (i.observation_id) i.quote, e.started_at::date AS said_on, s.speaker = 'me' AS me
             FROM im.idea_evidence ev JOIN im.items i USING (item_id)
             JOIN im.episodes e ON e.episode_id = i.episode_id AND e.current
             JOIN im.observation_speaker s USING (observation_id)
             WHERE ev.idea_id = %(id)s ORDER BY i.observation_id, i.created_at) o
           UNION ALL
           SELECT quote, said_at::date, said_by = 'me' FROM im.observations WHERE idea_id = %(id)s
           ORDER BY me DESC, said_on, quote LIMIT 2""", {"id": idea_id}).fetchall()
    return {"said": [{"by": "me" if me else "someone", "on": str(on), "words": q} for q, on, me in quotes],
            "summary": f"{title}. {statement}", **({"my_note": note} if note else {})}


def side(d: dict) -> list[str]:
    """A cluster on a sheet: up to two things said, mine first, with when; then Claude's summary, and my note."""
    lines = [f"> \"{q['words']}\" ({q['by']}, {q['on']})" for q in d["said"]]
    lines += [f"   Claude's summary: {d['summary']}"] + ([f"   My note: {d['my_note']}"] if "my_note" in d else [])
    return lines


def shown(d: dict, label: str) -> list[str]:
    """One side of a pair as B4's sheet shows it, under its label, A or B."""
    first, *rest = side(d) or ["(nothing said on record)"]
    return [f"{label}. {first.strip()}", *[f"   {x}" if x.startswith(">") else x for x in rest]]


def pair_text(a: dict, b: dict) -> str:
    """The pair as Terra is shown it: what B4's sheet showed of a pair, A first."""
    return "\n".join([*shown(a, "A"), "", *shown(b, "B")])


# --- Nominating, screening, judging ----------------------------------------------------------------------------------

def neighbours(ids: list, M: np.ndarray) -> dict[tuple, float]:
    """(a, b) with a < b -> cosine, for every pair at FLOOR or above where one is among the other's K nearest."""
    if len(ids) < 2:
        return {}
    U = M / np.linalg.norm(M, axis=1, keepdims=True)
    S = U @ U.T
    np.fill_diagonal(S, -np.inf)
    pairs = {}
    for i, nearest in enumerate(np.argsort(-S, axis=1, kind="stable")[:, :K]):
        for j in nearest:
            if S[i, j] >= FLOOR:
                pairs[tuple(sorted((ids[i], ids[j])))] = round(float(S[i, j]), 4)
    return pairs


def nominate(conn) -> dict[tuple, float]:
    """The pairs of cards worth asking about, by their embeddings in the current model (pub.embedding_model)."""
    rows = conn.execute(
        """SELECT e.idea_id, e.embedding::text FROM im.idea_embeddings e
           WHERE e.model_version = pub.embedding_model() AND e.idea_id IN (SELECT idea_id FROM pub.cards)
           ORDER BY e.idea_id""").fetchall()
    if not rows:
        return {}
    return neighbours([r[0] for r in rows], np.vstack([np.array(json.loads(r[1])) for r in rows]))


def _due(conn, nominated: dict, m: str, sides) -> tuple[list[tuple], list[tuple], dict]:
    """The nominated pairs to ask, as (a, b, text, its hash): never asked first, then those whose text changed, each
    the most similar first. The pairs the re-read gate holds, judged by another method with the same text, as (a, b,
    text, its hash, that method, its egress id), the most similar first. And the counts of those not asked."""
    mine = set(conn.execute("SELECT a, b FROM im.my_link_events").fetchall())
    kept = {(a, b): rest for a, b, *rest in conn.execute(
        "SELECT a, b, input_hash, method, egress_id FROM im.connection_verdicts")}
    due, held, counts = [], [], {"skipped": 0, "already_judged": 0}
    for (a, b), similarity in nominated.items():
        if (a, b) in mine:
            counts["skipped"] += 1
            continue
        text = pair_text(sides(a), sides(b))
        h = hashlib.sha256(text.encode()).hexdigest()
        old = kept.get((a, b))
        if old and old[:2] == [h, m]:
            counts["already_judged"] += 1
        elif old and old[0] == h:
            held.append(((-similarity, str(a), str(b)), (a, b, text, h, *old[1:])))
        else:
            due.append(((old is not None, -similarity, str(a), str(b)), (a, b, text, h)))
    return [pair for _, pair in sorted(due)], [pair for _, pair in sorted(held)], counts


def held_report(conn, held: list[tuple], screen: jev.Jev | None, chat: ModelConfig) -> dict | None:
    """The re-read gate's report on held pairs (reread.report). Their last requests: Terra's, and Jev's screen when
    the current question set has no answer for the pair's text."""
    if not held:
        return None
    qs = jev.question_set(screen, "edge")
    jevs: dict[str, tuple] = {}
    for key, asked, egress_id in conn.execute(
            """SELECT key_sha256, question_set, egress_id FROM im.jev_answers WHERE key_sha256 = ANY(%s)
               ORDER BY created_at""", ([p[3] for p in held],)):
        if jevs.get(key, ("", None))[0] != qs:
            jevs[key] = (asked, egress_id)
    ids = [p[5] for p in held] + [jevs[p[3]][1] for p in held if p[3] in jevs and jevs[p[3]][0] != qs]
    jev_model = screen.model if screen else jev.MODEL
    return reread.report("connect", [p[4] for p in held], method(chat),
                         reread.estimate(conn, [i for i in ids if i is not None],
                                         lambda model: jev_model if model.startswith("jev-") else chat.model))


def _record(conn, a, b, m: str, v: dict, h: str, segment_ids, egress_id) -> None:
    """Keep a verdict, and make the pair's connection, kinds and merge review say what it says."""
    label = v["verdict"]
    with conn.transaction():
        conn.execute(
            """INSERT INTO im.connection_verdicts (a, b, verdict, direction, analogy, why, belongs, method, input_hash,
                                                   segment_ids, egress_id)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
               ON CONFLICT (a, b) DO UPDATE SET verdict = excluded.verdict, direction = excluded.direction,
                 analogy = excluded.analogy, why = excluded.why, belongs = excluded.belongs, method = excluded.method,
                 input_hash = excluded.input_hash, segment_ids = excluded.segment_ids, egress_id = excluded.egress_id,
                 judged_at = now()""",
            (a, b, label, v.get("direction"), v.get("analogy"), v.get("why"), v["belongs"], m, h, list(segment_ids),
             egress_id))
        conn.execute("DELETE FROM im.connection_kinds WHERE a = %s AND b = %s AND method LIKE 'connect-%%'", (a, b))
        if label not in CONNECTED:
            conn.execute("""DELETE FROM im.connections WHERE a = %s AND b = %s
                              AND (method LIKE 'embedding:%%' OR method LIKE 'connect-%%')""", (a, b))
            return
        conn.execute("""INSERT INTO im.connections (a, b, method) VALUES (%s, %s, %s)
                        ON CONFLICT (a, b) DO UPDATE SET method = excluded.method, confidence = NULL,
                          decided_at = now()""", (a, b, m))
        if label in KINDS:
            conn.execute("INSERT INTO im.connection_kinds (a, b, kind, direction, method) VALUES (%s, %s, %s, %s, %s)",
                         (a, b, label, v.get("direction"), m))
        if label == "same":
            conn.execute("""INSERT INTO im.merge_review (kind, idea_ids, method)
                            SELECT 'judged_same', ARRAY[%(a)s, %(b)s]::uuid[], %(m)s
                            WHERE NOT EXISTS (SELECT 1 FROM im.merge_review
                                              WHERE kind = 'judged_same' AND idea_ids = ARRAY[%(a)s, %(b)s]::uuid[])""",
                         {"a": a, "b": b, "m": m})


def _judge(conn, judge, screen, chat: ModelConfig, text: str, h: str, state, segment_ids, monthly_cap_usd: float,
           cost: dict) -> tuple[dict | None, int | None]:
    """One pair, screened by Jev and, if it passes, typed by Terra: (its verdict, Terra's egress_id), or (None, None)
    when an answer failed. Adds what each request cost to `cost`; raises llm.Stop at the cap."""
    p, paid, _ = jev.ask(conn, screen, "edge", h, state, segment_ids=segment_ids, monthly_cap_usd=monthly_cap_usd)
    cost["jev"] += paid
    if p is None:
        return None, None
    belongs = round(p["belongs"], 3)
    if belongs < SCREEN_CUT:
        return {"verdict": "screened", "belongs": belongs}, None
    got = egress.ask(conn, judge, chat.model, chat.effort, SYSTEM, SCHEMA, text,
                     max_tokens=chat.max_tokens or MAX_TOKENS, segment_ids=segment_ids,
                     prompt_version=prompt_version(chat), monthly_cap_usd=monthly_cap_usd)
    cost["terra"] += got.cost_usd
    if got.failed:
        return None, None
    if got.refused:
        return {"verdict": "refused", "belongs": belongs}, got.egress_id
    label = got.data["label"]
    direction = DIRECTION.get(got.data["direction"]) if label in ("supports", "elaborates") else None
    return {"verdict": label, "direction": direction, "analogy": got.data["analogy"] or None,
            "why": got.data["why"], "belongs": belongs}, got.egress_id


def _pairs(conn, m: str):
    """The nominated pairs, the due ones, the held ones and the counts (_due), and how a cluster is shown."""
    nominated = nominate(conn)
    data: dict = {}

    def sides(i):
        if i not in data:
            data[i] = side_data(conn, i)
        return data[i]

    return nominated, *_due(conn, nominated, m, sides), sides


def held(conn, chat: ModelConfig = Config.connect) -> dict | None:
    """What the re-read gate holds (held_report), sending and writing nothing."""
    return held_report(conn, _pairs(conn, method(chat))[2], None, chat)


def stage(conn, judge, screen: jev.Jev | None, monthly_cap_usd: float = 20.0, limit: int | None = None,
          chat: ModelConfig = Config.connect, held_only: bool = False) -> dict:
    """Judge the nominated pairs that are due, at most `limit` of them (0: only count them; `judge` and `screen` may
    then be None). `judge` is Terra's client ([connect]), `screen` Jev's. A pair whose answer fails is logged and asked
    again next time; the cap, or a model that can't answer, stops the stage. Connections written are cited. What the
    re-read gate holds is reported under `held`; with `held_only`, only that is asked (`im reread connect`)."""
    m = method(chat)
    nominated, due, kept, counts, sides = _pairs(conn, m)
    if held_only:
        due = [p[:4] for p in kept]
    stats = {"method": m, "nominated": len(nominated), **counts, "due": len(due), "asked": 0, "screened": 0,
             "judged": 0, "connected": {}, "none": 0, "unsure": 0, "refused": 0, "failed": 0,
             "cost_usd": {"jev": 0.0, "terra": 0.0}, "stopped": None}
    done = set()
    for a, b, text, h in due[:limit]:
        segment_ids = egress.segments_of(conn, [a, b])
        try:
            v, egress_id = _judge(conn, judge, screen, chat, text, h,
                                  lambda a=a, b=b: {"a": sides(a), "b": sides(b)}, segment_ids, monthly_cap_usd,
                                  stats["cost_usd"])
        except llm.Stop as e:
            stats["stopped"] = str(e)[:200]
            break
        stats["asked"] += 1
        if v is None:
            stats["failed"] += 1
            continue
        _record(conn, a, b, m, v, h, segment_ids, egress_id)
        done.add((a, b))
        label = v["verdict"]
        stats["judged"] += label != "screened"
        if label in CONNECTED:
            stats["connected"][label] = stats["connected"].get(label, 0) + 1
        else:
            stats[label] += 1
    with conn.transaction():
        citations.cite(conn)
    stats["cost_usd"] = {k: round(c, 6) for k, c in stats["cost_usd"].items()}
    return reread.add(stats, held_report(conn, [p for p in kept if p[:2] not in done], screen, chat))
