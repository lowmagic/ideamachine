"""The neutral view (decision 0007): every cluster past the gates, newest first, no score, no verdict filter."""

import http.client
import json
import re
import uuid
import threading
from http.server import ThreadingHTTPServer

import psycopg
import pytest

from im import web, weight
from im.config import WeightConfig
from im.feedback import DismissReason

from helpers import table
from test_lattice import seeded  # noqa: F401
from test_weight import idea, matched, proposals  # noqa: F401

TOKEN = "a-long-enough-test-token"


def as_app(admin, fn, *args):
    with admin.transaction():
        admin.execute("SET LOCAL ROLE lattice_app")
        return fn(admin, *args)


def test_a_capture_i_discarded_still_shows(matched, admin):  # noqa: F811
    new = idea(matched, "Something new")
    matched.execute("""INSERT INTO im.item_verdicts (item_id, segment_ids, quote, verdict)
                       SELECT i.item_id, i.source_segment_ids, i.quote, 'discard'
                       FROM im.items i JOIN im.idea_evidence ev USING (item_id) WHERE ev.idea_id = %s""", (new,))
    assert table(matched, "SELECT 1 FROM pub.ideas WHERE idea_id = %s", new) == []     # pub.ideas hides it
    assert as_app(admin, lambda c: c.execute("SELECT maturity, by_me FROM pub.cards WHERE idea_id = %s",
                                             (new,)).fetchall()) == [("candidate", 1)]


def test_the_feed_is_newest_first_and_names_no_one(matched, admin):  # noqa: F811
    page = as_app(admin, web.feed, "candidates")
    # Broken builds was said earlier and weighs more (a canonical link): time orders, not weight.
    assert page.index("Something new") < page.index("Broken builds as signal")
    assert "Signal may not be true" in as_app(admin, web.feed, "ideas")             # the archive's, by when made
    shown = as_app(admin, web.card, str(idea(matched, "Something new")))
    assert "me ·" in shown and "anon A" not in shown and "Near by meaning" in shown
    assert as_app(admin, web.card, "00000000-0000-0000-0000-000000000000") is None


def test_keeping_from_a_card_is_my_feedback(matched, admin):  # noqa: F811
    new = idea(matched, "Something new")
    as_app(admin, web.act, str(new), "pinned")
    assert table(matched, "SELECT kept_as FROM im.idea_kept WHERE idea_id = %s", new) == [("pinned",)]
    as_app(admin, web.act, str(new), "unkeep")
    as_app(admin, web.act, str(new), "dismiss")
    assert table(matched, """SELECT kind, source FROM pub.feedback_events WHERE idea_id = %s
                             ORDER BY event_id""", new) == [
        ("idea_keep", "emanation"), ("idea_unkeep", "emanation"), ("idea_dismiss", "emanation")]
    assert table(matched, "SELECT kept_as FROM im.idea_kept WHERE idea_id = %s", new) == [(None,)]


def test_my_latest_event_is_the_last_to_arrive_whatever_time_it_carries(matched, admin):  # noqa: F811
    import json

    new = str(idea(matched, "Something new"))

    def events(*rows):   # as a client sends them, in one transaction: (at or None for now(), kind, payload)
        as_app(admin, lambda c: [c.execute("""INSERT INTO pub.feedback_events (at, kind, idea_id, payload)
                                               VALUES (coalesce(%s, now()), %s, %s, %s)""",
                                           (at, kind, new, json.dumps(p))) for at, kind, p in rows])

    def shown():
        return (table(matched, "SELECT starred, title FROM pub.ideas WHERE idea_id = %s", new)[0]
                + table(matched, "SELECT kept_as FROM im.idea_kept WHERE idea_id = %s", new)[0]
                + (dict(table(matched, "SELECT id::text, sensitive FROM pub.sensitivity")).get(new),))

    events((None, "star", {}), (None, "unstar", {}))   # one transaction: both carry the same now()
    assert shown() == (False, "Something new", None, None)
    # One device's events, made at 11:00, arrive before another's made at 10:00: what arrived last is my latest.
    later, earlier = "2026-09-01T11:00:00Z", "2026-09-01T10:00:00Z"
    events((later, "star", {}), (later, "idea_keep", {"as": "pinned"}), (later, "idea_sensitive", {"sensitive": False}),
           (later, "idea_correct", {"title": "Later title"}))
    events((earlier, "unstar", {}), (earlier, "idea_unkeep", {}), (earlier, "idea_sensitive", {"sensitive": True}),
           (earlier, "idea_correct", {"title": "Earlier title"}))
    assert shown() == (False, "Earlier title", None, True)



