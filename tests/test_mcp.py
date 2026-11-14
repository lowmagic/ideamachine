"""The MCP server (im.mcp, served by `im web` at /mcp): Lattice's 19 tools against today's pub, as the web view's
role, behind its token; my calls are feedback events from 'mcp'; ideas aren't rated; demo mode is the app's one
setting. Ported from Lattice's tests (lattice 6def765: tests/test_mcp.py, tests/test_audit.py)."""

import http.client
import json
import threading
from http.server import ThreadingHTTPServer

import psycopg
import pytest

from im import mcp, web

from helpers import HashEmbedder, table
from test_review import as_web_role

TOKEN = "a-long-enough-test-token"
NAMES = {"search_ideas", "get_idea", "best_ideas", "list_themes", "open_theme", "whats_new", "weekly_digest",
         "draft_material", "rename_theme", "merge_themes", "split_theme", "file_idea", "similar_ideas", "add_idea",
         "proposals", "keep_idea", "star_idea", "link_ideas", "note_idea"}


@pytest.fixture
def app(seeded_lattice, web_dsn, monkeypatch):
    """A tool call, through mcp.handle as /mcp makes it, as the web view's role: name, **arguments -> result."""
    monkeypatch.setattr(mcp, "_embedder", HashEmbedder())   # the model the fixture's ideas are embedded with

    def use(tool, /, **args):
        answer = mcp.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                             "params": {"name": tool, "arguments": args}},
                            lambda: psycopg.connect(web_dsn, autocommit=True))
        return answer.get("result") or answer
    return use


def idea_of(conn, word):
    return str(table(conn, "SELECT idea_id FROM pub.ideas WHERE statement ILIKE %s ORDER BY idea_id",
                     f"%{word}%")[0][0])


@pytest.fixture
def served(seeded_lattice, web_dsn, monkeypatch):
    """`im web` on a loopback port: its port."""
    monkeypatch.setenv("IM_WEB_TOKEN", TOKEN)
    monkeypatch.setattr(web.Handler, "dsn", web_dsn)
    server = ThreadingHTTPServer(("127.0.0.1", 0), web.Handler)
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    yield server.server_address[1]
    server.shutdown()
    thread.join()
    server.server_close()


def rpc(port, message, token=TOKEN, headers=None, method="POST"):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    c.request(method, "/mcp", body=json.dumps(message), headers={
        "Content-Type": "application/json", "Accept": "application/json, text/event-stream",
        **({"Authorization": f"Bearer {token}"} if token else {}), **(headers or {})})
    r = c.getresponse()
    body = r.read()
    return r.status, json.loads(body) if body else None


def test_it_needs_the_token_refuses_browsers_and_speaks_json_rpc(served):
    ping = {"jsonrpc": "2.0", "id": 1, "method": "ping"}
    assert rpc(served, ping, token=None)[0] == 401
    assert rpc(served, ping, token="wrong-token-0123456789")[0] == 401
    assert rpc(served, ping, headers={"Origin": "https://evil.example"})[0] == 403
    assert rpc(served, ping) == (200, {"jsonrpc": "2.0", "id": 1, "result": {}})
    status, body = rpc(served, {"jsonrpc": "2.0", "id": 0, "method": "initialize",
                                "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                                           "clientInfo": {"name": "test", "version": "0"}}})
    assert status == 200 and body["result"]["protocolVersion"] == "2025-06-18" and body["result"]["instructions"]
    assert rpc(served, {"jsonrpc": "2.0", "method": "notifications/initialized"}) == (202, None)
    tools = rpc(served, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})[1]["result"]["tools"]
    assert {t["name"] for t in tools} == NAMES
    assert all(t["inputSchema"]["additionalProperties"] is False for t in tools)
    assert rpc(served, {"jsonrpc": "2.0", "id": 3, "method": "nope"})[1]["error"]["code"] == -32601
    assert rpc(served, {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "drop"}})[1]["error"]
    batch = rpc(served, [ping, {"jsonrpc": "2.0", "method": "notifications/initialized"}])
    assert batch == (200, [{"jsonrpc": "2.0", "id": 1, "result": {}}])
    assert rpc(served, ping, method="GET")[0] == 405 and rpc(served, ping, method="DELETE")[0] == 405


