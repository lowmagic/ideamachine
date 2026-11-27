"""What I typed into the ChatGPT Idea Machine, read by extraction v5.3 as archive captures (`im import-chatgpt`).

The conversation's export (ChatGPT Exporter's Markdown) has every message I sent, in my words, with when I sent it.
A curation sheet marks each one: `keep` it as written, `drop` it (an instruction to ChatGPT, say), or `edit` it, with
the text to read instead. Only my messages are read: ChatGPT's replies, its numbering, groupings and
cross-references are never read or sent.

The kept messages are read a session at a time, so a follow-up is read with what it follows: a gap of SESSION_GAP
or more between two messages starts a new session. Each session goes out as one payload, as an episode's transcript
does, under policy B: one line per message, labeled me, with the times on the context line, since a capture's quote
is copied from its lines. A capture is said when the message its quote starts in was sent. It's stored as an
archive capture (migration 055): an item with no episode, and its own archive observation, which records whether I
edited the message. The matcher then places it among my speech in the order I said things. A session already read
(`im.archive_reads`) is never sent again.
"""

import json
import re
import uuid
from collections import Counter
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from im import egress, extract, llm

NAME = "chatgpt"
TZ = "America/New_York"
SESSION_GAP = timedelta(hours=6)

_TURN = re.compile(r"^## (Prompt|Response):\n(.+?)\n", re.M)
_WHEN = re.compile(r"(\d{1,2}/\d{1,2}/\d{4}, \d{1,2}:\d{2}:\d{2} [AP]M)")
_LINK = re.compile(r"\*\*Link:\*\* \[https://chatgpt\.com/c/([0-9a-f-]+)\]")
# The sheet: "## N", the message's time (minutes; "· follows N" is a reader's note), the message quoted with ">",
# then "mark:", "text:" (the edit, up to "note:") and "note:".
_ENTRY = re.compile(r"^## (\d+)[ \t]*$", re.M)
_SENT = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2})(?: · follows \d+)?[ \t]*$")
_FIELD = re.compile(r"^(mark|text|note):[ \t]?(.*)$")
MARKS = ("keep", "drop", "edit")


def parse(text: str, tz: str = TZ) -> tuple[str, list[dict]]:
    """The conversation's id, and my messages, in order: {"at", "text"}."""
    link = _LINK.search(text)
    if not link:
        raise ValueError("not a ChatGPT export: no **Link:** to a chatgpt.com conversation")
    zone = ZoneInfo(tz)
    marks = list(_TURN.finditer(text))
    messages = []
    for k, m in enumerate(marks):
        when = _WHEN.search(m.group(2))
        if not when:
            raise ValueError(f"no time on {m.group(1)} {k + 1}: {m.group(2)!r}")
        if m.group(1) != "Prompt":
            continue
        end = marks[k + 1].start() if k + 1 < len(marks) else len(text)
        body = text[m.end():end].split("\n---\nPowered by", 1)[0].strip()
        at = datetime.strptime(when.group(1), "%m/%d/%Y, %I:%M:%S %p").replace(tzinfo=zone)
        messages.append({"at": at, "text": body})
    return link.group(1), messages


def read_sheet(text: str) -> dict[int, dict]:
    """The sheet's entries by message number: {"sent", "quote", "mark", "text"}."""
    entries = list(_ENTRY.finditer(text))
    out = {}
    for k, m in enumerate(entries):
        body = text[m.end():entries[k + 1].start() if k + 1 < len(entries) else len(text)]
        e: dict = {"sent": None, "quote": [], "mark": [], "text": [], "note": []}
        field = None
        for line in body.splitlines():
            f = _FIELD.match(line)
            if f:
                field = f.group(1)
                e[field].append(f.group(2))
            elif field in ("text", "note"):
                e[field].append(line)
            elif field is None and line.startswith(">"):
                e["quote"].append(line[1:].removeprefix(" "))
            elif field is None and (sent := _SENT.match(line)):
                e["sent"] = sent.group(1)
        n = int(m.group(1))
        mark = " ".join(e["mark"]).strip()
        if mark not in MARKS:
            raise ValueError(f"## {n}: mark {mark!r} isn't one of {', '.join(MARKS)}")
        edit = "\n".join(e["text"]).strip()
        if mark == "edit" and not edit:
            raise ValueError(f"## {n}: marked edit, with no text:")
        out[n] = {"sent": e["sent"], "quote": "\n".join(e["quote"]), "mark": mark, "text": edit}
    return out


