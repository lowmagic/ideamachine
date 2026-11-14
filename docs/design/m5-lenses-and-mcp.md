# M5: the MCP server here, and lenses

_2026-11-14. Status: plan; my answers of 2026-11-14 are in, nothing built. The design ROADMAP-v2's M5 row asks for.
It settles how [decision 0005](../decisions/0005-presentation-inside-idea-machine.md)'s MCP server moves in under
[SPEC §2](../SPEC.md#2-contracts)'s contract, and the storage [decision 0013](../decisions/0013-lenses-and-boards.md)
leaves to M5._

**Done when** (ROADMAP-v2): the claude.ai connector works unchanged against the new endpoint, and 5 canonical lenses
are judged useful.

## Today

- No MCP server runs. Lattice served it until 2026-11-11 ([deploy.md](../deploy.md), "Lattice, removed"): 19 tools,
  10 that read and 9 that write (lattice `src/lattice/mcp.py:441-520`), on a tailnet listener with a bearer token and
  a public one behind OAuth, a Cloudflare tunnel and two WAF rules (lattice `docs/public-mcp.md`). The tunnel, the
  WAF rules and the claude.ai connector were removed by hand.
- `im web` (`src/im/web.py`) is one `ThreadingHTTPServer` as role `lattice_app`, on 127.0.0.1:8792 and, through
  `tailscale serve`, the tailnet on 8445. Its container runs the pipeline's image, so the `models` extra
  (sentence-transformers) is there.
- A lens is computed, never stored: `lens.relevance(seed)` lights the sky from one cluster. Themes are their own
  tables (`im.themes`, `im.idea_themes`), filed by Claude (`themes.file_stage`) and edited by my events (024). The
  Tree (`/tree`) and Grid (`/grid`) are boards, drawn with graph neighborhoods standing in for their lenses.
- The live database has no themes and no kept ideas (2026-11-14, through `im_reader`: 0 themes; of 1,065 cards,
  998 candidates and 67 observations, none kept). The themes stage can't start from none: the proposer groups only
  ideas the filer checked and found no theme for, and the filer checks nothing while there are no themes. The old
  database had themes from the seeded archive, which the rebuild left behind.

## Part 1: the MCP server

### Where it runs

In `im web`, as Lattice's did in `lattice serve`: the same process, role and container, two listeners.

- **Tailnet:** `/mcp` on the existing listener, with `Authorization: Bearer $IM_WEB_TOKEN`. It's Lattice's
  `LATTICE_TOKEN` renamed (deploy.md), so the value is the one the old connector config used. For clients on my own
  devices; claude.ai and ChatGPT call from their own servers and can't reach the tailnet.
- **Public:** a second listener on 127.0.0.1:8791 that serves MCP and OAuth and nothing else, opened only when
  `IM_PUBLIC_URL` is set, behind a Cloudflare tunnel (a `cloudflared` container again in `install/compose.yaml`)
  and the two WAF rules, as lattice `docs/public-mcp.md` describes: one rule lets in only the callers' address
  ranges, the other closes the sign-in page except while I add a connector. The app checks the caller's address
  again from `CF-Connecting-IP` against `IM_MCP_ALLOW`.
- **For Claude and ChatGPT both.** Lattice's OAuth registered only Claude's two callbacks and let in only
  Anthropic's range (`160.79.104.0/21`). Here the registration also takes ChatGPT's redirect URI, and the WAF rule
  and `IM_MCP_ALLOW` also take OpenAI's published egress ranges. ChatGPT expects the issuer in the authorization
  response (RFC 9207), which Lattice's OAuth already sends. More ranges let more callers knock; OAuth still decides
  who gets in.
- **Not Tailscale Funnel:** it hides the caller's address from the app (tailscale#12972), and it has no edge rules,
  so OAuth would be the only guard, and the sign-in page couldn't be closed between connectors.

The code moves in from Lattice nearly as it is: `mcp.py` (JSON-RPC over streamable HTTP, plain JSON responses, no
sessions; protocol versions 2025-11-25, 2025-06-18, 2025-03-26), `oauth.py` (its own OAuth 2.1 server: dynamic
registration for Claude's two callbacks only, PKCE S256, one-hour access tokens, 30-day rotated refresh tokens,
everything signed with a key derived from the token, nothing stored), and the listener and allowlist parts of
`server.py` and `auth.py`. As `src/im/mcp.py` and `src/im/oauth.py`, with `web.py` routing to them. The tools read
`pub` through `lattice_app`, never `im`, and append `pub.feedback_events` with `source = 'mcp'`.

### The 19 tools against today's `pub`

The contract is the tools' names, argument schemas and result shapes, not the views Lattice read. Each tool is
rewritten here against what `pub` has now, and keeps its shapes.

| Tool | Reads | Missing today |
|---|---|---|
| `search_ideas` | `pub.ideas`, matching title, statement, notes, my note, themes | `pub.ideas.notes`, `my_note` |
| `get_idea` | `pub.ideas`, `pub.evidence`, `pub.connections`, the rubric score | `notes`, `archive_ref`, `claude_title`, `claude_statement`, `my_note`, `corrected`; `pub.idea_scores` (053) |
| `best_ideas` | `pub.idea_scores`, `pub.ideas` | `pub.idea_scores` (053) |
| `list_themes`, `open_theme` | `pub.themes`, `pub.ideas` | nothing |
| `whats_new` | `pub.ideas`, `pub.proposals`, `pub.themes` | nothing |
| `weekly_digest` | `pub.digests`, `pub.ideas` | nothing |
| `draft_material` | as `get_idea`, plus a theme or a digest point | as `get_idea` |
| `similar_ideas` | `pub.idea_neighbours` by id; `pub.ideas_near(vector, int)` by text | both (058) |
| `rename_theme`, `merge_themes`, `split_theme`, `file_idea` | `pub.themes`, `pub.ideas`; theme events | nothing |
| `add_idea`, `proposals`, `keep_idea`, `star_idea`, `link_ideas`, `note_idea` | `pub.proposals`; feedback events | nothing |

(Lattice: `src/lattice/mcp.py:75-436`, `data.py:46-253`, `meaning.py:46-67`.) What `pub` gains:

- **`pub.ideas` gets its columns back,** appended so the view is replaced, not rebuilt: `my_note`, `claude_title`,
  `claude_statement` and `corrected` from `im.idea_text`; `notes` (the old archive elaboration) and `archive_ref`
  as NULL, since nothing has either since 057 and 058. `get_idea` returns `ref: null` and `notes: null`, which its
  shape allows.
- **`pub.ideas_near(vector, int)`** again, over `im.idea_embeddings` in `pub.embedding_model()`, for search by text.
  The query is embedded in the web process with `lattice.SentenceEmbedder`, the code that embedded the ideas, so
  the vectors match by construction (Lattice needed FastEmbed and a parity test). Loaded on the first text search:
  torch costs the web container a few hundred MB from then on. [UNMEASURED]
- **No `pub.idea_neighbours`:** by id, `similar_ideas` asks `pub.card_neighbours(seed, k)`, which already exists.

**The rubric's three tools.** Nothing has been scored since [0007](../decisions/0007-neutral-proposals.md), and 053
dropped the scores. The contract already has a shape for "nothing scored", which Lattice's tests pin (lattice
`tests/test_mcp.py:102-121`): `best_ideas` answers with a tool error, `get_idea` leaves out `ranking`, and
`draft_material` gives every idea `score: null`. So that's what they do. `best_ideas`'s error and its description
(not part of the contract) say ideas aren't rated, and point to `proposals`, `whats_new` and `search_ideas`. Once I
keep ideas, `best_ideas` lists those kept as canonical, most recently said first, `score` and the rubric's fields
null and `how_scored` saying so: my choice, not a rating, so 0007 holds. That changes what it returns, not its
shape.

