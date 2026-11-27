"""Weekly synthesis (ROADMAP Phase 2, slice 6): once a week Claude reads what the lattice gained and writes a digest.

A week runs Monday 00:00 to Monday 00:00 in local time (the machine's TZ). The run writes the last completed week's
digest once, when all of the week's speech has been read and matched; `im digest --again` rewrites it. Claude sees
ideas, not raw speech: each idea's title and statement
(my corrections win), its themes, how often it was said, and what I did with it that week (stars, links,
comparisons). Speakers aren't named, though an idea's text, and so the digest, may name people: it's written for
me. The digest keeps the segment IDs behind every idea it was shown, so forgetting any of that speech deletes it.
"""

from datetime import date, datetime, time, timedelta

from im import egress, llm
from im.config import ModelConfig

KINDS = ("recurring", "convergence", "sleeper", "project", "outside_value")

SYSTEM = """You write my weekly digest from my idea lattice: ideas captured from my conversations and notes, \
matched together over time and grouped into themes. You get the ideas said this week, ideas I added myself (the \
strongest signal of what I care about), the links between them, and what I did this week (stars, links, \
comparisons). Write 3 to 7 points, each one of these kinds:
- recurring: an idea or cluster I keep coming back to, and what that might mean.
- convergence: separate ideas, often from different themes or conversations, that point at the same thing.
- sleeper: an idea that matters more than it seemed when I said it (plain-sounding but general, or a key piece \
of something larger).
- project: something forming that I could build, write or do.
- outside_value: an idea worth something to a thoughtful stranger who doesn't know me: worth writing up or \
sharing, and to whom.
Every point cites the ideas it rests on, by number, and claims only what those ideas support. Prefer the week's \
ideas; bring in older ones where they connect. Skip a kind rather than force it. Write to me, plainly: a short \
title, then two to four sentences. Add a one- or two-sentence summary of the week. If the week was thin, say so \
briefly and write fewer points."""

SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "points": {"type": "array", "items": {
            "type": "object",
            "properties": {"kind": {"type": "string", "enum": list(KINDS)}, "title": {"type": "string"},
                           "body": {"type": "string"}, "ideas": {"type": "array", "items": {"type": "integer"}}},
            "required": ["kind", "title", "body", "ideas"], "additionalProperties": False}},
    },
    "required": ["summary", "points"], "additionalProperties": False,
}


def prompt_version(chat: ModelConfig = ModelConfig()) -> str:
    return llm.version(SYSTEM, SCHEMA, chat.model, chat.effort)


def last_week(today: date | None = None) -> date:
    """The Monday that starts the last completed week."""
    today = today or datetime.now().astimezone().date()
    return today - timedelta(days=today.weekday() + 7)


def _period(week: date) -> tuple[datetime, datetime]:
    start = datetime.combine(week, time()).astimezone()
    return start, datetime.combine(week + timedelta(days=7), time()).astimezone()


def _said(conn, start: datetime, end: datetime) -> list[tuple]:
    """Ideas in pub.ideas with captures said in the week: id, title, statement, themes, said that week, said in all,
    first said, starred. Most said first."""
    return conn.execute(
        """SELECT d.idea_id, d.title, d.statement, d.themes, w.n, d.n_evidence, d.first_said, d.starred
           FROM pub.ideas d
           JOIN (SELECT idea_id, count(*) AS n FROM pub.evidence
                 WHERE said_at >= %s AND said_at < %s AND verdict IS DISTINCT FROM 'discard' GROUP BY idea_id) w
             USING (idea_id)
           ORDER BY w.n DESC, d.idea_id""", (start, end)).fetchall()


def _added(conn, start: datetime, end: datetime) -> list[tuple]:
    """Ideas I added myself during the week."""
    return conn.execute(
        """SELECT d.idea_id, d.title, d.statement, d.themes, 0, d.n_evidence, d.first_said, d.starred
           FROM pub.ideas d
           WHERE d.origin = 'mine' AND d.created_at >= %s AND d.created_at < %s
           ORDER BY d.created_at, d.idea_id""", (start, end)).fetchall()


