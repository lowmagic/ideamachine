"""One card, read and corrected from the CLI: `im idea show ID` and `im idea edit ID`.

A correction is human input and lives only in pub.feedback_events (011): an `idea_correct` event carrying only the
fields I changed, and an `idea_note`, source `cli`. im.idea_text reads my latest of each field; an empty value reverts
to Claude's original, which im.ideas keeps. Every stage that reads a card's text sees a correction as changed input on
its next run, not as a version change, so the re-read gate doesn't hold it.
"""

import json
import re

SOURCE = "cli"
# The lengths Lattice allowed, as idea_add's payload still does (023).
LIMITS = {"title": 120, "statement": 1000, "note": 1000}


def find(conn, ref: str):
    """The cluster a full id or unique id prefix names. Raises ValueError for no match, an ambiguous prefix, or a
    cluster merged away (naming the cluster it's part of now)."""
    prefix = ref.strip().lower()
    rows = conn.execute("SELECT idea_id FROM im.ideas WHERE idea_id::text LIKE %s ORDER BY idea_id LIMIT 6",
                        (prefix + "%",)).fetchall() if re.fullmatch(r"[0-9a-f-]+", prefix) else []
    if not rows:
        raise ValueError(f"no cluster matches {ref!r}")
    if len(rows) > 1:
        raise ValueError(f"{ref!r} is ambiguous: " + ", ".join(str(r[0]) for r in rows[:5]))
    (idea,) = rows[0]
    into = conn.execute("SELECT into_id FROM im.merged_into WHERE idea_id = %s", (idea,)).fetchone()
    if into:
        raise ValueError(f"{idea} was merged into {into[0]}: use that one")
    return idea


def _text(conn, idea) -> tuple:
    return conn.execute("""SELECT title, statement, my_note, claude_title, claude_statement, corrected_at
                           FROM im.idea_text WHERE idea_id = %s""", (idea,)).fetchone()


def _card(title: str, statement: str, note: str | None) -> list[str]:
    return [f"  {title}", f"  {statement}"] + ([f"  my note: {note}"] if note else [])


def show(conn, ref: str) -> str:
    """The card's text as it counts now, whether I corrected it, its captures' quotes, and what was merged into it."""
    idea = find(conn, ref)
    title, statement, note, claude_title, claude_statement, corrected_at = _text(conn, idea)
    out = [str(idea), *_card(title, statement, note)]
    changed = [f for f, mine, orig in (("title", title, claude_title), ("statement", statement, claude_statement))
               if mine != orig]
    if changed:
        out.append(f"  corrected ({' and '.join(changed)}), last {corrected_at.astimezone():%Y-%m-%d %H:%M}; "
                   "Claude's:")
        out += [f"    {orig}" for f, orig in (("title", claude_title), ("statement", claude_statement)) if f in changed]
    else:
        out.append("  not corrected")
    quotes = conn.execute("""SELECT speaker, quote, said_at FROM pub.card_evidence WHERE idea_id = %s
                             ORDER BY said_at NULLS LAST, item_id""", (idea,)).fetchall()
    out.append(f"\ncaptures ({len(quotes)}):")
    out += [f"  {'' if at is None else f'{at.astimezone():%Y-%m-%d} '}{who or '?'}: \u201c{quote}\u201d"
            for who, quote, at in quotes]
    merged = conn.execute("""SELECT other_id, merged_at, title, statement, my_note FROM im.idea_merges
                             WHERE keep_id = %s ORDER BY merge_id""", (idea,)).fetchall()
    for other, at, m_title, m_statement, m_note in merged:
        out += [f"\nmerged in {at.astimezone():%Y-%m-%d}: {other}", *_card(m_title, m_statement, m_note)]
    return "\n".join(out)


def edit(conn, ref: str, title: str | None = None, statement: str | None = None, note: str | None = None,
         meaning_changed: bool = False) -> str:
    """Correct a card: one idea_correct event with the fields given and my mark of whether the meaning changed, and
    an idea_note if a note is given. An empty value reverts to Claude's original (no note, for the note). Raises
    ValueError, writing nothing, for an unknown or merged-away cluster, no field, or a field too long."""
    given = {k: v.strip() for k, v in (("title", title), ("statement", statement), ("note", note)) if v is not None}
    if not given:
        raise ValueError("nothing to change: give --title, --statement or --note")
    for k, v in given.items():
        if len(v) > LIMITS[k]:
            raise ValueError(f"the {k} is {len(v)} characters; at most {LIMITS[k]}")
    with conn.transaction():
        idea = find(conn, ref)
        before = _text(conn, idea)
        correct = {k: v for k, v in given.items() if k != "note"}
        events = ([("idea_correct", correct | {"meaning_changed": meaning_changed})] if correct else []) + (
            [("idea_note", {"note": given["note"]})] if "note" in given else [])
        for kind, payload in events:
            conn.execute("""INSERT INTO pub.feedback_events (kind, idea_id, payload, source)
                            VALUES (%s, %s, %s, %s)""", (kind, idea, json.dumps(payload), SOURCE))
        after = _text(conn, idea)
    return "\n".join([str(idea), "before:", *_card(*before[:3]), "after:", *_card(*after[:3])])