def test_reading_never_names_anyone(app, seeded_lattice):
    pipe = seeded_lattice
    names = {n for (n,) in table(pipe, "SELECT DISTINCT said_by FROM pub.evidence")} - {"me", "unknown"}
    assert names   # the fixture has other speakers
    themes = app("list_themes")["structuredContent"]["themes"]
    theme = app("open_theme", theme=themes[0]["name"].upper())["structuredContent"]
    assert theme["name"] == themes[0]["name"] and theme["ideas"]
    assert app("search_ideas", query="coffee")["structuredContent"]["ideas"]
    (busiest,) = table(pipe, """SELECT idea_id FROM pub.evidence WHERE said_by NOT IN ('me', 'unknown')
                                GROUP BY idea_id ORDER BY count(DISTINCT said_by) DESC LIMIT 1""")[0]
    result = app("get_idea", idea_id=str(busiest))
    assert not any(name in result["content"][0]["text"] for name in names)
    evidence = result["structuredContent"]["evidence"]
    assert {ev["said_by"] for ev in evidence} <= {"me", "unknown"} | {f"S{i}" for i in range(1, 20)}
    assert all("item_id" not in ev and "confidence" not in ev for ev in evidence)
    assert "ranking" not in result["structuredContent"]   # nothing is scored
    assert {"proposed_themes", "proposals_waiting"} <= set(app("whats_new", since="2026-01-01")["structuredContent"])


def test_mistakes_come_back_as_tool_errors(app):
    assert app("get_idea", idea_id="not-an-id")["isError"]
    assert app("get_idea", idea_id="00000000-0000-0000-0000-000000000000")["isError"]
    assert app("open_theme", theme="no theme is called this")["isError"]
    assert app("search_ideas")["isError"]
    assert app("whats_new", since="last tuesday")["isError"]
    assert app("weekly_digest", week="1999-01-01")["isError"]
    assert app("draft_material")["isError"] and app("similar_ideas")["isError"]
    assert app("keep_idea", idea_id="00000000-0000-0000-0000-000000000000", decision="maybe")["isError"]


def test_my_calls_are_feedback_events_from_mcp(app, seeded_lattice):
    pipe = seeded_lattice
    coffee, signal = idea_of(pipe, "coffee"), idea_of(pipe, "signal")
    assert not app("star_idea", idea_id=coffee).get("isError")
    assert app("get_idea", idea_id=coffee)["structuredContent"]["starred"] is True
    for decision in ("pinned", "unkeep", "dismiss"):
        assert not app("keep_idea", idea_id=coffee, decision=decision).get("isError")
    app("link_ideas", idea_id=coffee, other_idea_id=signal)
    app("note_idea", idea_id=coffee, note="  From the standup.  ")
    assert table(pipe, "SELECT kind, payload, source FROM pub.feedback_events WHERE idea_id = %s ORDER BY event_id",
                 coffee) == [("star", {}, "mcp"), ("idea_keep", {"as": "pinned"}, "mcp"), ("idea_unkeep", {}, "mcp"),
                             ("idea_dismiss", {}, "mcp"), ("link", {}, "mcp"),
                             ("idea_note", {"note": "From the standup."}, "mcp")]
    got = app("get_idea", idea_id=coffee)["structuredContent"]
    assert got["my_note"] == "From the standup." and "your link" in {c["kind"] for c in got["connections"]}
    assert app("link_ideas", idea_id=coffee, other_idea_id=coffee)["isError"]

    added = app("add_idea", title=" Mine ", statement="An idea I typed.", note="From a walk.",
                link_to=[signal])["structuredContent"]
    assert table(pipe, "SELECT payload, source FROM pub.feedback_events WHERE idea_id = %s AND kind = 'idea_add'",
                 added["idea_id"]) == [({"title": "Mine", "statement": "An idea I typed.", "note": "From a walk.",
                                         "link_to": [signal]}, "mcp")]
    assert app("get_idea", idea_id=added["idea_id"])["structuredContent"]["origin"] == "mine"   # at once


def test_my_corrections_show_beside_claudes_words(app, seeded_lattice, admin):
    coffee = idea_of(seeded_lattice, "coffee")
    before = app("get_idea", idea_id=coffee)["structuredContent"]
    assert before["corrected"] is False and "claude_title" not in before
    as_web_role(admin, "INSERT INTO pub.feedback_events (kind, idea_id, payload) VALUES ('idea_correct', %s, %s)",
                coffee, json.dumps({"title": "Coffee runs it"}))
    after = app("get_idea", idea_id=coffee)["structuredContent"]
    assert (after["title"], after["claude_title"], after["corrected"]) == ("Coffee runs it", before["title"], True)


def test_ideas_arent_rated(app, seeded_lattice):
    pipe = seeded_lattice
    best = app("best_ideas")["structuredContent"]   # the ideas I added start canonical
    assert best["ideas"] and all(i["score"] is None for i in best["ideas"]) and "Not scored" in best["how_scored"]
    for (i,) in table(pipe, "SELECT idea_id FROM pub.cards WHERE kept_as = 'canonical'"):
        app("keep_idea", idea_id=str(i), decision="unkeep")
    assert "doesn't rate ideas" in app("best_ideas")["content"][0]["text"]
    drafted = app("draft_material", theme="Models and signals")["structuredContent"]
    assert drafted["ideas"] and all(i["score"] is None for i in drafted["ideas"])