def test_only_a_cookie_signed_with_the_token_logs_in(monkeypatch):
    monkeypatch.setenv("IM_WEB_TOKEN", "a-long-enough-test-token")
    assert web.logged_in(f"other=1; {web.COOKIE}={web._session()}")
    assert not web.logged_in(f"{web.COOKIE}=forged") and not web.logged_in(None)
    monkeypatch.setenv("IM_WEB_TOKEN", "short")
    with pytest.raises(SystemExit):
        web.logged_in(f"{web.COOKIE}=x")


@pytest.fixture
def served(matched, web_dsn, monkeypatch):  # noqa: F811
    """The view, served on a loopback port as `im web` serves it: its port."""
    monkeypatch.setenv("IM_WEB_TOKEN", TOKEN)
    monkeypatch.setattr(web.Handler, "dsn", web_dsn)
    server = ThreadingHTTPServer(("127.0.0.1", 0), web.Handler)
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    yield server.server_address[1]
    server.shutdown()
    thread.join()
    server.server_close()


def request(port, method, path, form=None, cookie=None):
    """(status, headers, body) of one request to the view."""
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    c.request(method, path, body=form, headers={"Content-Type": "application/x-www-form-urlencoded"}
              | ({"Cookie": cookie} if cookie else {}))
    r = c.getresponse()
    return r.status, dict(r.getheaders()), r.read().decode()


def test_without_the_token_the_view_shows_nothing_and_takes_nothing(served, matched):  # noqa: F811
    new = str(idea(matched, "Something new"))
    events = lambda: table(matched, "SELECT count(*) FROM pub.feedback_events")  # noqa: E731
    before = events()
    for path in ("/", f"/idea/{new}", "/sky"):
        status, headers, body = request(served, "GET", path)
        assert (status, headers.get("Location")) == (303, "/login") and "Something new" not in body
    assert request(served, "POST", f"/idea/{new}", "action=dismiss")[0] == 403
    status, headers, _ = request(served, "POST", "/login", "token=another-long-enough-token")
    assert status == 403 and "Set-Cookie" not in headers
    assert events() == before

    status, headers, _ = request(served, "POST", "/login", f"token={TOKEN}")   # with it, both
    cookie = headers["Set-Cookie"].split(";")[0]
    assert status == 303 and "Something new" in request(served, "GET", "/", cookie=cookie)[2]
    assert request(served, "POST", f"/idea/{new}", "action=dismiss", cookie)[0] == 303
    assert events() == [(before[0][0] + 1,)]



def test_the_app_icons_ship_and_need_no_login():
    import json
    from importlib.resources import files

    static = files("im").joinpath("static")
    manifest = json.loads(static.joinpath("manifest.webmanifest").read_text())
    assert {i["src"] for i in manifest["icons"]} <= set(web.STATIC)
    for path in web.STATIC:
        assert static.joinpath(path.lstrip("/")).read_bytes()
    assert "/manifest.webmanifest" in web._page("t", "") and "/apple-touch-icon.png" in web._page("t", "")


def sky_data(page):
    """The data a sky page draws from."""
    import json
    import re
    return json.loads(re.search(r'<script type="application/json" id="d">(.*?)</script>', page, re.S)[1]
                      .replace("<\\/", "</"))


def test_the_sky_places_every_card_even_one_pub_ideas_hides(matched, admin):  # noqa: F811
    from im import lattice
    from helpers import HashEmbedder

    new = idea(matched, "Something new")
    matched.execute("""INSERT INTO im.item_verdicts (item_id, segment_ids, quote, verdict)
                       SELECT i.item_id, i.source_segment_ids, i.quote, 'discard'
                       FROM im.items i JOIN im.idea_evidence ev USING (item_id) WHERE ev.idea_id = %s""", (new,))
    lattice.place(matched, HashEmbedder().model_version)
    data = sky_data(as_app(admin, web.sky))
    cards = as_app(admin, lambda c: {r[0] for r in c.execute("SELECT idea_id::text FROM pub.cards")})
    assert {s[0] for s in data["stars"]} == cards and str(new) in cards   # discarded, so not in pub.ideas, but here
    assert data["rel"] is None and data["seed"] is None   # the home sky: no lens, the encodings light it


