"""Correcting a card from the CLI: `im idea edit` and `im idea show`."""

import pytest

from im import sensitivity

from helpers import assert_invariants, table
from test_merge import cards, idea, merge
from test_sensitivity import tagger


@pytest.fixture
def im_idea(seeded_lattice, monkeypatch, capsys):
    """`im idea ARGS`: what it printed."""
    from im import cli

    conn = seeded_lattice
    monkeypatch.setattr(cli.db, "pipeline_dsn", lambda: conn.info.dsn + " password=" + conn.info.password)

    def run(*args) -> str:
        cli.main(["idea", *map(str, args)])
        return capsys.readouterr().out
    return run


def test_a_corrected_statement_is_on_the_card_and_is_changed_input(seeded_lattice, im_idea):
    conn = seeded_lattice
    client, sent = tagger()
    n = sensitivity.tag_stage(conn, client)["tagged"]
    coffee = idea(conn, "coffee")
    title, statement = cards(conn)[coffee][:2]

    out = im_idea("edit", str(coffee)[:8], "--statement", "The tea order is the real meeting.", "--note", "office",
                  "--meaning-changed")

    assert out.index(statement) < out.index("after:") < out.index("The tea order is the real meeting.")
    assert cards(conn)[coffee][:2] == (title, "The tea order is the real meeting.")
    assert table(conn, """SELECT kind, payload, source FROM pub.feedback_events WHERE idea_id = %s
                          ORDER BY event_id""", coffee) == [
        ("idea_correct", {"statement": "The tea order is the real meeting.", "meaning_changed": True}, "cli"),
        ("idea_note", {"note": "office"}, "cli")]
    stats = sensitivity.tag_stage(conn, client)   # the changed card alone, sent again; nothing held
    assert (stats["tagged"], len(sent), stats.get("held")) == (1, n + 1, None)
    assert "corrected (statement)" in im_idea("show", coffee) and "my note: office" in im_idea("show", coffee)

    im_idea("edit", coffee, "--statement", "")   # reverts to Claude's
    assert cards(conn)[coffee][:2] == (title, statement)
    assert "not corrected" in im_idea("show", coffee)
    assert_invariants(conn)


def test_show_has_the_quotes_and_the_text_merged_in(seeded_lattice, im_idea, monkeypatch, capsys):
    conn = seeded_lattice
    builds, coffee = idea(conn, "Broken builds"), idea(conn, "coffee")
    coffee_title, coffee_statement = cards(conn)[coffee][:2]
    merge(conn, monkeypatch, capsys, builds, coffee)
    out = im_idea("show", builds)
    assert "merged in" in out and coffee_title in out and coffee_statement in out


def test_it_refuses_an_unknown_id_a_merged_cluster_and_no_field(seeded_lattice, im_idea, monkeypatch, capsys):
    conn = seeded_lattice
    builds, coffee = idea(conn, "Broken builds"), idea(conn, "coffee")
    merge(conn, monkeypatch, capsys, builds, coffee)
    events = table(conn, "SELECT count(*) FROM pub.feedback_events")
    for args, says in (((coffee, "--title", "x"), f"merged into {builds}"),
                       (("ffffffff-ffff", "--title", "x"), "no cluster"),
                       ((builds,), "nothing to change"),
                       ((builds, "--title", "x" * 121), "at most 120")):
        with pytest.raises(SystemExit, match=says):
            im_idea("edit", *args)
    with pytest.raises(SystemExit, match="no cluster"):
        im_idea("show", "%")   # a prefix is hex digits and hyphens, never a pattern
    assert table(conn, "SELECT count(*) FROM pub.feedback_events") == events