def _ws(text: str) -> str:
    return " ".join(text.split())


def curate(messages: list[dict], sheet: dict[int, dict]) -> list[dict]:
    """The messages the sheet keeps, in order: {"n", "at", "text", "edited"}, with the text I edited in. Fails unless
    the sheet has exactly the export's messages, by number, minute and text."""
    if extra := sorted(set(sheet) - set(range(1, len(messages) + 1))):
        raise ValueError(f"the sheet has messages the export doesn't: {extra}")
    kept = []
    for n, msg in enumerate(messages, 1):
        e = sheet.get(n)
        if e is None:
            raise ValueError(f"message {n} ({msg['at']:%Y-%m-%d %H:%M}) isn't on the sheet")
        if e["sent"] != f"{msg['at']:%Y-%m-%d %H:%M}" or _ws(e["quote"]) != _ws(msg["text"]):
            raise ValueError(f"## {n} isn't the export's message {n} ({msg['at']:%Y-%m-%d %H:%M})")
        if e["mark"] != "drop":
            edited = e["mark"] == "edit"
            kept.append({"n": n, "at": msg["at"], "text": e["text"] if edited else msg["text"], "edited": edited})
    return kept


def sessions(kept: list[dict]) -> list[list[dict]]:
    """The kept messages in sessions: a gap of SESSION_GAP or more starts a new one."""
    out: list[list[dict]] = []
    for m in kept:
        if out and m["at"] - out[-1][-1]["at"] < SESSION_GAP:
            out[-1].append(m)
        else:
            out.append([m])
    return out


def plan(export: str, sheet: str, tz: str = TZ) -> dict:
    conversation, messages = parse(export, tz)
    kept = curate(messages, read_sheet(sheet))
    return {"conversation": conversation, "messages": len(messages), "kept": kept, "sessions": sessions(kept)}


def source_ref(conversation: str, session: list[dict]) -> str:
    """A session's name in im.archive_reads: the conversation and when its first message was sent."""
    return f"{NAME}:{conversation}:{session[0]['at'].isoformat()}"


def payload(session: list[dict]) -> egress.Payload:
    """The session as extraction reads it: one line per message, mine, its whitespace collapsed (a quote is copied
    from one line or consecutive ones), and each line's time on the context line. Line n is message n - 1."""
    context = ("Typed messages I sent to a chat, not speech: each line is one whole message of mine. Sent "
               + ", ".join(f"[{n}] {m['at']:%A %Y-%m-%d %H:%M}" for n, m in enumerate(session, 1)) + ".")
    lines = "\n".join(f"[{n}] me: {_ws(m['text'])}" for n, m in enumerate(session, 1))
    return egress.Payload(None, f"Context: {context}\n\nTranscript:\n{lines}", list(range(len(session))), {"me": "me"})


def already_read(conn, p: dict) -> set[str]:
    """The plan's sessions extraction has read, under any model, prompt or policy: a capture of the same message
    again would be a second observation of it."""
    refs = [source_ref(p["conversation"], s) for s in p["sessions"]]
    return {r for (r,) in conn.execute("SELECT source_ref FROM im.archive_reads WHERE source_ref = ANY(%s)", (refs,))}


def report(p: dict, read: set[str]) -> dict:
    refs = [source_ref(p["conversation"], s) for s in p["sessions"]]
    return {"conversation": p["conversation"], "messages": p["messages"], "kept": len(p["kept"]),
            "edited": sum(m["edited"] for m in p["kept"]), "dropped": p["messages"] - len(p["kept"]),
            "sessions": len(p["sessions"]), "already_read": sum(r in read for r in refs),
            "to_read": sum(r not in read for r in refs)}


def _started_in(quote: str, texts: list[str], cited: int) -> int:
    """The message a capture's quote starts in, else the first one it cites."""
    at = " ".join(texts).find(_ws(quote))
    if at < 0:
        return cited
    end = 0
    for k, t in enumerate(texts):
        end += len(t) + 1
        if at < end:
            return k
    return cited