def test_a_lens_lights_its_seed_and_what_it_reaches(matched, admin):  # noqa: F811
    build = str(idea(matched, "Broken builds as signal"))
    (signal,) = table(matched, "SELECT idea_id::text FROM im.ideas WHERE statement LIKE '%%always correct%%'")[0]
    data = sky_data(as_app(admin, web.sky, build))
    assert data["seed"] == build and data["rel"][build] == 1.0
    assert data["rel"][signal] > 0   # it evolves from the signal idea
    assert all(0 < v <= 1 for v in data["rel"].values())
    assert max(v for k, v in data["rel"].items() if k != build) == 1.0   # the most relevant other is lit fully
    assert {s[0]: (s[1], s[2]) for s in data["stars"]} == \
        {s[0]: (s[1], s[2]) for s in sky_data(as_app(admin, web.sky))["stars"]}   # a lens moves nothing
    assert as_app(admin, web.sky, "00000000-0000-0000-0000-000000000000") is None


def test_lighting_the_sky_in_place_fetches_what_loading_the_lens_page_shows(served, matched):  # noqa: F811
    import json

    build = str(idea(matched, "Broken builds as signal"))
    _, headers, _ = request(served, "POST", "/login", f"token={TOKEN}")
    cookie = headers["Set-Cookie"].split(";")[0]
    page = sky_data(request(served, "GET", f"/sky?seed={build}", cookie=cookie)[2])
    status, headers, body = request(served, "GET", f"/sky/lens?seed={build}", cookie=cookie)
    got = json.loads(body)
    assert (status, headers["Content-Type"]) == (200, "application/json")
    assert got == {"seed": build, "rel": page["rel"], "title": "Broken builds as signal"}   # the map isn't sent again
    assert request(served, "GET", "/sky/lens?seed=00000000-0000-0000-0000-000000000000", cookie=cookie)[0] == 404
    assert request(served, "GET", f"/sky/lens?seed={build}")[0] == 303   # no lens without logging in


def test_demo_mode_blanks_a_sensitive_star_and_leaves_it_out_of_every_list(matched, admin):  # noqa: F811
    import json

    from im import lattice
    from helpers import HashEmbedder

    new = str(idea(matched, "Something new"))
    as_app(admin, lambda c: c.execute("""INSERT INTO pub.feedback_events (kind, idea_id, payload)
                                         VALUES ('idea_sensitive', %s, %s)""", (new, json.dumps({"sensitive": True}))))
    lattice.place(matched, HashEmbedder().model_version)
    assert "Something new" in as_app(admin, web.feed, "candidates")
    assert "Something new" not in as_app(admin, web.feed, "candidates", 0, True)   # a list leaves it out
    hidden = as_app(admin, web.card, new, True)
    assert "Hidden in demo mode" in hidden and "A new thing" not in hidden and "action" not in hidden
    stars = {s[0]: s for s in sky_data(as_app(admin, web.sky, None, True))["stars"]}
    assert set(stars) == {s[0] for s in sky_data(as_app(admin, web.sky))["stars"]}   # the map keeps its shape
    assert stars[new][6] == ""                                                       # its star, blank
    # What any presentation reads in demo mode holds nothing it could show by mistake.
    assert as_app(admin, lambda c: c.execute("SELECT title, statement, redacted FROM pub.demo_cards WHERE idea_id = %s",
                                             (new,)).fetchall()) == [(None, None, True)]
    assert as_app(admin, lambda c: c.execute("SELECT count(*) FROM pub.demo_card_evidence WHERE idea_id = %s",
                                             (new,)).fetchall()) == [(0,)]


def test_demo_mode_is_one_setting_off_only_with_the_token_and_takes_nothing(served, matched):  # noqa: F811
    new = str(idea(matched, "Something new"))
    _, headers, _ = request(served, "POST", "/login", f"token={TOKEN}")
    cookie = headers["Set-Cookie"].split(";")[0]
    assert request(served, "POST", "/demo", "action=on", cookie)[0] == 303
    assert "Set-Cookie" not in request(served, "POST", "/demo", "action=on", cookie)[1]   # the app's, not a browser's
    assert "leave demo mode" in request(served, "GET", "/", cookie=cookie)[2]
    assert request(served, "POST", f"/idea/{new}", "action=dismiss", cookie)[0] == 403
    assert request(served, "POST", f"/idea/{new}", "action=dismiss&reason=not_for_me", cookie)[0] == 403
    assert "saying why" not in request(served, "GET", f"/idea/{new}", cookie=cookie)[2]
    # Whoever I hand the phone to can't turn it off: that takes the token.
    assert request(served, "POST", "/demo", "action=off", cookie)[0] == 403
    assert request(served, "POST", "/demo", "action=off&token=another-long-enough-token", cookie)[0] == 403
    assert "leave demo mode" in request(served, "GET", "/", cookie=cookie)[2]
    assert request(served, "POST", "/demo", f"action=off&token={TOKEN}", cookie)[0] == 303
    assert "leave demo mode" not in request(served, "GET", "/", cookie=cookie)[2]