**Pseudonymization** stays as Lattice did it (`mcp.py:50-57`): in `get_idea`'s evidence and in each
`draft_material` answer, every speaker but `me` and `unknown` becomes `S1`, `S2`... in order of appearance, one
numbering per answer. It covers speaker labels only: quotes, titles and digest text go as they are, as they did.

**Demo mode is one setting for the whole app,** read on every request by every page, `/sky/lens` and `/mcp`. It's
switched at runtime, from the web view or `im demo on|off`, by a feedback event (`demo_on`, `demo_off`; the latest
wins), so I can show someone the map from my phone without a restart. Switching it off from the web view asks for
the token again, so whoever I hand the phone to can't. `im web --demo` still forces it on. While
it's on, everything hides what `pub.demo_hidden_ideas` and `pub.sensitivity` hide, the web view's demo mode as it
is; MCP serves only the nine read tools Lattice's demo did (not `proposals`, no writes); and swear words are blacked
out in every string, as Lattice did (`mcp.py:533-582`). The per-browser demo link, `/demo/mcp` and Lattice's
`LATTICE_DEMO_HIDE` list go: one definition of hidden, so no two endpoints disagree. Dropping `/demo/mcp` doesn't
touch the contract, which is the tools.

**Verified by** Lattice's tests, ported (lattice `tests/test_mcp.py`, `test_audit.py`, `test_public_mcp.py`): the
handshake and errors, no raw speaker label in any answer, the exact feedback each write appends, the theme edits,
drafting, the OAuth flow and token binding, and demo mode, here the one setting. Then the done-when check: the
claude.ai connector, added again, calls a read and a write tool; and a ChatGPT connector does the same.

