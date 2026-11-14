# Installing and running Idea Machine

How to set up a dev environment, run the pipeline and its tests, and how `im run` works step by step.
Commands run from the repository root. Production runs on the NAS: see [deploy.md](deploy.md). Design and
plans are in [ROADMAP.md](ROADMAP.md).

## Dev setup (eeyore)

```sh
cp .env.example .env              # pick passwords; .env is gitignored
podman compose up -d              # Postgres 16 + pgvector on 127.0.0.1:55432 (docker compose works too)
set -a; . ./.env; set +a
uv sync --extra models          # CPU-only torch and sentence-transformers: the lattice's embeddings
uv run im migrate                 # im schema
uv run im load-fixtures           # a synthetic Hearsay stream in $IM_STREAM_DIR (dev/stream)
uv run im run                     # import + segment + queue taps and notes to self + extract ideas with Claude + match; idempotent
uv run im queue                   # the attention queue: what I tapped the pendant for or called a note to self, with its lines, newest first
uv run im queue remove 3          # take item #3 off the queue (only marks it removed; nothing else changes)
uv run im pending                 # what the next run would send, and what the re-read gate holds per stage, with its estimated cost; sends nothing
uv run im reread extract --limit 20   # approve held re-reads: 20 episodes read again under the current prompt (0: only the count and estimate)
uv run im ideas                   # what Claude captured; --review to keep or discard each item
uv run im ideas --discards        # what I discarded and why: material for revising the extraction prompt
uv run im trial v5.3 --model openai/gpt-6.1-sol   # B3: a model on the trial sample, vs Opus
uv run im trial v5.3 --model claude-opus-5-5 --replicate 2   # Opus against itself: the ceiling for recall
uv run im trial v5 --replicate 2   # extraction v5 (decision 0012) on the trial sample, read twice; the report counts the capture checks each read fails
uv run im trial v5 --model openai/gpt-6.1-sol --report --same-point   # also count a thought the two quoted from different lines, where Jev says they make the same point (sends only those pairs, to Jev; kept)
uv run im audit-misses sheet saved/misses-terra v5.3 --model openai/gpt-6.1-terra   # its misses among Opus's solid captures, blind beside ones it found; score my marks
uv run im audit-misses sheet saved/bar-sol v5 --model openai/gpt-6.1-sol --both-ways   # 20 captures only Sol made, 20 only Opus made, 20 both made, blind: whose bar is right?
uv run im trial v5.4 --sample 40 --seed 54 --exclude saved/eligibility-1   # the moment-bound-remarks trial (ROADMAP-v2), step 1: v5.4 on Opus (the configured model and effort) on 40 episodes I speak in, none an eligibility sheet showed (it says how many it held out); the report points to step 2
uv run im audit-misses sheet saved/v54-1 v5.4 --model claude-opus-5-5 --live   # step 2: against v5.3's live captures on those episodes, every capture only v5.3 made (removed), every one only v5.4 made (added) and 20 both made, blind, marked eligible/ineligible/uncertain under eligibility rubric 1; sends nothing
uv run im audit-misses score saved/v54-1   # step 3, once marked: removed (n), removed ineligible (k, 95% interval), valid baseline (N), valid lost (m of N), added (a), added ineligible
uv run im hard-set saved/hard-1 --per-case 6 --controls 20   # a deliberate hard set of episodes (irony, fragments, relayed thoughts, short thoughts, ones I only acknowledge, ones answering someone), plus controls from the trials' random sample, each as extraction would send it; sends nothing
uv run im mishearings list   # the transcription's mishearings, proposed by v5 captures or added by me; then `confirm HEARD MEANT` (also adds one) or `reject HEARD MEANT`
uv run im audit-edges sheet saved/b4-4 --size 75 --seed 7 --exclude saved/b4-1 saved/b4-2 saved/b4-3   # B4: a fresh sheet with no pair I've marked before
uv run im audit-edges draft saved/b4-3 --model jev-1.14.0   # B4: Jev answers a sheet; score shows connection at every cut
uv run im audit-edges review saved/b4-1 saved/b4-2   # B4: pairs where a chat model's draft disagrees with my mark, with its case; a pair I left unmarked takes the models' mark where they agree (--all lists those too, to spot-check); answer in review.md, run again to apply
uv run im audit-eligibility sheet saved/eligibility-1   # capture eligibility (docs/design/eligibility-audit.md): 100 live captures at random, with their lines, under the frozen rubric; sends nothing (the archive's captures are left out and counted)
uv run im audit-eligibility draft saved/eligibility-1 --model openai/gpt-6.1-terra   # a model labels every item eligible, ineligible or uncertain, blind to other drafts; then --model google/gemini-4-carbon; no Anthropic model (Opus wrote the captures)
uv run im audit-eligibility review saved/eligibility-1   # review.md: the items the drafts split on and 20 they agree on, shuffled, no labels; mark them, run again to read the marks back; then `score` (model agreement, my validation, the ineligible rate)
uv run im audit-lens draft saved/lens-pilot-1 --limit 20   # the lens pilot (M5 design): Jev answers the wordings in the folder's lenses.json (kept in saved/, never in the repo) about 20 cards, one request each; without --limit, every card not answered yet
uv run im audit-lens report saved/lens-pilot-1   # per lens: cards asked, how many pass its cut, the spread, how its wordings agree, the members lenses share; near-cut.md, the cards either side of each cut, to reword from; sends nothing
uv run im bench-match run saved/b1-1 --limit 20   # B1: Opus's logged match decisions, replayed through Jev; `score` compares
uv run im audit-tension draft saved/tension-2 --model jev-1.14.0   # Jev drafts a probe sheet (via OpenRouter); --probes asks more
                                  # (OpenRouter models need OPENROUTER_API_KEY)
uv run im import-chatgpt saved/chatgpt-idea-machine.md saved/chatgpt-curate-sheet.md   # what the ChatGPT Idea Machine's kept messages would send, a session at a time; --write reads them with extraction v5.3 into archive captures, once
uv run im lattice                 # lattice counts: ideas, evidence, connections
uv run im feedback --reasons      # the cards I dismissed, each counted once by the latest reason I gave (unknown: none given)
uv run im connect --limit 0      # the connection judge: how many pairs are nominated and due, sending nothing
uv run im connect --limit 100    # Jev screens up to 100 due pairs, Terra judges those that pass; without --limit, every due pair
uv run im merges --limit 0       # the merge screen: likely duplicate clusters, likeliest first by what Jev already answered, sending nothing
uv run im merges --top 50        # Jev asks same_v2 about every pair not answered for its current text, then the top 50
uv run im merge KEEP OTHER        # merge cluster OTHER into KEEP (full ids): its captures, observations and connections move; KEEP's title and statement stay
uv run im merge --distinct A B    # mark two clusters as distinct points: im merges stops listing them
uv run im idea show ID           # a card (full id or unique prefix): its text, whether I corrected it, its quotes, and the text of the clusters merged into it
uv run im idea edit ID --statement "..."   # correct a card's --title, --statement or --note; '' reverts to Claude's
uv run im themes                  # themes; im themes pin|unpin|reject <name>, im themes rename <name> <new name>
uv run im load-fixtures --scenario edited   # the same stream after every kind of correction and a forget
uv run im run
uv run im check                   # invariant queries
uv run im show <episode-prefix>
uv run im reset --stage segment   # drop episodes; the next run rebuilds them
make check                        # the whole suite; fails without the IM_* env vars
uv run pytest                     # a quick run: without them the database tests skip instead
```