def test_a_dismissal_may_say_why_and_each_reason_is_counted(matched, admin, monkeypatch, capsys):  # noqa: F811
    from im import cli

    unsaid, *said = [str(i) for (i,) in table(matched, "SELECT idea_id FROM pub.cards ORDER BY idea_id")]
    assert len(said) == len(DismissReason)
    for card, reason in zip(said, DismissReason):   # a duplicate may name which card it duplicates
        as_app(admin, web.act, card, "dismiss", reason, unsaid if reason == DismissReason.DUPLICATE else None)
    as_app(admin, web.act, unsaid, "dismiss")   # no reason: dismissed, exactly as before; why is unknown
    assert table(matched, """SELECT idea_id::text, payload, source FROM pub.feedback_events
                             WHERE kind = 'idea_dismiss' ORDER BY event_id""") == [
        *[(card, {"reason": r} | ({"of": unsaid} if r == "duplicate" else {}), "emanation")
          for card, r in zip(said, DismissReason)],
        (unsaid, {}, "emanation")]
    as_app(admin, web.act, said[0], "dismiss")   # dismissed again without a reason: the reason I gave stands
    monkeypatch.setattr(cli.db, "pipeline_dsn", lambda: matched.info.dsn + " password=" + matched.info.password)
    cli.main(["feedback", "--reasons"])
    assert json.loads(capsys.readouterr().out) == {"dismissed": 5, "not_a_thought": 1, "misrepresents": 1,
                                                   "duplicate": 1, "not_for_me": 1, "unknown": 1}


def test_a_dismissal_closes_its_proposal_with_a_reason_or_without(matched, admin):  # noqa: F811
    weight.stage(matched, WeightConfig(proposals_per_week=5))
    (first, _), (second, _) = proposals(matched)
    as_app(admin, web.act, str(idea(matched, first)), "dismiss", "not_for_me")
    as_app(admin, web.act, str(idea(matched, second)), "dismiss")
    assert weight.stage(matched, WeightConfig(proposals_per_week=5))["dismissed"] == 2
    assert proposals(matched) == [(first, "dismissed"), (second, "dismissed")]


def test_a_reason_is_one_of_the_list_and_only_on_a_dismissal(served, matched, admin):  # noqa: F811
    new, other = str(idea(matched, "Something new")), str(idea(matched, "Broken builds as signal"))
    _, headers, _ = request(served, "POST", "/login", f"token={TOKEN}")
    cookie = headers["Set-Cookie"].split(";")[0]
    page = request(served, "GET", f"/idea/{new}", cookie=cookie)[2]
    assert "dismiss, saying why" in page and 'value="misrepresents"' in page
    events = lambda: table(matched, "SELECT count(*) FROM pub.feedback_events")  # noqa: E731
    before = events()
    for form in ("action=dismiss&reason=boring", "action=pinned&reason=not_for_me",
                 f"action=dismiss&reason=not_for_me&of={other}", f"action=dismiss&reason=duplicate&of={new}",
                 "action=dismiss&reason=duplicate&of=00000000-0000-0000-0000-000000000000",   # not a card
                 f"action=dismiss&of={other}"):
        assert request(served, "POST", f"/idea/{new}", form, cookie)[0] == 400, form
    assert events() == before
    for payload in ({"reason": "boring"}, {"reason": "not_for_me", "of": other}, {"reason": "duplicate", "of": "x"}):
        with pytest.raises(psycopg.errors.CheckViolation):   # whoever writes it
            as_app(admin, lambda c, p: c.execute("""INSERT INTO pub.feedback_events (kind, idea_id, payload)
                                                    VALUES ('idea_dismiss', %s, %s)""", (new, json.dumps(p))), payload)
    assert request(served, "POST", f"/idea/{new}", f"action=dismiss&reason=duplicate&of={other}", cookie)[0] == 303
    assert events() == [(before[0][0] + 1,)]


