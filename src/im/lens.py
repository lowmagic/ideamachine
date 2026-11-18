"""Lenses (decision 0013): stored ones, and the view from a cluster, which isn't one.

A stored lens is a query, filters and pins (pub.lenses, pub.lens_members, migration 069): a cluster is in it if it's
pinned in, or it passes the query and the filters and isn't pinned out. Its kinds here are `all` (the filters decide)
and `none` (only the pins). I make and change lenses, and pin cards in or out, by feedback events (`record`), applied
the moment they're sent; boards place lenses in the places of a drawing (the Tree's sephirot and paths, the Grid's
cells).

The view from a cluster says how relevant every cluster is to its seed. It never says where anything goes:
positions are the pipeline's, one map for every view. Relevance is personalized PageRank from the seed over every
connection (pub.card_links, one per pair) and lineage link (pub.card_lineage, followed both ways, decision 0010), with
the seed's nearest cards by meaning (pub.card_neighbours) added as edges from the seed, weighted by similarity. A kind
adds its weight to its connection (KIND_WEIGHT, 0 until something shows a kind should pull harder). The seed is 1;
the rest are scaled so the most relevant other cluster is 1 too, so the view always has something lit around its
seed. With no seed there's nothing to compute: every cluster is equally relevant, and the page's encodings light the
sky. A global PageRank would be a score of what matters, which decision 0007 rules out.

It reads only pub, as the web app's role (lattice_app), and writes only feedback events.
"""

import json
import uuid
from collections import defaultdict

KINDS = {"all": "every card the filters admit", "none": "only the cards I pin",
         "jev": "the cards Jev's answers put at or above its cut"}
MADE_HERE = ("all", "none")   # a `jev` query is built offline and imported (lens_run.import_file, decision 0013)
STATES = {"pinned": "kept at hand", "active": "active", "archived": "archived"}
MATURITIES = ("observation", "candidate", "idea")
KEPT = ("canonical", "pinned", "unkept")
# The Tree's places in its drawing's order (tree.html): the ten sephirot, then the 22 paths by their letters.
TREE_PLACES = ("kether", "chokmah", "binah", "chesed", "geburah", "tiphareth", "netzach", "hod", "yesod", "malkuth",
               "aleph", "beth", "gimel", "daleth", "heh", "vav", "zain", "cheth", "teth", "yod", "kaph", "lamed",
               "mem", "nun", "samekh", "ayin", "peh", "tzaddi", "qoph", "resh", "shin", "tav")
LENS_EVENTS = ("lens_add", "lens_edit", "lens_state", "lens_pin", "lens_unpin", "board_place", "board_remove")


def _views(demo: bool) -> str:
    return "pub.demo_" if demo else "pub."


def record(conn, kind: str, payload: dict, idea_id: str | None = None, source: str = "emanation") -> None:
    """One lens or board event, as I'd send it from any view. Its shape is the database's to refuse
    (psycopg.errors.CheckViolation); what it did is applied at once."""
    if kind not in LENS_EVENTS:
        raise ValueError(f"not a lens event: {kind}")
    conn.execute("INSERT INTO pub.feedback_events (kind, idea_id, payload, source) VALUES (%s, %s, %s, %s)",
                 (kind, idea_id, json.dumps(payload), source))


def add(conn, name: str, description: str = "", kind: str = "all", filters: dict | None = None,
        source: str = "emanation") -> str | None:
    """Make a lens: its id, or None if another lens has that name."""
    lens_id = str(uuid.uuid4())
    record(conn, "lens_add", {"lens_id": lens_id, "name": name, "description": description, "query": {"kind": kind}}
           | ({"filters": filters} if filters else {}), source=source)
    return lens_id if conn.execute("SELECT 1 FROM pub.lenses WHERE lens_id = %s", (lens_id,)).fetchone() else None


