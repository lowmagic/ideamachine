# M6 design: Emanation (notes)

_2026-11-02. Status: notes, not a design yet. Milestone M6 in [ROADMAP-v2.md](../ROADMAP-v2.md). Collected from a
conversation about visualizing the lattice (night sky, river, strata, and others); what I agreed with, and where it
meets decisions already made. Nothing here is decided until the design is written._

## Direction

Emanation is one temporal, semantic graph explorer with several visual encodings, not several visualizations. The
data has six dimensions (semantic, topological, temporal, lifecycle, activity, provenance); each encoding picks a few
to show rather than all six at once.

For M6:

1. **A sky as the home view.** Stars are clusters, placed by semantic neighborhood. ~~The layout updates
   incrementally and stays put.~~ Not required since [0010](../decisions/0010-connections-kinds-lenses.md): Emanation
   is the fluid view, and Trellis (pinned lenses laid out by hand; Lattice v2 in 0010) is the stable one.
2. **Lenses as zoom, not separate pages.** Subjects → clusters → candidates → observations → the original
   utterances. On the home sky a lens lights its results where they are; opened on its own, it lays them out by their
   connections (a force layout started from its last run's positions) and draws each neighborhood as a cloud with
   its synthesis proposal over it ([0010](../decisions/0010-connections-kinds-lenses.md)).
3. **Proposed connections as faint dashed links.** The "negative space": connections the evidence suggests that I
   haven't made. These are hypotheses, never edges, until I keep them. Not to be confused with a bridge idea, which
   is an existing idea that touches two or more lenses (as Lattice v1's bridges touch two themes).

**2D, not 3D** (2026-11-04). Depth hides neighbors and makes you orbit to find them; motion (replay, new evidence
arriving, the camera flying to a lens) earns more than a third axis of meaning. A faster renderer (WebGL) doesn't
need 3D positions, and changing the pipeline's stable positions to 3D later would reset where everything is.

## Encodings

| Channel | Shows | Why it's safe |
|---|---|---|
| Sharpness | How recently it was said | A fact |
| Brightness | Recurrence (distinct conversations), decaying with time | A fact |
| Size | Lifecycle, and canonical vs pinned | My decision |
| Color | Semantic neighborhood | Descriptive |
| Links | Connections; their kinds and cited observations on inspection | Evidence-backed |

**No significance channel.** [Decision 0007](../decisions/0007-neutral-proposals.md) closed M3 because no signal
predicted what I keep, so weight orders nothing. Encoding weight as brightness or size would put that ranking back
in visually.

## The negative-space map is synthesis, drawn

It doesn't need a new subsystem. The spec already has the parts: embeddings nominate pairs, the edge model answers
`none` as a real answer ([SPEC §5](../SPEC.md#5-edge-typing)), and the synthesis stage writes an idea from a neighborhood
of connected clusters, which arrives as a proposal (whether that beats proposing its best existing point is an A/B
test, [0010](../decisions/0010-connections-kinds-lenses.md)). The dashed links are:

- connections proposed and not yet decided, and
- possibly pairs that are close by embedding where Jev answered `none` or fell below the confidence floor.

Clicking one shows the cited utterances on both sides. Keeping a proposed connection makes it a connection; keeping
a synthesis makes it an idea, linked to what it was made from.

## Conflicts with decisions already made

- ~~[Decision 0005](../decisions/0005-presentation-inside-idea-machine.md) drops global positions for lens
  layouts.~~ Resolved by amending 0005 (2026-11-02): positions stay in the pipeline, a lens returns relevance, and the
  home sky is the lens with no seed.
  Amended again by [0010](../decisions/0010-connections-kinds-lenses.md) (2026-11-06): the one map need not stay put.

## First sky (`im web`, `/sky`)

- Every card at its pipeline position (`pub.card_positions`; the pipeline places every card, not only Lattice's).
- Brightness: distinct conversations. Sharpness: days since last said (older is softer and wider). Size: lifecycle
  (observation, candidate, pinned, canonical). Color: the direction from the map's center, so neighbors share a
  hue. Edges: faint, every kind.
- `/sky?seed=<id>` is a lens: personalized PageRank from the seed over edges, plus the seed's nearest by meaning
  (`lens.relevance`). Unlit clusters dim; the camera frames the lit ones.
- Tap a star for its title and facts, then open its card or make it the lens. Drag to pan, pinch or scroll to zoom.
- Drawn as stars (2026-11-13): a white core with a faint halo of its color, and four spikes that shorten with age, so
  sharpness still reads as recency. Behind them, a haze summed from the stars themselves: where cards sit close, the
  sky glows their color. No mark that isn't a card.
- Motion never changes where a star rests. Stars twinkle slowly, the sharp ones most. A tap sends a pulse out along
  the star's connections, four hops, with less energy each hop. The pulse draws each link as it crosses it, and each
  star it strikes flares and is knocked a few pixels the way the pulse was going, then springs back to its place.
- A tap lights a star, ringed and named, and keeps what's picked; a tap on the lit star picks it. A tap on nothing
  puts the lit star out, and another clears what's picked. So a stray tap while panning changes nothing. A lit star
  is a bigger target: a tap within 36 px of it picks it, whatever else is nearer, and a faint circle shows that
  reach. The same on Trails and the tree.
- While a star is picked, only the links it reaches are drawn, fainter each hop out; up to eight of the cards it's
  connected to are named; and the rest of the sky dims. Stars aren't draggable, so a drag always pans. With reduced
  motion asked for, nothing animates.
- **Replay** (2026-11-13). A button plays the archive as it grew, from the first card said to the last, in 45
  seconds, with a bar to scrub and the day and the count of cards so far. A card is on the sky once it has been
  said, flaring as it arrives, and a link once both its cards have. It replays when things were said on today's map:
  where a card sits and what it's connected to are as they are now, which is the caution under Double exposure
  below. A typed card, with no time said, is always there.
- **Lit cards.** `/sky?lit=<ids>` lights those cards where they are, as a lens lights its results, and frames them.
  A thread's row and a grid cell link to it.

## Trails (`im web`, `/trails`)

The sky's map, drawn through time. The sky shows where a thought sits and nothing of when it was said beyond a soft
edge; a trail shows the order.

- A **moment** is one capture as it was said: a card, a conversation, a time (`pub.card_moments`, migration 063). It's
  timed by the capture's own first cited line, where `pub.card_evidence` gives every capture of an episode the
  episode's start.
- A **trail** is one conversation's moments in order, drawn as a curve from star to star that brightens toward its
  end. The last five conversations stay as fading afterimages.
- The strip along the bottom has one column per conversation, in the order they began, a block per moment in its
  star's hue: how much each held, and which parts of the map it visited. Columns, not a time axis, so a quiet month
  or an archive capture years old doesn't squeeze the rest.
- Step through conversations (arrow keys, or drag along the strip), or play them in order. The list names each stop
  with its time and who said it. Tapping a star goes to the latest conversation that passed through it.
  `/trails?at=<id>` opens on a card's conversation.
- **Returns and new ground.** A stop returns to a card when it's connected to one first said earlier, in another
  conversation: an echo seen from the later side. It gets a ring; the list says what it returns to and how long
  after; picking it draws a dashed line back; and its block on the strip is dim, so a column's bright blocks are that
  conversation's new ground, as far as the connections know.
- **Heat.** A lit stop glows wider the more arousal Hearsay measured in my voice (048), and the list shows it as a
  bar. A fact about the voice, never a judgment of the thought. On the live archive 1,002 of 1,046 speech moments
  have one, spread from 0.1 to 0.8.
- On a trail of more than 16 stops the numbers show only at its ends and around the stop picked, whose legs in and
  out are drawn bright. Live conversations run to 82 stops.
- **The pulse** (2026-11-13). Stars and haze are drawn as the sky draws them, a star gaining its spikes when a trail
  lights it. As a trail is drawn its head is a pulse with a short bright tail: each stop it reaches flares and is
  knocked the way the trail was going, then springs back. With reduced motion asked for, the stops only light.
- A tap on one of the trail's own stops picks it. A tap on any other star lights it, ringed and named, and a second
  tap goes to its conversation, so a stray tap while panning never leaves the trail.
- Nothing ranks: conversations come in the order they happened, and stars keep the sky's size and hue.
- Positions are today's. A trail says when things were said, never where they sat on the map at the time, which is
  the same caution as double exposure below.
- Demo mode: a sensitive card keeps its star, blank, and has no moments, so a trail passes it by.

## Crossroads (`im web`, `/crossroads/<id>`)

A reading view: one card, with the two kinds of neighbor every thought here has.

- **Across, in time:** what was said just before and just after it in its own conversations. Usually about
  something else: that's the point.
- **Up and down, in meaning:** what's like it, said at another time: its connections and lineage first, then its
  nearest by meaning, earlier above and later below, each with how far apart they were said ("41 days earlier").
  A door across isn't repeated here.
- **Where the doors are on the map** (2026-11-13). Each door has a small arrow: the way its card lies from this
  one on the sky's map. Under the card a rose draws one ray per door, in that direction, longer for the farther
  ones, colored as its arm. Direction on the map is semantic neighborhood, not a connection. The doors come in from
  their sides when the page opens.
- Every door opens that card's crossroads, and the arrow keys walk. `/crossroads` picks a card at random: nothing
  chooses it ([0007](../decisions/0007-neutral-proposals.md)).
- "Near by meaning" is an embedding neighbor, named as that, never as a connection
  ([0010](../decisions/0010-connections-kinds-lenses.md)).

## Echoes (`im web`, `/echoes`)

What I came back to. Almost every capture founds its own cluster (1,049 of 1,065 cards were said in one
conversation, 2026-11-13), so recurrence doesn't show as a cluster said twice. It shows as connections: of 1,015, 511
join two cards said in different conversations.

- An **echo** is a connection between two cards with no conversation in common. The earlier is shown beside the
  later, with how far apart they were first said, the connection's kinds and their direction ("the later elaborates
  the earlier"), and the judge's reason and the structure it named, marked as the judge's (`pub.card_judgments`,
  migration 064).
- Above the list, every echo is an arc over the archive's time, from the earlier card to the later, colored by kind.
  An arc links to its row, and pointing at either lights the other. The arcs draw themselves when the page opens,
  oldest first.
- Newest first, by when the later card was said. Kind (elaborates, supports, tension, analogous) and gap (a week, a
  month) only narrow the list.
- Embedding neighbors the judge hasn't connected aren't echoes: a shared subject is no connection
  ([0010](../decisions/0010-connections-kinds-lenses.md)). Asking for every card's neighbors would also take minutes:
  `pub.card_neighbours` takes 0.3 s a card on the live archive.
- Demo mode leaves out a pair with a sensitive card.

## Threads (`im web`, `/threads`)

What runs through the conversations: the loom. Echoes shows returns a pair at a time; a thread is a whole
neighborhood that keeps coming back.

- A **thread** is a graph neighborhood of at least 3 cards said in at least 2 conversations. Neighborhoods come from
  the connections alone, by label propagation (`lens.neighbourhoods`): each card joins the neighborhood its
  connections pull it to hardest, a connection pulling with 1 plus the number of cards both its ends are connected
  to. So a connection inside a tight group outweighs a lone bridge out of it, and a broad card connected a little to
  everything doesn't fuse the neighborhoods it touches, which is the caution in
  [0010](../decisions/0010-connections-kinds-lenses.md). The same connections always give the same neighborhoods. On
  the live archive (2026-11-13): 182 neighborhoods, the largest of 18 cards; 78 are threads, holding 488 of the 1,065
  cards, the longest through 9 of 69 conversations. Connected components would make one of 473.
- Each thread is a row across every conversation, in the order they began, with a knot where it was said, bigger for
  more cards. A knot opens that conversation's trail. Opening a row lists the thread's cards in the order said.
- A thread is named by the first card said in it and colored as the sky colors the middle of its cards. Rows come
  in the order the threads began. Nothing ranks them.
- A thread is not a lens or a theme: nothing names its subject, and it writes nothing. It's the neighborhood that a
  synthesis would be proposed over, so this view is also where to look at what synthesis would be given.
- An opened row links to its cards lit on the sky (`/sky?lit=`) and on the tree (`/tree?lit=`).
- Demo mode: a sensitive card is in no thread.

## Apart (`im web`, `/apart`)

The negative space, as a list: pairs near enough by meaning to be asked about, that the connection judge kept apart.

- A row is a `none` from the judge (`pub.card_judgments`): the earlier card beside the later, how far apart they
  were said, and the judge's reason, which says where it drew the line between sharing a subject and being
  connected.
- Newest first, 50 to a page. Nothing here is a connection, and the page makes none.
- Pairs the screen kept from the judge aren't shown: nothing was said about them.
- Not seen on the live archive yet (2026-11-13): the reader role can't read verdicts until migration 064 is there,
  so how many rows this has isn't known.

## Tree (`im web`, `/tree`)

A first Trellis view, ahead of the lenses it's for: the Tree of Life as a board of 32 lenses (the ten sephirot and
the 22 paths, [0013](../decisions/0013-lenses-and-boards.md)), turned in three dimensions.

- **A lens where I put one, a stand-in elsewhere.** A sephira or path holds the lens I placed there on the Tree board
  (`/board/tree`, M5 slice 3); one I haven't filled holds one of the 32 largest graph neighborhoods
  (`lens.neighbourhoods`), the n-th largest at the n-th place, the largest on Kether. Which neighborhood is on which
  place means nothing, and the page says so. On the live archive (2026-11-13) the stand-ins put 294 of 1,065 cards on
  the tree.
- **Three dimensions, here only.** §Direction keeps the sky in 2D because depth hides neighbors. A board I lay out by
  hand is different: its shape is the point, and the tree has one. The side pillars stand back from the middle one,
  and it turns slowly until it's touched. Drag turns it, pinch or scroll zooms; nothing is draggable.
- A sephira's cards are a ball around it, colored as the sephira (the Queen scale); a path's are a sleeve along it,
  colored from one end to the other. A card on no lens is in the far sky, a shell of faint stars around the tree.
- **A card on several lenses** has a star on each, joined by a double line, bright when the card is lit or picked.
  Picking either star picks the card. The stand-in for this: a card is also on a lens when two of its connections
  lead into that lens's neighborhood (21 cards on the live archive). If real lenses make the double lines a tangle,
  draw them only for the card that's lit or picked.
- **A tap lights, a second tap picks.** The first tap rings a star and names it, and keeps what's picked; a tap on
  the lit star picks it and sends the sky's pulse out along its connections, each drawn to the nearest star of the
  card it reaches. A tap on nothing puts the lit star out; another clears what's picked. A tap on a sephira or the
  middle of a path lights the cards on it.
- Demo mode: a sensitive card is in the far sky, blank, with no connections.

## Grid (`im web`, `/grid`)

The plainer Trellis board: a grid of lenses, one to a cell.

- **Lenses first, then stand-ins.** The lenses I placed on the Grid board (`/board/grid`, M5 slice 3), in the order
  of their numbered cells; then, as on the tree, each graph neighborhood of at least 3 cards (`lens.neighbourhoods`),
  named by the first card said in it, in the order they began. On the live archive (2026-11-13): 98 neighborhoods.
- A cell draws its cards as a small constellation: each star where the sky puts its card relative to the others in
  the cell, in the sky's color, with the connections among them. A star opens its card's crossroads.
- Nothing ranks the cells: a lens's cell is where I put it, and the stand-ins come in the order they began.
- A cell links to its cards lit on the sky and on the tree. `/tree?lit=<ids>` lights those cards wherever they sit
  on the tree, until a tap on nothing.
- Demo mode: a sensitive card is in no cell.

## Views the data could carry now (ideas, 2026-11-12)

None of these needs lenses, so none waits for M5. Each reads only what `pub` already has.

- **Two voices.** Trails with the not-me moments drawn apart, to show where other people take me. Nothing to draw
  yet: every moment on the live archive is mine (1,026 by voice, 69 by diarization; 2026-11-13).
- **Analogies.** The judge names the shared structure when two cards are analogous (`pub.card_judgments.analogy`),
  and Echoes can narrow to those. An analogy is a lens ([0010](../decisions/0010-connections-kinds-lenses.md)), so its
  own view waits for lenses: each named structure a lens, its results the other instances.
- **Keeping from these views.** Apart and Echoes only read. A connection I make or remove is a `link` or `unlink`
  event already (045); a button on a row would be the "keep a proposed connection" of the negative space above.
- **Negative space on the sky.** Apart is a list. The dashed links of §Direction would draw the same pairs on the
  map, once it's clear from the list how many there are.

## Later encodings (not M6)

- **Mycelium:** growth and lifecycle. More grounded than it looks, because utterance → observation → candidate → idea and
  `evolves` edges are real lineage. Only links between clusters must not imply causation.
- **Seismograph:** periods when the landscape changes quickly (novel regions, sudden cross-links, new
  contradictions). Call them events, not discoveries.
- **Weather:** attention as moving systems over semantic space; replay a month in thirty seconds.
- **Double exposure:** then vs now. The ledger and the edges' `method`/model version mostly record when something
  was inferred. **Check first:** if embeddings get recomputed, past positions can't be rebuilt honestly, and
  relationships inferred in November mustn't be replayed into July.
- **River, strata, orrery:** attention trends, persistent preoccupations, canonical ideas as gravity (an alternative
  model view, not the default).
