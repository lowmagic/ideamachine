"""Sensitivity tags (decision 0012): eight descriptive tags on each idea and lens, so demo mode, in any presentation
layer, can leave out what I'd rather not show (the `demo` schema).

Jev answers one yes or no per tag (jev.QUESTIONS["sensitivity"]) about each shown idea (title, statement, the
archive's notes and my note, my corrections winning, and up to three of its quotes, since swearing is usually in what
was said, with my readings of those I corrected) and each lens, shown as the theme it's listed as (name and
description; themes became lenses in M5). A
shown idea is any presentation's: in pub.ideas or on a card, which also shows a cluster whose every capture I
discarded. A tag holds at CUT or above, and Jev's probabilities are kept beside the tags, so the cut can be set later
from my own marks. The tags describe; pub.sensitivity counts anything with a tag as sensitive, and my own marks
(idea_sensitive, theme_sensitive) win there, either way. An idea or lens is asked about again when its text changes,
at most MAX_PER_RUN a run. One tagged under another question set or cut with the same text is held by the re-read
gate (im.reread), its tags standing, until `im reread sensitivity`.
"""

import hashlib
import json

from im import egress, jev, llm, reread

TAGS = ("profanity", "sex", "gender_politics", "politics", "drugs", "offensive", "violence", "private_person")
CUT = 0.5          # not yet set from marks; recall first, since a tag only hides something in a demo
QUOTES = 3
MAX_PER_RUN = 500


def tagger_version(client: jev.Jev | None) -> str:
    return f"{jev.question_set(client, 'sensitivity')}@{CUT}"


def _hash(*parts) -> str:
    return hashlib.sha256(json.dumps(parts).encode()).hexdigest()[:16]


def _key(item: dict) -> tuple[str, str]:
    """(kind, hash) of an idea or theme as Jev is shown it."""
    if "theme" in item:
        return "theme", _hash(item["theme"], item.get("description", ""))
    return "idea", _hash(item["title"], item["statement"], item.get("notes", ""), item.get("my_note", ""),
                         item.get("said", []), *([item["my_readings"]] if "my_readings" in item else []))


def _todo(conn, tv: str) -> tuple[list[tuple], list[tuple]]:
    """(kind, id, the item as Jev is shown it, its hash) for each idea and theme not tagged under `tv` for its text,
    split by the re-read gate: (to tag, held), held being those tagged under another version for the same text, with
    that version after the hash."""
    rows = [("idea", i, {"title": t, "statement": st} | ({"notes": n} if n else {}) | ({"my_note": m} if m else {})
             | ({"said": q} if q else {}) | ({"my_readings": r} if r else {}), h, v)
            for i, t, st, n, m, q, r, h, v in conn.execute(
            """SELECT d.idea_id, x.title, x.statement, coalesce(d.notes, ''), coalesce(x.my_note, ''),
                      ARRAY(SELECT left(ce.quote, 200) FROM pub.card_evidence ce WHERE ce.idea_id = d.idea_id
                            ORDER BY ce.said_at, ce.item_id LIMIT %(quotes)s),
                      ARRAY(SELECT y.mine FROM (
                              SELECT CASE WHEN ce.reading IS DISTINCT FROM ce.claude_reading
                                          THEN left(ce.reading, 400) END AS mine
                              FROM pub.card_evidence ce WHERE ce.idea_id = d.idea_id
                              ORDER BY ce.said_at, ce.item_id LIMIT %(quotes)s) y WHERE y.mine IS NOT NULL),
                      s.input_hash, s.tagger_version
               FROM im.ideas d JOIN im.idea_text x USING (idea_id) LEFT JOIN im.idea_sensitivity s USING (idea_id)
               WHERE d.idea_id IN (SELECT idea_id FROM pub.ideas UNION SELECT idea_id FROM pub.cards)
               ORDER BY d.created_at, d.idea_id""", {"quotes": QUOTES})]
    rows += [("theme", i, {"theme": n} | ({"description": d} if d else {}), h, v) for i, n, d, h, v in conn.execute(
        """SELECT l.lens_id, l.name, l.description, s.input_hash, s.tagger_version
           FROM im.lenses l LEFT JOIN im.lens_sensitivity s USING (lens_id) WHERE l.state <> 'archived'
           ORDER BY l.created_at, l.lens_id""")]
    todo, held = [], []
    for kind, id_, item, h, v in rows:
        now = _key(item)[1]
        if v == tv and h == now:
            continue
        if h == now:
            held.append((kind, id_, item, now, v))
        else:
            todo.append((kind, id_, item, now))
    return todo, held