def test_the_reasons_are_the_ones_the_database_takes(admin):
    (check,) = table(admin, """SELECT pg_get_constraintdef(oid) FROM pg_constraint
                              WHERE conname = 'feedback_events_dismiss_reason'""")[0]
    assert set(re.findall(r"'(\w+)'", check)) - {"idea_dismiss", "reason", "of"} == set(DismissReason)


class Nearby:
    """What the lens reads: connections and lineage with their weight, and the seed's nearest cards by meaning."""

    def __init__(self, links, near):
        self.links, self.near = links, near

    def execute(self, sql, args=None):
        return iter(self.near if "card_neighbours" in sql else self.links)


def test_a_seed_nothing_is_like_lights_only_itself():
    from im import lens

    assert lens.relevance(Nearby([], [("b", -0.2), ("c", 0.0)]), "a") == {"a": 1.0}
    assert lens.relevance(Nearby([("b", "c", 1.0)], [("b", 0.0)]), "a") == {"a": 1.0}   # linked, but not to the seed


def test_a_trail_is_a_conversations_moments_in_the_order_said(matched, admin):  # noqa: F811
    from im import lattice
    from helpers import HashEmbedder

    lattice.place(matched, HashEmbedder().model_version)
    build, coffee, new = (str(idea(matched, t)) for t in (
        "Broken builds as signal", "The coffee order is the real meeting.", "Something new"))
    data = sky_data(as_app(admin, web.trails))
    ids = [s[0] for s in data["stars"]]
    assert ids == [s[0] for s in sky_data(as_app(admin, web.sky))["stars"]]   # the sky's map, star for star
    # Both captures of the one episode share its start in pub.card_evidence; a moment is timed by its own lines.
    assert [(ids[star], conv, who) for star, conv, _, who, _ in data["moments"]] == [
        (build, 0, 0), (coffee, 0, 2), (new, 1, 0)]
    assert data["moments"][0][2] < data["moments"][1][2]
    assert [m[4] for m in data["moments"]] == [-1, -1, 0.72]           # the arousal in my voice, where measured
    judged(matched, uuid.UUID(new), uuid.UUID(build), "tension")       # a connection: what a stop can return to
    assert [*sorted((ids.index(build), ids.index(new))), "tension"] in sky_data(as_app(admin, web.trails))["links"]
    assert "c-conv" not in as_app(admin, web.trails)                   # a conversation is a number, never its id
    assert sky_data(as_app(admin, web.trails, new))["at"] == new
    assert as_app(admin, web.trails, "00000000-0000-0000-0000-000000000000") is None


def test_a_crossroads_has_doors_across_its_conversation_and_to_what_is_like_it(matched, admin):  # noqa: F811
    build, coffee = (str(idea(matched, t)) for t in ("Broken builds as signal", "The coffee order is the real meeting."))
    page = as_app(admin, web.crossroads, build)
    across, like = page.split('<section class="right">')[1].split('<section class="down">')
    assert f'href="/crossroads/{coffee}"' in across and "moments later, same conversation" in across
    assert "nothing" in page.split('<section class="left">')[1].split("<main>")[0]   # it opened its conversation
    assert "Something new" in like and "near by meaning · 1 hour later" in like
    assert coffee not in like                                   # a door across isn't repeated among what's like it
    signal, grape = table(matched, "SELECT a::text, b::text FROM pub.card_links")[0]
    assert f'href="/crossroads/{grape}"><small>connected' in as_app(admin, web.crossroads, signal)
    judged(matched, idea(matched, "Something new"), uuid.UUID(build), "tension")   # a kind names the door
    assert "<small>tension · 1 hour later</small><b>Something new" in as_app(admin, web.crossroads, build)
    assert "Did you see the build broke again?" in page and "anon A" not in page
    back = as_app(admin, web.crossroads, coffee).split('<section class="left">')[1].split("<main>")[0]
    assert f'href="/crossroads/{build}"' in back and "moments earlier" in back
    assert as_app(admin, web.crossroads, "00000000-0000-0000-0000-000000000000") is None
    assert as_app(admin, web.anywhere) in {r[0] for r in table(matched, "SELECT idea_id::text FROM pub.cards")}