def _unread(conn, start: datetime, end: datetime) -> bool:
    """Whether speech in the week hasn't reached the lattice yet: an episode extraction hasn't read (one where I don't
    speak it never reads), or a capture matching hasn't placed. Until it has, the week's ideas aren't all there to
    digest."""
    return conn.execute(
        """SELECT EXISTS (
             SELECT 1 FROM im.episodes e
             WHERE e.current AND e.started_at < %s AND e.ended_at >= %s
               AND ((e.kind <> 'others_only'
                     AND NOT EXISTS (SELECT 1 FROM im.extractions x WHERE x.episode_id = e.episode_id))
                    OR EXISTS (SELECT 1 FROM im.items i WHERE i.episode_id = e.episode_id
                               AND NOT EXISTS (SELECT 1 FROM im.item_matches m WHERE m.item_id = i.item_id))))""",
        (end, start)).fetchone()[0]


def _line(n: int, row: tuple) -> str:
    _, title, statement, theme_names, week_n, total, first, starred = row
    facts = [f"themes: {', '.join(theme_names)}" if theme_names else "no theme"]
    if week_n:
        facts.append(f"said {week_n}x this week, {total}x in all")
    elif total:
        facts.append(f"said {total}x in all")
    if first:
        facts.append(f"first said {first.astimezone():%Y-%m-%d}")
    if starred:
        facts.append("starred")
    return f"{n}. {title}: {statement} [{'; '.join(facts)}]"


def payload(conn, week: date) -> tuple[str, list]:
    """The text Claude reads for the week, and the idea ids in the order it numbers them."""
    start, end = _period(week)
    said = _said(conn, start, end)
    added = [r for r in _added(conn, start, end) if r[0] not in {s[0] for s in said}]
    ideas = said + added
    num = {r[0]: n for n, r in enumerate(ideas, 1)}
    links = conn.execute(
        """SELECT a, b, kind FROM pub.connections WHERE a = ANY(%s) AND b = ANY(%s) ORDER BY a, b""",
        (list(num), list(num))).fetchall()
    events = conn.execute(
        """SELECT f.kind, f.idea_id, f.other_idea_id, f.payload->>'winner' FROM pub.feedback_events f
           WHERE f.at >= %s AND f.at < %s AND f.kind IN ('star', 'link', 'idea_compare')
           ORDER BY f.event_id""", (start, end)).fetchall()
    activity = []
    for kind, a, b, winner in events:
        if a not in num or (b is not None and b not in num):
            continue
        if kind == "star":
            activity.append(f"starred {num[a]}")
        elif kind == "link":
            activity.append(f"linked {num[a]} and {num[b]}")
        elif winner == "tie":
            activity.append(f"compared {num[a]} and {num[b]}: a tie")
        else:
            better, worse = (a, b) if winner == "first" else (b, a)
            activity.append(f"preferred {num[better]} over {num[worse]}")
    text = [f"Week of {week:%Y-%m-%d} to {week + timedelta(days=6):%Y-%m-%d}.", "",
            f"Said this week ({len(said)} ideas):", *(_line(num[r[0]], r) for r in said), "",
            *([f"Added by me this week ({len(added)} ideas):", *(_line(num[r[0]], r) for r in added), ""]
              if added else [])]
    if links:
        text += ["", "Links: " + "; ".join(f"{num[a]}-{num[b]} ({k})" for a, b, k in links)]
    if activity:
        text += ["", "What I did this week: " + "; ".join(activity) + "."]
    return "\n".join(text), [r[0] for r in ideas]