def held_report(conn, held: list[tuple], tv: str, model: str) -> dict | None:
    """The re-read gate's report on held ideas and themes (reread.report). Their last requests are found by what
    they showed: the version they were logged under and the hash of the item in their payload."""
    if not held:
        return None
    last = {}
    for egress_id, pv, payload in conn.execute(
            """SELECT egress_id, prompt_version, payload FROM im.egress_log
               WHERE prompt_version = ANY(%s) AND error IS NULL AND payload IS NOT NULL ORDER BY egress_id""",
            ([f"sensitivity:{v}" for v in {x[4] for x in held}],)):
        last[(pv.removeprefix("sensitivity:"), *_key(json.loads(payload)["state"]["item"]))] = egress_id
    ids = [last.get((v, kind, h)) for kind, _, _, h, v in held]
    return reread.report("sensitivity", [x[4] for x in held], tv,
                         reread.estimate(conn, [i for i in ids if i is not None], lambda _: model))


def held(conn) -> dict | None:
    """What the re-read gate holds (held_report), sending and writing nothing."""
    tv = tagger_version(None)
    return held_report(conn, _todo(conn, tv)[1], tv, jev.MODEL)


def tag_stage(conn, client: jev.Jev, monthly_cap_usd: float = 20.0, limit: int | None = MAX_PER_RUN,
              held_only: bool = False) -> dict:
    """Tag what's new or changed, one request each, at most `limit`. One that failed is asked again on a later run.
    What the re-read gate holds is reported under `held`; with `held_only`, only that is tagged
    (`im reread sensitivity`)."""
    tv = tagger_version(client)
    todo, kept = _todo(conn, tv)
    if held_only:
        todo = [x[:4] for x in kept]
    stats = {"tagger_version": tv, "pending": len(todo), "tagged": 0, "sensitive": 0, "failed": 0, "stopped": None}
    done = set()
    for kind, id_, item, h in todo[:limit]:
        body = client.body("sensitivity", {"item": item})
        try:
            a = egress.send(conn, lambda body=body: client.complete(body), model=client.model,
                            payload=json.dumps(body), segment_ids=egress.segments_of(conn, [id_]) if kind == "idea"
                            else [], prompt_version=f"sensitivity:{tv}",
                            monthly_cap_usd=monthly_cap_usd, read=lambda r: jev.read("sensitivity", r))
        except llm.Stop as e:
            stats["stopped"] = str(e)[:200]
            break
        if a.failed:
            stats["failed"] += 1
            continue
        p = {t: round(a.data[t], 3) for t in TAGS}
        tags = [t for t in TAGS if p[t] >= CUT]
        table, key = ("idea_sensitivity", "idea_id") if kind == "idea" else ("lens_sensitivity", "lens_id")
        conn.execute(
            f"""INSERT INTO im.{table} ({key}, categories, probabilities, input_hash, tagger_version)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT ({key}) DO UPDATE SET categories = excluded.categories,
                  probabilities = excluded.probabilities, input_hash = excluded.input_hash,
                  tagger_version = excluded.tagger_version, tagged_at = now()""",
            (id_, tags, json.dumps(p), h, tv))
        done.add(id_)
        stats["tagged"] += 1
        stats["sensitive"] += bool(tags)
    return reread.add(stats, held_report(conn, [x for x in kept if x[1] not in done], tv, client.model))