def test_demo_mode_keeps_a_sensitive_card_off_every_trail_and_behind_no_door(matched, admin):  # noqa: F811
    import json

    from im import lattice
    from helpers import HashEmbedder

    build, coffee = (str(idea(matched, t)) for t in ("Broken builds as signal", "The coffee order is the real meeting."))
    as_app(admin, lambda c: c.execute("""INSERT INTO pub.feedback_events (kind, idea_id, payload)
                                         VALUES ('idea_sensitive', %s, %s)""", (coffee, json.dumps({"sensitive": True}))))
    lattice.place(matched, HashEmbedder().model_version)
    data = sky_data(as_app(admin, web.trails, None, True))
    star = [s[0] for s in data["stars"]].index(coffee)
    assert data["stars"][star][4] == "" and star not in {m[0] for m in data["moments"]}   # its star, blank, on no trail
    assert coffee not in as_app(admin, web.crossroads, build, True)
    hidden = as_app(admin, web.crossroads, coffee, True)
    assert "Hidden in demo mode" in hidden and "coffee" not in hidden
    for _ in range(20):
        assert as_app(admin, web.anywhere, True) != coffee


def test_the_new_views_are_served_behind_the_login(served, matched):  # noqa: F811
    from im import lattice
    from helpers import HashEmbedder

    lattice.place(matched, HashEmbedder().model_version)
    new = str(idea(matched, "Something new"))
    for path in ("/trails", "/echoes", "/threads", "/apart", "/tree", "/grid", "/crossroads", f"/crossroads/{new}"):
        assert request(served, "GET", path)[0] == 303 and request(served, "GET", path)[1]["Location"] == "/login"
    _, headers, _ = request(served, "POST", "/login", f"token={TOKEN}")
    cookie = headers["Set-Cookie"].split(";")[0]
    assert sky_data(request(served, "GET", f"/trails?at={new}", cookie=cookie)[2])["at"] == new
    status, headers, _ = request(served, "GET", "/crossroads", cookie=cookie)
    assert status == 303 and headers["Location"].startswith("/crossroads/")       # somewhere, at random
    assert "Something new" in request(served, "GET", headers["Location"], cookie=cookie)[2]
    assert request(served, "GET", f"/crossroads/{new}", cookie=cookie)[0] == 200
    assert "<h1>Echoes</h1>" in request(served, "GET", "/echoes?kind=tension&gap=week", cookie=cookie)[2]
    assert "<h1>Threads</h1>" in request(served, "GET", "/threads", cookie=cookie)[2]
    assert "<h1>Apart</h1>" in request(served, "GET", "/apart?page=2", cookie=cookie)[2]
    assert "<h1>Grid</h1>" in request(served, "GET", "/grid", cookie=cookie)[2]
    assert new in [s[0] for s in sky_data(request(served, "GET", "/tree", cookie=cookie)[2])["stars"]]
    assert request(served, "GET", "/trails?at=nope", cookie=cookie)[0] == 404
    assert request(served, "GET", "/crossroads/nope", cookie=cookie)[0] == 404


def judged(conn, a, b, verdict, direction=None, why="Because.", analogy=""):
    """As the connection judge records a pair (connect._record): its verdict, and a connection with its kind."""
    a, b, flip = min(a, b), max(a, b), a > b
    direction = {"a>b": "b>a", "b>a": "a>b"}.get(direction, direction) if flip else direction
    conn.execute("""INSERT INTO im.connection_verdicts (a, b, verdict, direction, analogy, why, belongs, method,
                                                        input_hash) VALUES (%s, %s, %s, %s, %s, %s, .9, 'connect-t', 'h')""",
                 (a, b, verdict, direction, analogy, why))
    if verdict != "none":
        conn.execute("INSERT INTO im.connections (a, b, method) VALUES (%s, %s, 'connect-t')", (a, b))
    if verdict in ("supports", "elaborates", "tension"):
        conn.execute("INSERT INTO im.connection_kinds (a, b, kind, direction, method) VALUES (%s, %s, %s, %s, 'connect-t')",
                     (a, b, verdict, direction))