Settings are env vars:

- `IM_DATABASE_URL`: the pipeline's database. Role `im_pipeline` owns `im.*` and nothing else.
- `IM_STREAM_DIR`: Hearsay's utterance stream. Idea Machine only reads it. `im load-fixtures` writes only to
  directories it created itself.
- `ANTHROPIC_API_KEY` (or an `ant auth login` profile): for idea extraction. Without it `im run` skips extraction
  and says so. What's sent, and how, is in [decisions/0004-claude-extraction.md](decisions/0004-claude-extraction.md).
- `IM_WEB_DATABASE_URL` and `IM_WEB_TOKEN`: for `im web`, the neutral view (every cluster past the gates, newest
  first, no score; [decision 0007](decisions/0007-neutral-proposals.md)). It reads `pub` and appends feedback as
  role `lattice_app`, logs in with its token, and listens on 127.0.0.1:8792. A link toggles demo mode, where
  nothing sensitive shows and nothing can be kept or dismissed; `im web --demo` serves demo mode only.
- `IM_DEV_ADMIN_URL`: the local dev superuser, used only by tests to create and drop `im_test`. Code refuses it
  unless every host it (or `PGHOST`) names is localhost, a loopback address or a socket directory. Each test gets a
  fresh `im_test` database and stream directory, and none of the other settings here.