def test_draft_material_keeps_every_idea_once_and_names_no_one(app, seeded_lattice):
    pipe = seeded_lattice
    got = app("draft_material", theme="Models and signals")["structuredContent"]
    titles = {i["title"] for i in got["ideas"]}
    order = got["suggested_order"]
    assert sorted(sum(order["connected_groups"], []) + order["loose_ends"]) == sorted(titles)
    names = {n for (n,) in table(pipe, "SELECT DISTINCT said_by FROM pub.evidence")} - {"me", "unknown"}
    assert not {q["said_by"] for i in got["ideas"] for q in i["quotes"]} & names
    picked = [i["id"] for i in got["ideas"][:2]]
    assert len(app("draft_material", idea_ids=picked)["structuredContent"]["ideas"]) == len(picked)
    assert app("draft_material", theme="x", idea_ids=picked)["isError"]


def test_a_reading_order_keeps_neighbours_together():
    ids = list("abcdef")
    links = [{"a": "a", "b": "b"}, {"a": "b", "b": "c"}, {"a": "d", "b": "e"}]
    groups, loose = mcp._reading_order(ids, links, {i: 0 for i in ids} | {"f": 5})
    assert groups == [["b", "a", "c"], ["d", "e"]] and loose == ["f"]


def test_shaping_themes_by_conversation(app, seeded_lattice):
    pipe = seeded_lattice
    coffee = idea_of(pipe, "coffee")
    renamed = app("rename_theme", theme="MODELS AND SIGNALS", name="Signals")["structuredContent"]
    assert renamed["renamed"] == "Models and signals" and renamed["theme"]["name"] == "Signals"
    assert app("rename_theme", theme="Signals", name="Jokes")["isError"]   # another theme's name isn't taken
    split = app("split_theme", theme="Signals", name="Coffee", idea_ids=[coffee])["structuredContent"]
    assert split["new_theme"]["name"] == "Coffee" and split["new_theme"]["ideas"] == 1
    assert "Coffee" not in app("file_idea", idea_id=coffee, theme="Coffee", remove=True)["structuredContent"]["themes"]
    assert "Coffee" in app("file_idea", idea_id=coffee, theme="Coffee")["structuredContent"]["themes"]
    merged = app("merge_themes", theme="Coffee", into="Signals")["structuredContent"]
    assert merged["into"]["name"] == "Signals" and table(pipe, "SELECT 1 FROM pub.themes WHERE name = 'Coffee'") == []
    assert app("merge_themes", theme="Jokes", into="Jokes")["isError"]
    assert {k for (k,) in table(pipe, "SELECT DISTINCT source FROM pub.feedback_events WHERE theme_id IS NOT NULL")} \
        == {"mcp"}


def test_ideas_like_this_by_id_and_by_words(app, seeded_lattice):
    coffee = idea_of(seeded_lattice, "coffee")
    like = app("similar_ideas", idea_id=coffee)["structuredContent"]["ideas"]
    assert like and coffee not in [i["id"] for i in like]
    assert [i["similarity"] for i in like] == sorted((i["similarity"] for i in like), reverse=True)
    found = app("similar_ideas", text="the coffee order is the real meeting")["structuredContent"]["ideas"]
    assert found and found[0]["id"] == coffee
    assert app("similar_ideas", idea_id=coffee, text="both")["isError"]


def test_demo_mode_is_one_setting_for_mcp_too(app, seeded_lattice, admin):
    pipe = seeded_lattice
    coffee = idea_of(pipe, "coffee")
    as_web_role(admin, "INSERT INTO pub.feedback_events (kind, idea_id, payload) VALUES ('idea_sensitive', %s, %s)",
                coffee, json.dumps({"sensitive": True}))
    assert app("get_idea", idea_id=coffee)["structuredContent"]   # demo mode is off: all of it
    rude = app("add_idea", title="Shit happens", statement="Plans fail anyway.")["structuredContent"]["idea_id"]

    web.set_demo(admin, True)
    assert app("get_idea", idea_id=coffee)["isError"]                    # as if it weren't there
    assert coffee not in json.dumps(app("search_ideas", query="coffee"))
    assert app("search_ideas", query="happens")["structuredContent"]["ideas"][0]["title"] == "████ happens"
    assert "error" in app("star_idea", idea_id=rude)                     # writes don't exist
    assert "error" in app("proposals")

    web.set_demo(admin, False)
    assert app("get_idea", idea_id=coffee)["structuredContent"]["id"] == coffee
    assert table(pipe, "SELECT kind, source FROM pub.feedback_events WHERE kind LIKE 'demo_%%' ORDER BY event_id") \
        == [("demo_on", "emanation"), ("demo_off", "emanation")]