def test_an_echo_is_a_connection_across_conversations_the_earlier_beside_the_later(matched, admin):  # noqa: F811
    build, coffee, new = (idea(matched, t) for t in (
        "Broken builds as signal", "The coffee order is the real meeting.", "Something new"))
    judged(matched, new, build, "elaborates", "a>b", "The sensors carry the build's idea further.", "a signal nobody reads")
    judged(matched, build, coffee, "tension")            # said in one conversation: connected, but no echo
    page = as_app(admin, web.echoes)
    assert "1 connection between" in page and page.count("<li ") == 1
    row = page.split("<ol>")[1]
    assert row.index("Broken builds as signal") < row.index("1 hour") < row.index("Something new")
    assert "the later elaborates the earlier" in row and 'class="elaborates"' in row
    assert "The sensors carry the build&#x27;s idea further." in row and "Same structure: a signal nobody reads" in row
    assert "Did you see the build broke again?" in row and f'href="/crossroads/{new}"' in row
    assert page.count("<path") == 1                                               # its arc over the archive's time
    for narrowed in (("tension",), ("all", "week")):                                # filters only narrow
        assert "0 connections" in as_app(admin, web.echoes, *narrowed)
    assert "1 connection between" in as_app(admin, web.echoes, "analogy", "nonsense")   # an unknown filter is none
    # The judge's `none` is kept for views to read, and is no connection.
    judged(matched, coffee, new, "none")
    assert as_app(admin, lambda c: c.execute("SELECT count(*) FROM pub.card_judgments WHERE verdict = 'none'"
                                             ).fetchone()) == (1,)
    assert as_app(admin, web.echoes).count("<li ") == 1
    # Apart shows the pair the judge kept apart, with its reason: the earlier beside the later, and no connection.
    page = as_app(admin, web.apart)
    assert "1 pair near by meaning" in page and page.count("<li ") == 1 and "Because." in page
    assert page.index("coffee order") < page.index("1 hour apart") < page.index("Something new")
    assert "Nothing here." in as_app(admin, web.apart, 1)                           # a page past the end
    as_app(admin, lambda c: c.execute("""INSERT INTO pub.feedback_events (kind, idea_id, payload)
                                         VALUES ('idea_sensitive', %s, '{"sensitive": true}')""", (new,)))
    assert "0 connections" in as_app(admin, web.echoes, "all", "any", True)         # demo mode leaves the pair out
    assert "0 pairs" in as_app(admin, web.apart, 0, True)


def test_a_broad_card_does_not_fuse_the_neighbourhoods_it_touches():
    from im import lens

    left, right = [("a", "b"), ("b", "c"), ("a", "c")], [("x", "y"), ("y", "z"), ("x", "z")]
    assert lens.neighbourhoods(left + right + [("c", "x")]) == [["a", "b", "c"], ["x", "y", "z"]]   # one bridge
    assert lens.neighbourhoods([("b", "a"), ("a", "a")]) == [["a", "b"]] and lens.neighbourhoods([]) == []
    pairs = left + right + [("c", "x"), ("p", "q")]
    assert lens.neighbourhoods(pairs) == lens.neighbourhoods(list(reversed(pairs)))                  # the same every time


def test_a_thread_is_a_neighbourhood_said_in_more_than_one_conversation(matched, admin):  # noqa: F811
    from im import lattice
    from helpers import HashEmbedder

    lattice.place(matched, HashEmbedder().model_version)
    build, coffee, new = (idea(matched, t) for t in (
        "Broken builds as signal", "The coffee order is the real meeting.", "Something new"))
    judged(matched, build, coffee, "related")
    assert "0 threads" in as_app(admin, web.threads)            # two cards, one conversation: no thread
    judged(matched, coffee, new, "related")
    judged(matched, build, new, "related")
    page = as_app(admin, web.threads)
    row = page.split("<details")[1]
    assert "1 thread:" in page and "<b>Broken builds as signal</b>" in row.split("</summary>")[0]   # the first said
    assert "3 cards · 2 conversations · over 1 hour" in row and row.count("<circle") == 2            # a knot in each
    assert f'href="/trails?at={build}"' in row and f'href="/trails?at={new}"' in row
    lit = ",".join(str(i) for i in (build, coffee, new))                    # its cards, lit on the sky or the tree
    assert f'href="/sky?lit={lit}"' in row and f'href="/tree?lit={lit}"' in row
    listed = row.split("<ol>")[1]
    assert listed.index("Broken builds") < listed.index("coffee order") < listed.index("Something new")
    as_app(admin, lambda c: c.execute("""INSERT INTO pub.feedback_events (kind, idea_id, payload)
                                         VALUES ('idea_sensitive', %s, '{"sensitive": true}')""", (new,)))
    assert "0 threads" in as_app(admin, web.threads, True)       # demo mode: a sensitive card is in no thread