`IM_CONFIG` can name a TOML file that overrides the defaults in `src/im/config.py`. An unknown key in a section
stops the run, except a retired one, which is ignored with a warning until it's deleted: `[triage]` (with routing,
decision 0014), `[extract] interests` (with prompts `v1` to `v4`, decision 0012) and `[egress] names`,
`public_names` and `my_names` (with policy C, decision 0008). Every segmentation value goes into the episodes'
`stage_version`, so changing one re-derives episodes on the next run.

Every stage that asks a chat model takes its model and effort from the file:

| Section | Stage | `model` | `effort` |
|---|---|---|---|
| `[extract]` | extraction (also `prompt`, default and only live one `v5.3`; `monthly_cap_usd`, `max_episodes_per_run`) | `claude-opus-5-5` | `medium` |
| `[match]` | the lattice's matcher | `claude-opus-5-5` | `low` |
| `[themes]` | describing, filing and proposing themes | `claude-opus-5-5` | `low` |
| `[digest]` | the weekly digest | `claude-opus-5-5` | `low` |
| `[connect]` | the connection judge (each `im run`, and `im connect`; also `max_pairs_per_run`) | `openai/gpt-6.1-terra` | `low` |

Both go into the stage's version. A version change never sends again by itself what was already done: the re-read
gate (below) holds it. Try a new extraction prompt first with `im trial`. The matcher and the digest use a new one
from then on; what they've already done stays.

The sensitivity tags have no section: Jev (`jev.MODEL`) answers one yes or no per tag about every idea and theme,
and asks again when its text changes; a change of the questions or the cut (`sensitivity.CUT`) is held by the gate.

`im run` and `im connect` judge pairs of clusters (SPEC §5). The judge nominates every two cards at cosine ≥ 0.65
where one is among the other's 10 nearest, leaving out pairs I linked or unlinked. Jev screens each, and Terra
(`[connect]`) judges the pairs Jev rates at 0.39 or above with B4's prompt. Every verdict is kept in
`im.connection_verdicts`, so a pair is asked again only when its text as shown changes, never-asked pairs first; a
pair judged under another version of the judge (its prompt, model, effort, or the screen's question or cut) is held
by the re-read gate. A connection replaces the pair's embedding stand-in; `none`, `unsure`, a refusal or a screened
pair removes it, and the stand-ins rebuilt each run leave it out. It spends under the same monthly cap as the rest
of `im run` (`extract.monthly_cap_usd`) and reports what it found and what Jev and Terra cost. `im run` asks about at
most `[connect] max_pairs_per_run` pairs (default 500, about $0.60 when that many are due; 0 only counts them), so a
backlog is worked through over runs; `im connect --limit N` asks about N now.

`im merge KEEP OTHER` merges two clusters that make one point, sending nothing. OTHER's captures, typed observations
and match decisions move to KEEP, its connections and lineage are re-pointed (KEEP's own stand, one between the two
goes, and the embedding stand-ins are rebuilt next run), and its connection verdicts are dropped, so the judge asks
about KEEP's new neighbors on its own. My links on OTHER count for KEEP, and how I kept OTHER carries over if KEEP
wasn't kept. KEEP's title, statement and note stay; OTHER's, as shown, are printed and kept in `im.idea_merges`, which
records what moved. OTHER is no card and no matcher candidate from then on, and a `merge_review` row whose clusters
are now one is resolved. It refuses the same id twice, an unknown id, and a cluster already merged away.

`im idea edit ID` corrects a card, as Lattice did: one `idea_correct` event with only the fields given (`--title`,
`--statement`) and an `idea_note` for `--note`, source `cli`, printing the card before and after. An empty value
reverts to Claude's original (no note, for the note). A title takes at most 120 characters, a statement and a note
1000. It takes a full id or a unique prefix, and refuses an unknown id, a cluster merged away (naming the one it's
part of now), and an edit with no field. Every stage that reads the card's text sees the change as changed input on
the next run, not a version change, so the re-read gate doesn't hold it. After a merge, `im idea show KEEP` prints
the title and statement each merged cluster had, to fold its detail into KEEP's with `im idea edit`.

`im merges` is the merge screen (B9m). Its pairs are the open `merge_review` rows (`duplicate`, `judged_same`) and
the pairs the connection judge nominates (one definition of near), both still cards, leaving out pairs I unlinked
or marked distinct (`im merge --distinct`). Jev (`jev.MODEL`) answers the matcher's `same_v2` ("is `a` another way of
saying `b`?") on each cluster's title and statement, and the list is ordered by it, likeliest first; pairs not asked
yet come last. Answers are kept in `im.jev_answers` under a hash of both texts, so a pair is asked again only when
either changes, and one answered under another question set is held by the re-read gate, listed by its old answer.
`--limit N` asks about at most N pairs (review rows first, then the nearest), `--limit 0` none, needing no key, and
it spends under the monthly cap (`extract.monthly_cap_usd`). It writes nothing but Jev's answers and never merges:
`im merge KEEP OTHER` does, by hand.

Each section also takes `max_tokens`, a request's output budget, reasoning included (OpenRouter counts it there).
It isn't part of any version, so raising it changes nothing already done: a request that ran out is simply asked
again. Unset, extraction and the stages get 32,000 (`llm.MAX_TOKENS`) and the matcher and the connection judge
4,000. `im trial --max-tokens` overrides `[extract]`'s for one trial.

Set `CUDA_VISIBLE_DEVICES=""` on a box with a GPU to measure what the NAS's CPU will do.

## Real data on eeyore

Until Idea Machine runs on the NAS, `dev/pull-stream.sh` copies Hearsay's stream from the NAS (it only reads
there) into `~/.local/share/ideamachine/stream`. Point `IM_STREAM_DIR` there and `IM_DATABASE_URL` at a
database of its own (`im`), separate from the fixture one (`im_dev`). Then `dev/pull-stream.sh && uv run im run`.