MARKS = {True: "meaning changed", False: "wording only", None: "unmarked"}


def corrections(conn) -> str:
    """The error log: every card whose title or statement I corrected, and every capture whose reading I corrected,
    Claude's beside mine, with my mark of whether the meaning changed; and, for each, the connections the judge asked
    about again since, each verdict as it stood before then and as it stands now. Counts first."""
    cards: dict = {}
    for idea, title, claude_title, statement, claude_statement, mark, first in conn.execute(
            """SELECT x.idea_id, x.title, x.claude_title, x.statement, x.claude_statement,
                      (SELECT (f.payload->>'meaning_changed')::boolean FROM pub.feedback_events f
                       WHERE f.idea_id = x.idea_id AND f.kind = 'idea_correct' ORDER BY f.event_id DESC LIMIT 1),
                      (SELECT min(f.at) FROM pub.feedback_events f
                       WHERE f.idea_id = x.idea_id AND f.kind = 'idea_correct')
               FROM im.idea_text x JOIN pub.cards c USING (idea_id)
               WHERE x.title IS DISTINCT FROM x.claude_title OR x.statement IS DISTINCT FROM x.claude_statement
               ORDER BY 7"""):
        c = cards.setdefault(idea, {"title": title, "first": first, "fixes": []})
        c["fixes"] += [(f, mark, theirs, mine) for f, theirs, mine in (("title", claude_title, title),
                                                                       ("statement", claude_statement, statement))
                       if theirs != mine]
    for idea, title, quote, claude_reading, reading, mark, at in conn.execute(
            """SELECT ce.idea_id, c.title, ce.quote, r.claude_reading, r.reading, r.meaning_changed, r.corrected_at
               FROM pub.card_evidence ce JOIN im.item_reading r USING (item_id) JOIN pub.cards c USING (idea_id)
               WHERE r.corrected ORDER BY r.corrected_at, ce.item_id"""):
        c = cards.setdefault(idea, {"title": title, "first": at, "fixes": []})
        c["first"] = min(c["first"], at)
        c["fixes"].append((f"reading of \u201c{quote}\u201d", mark, claude_reading, reading))
    asked = {}
    for idea, c in cards.items():
        asked[idea] = conn.execute(
            """SELECT DISTINCT ON (r.a, r.b) r.verdict, v.verdict, o.title
               FROM im.connection_verdicts_replaced r JOIN im.connection_verdicts v USING (a, b)
               JOIN im.idea_text o ON o.idea_id = CASE WHEN r.a = %(i)s THEN r.b ELSE r.a END
               WHERE %(i)s IN (r.a, r.b) AND r.replaced_at >= %(at)s
               ORDER BY r.a, r.b, r.replaced_at""", {"i": idea, "at": c["first"]}).fetchall()
    fixes = [f for c in cards.values() for f in c["fixes"]]
    marks = {label: sum(MARKS[f[1]] == label for f in fixes) for label in MARKS.values()}
    pairs = [p for ps in asked.values() for p in ps]
    out = [f"{len(fixes)} corrections on {len(cards)} cards: " + ", ".join(f"{n} {k}" for k, n in marks.items())
           + f". Connections asked again since: {len(pairs)}, {sum(old != new for old, new, _ in pairs)} changed."]
    for idea, c in sorted(cards.items(), key=lambda kv: kv[1]["first"]):
        out += ["", f"{str(idea)[:8]}  {c['title']}  (first corrected {c['first'].astimezone():%Y-%m-%d})"]
        for what, mark, theirs, mine in c["fixes"]:
            out += [f"  {what} [{MARKS[mark]}]", f"    Claude's: {theirs or ''}", f"    mine:     {mine}"]
        changed = [(old, new, other) for old, new, other in asked[idea] if old != new]
        if asked[idea]:
            out.append(f"  connections asked again: {len(asked[idea])}, {len(changed)} changed")
            out += [f"    {old} -> {new}: {other}" for old, new, other in changed]
    return "\n".join(out)
