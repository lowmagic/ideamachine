# Synthesis: design and blind trial

_Plan, 2026-11-27. The stage behind [ROADMAP-v2.md](../ROADMAP-v2.md)'s Synthesis row and
[decision 0010](../decisions/0010-connections-kinds-lenses.md): a connected neighborhood of clusters can produce one
higher-level idea that cites the underlying observations and links to its source clusters via `im.lineage`
(`kind = 'synthesizes'`). Arrives as a proposal; only I keep an idea._

## 1. Eval before trust: blind trial first

Before wiring Synthesis into `im run` or adding any migration, we run a blind A/B trial (`im trial-synthesis`,
`src/im/synth_trial.py`) on real connected neighborhoods from the live lattice:

1. **What qualifies as a neighborhood (`sheet`):**
   - Graph neighborhoods computed by `lens.neighbourhoods` over `pub.card_links` (label propagation weighted by shared
     neighbors, identical to `/threads` — so a broad hub connected loosely to many topics does not fuse unrelated
     groups).
   - At least **3 cards** spanning at least **2 distinct conversations** (`MIN_CARDS = 3`, `MIN_CONVERSATIONS = 2`).
   - Excludes cards that are already syntheses (`im.lineage` `kind = 'synthesizes'`) and dismissed cards.
   - If a neighborhood has more than **12 cards** (`MAX_CARDS = 12`), the model is shown the 12 most central cards
     (highest internal degree within the neighborhood, tie-broken by earliest said).
   - Each neighborhood's **baseline** ("best existing card") is its most central card (highest internal degree,
     tie-broken by conversations then earliest said).

2. **Model drafts (`draft --model M [--effort medium]`):**
   - Sends each sampled neighborhood (card titles, statements, up to 2 `"me"` quotes per card, and internal
     connections with any suggested kinds from `im.connection_kinds`) to model `M` using
     `src/im/prompts/synthesize_v1.txt`.
   - Every request goes through `egress.ask` under the monthly cap and current privacy policy, collecting
     `segment_ids` from the neighborhood's source items. Resumable: re-running skips neighborhoods already drafted.
   - Writes nothing to `im.ideas` or `im.lineage`.
   - Candidate models to compare first: `openai/gpt-6.1-terra` (`medium`) and `thinkingmachines/inkling-2-small`
     (`medium`).

3. **Prompt contract (`synthesize_v1.txt`):**
   - Returns structured JSON:
     - `synthesizable` (`bool`): `false` when the cards are merely topical, redundant, or better left as separate
       points.
     - `title` (`string`): concise direct title (≤ 8 words, no prefix labels).
     - `statement` (`string`): 1–3 direct sentences in first person ("I", "my") capturing the overarching principle,
       pattern, or tension across the neighborhood, grounded strictly in what was actually said (no invented claims,
       no meta-commentary like *"These ideas explore..."*).
     - `source_ids` (`int[]`): 1-based card numbers from the neighborhood that the synthesis draws on (at least 2
       when `synthesizable` is `true`).
     - `reason` (`string`): one-line rationale for the synthesis (or why `synthesizable` is `false`).

4. **Blind review & scoring (`review`, `score`):**
   - `review` writes `review.md` and `review-key.json` (never overwriting an existing `review.md`).
   - For each neighborhood where at least one model proposed a synthesis, the options — each model's synthesis plus
     the neighborhood's **best existing card** baseline — are shuffled deterministically and shown as unlabeled
     `Option A`, `Option B`, `Option C` (plus `none` if nothing deserves to be proposed).
   - `score` tallies the blind picks: win rate per model, how often a synthesis beat the best existing card, decline
     rate (`synthesizable: false`), and cost per neighborhood.

## 2. Pass bar for building the live stage

- At least one candidate model's synthesis is preferred over the best existing card (and `none`) on **≥ 50%** of
  marked neighborhoods where it proposes a synthesis, with no fabricated claims.
- If neither model beats the best existing card, Synthesis is not wired into `im run` (the existing cards and
  `/threads` already do the job).

## 3. Phase 2: live stage (only after the trial passes)

- New versioned stage `im.synthesize` behind the re-read gate (`im.reread`), capped per run and by `monthly_cap_usd`.
- Keyed by the neighborhood's sorted member card IDs + text hash so a neighborhood is re-synthesized only when its
  membership or member text changes (and never re-proposed if already dismissed).
- Writes a proposed `im.ideas` row (`origin = 'captured'`, `maturity = 'candidate'`), `im.lineage` rows
  (`from_id = synthesis`, `to_id = source`, `kind = 'synthesizes'`, `cited_observation_ids` copied from the cited
  source cards' origin observations so `checks.UNCITED` holds), and displays synthesized proposals in `/threads` and
  the feed with a clear *"synthesizes N cards"* link list.