## On the NAS

Production runs on TrueNAS: see [deploy.md](deploy.md). eeyore is for development.

## Running on its own (eeyore, before the move)

`dev/systemd/install.sh` links and enables two user units:

- `ideamachine-db.service` keeps the Postgres container running. Its port forward on 127.0.0.1:55432 then lives in
  that service and not in whichever shell started the container, and it comes back after a reboot (lingering is
  on for this user).
- `ideamachine-run.timer` runs `dev/pull-stream.sh && im run` hourly at :45, after Hearsay's transcription (:00)
  and reprocess (:30). Logs: `journalctl --user -u ideamachine-run`.

Then the only manual step is `im ideas --review`.

## How `im run` works

One REPEATABLE READ transaction:

1. **Import** ([ROADMAP §3.3](ROADMAP.md)). Re-read conversations whose `revision` changed in `index.json`, checking each file
   against its revision (a mismatch waits for the next run). Store new utterance versions in `im.source_segments`,
   keyed by content (conversation, start, end and text; any other field is an annotation), so unchanged speech
   keeps its ID when Hearsay renumbers or adds a field. Link replaced versions: by
   `utterance_id` when the transcript is the same, by time overlap otherwise. Then purge everything that overlaps
   a `forgotten.json` span.
2. **Purge.** Delete everything made from a forgotten segment: every episode (current or retired) that contains
   one; every idea, digest, verdict and observation made from one; and the feedback events on captures of it, even
   once the capture is gone. A note to self whose line is forgotten leaves the attention queue, and every other item
   there loses the forgotten lines it marked.
3. **Segment.** Re-segment conversations whose membership changed, or whose episodes are stale or missing.
   Episodes are content-addressed (`uuid5(stage_version, input_hash)`), so unchanged episodes aren't touched,
   ones that no longer come out are retired (`current = false`), and new ones are inserted.

Then, outside that transaction:

- **Attention queue** (`src/im/attention.py`, code only, before anything is sent). Each pendant tap Hearsay lists
  becomes an item marking my lines from 30 s before it to 30 s after, and each line of mine where I say "note to
  self" (`attention.NOTE_TO_SELF`) one marking that line and my next ones, up to three within two minutes. Items
  already there are re-anchored to the current lines: a tap's item is keyed by its time, so it's the same item
  after a conversation is split or merged; a note's by its line, so when a re-transcription replaces that line, the
  new one takes the item over if it descends from the old one and still says "note to self" (else the item stays,
  as first heard). Re-segmenting touches no item, and a second run changes nothing. An item changes nothing about
  the speech: extraction, captures and cards go on as if it weren't there. `im queue remove` marks one removed,
  for good: a later run never brings it back. There's no `im reset --stage` for it, since the removals are mine and
  the anchors are re-derived every run.
- **Extraction** ([decisions/0004](decisions/0004-claude-extraction.md)). Claude reads each current episode where I
  speak whose payload no read has had: new speech, and speech whose payload changed (re-transcribed, a tap added),
  newest first. A payload already read on another episode under the same version is reused, sending nothing. An
  episode read under an earlier prompt, model or policy with the same payload is held by the re-read gate (below),
  its captures staying live.