### `open_lens`

The one new tool: `open_lens(lens, limit)`, a lens by name or id. It answers with the lens's name, description,
how much of the map it has judged ("judged 300 of 1,065"), and its members as brief ideas, each with why it's in (a
pin, or the query's score). In demo mode too, with the same hiding.

## Part 2: lenses

As [0013](../decisions/0013-lenses-and-boards.md) decided: a lens is a query, filters and pins; a cluster is in it
if it's pinned in, or it passes the query and the filters and isn't pinned out. Viewing from a cluster
(`lens.relevance`) stays as it is.

### Storage

My input is feedback events, as for themes (024): a lens is made, edited, pinned, archived and placed on a board by
events (`lens_add`, `lens_edit`, `lens_pin`, `lens_unpin`, `lens_archive`, `board_place`...), applied on insert by
a trigger, so the web view and MCP can do it as `lattice_app`. The tables they write:

- **`im.lenses`:** id, name, description, `query` (jsonb: its kind and that kind's parameters), `query_version`
  (the hash of `query`), `filters` (jsonb), `state` (`active`, `pinned` or `archived`), created.
- **`im.lens_pins`:** lens, in or out, and what it's anchored to: the pinned capture's `segment_ids`, or an
  archive or typed observation's `source_ref`. A view resolves each pin to the cluster that holds that speech now.
  Forgetting a pin's segments deletes the pin.
- **`im.lens_answers`:** lens, cluster, `query_version`, the cluster's text hash, in or out, score, reason, and the
  egress row it came from. Derived: it cascades with the cluster, and a changed text or version is asked again.
- **`im.boards`, `im.board_lenses`:** a board's name and drawing (`tree`, `grid`), and each lens on it with the
  place it holds there: a sephira or a path on the Tree, a cell on the Grid. The Tree and the Grid are the first two
  boards, made empty: nothing is imported. A place I haven't given a lens shows its neighborhood stand-in, as now.
- **`pub.lenses`, `pub.lens_members`** (lens, cluster, why: `pin`, `query`; score), and demo versions that leave out
  `pub.demo_hidden_ideas`.

### Query kinds

`all` and `none` first, then `jev`: a set of Nouls, a rule combining them (`any k at ≥ cut`, `mean ≥ cut`) and the
cut. The state Jev sees is the cluster's title and statement, as the sensitivity tagger's is. A `jev` lens's run
asks, by default, only the clusters nearest its description by embedding, up to a maximum; `im lens run ID --all`
asks the rest. Every request goes through `egress.send` and the monthly cap. At Jev's price a lens over the archive
costs about a cent; the cost is time, a request per cluster. [UNMEASURED]

Filters, first: maturity, kept as, said by me, said since or until, sensitive. Each is a condition on `pub.cards`
and `pub.sensitivity`, checked in SQL.

**Editing a query is my request to ask again,** so it isn't held by the re-read gate: the gate holds what a version
change would send unasked. A new Jev model or question wording that I didn't make is held, as every other stage's
is.

### Where they show

- `/lens/<id>`: the members, and why each is in; pin in or out from a card.
- The sky lit by a lens, as from a cluster now, through `/sky/lens`.
- The Tree and the Grid read their board: a place with a lens shows that lens's members. I build a board from its
  drawing, picking or making the lens for a place (`board_place`, `board_remove`).
- `im lens list | show | run | import FILE`, the last for a long query built offline (0013).

### Themes become lenses

As built, a theme is a lens but for two things. The filer files each idea into at most two of all the themes at
once (`themes.FILE_SYSTEM`), so adding a theme changes what the others get, where 0013's lenses are flat. And the
proposer makes themes. In M5 each theme becomes a lens judged on its own: its name and description its query (a
`jev` lens), my filing corrections its pins, and pin, rename, merge and split lens events.

- `pub.themes` becomes a view over the lenses, with its columns as they are, so the theme tools keep their shapes;
  every lens is listed as a theme. Theme events (`rename_theme`, `merge_theme`, `split_theme`, `file_idea`,
  `unfile_idea`) are applied to lenses by the trigger: a merge moves the pins and archives the lens merged away; a
  split makes a lens with the ideas given pinned in.
- There are no themes to migrate (above), so `im.themes`, `im.idea_themes`, `im.theme_checks` and
  `im.theme_rejections` go, with the filer, the proposer and the describer.
- Whether something proposes lenses from clusters in none, as the proposer did themes, is left until there are
  lenses to judge it by.

## Slices

One PR each, in order:

1. `pub` additions, the MCP server on the tailnet, and demo mode as one setting, with Lattice's tests ported.
2. The public listener, OAuth for Claude and ChatGPT, and the tunnel in `install/`. Done when both connectors work.
3. Lens storage and events, kinds `all` and `none`, pins, filters, `pub.lens_members`, `/lens/<id>`, `open_lens`;
   boards stored, the Tree and the Grid reading them.
4. The `jev` kind and `im lens run`.
5. Themes become lenses.
6. The canonical lenses, judged under bars registered in im-evals first.

## The canonical lenses

Six, chosen 2026-11-14, each a `jev` lens, pinned: three topics (what a card is about) and three questions (what
I'd ask of the map). Their names, descriptions and Jev questions are personal, so they live in im-evals, which is
private, and reach `im` as data (`im lens import FILE`, 0013); nothing in this repo names them. ROADMAP-v2 asks for
5 judged useful, so M5 is done when 5 of the 6 pass.

**Tried first,** before the sheets: a pilot (`im audit-lens`, with its own pre-registration) has Jev answer every
lens's wordings about every card, from a `lenses.json` kept in saved/, and reports what each lens lets in and the
cards near its cut, to reword from. Nothing it writes is a lens.

Judged twice, both bars registered in im-evals before any run, and my use kept apart from both:

- **Correct.** For each lens, a blind sheet: 20 members drawn at random, and 20 near non-members (the clusters
  nearest its description by embedding that the lens left out), shuffled, nothing saying which is which. I mark each
  in or out. Precision among members, and how many near non-members I put in; rates across the archive are weighted
  by each stratum's size, since neither half is a uniform draw.
- **Better than embedding alone.** The baseline is the clusters nearest the lens's description by embedding, as
  many as the lens has members. The lens has to beat it on my marks, or the query isn't earning its cost and the
  cheaper kind does. The sheet adds 20 of the baseline's members the lens left out.
- **Used.** Whether I open each lens again over the next two weeks, recorded as an observation beside the bars,
  unblinded, never mixed into them.

## Still open

- Each lens's description and Jev questions: drafted in im-evals, for me to edit, before the sheets.
- The bars' numbers, in the pre-registration (a starting point: precision ≥ 0.8, and above the baseline's).