def digest_stage(conn, client, monthly_cap_usd: float = 20.0, week: date | None = None, again: bool = False,
                 chat: ModelConfig = ModelConfig()) -> dict:
    """Write the week's digest (by default the last completed week) unless it exists. It waits while the week has
    speech extraction or matching hasn't got to (the cap, an outage, a backlog), and is written on a later run: a
    digest written then would miss those ideas for good. A week with nothing said gets a short digest without asking
    Claude. `chat`: the model and effort ([digest] in im.toml)."""
    week = week or last_week()
    stats = {"week": week.isoformat(), "written": False, "points": 0, "failed": 0, "stopped": None}
    if conn.execute("SELECT 1 FROM im.digests WHERE week_start = %s", (week,)).fetchone() and not again:
        return stats
    start, end = _period(week)
    if _unread(conn, start, end):
        stats["waiting"] = True
        return stats
    text, ids = payload(conn, week)
    pv = prompt_version(chat)
    if not _said(conn, start, end) and not _added(conn, start, end):
        answer, segs = {"summary": "Nothing new was said this week.", "points": []}, []
    else:
        try:
            a = egress.ask(conn, client, chat.model, chat.effort, SYSTEM, SCHEMA, text, prompt_version=pv,
                           segment_ids=egress.segments_of(conn, ids), monthly_cap_usd=monthly_cap_usd,
                           max_tokens=chat.max_tokens)
        except llm.Stop as e:
            stats["stopped"] = str(e)[:200]
            return stats
        if a.refused:
            stats["stopped"] = "refused"
            return stats
        if a.failed:   # unwritten, so asked again next run
            stats["failed"] = 1
            return stats
        answer, segs = a.data, egress.segments_of(conn, ids)
    with conn.transaction():
        conn.execute("DELETE FROM im.digests WHERE week_start = %s", (week,))
        (digest_id,) = conn.execute(
            """INSERT INTO im.digests (week_start, period_start, period_end, summary, prompt_version, segment_ids)
               VALUES (%s, %s, %s, %s, %s, %s) RETURNING digest_id""",
            (week, start, end, answer["summary"][:2000], pv, segs)).fetchone()
        ord_ = 0
        for p in answer["points"]:
            cited = [ids[n - 1] for n in dict.fromkeys(p["ideas"]) if 1 <= n <= len(ids)]
            if not cited or p["kind"] not in KINDS:   # a point must rest on ideas it was shown
                continue
            ord_ += 1
            conn.execute(
                """INSERT INTO im.digest_points (digest_id, ord, kind, title, body, idea_ids)
                   VALUES (%s, %s, %s, %s, %s, %s)""",
                (digest_id, ord_, p["kind"], p["title"][:200], p["body"][:2000], cited))
    stats.update(written=True, points=ord_, digest_id=digest_id)
    return stats


def show(conn, week: date | None = None) -> str:
    row = conn.execute(
        """SELECT digest_id, week_start, summary, points FROM pub.digests
           WHERE %s::date IS NULL OR week_start = %s ORDER BY week_start DESC LIMIT 1""", (week, week)).fetchone()
    if not row:
        return "No digest yet."
    digest_id, week_start, summary, points = row
    titles = dict(conn.execute("SELECT idea_id::text, title FROM pub.ideas"))
    out = [f"Week of {week_start:%Y-%m-%d}", "", summary]
    for p in points:
        out += ["", f"{p['point']}. [{p['kind']}] {p['title']}", p["body"],
                *(f"   - {titles.get(i, i)}" for i in p["idea_ids"])]
    return "\n".join(out)


def stored_weeks(conn, limit: int = 1) -> list[date]:
    """The most recent weeks with a stored digest, newest first."""
    return [w for (w,) in conn.execute("SELECT week_start FROM im.digests ORDER BY week_start DESC LIMIT %s", (limit,))]