def _store(conn, conversation: str, session: list[dict], p: egress.Payload, model: str, pv: str, egress_id: int,
           items: list[dict]) -> None:
    """The session's captures, each with its archive observation, and the read itself, in one transaction."""
    ref, texts, per_message = source_ref(conversation, session), [_ws(m["text"]) for m in session], Counter()
    with conn.transaction():
        for i, it in enumerate(items):
            m = session[_started_in(it["quote"], texts, it["source_segment_ids"][0])]
            per_message[m["n"]] += 1
            (observation,) = conn.execute(
                """INSERT INTO im.observations (segment_ids, first_seen, source, quote, said_at, said_by, conversation,
                                                source_ref, edited)
                   VALUES ('{}', %s, 'archive', %s, %s, 'me', %s, %s, %s) RETURNING observation_id""",
                (m["at"], it["quote"], m["at"], f"{NAME}:{conversation}",
                 f"{NAME}:{conversation}:{m['at'].isoformat()}#{per_message[m['n']]}", m["edited"])).fetchone()
            conn.execute(
                """INSERT INTO im.items (item_id, said_by, quote, gist, themes, source_segment_ids, egress_id, model,
                     prompt_version, privacy_policy_version, schema_version, stage_version, input_hash, reading,
                     attributed_to, mishearings, checks, observation_id)
                   VALUES (%s, 'me', %s, %s, '{}', '{}', %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                (uuid.uuid5(extract.ITEM_NS, f"{ref}|{model}|{pv}|{p.policy}|{p.sha256}|{i}"), it["quote"],
                 it["gist"], egress_id, model, pv, p.policy, extract.SCHEMA_VERSION, extract.STAGE_VERSION, p.sha256,
                 it["reading"], it["attributed_to"], json.dumps(it["mishearings"]), it["checks"], observation))
        conn.execute(
            """INSERT INTO im.archive_reads (source_ref, model, prompt_version, privacy_policy_version, input_hash,
                 egress_id, n_items) VALUES (%s, %s, %s, %s, %s, %s, %s)""",
            (ref, model, pv, p.policy, p.sha256, egress_id, len(items)))


def import_sessions(conn, client, p: dict, model: str, effort: str, monthly_cap_usd: float,
                    max_tokens: int | None = None, review_client=None, review_model: str | None = None,
                    review_effort: str = extract.ExtractConfig.review_effort) -> dict:
    """Read each session not read yet with extraction v5.3 (and optional Pass-2 review) and store what it captures."""
    if review_model and review_client is None:
        raise ValueError("review_client is required when review_model is set")
    pv = extract.prompt_version(model, effort, extract.SYSTEM_V5_3, extract.SCHEMA_V5, review_model, review_effort)
    read = already_read(conn, p)
    stats = {"model": model, "prompt_version": pv, "sessions": len(p["sessions"]), "already_read": 0, "read": 0,
             "items": 0, "refused": 0, "failed": 0, "cost_usd": 0.0, "stopped": None}
    for session in p["sessions"]:
        if source_ref(p["conversation"], session) in read:
            stats["already_read"] += 1
            continue
        sent = payload(session)
        try:
            a = egress.ask(conn, client, model, effort, extract.SYSTEM_V5_3, extract.SCHEMA_V5, sent.text,
                           segment_ids=[], prompt_version=pv, monthly_cap_usd=monthly_cap_usd, policy=sent.policy,
                           episode_id=None, max_tokens=max_tokens,
                           read=lambda answer: extract._items(sent, answer["items"]))
        except llm.Stop as e:
            stats["stopped"] = str(e)[:200]
            break
        stats["cost_usd"] += a.cost_usd
        if a.failed:   # logged and counted, not recorded as read: sent again on the next --write
            stats["failed"] += 1
            continue
        items = a.data or []
        if review_model and items and not a.refused:
            try:
                r, reviewed, _, _ = extract._review(
                    conn, review_client, sent, items, pv, review_model, review_effort, monthly_cap_usd,
                    max_tokens=max_tokens, segment_ids=[], episode_id=None)
            except llm.Stop as e:
                stats["stopped"] = str(e)[:200]
                break
            stats["cost_usd"] += r.cost_usd
            if reviewed is None:
                stats["failed"] += 1
                continue
            items = reviewed
        stats["read"] += 1
        stats["refused"] += a.refused
        _store(conn, p["conversation"], session, sent, model, pv, a.egress_id, items)
        stats["items"] += len(items)
    stats["cost_usd"] = round(stats["cost_usd"], 4)
    return stats