def test_the_tree_stands_a_neighbourhood_in_for_each_lens(matched, admin):  # noqa: F811
    import json

    build, coffee, new = (str(idea(matched, t)) for t in (
        "Broken builds as signal", "The coffee order is the real meeting.", "Something new"))
    before = sky_data(as_app(admin, web.tree))
    lenses = {s[0]: s[2] for s in before["stars"]}
    assert lenses[build] == lenses[coffee] == lenses[new] == [] and len(before["named"]) == 1   # no connection: no lens
    judged(matched, build, coffee, "related")
    data = sky_data(as_app(admin, web.tree))
    ids = [s[0] for s in data["stars"]]
    lenses = {s[0]: s[2] for s in data["stars"]}
    assert lenses[build] == lenses[coffee] and lenses[new] == []           # a second neighbourhood, on a lens of its own
    assert sorted(set(map(tuple, lenses.values()))) == [(), (0,), (1,)]
    assert sorted((ids.index(build), ids.index(coffee))) in data["links"] and len(data["links"]) == 2
    # Named by the first card said in it, and these two were said together.
    assert data["named"][lenses[build][0]] in ("Broken builds as signal", "The coffee order is the real meeting.")
    as_app(admin, lambda c: c.execute("""INSERT INTO pub.feedback_events (kind, idea_id, payload)
                                         VALUES ('idea_sensitive', %s, '{"sensitive": true}')""", (coffee,)))
    demo = sky_data(as_app(admin, web.tree, True))
    assert [s[0] for s in demo["stars"]] == ids                            # demo mode: every card keeps a star
    hidden = {s[0]: s[2] for s in demo["stars"]}
    assert hidden[build] == hidden[coffee] == [] and demo["links"] == before["links"]   # on no lens, unlinked
    assert "coffee order" not in json.dumps(demo)                          # and blank



def test_the_sky_says_when_each_card_was_first_said_for_replay(matched, admin):  # noqa: F811
    from im import lattice
    from helpers import HashEmbedder

    lattice.place(matched, HashEmbedder().model_version)
    new = idea(matched, "Something new")
    first = table(matched, "SELECT first_said FROM pub.cards WHERE idea_id = %s", new)[0][0]
    star = {s[0]: s for s in sky_data(as_app(admin, web.sky))["stars"]}[str(new)]
    assert star[8] == int(first.timestamp())


def test_a_grid_cell_is_a_neighbourhood_drawn_as_the_sky_places_it(matched, admin):  # noqa: F811
    from im import lattice
    from helpers import HashEmbedder

    lattice.place(matched, HashEmbedder().model_version)
    build, coffee, new = (idea(matched, t) for t in (
        "Broken builds as signal", "The coffee order is the real meeting.", "Something new"))
    assert "0 cells" in as_app(admin, web.grid)                  # the fixture's pair is too few for a cell
    for a, b in ((build, coffee), (coffee, new), (build, new)):
        judged(matched, a, b, "related")
    page = as_app(admin, web.grid)
    cell = page.split("<section>")[1]
    assert "1 cell." in page and cell.count("<circle") == 3 and cell.count("<line") == 3   # its cards and connections
    assert "3 cards" in cell and all(f'href="/crossroads/{i}"' in cell for i in (build, coffee, new))
    assert 'href="/sky?lit=' in cell and 'href="/tree?lit=' in cell
    as_app(admin, lambda c: c.execute("""INSERT INTO pub.feedback_events (kind, idea_id, payload)
                                         VALUES ('idea_sensitive', %s, '{"sensitive": true}')""", (new,)))
    assert "0 cells" in as_app(admin, web.grid, True)             # demo mode: a sensitive card is in no cell


def test_crossroads_shows_where_its_doors_are_on_the_map(matched, admin):  # noqa: F811
    from im import lattice
    from helpers import HashEmbedder

    build, new = (idea(matched, t) for t in ("Broken builds as signal", "Something new"))
    judged(matched, build, new, "related")
    lattice.place(matched, HashEmbedder().model_version)
    page = as_app(admin, web.crossroads, str(build))
    assert '<svg class="rose"' in page and f'<a href="#d{new}"><line class="like"' in page   # a ray to the door
    door = page.split(f'id="d{new}"')[1].split("</a>")[0]
    assert '<svg class="way"' in door                                                # and an arrow on the door


def test_an_echo_arc_is_drawn_in_its_turn(matched, admin):  # noqa: F811
    build, new = (idea(matched, t) for t in ("Broken builds as signal", "Something new"))
    judged(matched, build, new, "related")
    page = as_app(admin, web.echoes)
    assert page.count("<path pathLength") == page.count('<li id="e') >= 1 and 'style="--d: 0"' in page