- **Quotes** (`src/im/quotes.py`). Each new capture's quote is lined up with the lines it cites and the line
  either side, and the capture then cites every line its words are on: a model's citation can stop a line short or
  point a line off. Citations only grow, and the capture's observation, its idea, my verdicts on it, the feedback
  events on it and the matcher's request that sent it grow with it, so forgetting any line it quotes reaches all of
  them. A line label copied into the quote ("[23] me: ") is dropped. `im.items.quote_in_lines` keeps the share of
  the quote's words found; under 0.6 the quote isn't from its lines. The first run after migration 044 does this
  for every existing capture.
- **Match into the lattice.** Each new capture is shown the nearest clusters on its side (relayed or my own; local
  embeddings), and the matcher says of each whether the capture makes the same point. It joins the nearest that
  does, else starts a new cluster. Two or more matches are logged in `im.merge_review` as a possible duplicate, for
  my review. Related-idea edges are rebuilt, except between clusters the connection judge has a verdict on. The web
  view reads the result through schema `pub`
  ([ROADMAP §3.5](ROADMAP.md)).
- **Digest.** Claude writes the last completed week's digest (Monday to Monday) once every episode of that week
  has been read and its captures matched; until then the run leaves it, with `"waiting": true` in its stats, and a
  later run writes it.
- **Connections.** Last of the paid stages, the connection judge (above) asks about the pairs that are due, at most
  `[connect] max_pairs_per_run` a run, and its connections replace their stand-ins; its stats are under `connect`. A
  run with nothing due sends nothing.
- **Observations and weight** ([M2 design](design/m2-observations-candidates-ideas.md)). Each new capture joins
  the observation it re-captures, or starts one. Each cluster is weighed (`[weight]` in `im.toml`), and up to 10
  candidates a week, the most recently said first, are proposed to me to keep as canonical or pinned. `im tune knob=value ...` previews a change to
  those settings, writing nothing, and prints the `[weight]` block to paste.

Every stage that asks a model (extraction, matching, themes, sensitivity, the digest, the connection judge) stops,
saying why in its stats, when the month's spend reaches `extract.monthly_cap_usd`, when the API can't answer now, or
when no request would get through (the credentials or the account's credit rejected, a model id that doesn't exist);
the stages after it still run. A request whose reply fails (an error response, no answer, one cut off or not in the
schema) is logged in `im.egress_log` with what it cost, counted in the stage's
`failed`, and asked again on the next run; the stage goes on to its next item. A refused filing, scoring or
tagging batch is skipped the same way and asked again, rather than recorded or stopping the batches after it; a
refused extraction is recorded as read, with nothing captured, and a refused match makes a new idea.

### The re-read gate

Re-sending work already done because a version changed is never automatic (`im.reread`). A version is what a
stage's answer depends on besides its input: a prompt's wording or schema, the model and effort, the privacy policy,
a Jev question set, a cut, a method. When one changes, each stage that would send old work again splits what's due:

- **New work** (never done under any version) and **work whose own input changed** (an episode whose payload
  changed, say re-transcribed or tapped; a card whose text changed; an idea checked against another set of themes)
  go ahead under the current version, as before.
- **Version re-sends** (done before under another version, input unchanged) are **held**. Their old results stay
  live: an episode read under an earlier prompt keeps its captures, and everything downstream reads captures of any
  version side by side; a held pair keeps its verdict, an idea its tags or its "fits no theme", a merge pair its
  answer. An episode re-derived with the same payload (a segmentation change) takes the earlier read's captures.

The gated stages are extraction (`extract`), the themes filer (`themes`), sensitivity (`sensitivity`), the
connection judge (`connect`) and the merge screen (`merges`). The matcher never asks about a capture twice whatever
its version, and the digest writes each week once, so neither has anything to hold.

`im run`'s stats and `im pending` show, under `held` for each stage with held work, how many, from which version(s)
to which, `estimated_usd` and how it was estimated, and the command that approves it. The estimate is the logged
cost (`im.egress_log`) of those items' last requests, each counted once; when the model changed and both prices are
in `llm.PRICES` it's scaled by the price ratio, and otherwise it says it's the old model's cost. `im pending` sends
nothing.

`im reread STAGE` is the approval, and nothing else is: there's no stored approval. It sends the stage's held work
now, through the stage's own code, under the monthly cap (`extract.monthly_cap_usd`); `--limit N` sends at most N
(a sample, say) and leaves the rest held, and `--limit 0` only prints how many are held and the estimate. Captures
read again are placed, observed and matched by the next `im run`; a re-capture joins its observation's cluster
unasked, so only what the new prompt captures that the old one didn't goes to the matcher.