def stored(conn, week: date) -> dict | None:
    """The stored digest for `week`, with its points, cited idea titles, and last logged request info."""
    row = conn.execute(
        "SELECT digest_id, summary, prompt_version, written_at FROM im.digests WHERE week_start = %s",
        (week,)).fetchone()
    if not row:
        return None
    digest_id, summary, pv, written_at = row
    titles = dict(conn.execute("SELECT idea_id::text, title FROM im.idea_text"))
    points = []
    for ord_, kind, title, body, idea_ids in conn.execute(
            """SELECT ord, kind, title, body, idea_ids FROM im.digest_points
               WHERE digest_id = %s ORDER BY ord""", (digest_id,)):
        ids = [str(i) for i in idea_ids]
        points.append({"point": ord_, "kind": kind, "title": title, "body": body,
                       "idea_ids": ids, "idea_titles": [titles.get(i, i) for i in ids]})
    req = conn.execute(
        """SELECT model, served_by, cost_usd::float, input_tokens, output_tokens
           FROM im.egress_log WHERE prompt_version = %s AND error IS NULL AND payload LIKE %s
           ORDER BY egress_id DESC LIMIT 1""", (pv, f"Week of {week:%Y-%m-%d} to %")).fetchone()
    if req is None:
        req = conn.execute(
            """SELECT model, served_by, cost_usd::float, input_tokens, output_tokens
               FROM im.egress_log WHERE prompt_version = %s AND error IS NULL
               ORDER BY egress_id DESC LIMIT 1""", (pv,)).fetchone()
    meta = {"model": req[0], "served_by": req[1], "cost_usd": req[2],
            "input_tokens": req[3], "output_tokens": req[4]} if req else {}
    return {"digest_id": digest_id, "prompt_version": pv,
            "written_at": written_at.isoformat() if written_at else None,
            **meta, "summary": summary, "points": points}


def _validate_digest_answer(answer: dict) -> dict:
    if not isinstance(answer, dict) or not isinstance(answer.get("summary"), str) \
            or not isinstance(answer.get("points"), list):
        raise ValueError("digest answer missing summary or points")
    for p in answer["points"]:
        if not isinstance(p, dict) or not isinstance(p.get("kind"), str) \
                or not isinstance(p.get("title"), str) or not isinstance(p.get("body"), str) \
                or not isinstance(p.get("ideas"), list):
            raise ValueError("digest point missing kind, title, body, or ideas")
    return answer


def compare(conn, client, week: date, chat: ModelConfig = ModelConfig(), monthly_cap_usd: float = 20.0) -> dict:
    """Run `chat` on `week`'s digest payload without writing im.digests, alongside any stored digest."""
    start, end = _period(week)
    unread = _unread(conn, start, end)
    text, ids = payload(conn, week)
    ref = stored(conn, week)
    cand: dict = {
        "model": chat.model, "effort": chat.effort, "served_by": None, "cost_usd": 0.0,
        "input_tokens": None, "output_tokens": None, "refused": False, "failed": False,
        "error": None, "stopped": None, "summary": None,
        "points_returned": 0, "points_valid": 0, "points_dropped": 0, "points": [],
    }
    if not ids:
        cand["skipped"] = "nothing said or added this week"
        return {"week": week.isoformat(), "ideas_shown": 0, "unread": unread, "candidate": cand, "stored": ref}
    pv = f"digest-compare:{prompt_version(chat)}"
    try:
        a = egress.ask(conn, client, chat.model, chat.effort, SYSTEM, SCHEMA, text, prompt_version=pv,
                       segment_ids=egress.segments_of(conn, ids), monthly_cap_usd=monthly_cap_usd,
                       max_tokens=chat.max_tokens, read=_validate_digest_answer)
    except llm.Stop as e:
        cand["stopped"] = str(e)[:200]
        return {"week": week.isoformat(), "ideas_shown": len(ids), "unread": unread, "candidate": cand, "stored": ref}
    cand["cost_usd"] = a.cost_usd
    if a.reply:
        cand["served_by"] = a.reply.served_by
        cand["input_tokens"] = a.reply.input_tokens
        cand["output_tokens"] = a.reply.output_tokens
    cand["refused"] = a.refused
    cand["failed"] = a.failed
    cand["error"] = a.error
    if a.data:
        titles = dict(conn.execute("SELECT idea_id::text, title FROM im.idea_text"))
        raw_points = a.data["points"]
        valid = []
        ord_ = 0
        for p in raw_points:
            cited = [ids[n - 1] for n in dict.fromkeys(p["ideas"])
                     if isinstance(n, int) and not isinstance(n, bool) and 1 <= n <= len(ids)]
            if not cited or p["kind"] not in KINDS:
                continue
            ord_ += 1
            c_ids = [str(i) for i in cited]
            valid.append({"point": ord_, "kind": p["kind"], "title": p["title"][:200], "body": p["body"][:2000],
                          "idea_ids": c_ids, "idea_titles": [titles.get(i, i) for i in c_ids]})
        cand.update(
            summary=a.data["summary"][:2000],
            points_returned=len(raw_points),
            points_valid=len(valid),
            points_dropped=len(raw_points) - len(valid),
            points=valid,
        )
    return {"week": week.isoformat(), "ideas_shown": len(ids), "unread": unread, "candidate": cand, "stored": ref}