def stored(conn, demo: bool = False) -> list[dict]:
    """Every lens, those kept at hand first and the archived last, then by name, with how many cards it holds."""
    cur = conn.execute(f"""SELECT l.lens_id::text AS id, l.name, l.description, l.kind, l.query, l.filters, l.state,
                                  (SELECT count(*) FROM {_views(demo)}lens_members m WHERE m.lens_id = l.lens_id)
                                    AS cards
                           FROM pub.lenses l
                           ORDER BY l.state = 'pinned' DESC, l.state = 'archived', lower(l.name)""")
    names = [c.name for c in cur.description]
    return [dict(zip(names, r)) for r in cur.fetchall()]


def find(conn, ref: str) -> dict:
    """The lens a reference names: its id, its name in any case, or else the one lens whose name contains it.
    ValueError otherwise, naming the lenses there are."""
    ref = str(ref).strip()
    every = stored(conn)
    for test in (lambda x: x["id"] == ref, lambda x: x["name"].lower() == ref.lower()):
        hit = [x for x in every if test(x)]
        if hit:
            return hit[0]
    hit = [x for x in every if ref and ref.lower() in x["name"].lower()]
    if len(hit) == 1:
        return hit[0]
    there = ", ".join(x["name"] for x in every) or "none yet"
    raise ValueError(f"{ref!r} matches {'several lenses' if hit else 'no lens'}; the lenses: {there}")


def members(conn, lens_id: str, demo: bool = False, limit: int | None = None) -> list[dict]:
    """A lens's cards, with why each is in (`pin` or `query`) and the query's score where it gave one: those I
    pinned first, then the most recently said. In demo mode a hidden card is in no lens."""
    v = _views(demo)
    cur = conn.execute(f"""SELECT c.*, m.why, m.score FROM {v}lens_members m JOIN {v}cards c USING (idea_id)
                           WHERE m.lens_id = %s ORDER BY m.why = 'pin' DESC, c.last_seen DESC, c.idea_id
                           LIMIT %s""", (lens_id, limit))
    names = [c.name for c in cur.description]
    return [dict(zip(names, r)) for r in cur.fetchall()]


def judged(conn, lens: dict, demo: bool = False) -> tuple[int, int]:
    """How much of the map a lens's query has judged, and of how many cards (pub.lens_judged): a `jev` lens the cards
    it has answered under its current query; `all` and `none` ask nothing, so they've judged every card."""
    return conn.execute("SELECT asked, cards FROM pub.lens_judged WHERE lens_id = %s", (lens["id"],)).fetchone()


def describe(lens: dict) -> str:
    """The query and the filters, in words."""
    f = lens["filters"] or {}
    parts = [KINDS[lens["kind"]]]
    if lens["kind"] == "jev":
        q = lens["query"]
        n = len(q["deciding"])
        parts[0] = (f"Jev: {n} question{'s' * (n != 1)}, "
                    + ("the likeliest" if q["rule"] == "max" else "their mean") + f" at {q['cut']} or above")
    if "maturity" in f:
        parts.append(" or ".join(f["maturity"]))
    if "kept_as" in f:
        parts.append(" or ".join("not kept" if k == "unkept" else f"kept as {k}" for k in f["kept_as"]))
    if "said_by_me" in f:
        parts.append("said by me" if f["said_by_me"] else "not said by me")
    if "sensitive" in f:
        parts.append("sensitive" if f["sensitive"] else "not sensitive")
    if "said_since" in f or "said_until" in f:
        parts.append(f"said {f.get('said_since', '…')} to {f.get('said_until', '…')}")
    return " · ".join(parts) if lens["kind"] != "none" else parts[0]


def places(conn, drawing: str) -> dict[str, dict]:
    """A board's places that hold a lens, by drawing (`tree`, `grid`): place -> its lens."""
    cur = conn.execute("""SELECT p.place, l.lens_id::text AS id, l.name, l.kind, l.state
                          FROM pub.board_lenses p JOIN pub.boards b USING (board_id) JOIN pub.lenses l USING (lens_id)
                          WHERE b.drawing = %s""", (drawing,))
    return {place: {"id": i, "name": name, "kind": kind, "state": state} for place, i, name, kind, state in cur}


def board(conn, drawing: str) -> str:
    """The id of the board a drawing draws."""
    return str(conn.execute("SELECT board_id FROM pub.boards WHERE drawing = %s ORDER BY created_at LIMIT 1",
                            (drawing,)).fetchone()[0])


RESTART = 0.15     # the chance each step of jumping back to the seed
NEAR = 20          # nearest by meaning, joined to the seed
STEPS = 60
KIND_WEIGHT = 0.0  # what each kind on a connection adds to its 1 (decision 0010)


def relevance(conn, seed: str) -> dict[str, float]:
    """Cluster id -> relevance to the seed, in [0, 1]. Clusters it doesn't reach are left out (0)."""
    weights: dict = defaultdict(lambda: defaultdict(float))
    for a, b, w in conn.execute(
            """SELECT a::text, b::text, 1 + %s::float8 * cardinality(kinds) FROM pub.card_links
               UNION ALL SELECT from_id::text, to_id::text, 1::float8 FROM pub.card_lineage""", (KIND_WEIGHT,)):
        if a != b:
            weights[a][b] += float(w)
            weights[b][a] += float(w)
    for other, similarity in conn.execute("SELECT idea_id::text, similarity FROM pub.card_neighbours(%s, %s)",
                                          (seed, NEAR)):
        if float(similarity) > 0:   # an edge of weight 0 would leave a card with nowhere to go
            weights[seed][other] += float(similarity)
            weights[other][seed] += float(similarity)
    if seed not in weights:
        return {seed: 1.0}
    out_sum = {n: sum(w.values()) for n, w in weights.items()}
    r = {seed: 1.0}
    for _ in range(STEPS):
        nxt = defaultdict(float, {seed: RESTART})
        for n, mass in r.items():
            for m, w in weights[n].items():
                nxt[m] += (1 - RESTART) * mass * w / out_sum[n]
        r = nxt
    top = max((v for n, v in r.items() if n != seed), default=0.0)
    return {n: (1.0 if n == seed else round(min(v / top, 1.0), 4)) for n, v in r.items() if v > 0 and top > 0}


ROUNDS = 30   # label propagation settles in a handful on the live graph


def neighbourhoods(pairs) -> list[list[str]]:
    """Graph neighbourhoods (decision 0010): sets of clusters that connections hold together, from every connection
    as an (a, b) pair. Label propagation: each cluster starts as its own neighbourhood and, taken in id order, joins
    the one its connections pull it to hardest (the lowest id on a tie), until nothing moves. A connection pulls with
    1 plus the number of clusters both its ends are connected to, so one inside a tight group outweighs a lone bridge
    out of it, and a broad cluster connected a little to everything doesn't fuse the neighbourhoods it touches, which
    connected components would. The same pairs always give the same neighbourhoods. A cluster with no connection
    is in none. Each is sorted, and they come largest first."""
    near: dict = defaultdict(set)
    for a, b in pairs:
        if a != b:
            near[a].add(b)
            near[b].add(a)
    label = {n: n for n in near}
    for _ in range(ROUNDS):
        moved = False
        for n in sorted(near):
            pull: dict = defaultdict(int)
            for m in near[n]:
                pull[label[m]] += 1 + len(near[n] & near[m])
            most = max(pull.values())
            if pull.get(label[n], 0) < most:
                label[n] = min(k for k, v in pull.items() if v == most)
                moved = True
        if not moved:
            break
    groups: dict = defaultdict(list)
    for n, k in label.items():
        groups[k].append(n)
    return sorted((sorted(g) for g in groups.values()), key=lambda g: (-len(g), g))