def _format_side(label: str, d: dict | None) -> list[str]:
    if not d:
        return [f"## {label}", "", "(none stored for this week)", ""]
    meta = [f"model: {d.get('model') or '?'}"]
    if d.get("effort"):
        meta.append(f"effort: {d['effort']}")
    if d.get("served_by"):
        meta.append(f"served_by: {d['served_by']}")
    if d.get("cost_usd") is not None:
        meta.append(f"cost: ${d['cost_usd']:.6f}")
    if d.get("input_tokens") is not None or d.get("output_tokens") is not None:
        meta.append(f"tokens: {d.get('input_tokens') or 0} in / {d.get('output_tokens') or 0} out")
    lines = [f"## {label}", "", ", ".join(meta), ""]
    if d.get("skipped"):
        lines += [f"Skipped: {d['skipped']}", ""]
    if d.get("stopped"):
        lines += [f"Stopped: {d['stopped']}", ""]
    if d.get("failed"):
        lines += [f"Failed: {d['error']}", ""]
    if d.get("refused"):
        lines += ["Refused.", ""]
    if d.get("summary"):
        lines += [d["summary"], ""]
    for p in d.get("points", []):
        lines += [f"{p['point']}. [{p['kind']}] **{p['title']}**", p["body"],
                  *(f"   - {t}" for t in p.get("idea_titles", [])), ""]
    return lines


def format_compare(result: dict) -> str:
    """Render a side-by-side Markdown comparison of the stored digest and the candidate digest."""
    c, s = result["candidate"], result["stored"]
    s_cost = f"${s['cost_usd']:.6f}" if s and s.get("cost_usd") is not None else "-"
    s_toks = f"{s.get('input_tokens') or 0} / {s.get('output_tokens') or 0}" if s else "-"
    s_pts = f"{len(s['points'])} / 0" if s else "-"
    c_toks = f"{c.get('input_tokens') or 0} / {c.get('output_tokens') or 0}"
    c_pts = f"{c['points_valid']} / {c['points_dropped']}"
    lines = [
        f"# Digest comparison: week of {result['week']}",
        "",
        f"Ideas shown: {result['ideas_shown']}"
        + (" (week still has unread/unmatched speech)" if result["unread"] else ""),
        "",
        "| Side | Model | Served by | Cost ($) | Tokens (in / out) | Points (valid / dropped) |",
        "|---|---|---|---|---|---|",
        f"| Stored | {(s or {}).get('model') or '-'} | {(s or {}).get('served_by') or '-'} | "
        f"{s_cost} | {s_toks} | {s_pts} |",
        f"| Candidate | {c['model']} ({c['effort']}) | {c.get('served_by') or '-'} | "
        f"${c['cost_usd']:.6f} | {c_toks} | {c_pts} |",
        "",
        *_format_side("Stored digest", s),
        *_format_side("Candidate digest", c),
    ]
    return "\n".join(lines)
