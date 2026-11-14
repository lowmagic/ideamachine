"""The `im` command."""

import argparse
import json
from pathlib import Path

from im import config, db, fixtures, migrate, pipeline, reread
from im.checks import run_checks
from im.show import show


def _print(obj) -> None:
    print(json.dumps(obj, indent=2, default=str))


def cmd_migrate(args) -> None:
    with db.connect(db.pipeline_dsn()) as conn:
        _print({"applied": migrate.apply(conn, migrate.IM)})


def cmd_load_fixtures(args) -> None:
    _print(fixtures.write(Path(args.dir) if args.dir else db.stream_dir(), args.scenario))


def cmd_run(args) -> None:
    """Every stage, recorded on one im.runs row, including a failure."""
    cfg = config.load()
    with db.connect(db.pipeline_dsn()) as conn:
        try:
            stats = _run_stages(conn, cfg)
        except Exception as e:
            partial = dict(getattr(e, "stats", {}))
            _record_run(conn, partial.pop("run_id", None), partial | {"error": f"{type(e).__name__}: {e}"[:500]})
            raise
        _record_run(conn, stats.pop("run_id"), stats)
        _print(stats)


def _record_run(conn, run_id, stats: dict) -> None:
    import json

    if run_id is None:   # the first stage failed, and its transaction (with the row) rolled back
        conn.execute("INSERT INTO im.runs (command, finished_at, stats) VALUES ('run', now(), %s)", (json.dumps(stats),))
    else:
        conn.execute("UPDATE im.runs SET finished_at = now(), stats = %s WHERE run_id = %s", (json.dumps(stats), run_id))


def _run_stages(conn, cfg) -> dict:
    stats = pipeline.run(conn, cfg, db.stream_dir()) | {"monthly_cap_usd": cfg.extract.monthly_cap_usd}
    try:
        from im import attention, feedback

        # Code only, before anything that can stop the run: a tap or a note to self is queued whatever Claude does.
        stats["attention"] = attention.stage(conn)

        # Before extraction and matching, so captures I discarded stay out of the lattice.
        stats["feedback"] = feedback.apply_item_feedback(conn)
        if cfg.extract.enabled:
            stats["extract"] = _extract(conn, cfg)
        from im import quotes

        # After extraction, before anything reads a new capture's segments (verdicts, observations, matching).
        stats["quotes"] = quotes.place_stage(conn)
        # After extraction (the new items exist) and before matching (a carried discard counts).
        stats["verdicts_carried"] = feedback.carry_verdicts(conn)
        from im import observations

        stats["observations"] = observations.assign_stage(conn)
        if cfg.lattice.enabled:
            stats["lattice"] = _lattice(conn, cfg)
            stats["themes"] = _themes(conn, cfg)
            stats["sensitivity"] = _sensitivity(conn, cfg)
            stats["digest"] = _digest(conn, cfg)
            # After matching, whose relate rebuilds the stand-ins the judge replaces; last of the paid stages, so a
            # backlog of pairs spends what the month's cap leaves.
            stats["connect"] = _connect(conn, cfg)
        from im import weight

        stats["weight"] = weight.stage(conn, cfg.weight)   # after matching: weight reads the clusters
    except Exception as e:   # keep what's known so far, including run_id, for the record
        e.stats = stats
        raise
    return stats


def _extract(conn, cfg) -> dict:
    """Run extraction, or say why it couldn't run (no credentials, API down); the run goes on either way. A stage
    stops itself, saying why, at the cap or when Claude can't answer or rejects the credentials (llm.Stop)."""
    import anthropic

    from im import extract, llm

    x = cfg.extract
    if x.prompt not in extract.LIVE:
        return {"stopped": f"extract.prompt {x.prompt} isn't live; `im run` reads with {', '.join(extract.LIVE)}"}
    try:
        client = llm.client_for(x.model, cfg.egress.zdr)
        return extract.run_stage(conn, client, x.model, x.effort, x.monthly_cap_usd, x.max_episodes_per_run,
                                 extract.PROMPTS[x.prompt], x.max_tokens)
    except (anthropic.AnthropicError, SystemExit) as e:  # e.g. no credentials configured at all
        return {"stopped": f"Claude unavailable: {e}"}


def _lattice(conn, cfg) -> dict:
    """Match new captures into the lattice, or say why it couldn't run; the run goes on either way."""
    import anthropic

    from im import lattice, llm
    from im.lattice import SentenceEmbedder

    try:
        return lattice.match_stage(conn, llm.client_for(cfg.match.model, cfg.egress.zdr),
                                   SentenceEmbedder(cfg.lattice.embedding_model), cfg.extract.monthly_cap_usd,
                                   cfg.lattice.max_items_per_run, cfg.match)
    except (anthropic.AnthropicError, SystemExit) as e:
        return {"stopped": f"Claude unavailable: {e}"}


def _themes(conn, cfg) -> dict:
    """Apply my theme feedback, file unthemed ideas, propose themes for the leftovers."""
    import anthropic

    from im import llm, themes

    out = {"feedback": themes.apply_feedback(conn)}
    try:
        client = llm.client_for(cfg.themes.model, cfg.egress.zdr)
        # Described first: the filer reads the descriptions.
        out["describe"] = themes.describe_stage(conn, client, cfg.extract.monthly_cap_usd, cfg.themes)
        out["file"] = themes.file_stage(conn, client, cfg.extract.monthly_cap_usd, cfg.themes)
        out["propose"] = themes.propose_stage(conn, client, cfg.extract.monthly_cap_usd, cfg.themes)
    except (anthropic.AnthropicError, SystemExit) as e:
        out["stopped"] = f"Claude unavailable: {e}"
    return out


def _sensitivity(conn, cfg) -> dict:
    """Tag ideas and themes for demo mode, or say why it couldn't; the run goes on either way."""
    from im import jev, sensitivity

    try:
        client = jev.Jev()
    except SystemExit as e:   # no key for Jev
        return {"stopped": str(e)}
    return sensitivity.tag_stage(conn, client, cfg.extract.monthly_cap_usd)


def _connect(conn, cfg) -> dict:
    """Judge the pairs due, at most [connect] max_pairs_per_run, or say why it couldn't; the run goes on either way."""
    from im import connect

    c = cfg.connect
    try:
        judge, screen = _judges(cfg) if c.max_pairs_per_run != 0 else (None, None)
    except SystemExit as e:
        return {"stopped": str(e)}
    return connect.stage(conn, judge, screen, cfg.extract.monthly_cap_usd, c.max_pairs_per_run, c)


def _judges(cfg):
    """The connection judge's clients, Terra's ([connect]) and Jev's, or SystemExit saying which key is missing."""
    import anthropic

    from im import jev, llm

    try:
        return llm.client_for(cfg.connect.model, cfg.egress.zdr), jev.Jev()
    except (anthropic.AnthropicError, SystemExit) as e:   # no key for OpenRouter, Jev or Claude
        raise SystemExit(f"the connection judge can't run: {e}") from None


def _digest(conn, cfg, week=None, again=False) -> dict:
    """Write last week's digest if it isn't written yet, or say why it couldn't; the run goes on either way."""
    import anthropic

    from im import digest, llm

    try:
        client = llm.client_for(cfg.digest.model, cfg.egress.zdr)
        return digest.digest_stage(conn, client, cfg.extract.monthly_cap_usd, week, again, cfg.digest)
    except (anthropic.AnthropicError, SystemExit) as e:
        return {"stopped": f"Claude unavailable: {e}"}


def cmd_digest(args) -> None:
    """Print a weekly digest (the latest by default); --write writes it first, --again rewrites it."""
    from datetime import date, timedelta

    from im import digest

    week = date.fromisoformat(args.week) if args.week else None
    if week:
        week -= timedelta(days=week.weekday())   # its Monday
    with db.connect(db.pipeline_dsn()) as conn:
        if args.write or args.again:
            week = week or digest.last_week()
            print(json.dumps(_digest(conn, config.load(), week, args.again), default=str))
        print(digest.show(conn, week))


def cmd_themes(args) -> None:
    from im import themes

    with db.connect(db.pipeline_dsn()) as conn:
        if args.action != "list":
            try:
                theme_id = themes.find(conn, args.theme)
            except ValueError as e:
                raise SystemExit(str(e)) from None
            kind = {"pin": "pin_theme", "unpin": "unpin_theme", "rename": "rename_theme", "reject": "reject_theme"}
            if args.action == "rename" and not args.name:
                raise SystemExit("rename needs the new name")
            themes.record(conn, kind[args.action], theme_id, {"name": args.name} if args.action == "rename" else None)
            print(themes.apply_feedback(conn))
        print(themes.listing(conn))


def cmd_pending(args) -> None:
    """What the next run would send, without sending anything, and what the re-read gate holds, per stage. After
    moving the database to another machine, `already_extracted_would_resend` must be 0: anything else means the
    payloads changed (e.g. the time zone), and the run would re-send episodes and orphan my verdicts."""
    from im import extract, lattice, themes

    cfg = config.load()
    with db.connect(db.pipeline_dsn()) as conn:
        prompt = args.prompt or cfg.extract.prompt
        pv = extract.prompt_version(cfg.extract.model, cfg.extract.effort, extract.PROMPTS[prompt])
        todo, held = extract._pending(conn, cfg.extract.model, pv)
        ids = [p.episode_id for p in todo]
        # Those read before under any version: their payload changed. What each one's latest read cost, a read
        # shared by reuse counted once.
        (resend,) = conn.execute("SELECT count(DISTINCT episode_id) FROM im.extractions WHERE episode_id = ANY(%s)",
                                 (ids,)).fetchone()
        (last_read,) = conn.execute(
            """SELECT sum(g.cost_usd) FROM im.egress_log g WHERE g.egress_id IN (
                 SELECT DISTINCT ON (episode_id) egress_id FROM im.extractions
                 WHERE episode_id = ANY(%s) AND model = %s ORDER BY episode_id, created_at DESC)""",
            (ids, cfg.extract.model)).fetchone()
        out = {"prompt_version": pv, "episodes_to_extract": len(todo), "already_extracted_would_resend": resend,
               "their_last_reads_cost_usd": round(float(last_read or 0), 2),
               "items_to_match": len(lattice._pending_items(conn)),
               "ideas_to_file": len(themes._unthemed(conn, cfg.themes)[0])}
        reports = {"extract": extract.held_report(conn, held, cfg.extract.model, pv)} | {
            stage: _held(conn, cfg, stage) for stage in ("themes", "sensitivity", "connect", "merges")}
        out["held"] = {stage: r for stage, r in reports.items() if r}
    _print(out)
    if args.expect_none_resent and out["already_extracted_would_resend"]:
        raise SystemExit(1)


def _held(conn, cfg, stage: str) -> dict | None:
    """What the re-read gate holds in `stage` (reread.report, or None), sending and writing nothing."""
    from im import connect, extract, merge, sensitivity, themes

    if stage == "extract":
        x = cfg.extract
        pv = extract.prompt_version(x.model, x.effort, extract.PROMPTS[x.prompt])
        return extract.held_report(conn, extract._pending(conn, x.model, pv)[1], x.model, pv)
    if stage == "themes":
        return themes.held(conn, cfg.themes)
    if stage == "sensitivity":
        return sensitivity.held(conn)
    if stage == "connect":
        return connect.held(conn, cfg.connect)
    return merge.screen(conn, None, 0)[1].get("held")


def cmd_reread(args) -> None:
    """The re-read gate's approval: send a stage's held work now, through the stage, under the monthly cap; at most
    --limit items (0: only what's held and the estimate, sending nothing)."""
    cfg = config.load()
    with db.connect(db.pipeline_dsn()) as conn:
        if args.limit == 0:
            _print(_held(conn, cfg, args.stage) or {"held": 0})
        else:
            _print(_reread(conn, cfg, args.stage, args.limit))


def _reread(conn, cfg, stage: str, limit: int | None) -> dict:
    """Run `stage` on its held work only, at most `limit` items (None: all)."""
    from im import connect, extract, jev, llm, merge, sensitivity, themes

    cap = cfg.extract.monthly_cap_usd
    if stage == "extract":
        x = cfg.extract
        if x.prompt not in extract.LIVE:
            raise SystemExit(f"extract.prompt {x.prompt} isn't live; `im run` reads with {', '.join(extract.LIVE)}")
        return extract.run_stage(conn, llm.client_for(x.model, cfg.egress.zdr), x.model, x.effort, cap, limit,
                                 extract.PROMPTS[x.prompt], x.max_tokens, held_only=True)
    if stage == "themes":
        return themes.file_stage(conn, llm.client_for(cfg.themes.model, cfg.egress.zdr), cap, cfg.themes,
                                 held_only=True, limit=limit)
    if stage == "sensitivity":
        return sensitivity.tag_stage(conn, jev.Jev(), cap, limit, held_only=True)
    if stage == "connect":
        return connect.stage(conn, *_judges(cfg), cap, limit, cfg.connect, held_only=True)
    return merge.screen(conn, jev.Jev(), limit, cap, held_only=True)[1]


def cmd_trial(args) -> None:
    """Try a candidate extraction model or prompt on a fixed sample of episodes, and compare it with a reference."""
    from im import extract, jev, llm, trial

    cfg = config.load()
    x = cfg.extract
    system, schema = extract.PROMPTS[args.prompt], extract.SCHEMA_V5
    model, effort = args.model or x.model, args.effort or x.effort
    ref_model, _, ref_effort = (args.against or f"{x.model}:{x.effort}").partition(":")
    ref_effort = ref_effort or x.effort
    cap = x.monthly_cap_usd if args.monthly_cap_usd is None else args.monthly_cap_usd
    with db.connect(db.pipeline_dsn()) as conn:
        out = {}
        exclude = set()
        if args.exclude:
            exclude, sampleable = trial.held_out(conn, args.exclude)
            out["held_out"] = {"audit_episodes": len(exclude), "excluded_from_the_pool": sampleable}
        candidate = (trial.version(model, effort, system, args.replicate, schema), model)
        reference = (trial.version(ref_model, ref_effort, system, schema=schema), ref_model)
        if not args.report:
            client = llm.client_for(model, cfg.egress.zdr)
            out["trial"] = trial.run(conn, client, system, model, effort, cap, args.limit, args.sample,
                                     args.replicate, schema, args.max_tokens or x.max_tokens, args.seed, exclude)
        if candidate == reference:   # a new prompt on the configured model: nothing to compare with in the trials
            out["report"] = ("the reference is this read itself; compare it with the live read: "
                             f"im audit-misses sheet FOLDER {args.prompt} --model {model} --live")
            _print(out)
            return
        same = None
        if args.same_point:
            judge = jev.Jev()
            out["same_point"] = trial.same_point(conn, judge, candidate, reference, cap)
            same = jev.kept(conn, jev.question_set(judge, "match"))
        out["report"] = trial.report(conn, candidate, reference, same)
    _print(out)


def cmd_mishearings(args) -> None:
    """The transcription's mishearings: list them, or confirm, add or reject a pair."""
    from im import mishearings

    with db.connect(db.pipeline_dsn()) as conn:
        if args.action == "list":
            for heard, meant, status, seen in mishearings.pairs(conn):
                print(f"{status:<12} {seen:>4}  {heard} -> {meant}")
            return
        if not (args.heard and args.meant):
            raise SystemExit(f"{args.action} needs what was heard and what was meant: im mishearings {args.action} "
                             "HEARD MEANT")
        mishearings.decide(conn, args.heard, args.meant, "rejected" if args.action == "reject" else "confirmed")
        print(f"{args.heard} -> {args.meant}: {'rejected' if args.action == 'reject' else 'confirmed'}")


def cmd_tune(args) -> None:
    """Preview what changing the weight knobs would do. Writes nothing; paste the printed [weight] into im.toml."""
    from im import weight

    with db.connect(db.pipeline_dsn()) as conn:
        _print(weight.tune(conn, config.load().weight, weight.parse_overrides(args.knobs), args.top))


def cmd_audit_edges(args) -> None:
    """B4: a blind sheet of pairs of clusters for me to type, then my marks tallied."""
    from pathlib import Path

    from im import edge_audit

    folders = [Path(f) for f in args.folder]
    if args.action == "score":
        _print(edge_audit.score(folders[0]) if len(folders) == 1 else edge_audit.pooled(folders))
        return
    if args.action == "review":
        _print(edge_audit.review(folders, args.all))
        return
    if len(folders) > 1:
        raise SystemExit("only score and review take several folders")
    args.folder = folders[0]
    with db.connect(db.pipeline_dsn()) as conn:
        if args.action == "draft":
            from im import jev, llm

            cfg = config.load()
            client = jev.Jev(model=args.model) if args.model.startswith("jev") \
                else llm.client_for(args.model, cfg.egress.zdr)
            _print(edge_audit.draft(conn, client, Path(args.folder), args.model, args.effort,
                                    cfg.extract.monthly_cap_usd))
            return
        _print(edge_audit.write(conn, Path(args.folder), args.seed, args.size, [Path(f) for f in args.exclude]))


def cmd_audit_eligibility(args) -> None:
    """Capture eligibility: a random sample of live captures, labeled by two models under a frozen rubric; the items
    they split on, and some they agree on, marked by me blind; then their agreement and the ineligible rate."""
    from pathlib import Path

    from im import eligibility_audit, llm

    folder = Path(args.folder)
    if args.action == "review":
        _print(eligibility_audit.review(folder))
        return
    if args.action == "score":
        _print(eligibility_audit.score(folder))
        return
    cfg = config.load()
    with db.connect(db.pipeline_dsn()) as conn:
        if args.action == "draft":
            if not args.model:
                raise SystemExit("draft needs --model, the model labeling, e.g. openai/gpt-6.1-terra")
            cap = cfg.extract.monthly_cap_usd if args.monthly_cap_usd is None else args.monthly_cap_usd
            _print(eligibility_audit.draft(conn, llm.client_for(args.model, cfg.egress.zdr), folder, args.model,
                                           args.effort, cap))
            return
        _print(eligibility_audit.write(conn, folder, eligibility_audit.live_read(cfg.extract), args.seed, args.size))


def cmd_audit_lens(args) -> None:
    """The lens pilot: Jev answers each lens's wordings (lenses.json in the folder) about every card; then what they
    let in, and the cards near each cut."""
    from pathlib import Path

    from im import jev, lens_audit

    folder = Path(args.folder)
    with db.connect(db.pipeline_dsn()) as conn:
        if args.action == "report":
            _print(lens_audit.report(conn, folder))
            return
        cap = config.load().extract.monthly_cap_usd if args.monthly_cap_usd is None else args.monthly_cap_usd
        _print(lens_audit.draft(conn, jev.Jev(), folder, args.limit, cap))



def cmd_audit_probe(args) -> None:
    """Analogy or tension: a blind sheet asking one question of pairs proposed several ways, then my marks tallied."""
    from pathlib import Path

    from im import probe_audit

    folder = Path(args.folder)
    if args.action == "score":
        _print(probe_audit.score(folder))
        return
    cfg = config.load()
    with db.connect(db.pipeline_dsn()) as conn:
        if args.action == "draft":
            from im import llm

            if args.model.startswith("jev"):
                from im import jev

                client = jev.Jev(model=args.model)
            else:
                client = llm.client_for(args.model, cfg.egress.zdr)
            _print(probe_audit.draft(conn, client, folder, args.model, args.effort, args.quotes_only,
                                     args.probes, cfg.extract.monthly_cap_usd))
            return
        if args.question == "summary":
            from im import summary_audit

            _print(summary_audit.write(conn, folder, args.seed, args.size or summary_audit.SIZE))
            return
        from im import llm
        from im.lattice import SentenceEmbedder

        embedder = SentenceEmbedder(cfg.lattice.embedding_model) if args.question == "analogy" else None
        jev_client = None
        if args.question == "analogy" and args.only in (None, "jev"):
            from im import jev

            jev_client = jev.Jev()
        client = llm.client_for(probe_audit.FACET_MODEL, cfg.egress.zdr)   # it also asks PICK_MODEL through it
        _print(probe_audit.write(conn, client, embedder, args.question, folder, args.seed, args.only,
                                 args.size, [Path(f) for f in args.exclude], jev_client,
                                 args.pool or probe_audit.JEV_POOL, cfg.extract.monthly_cap_usd))


def cmd_hard_set(args) -> None:
    """A deliberate hard set of episodes for checking an extraction prompt, each as extraction would send it."""
    from pathlib import Path

    from im import hard_set

    with db.connect(db.pipeline_dsn()) as conn:
        _print(hard_set.write(conn, Path(args.folder), args.per_case, args.seed, args.controls))


def cmd_audit_misses(args) -> None:
    """A blind sheet of the reference's solid captures, missed or found by a cheaper model in a trial, or (--both-ways)
    of the captures only one of them made and those both made, or (--live) a trial against the live read, marked
    under the eligibility rubric; my marks."""
    from pathlib import Path

    from im import eligibility_audit, extract, miss_audit, probe_audit, trial

    folder = Path(args.folder)
    if args.action == "score":
        live = json.loads((folder / "key.json").read_text()).get("benchmark") == "live"
        _print(miss_audit.score_live(folder) if live else probe_audit.score(folder))
        return
    if not args.prompt or not args.model:
        raise SystemExit("sheet needs the trial's prompt and candidate, e.g. v5.3 --model openai/gpt-6.1-terra")
    cfg = config.load()
    x = cfg.extract
    system, schema = extract.PROMPTS[args.prompt], extract.SCHEMA_V5
    effort = args.effort or x.effort
    candidate = (trial.version(args.model, effort, system, schema=schema), args.model)
    with db.connect(db.pipeline_dsn()) as conn:
        if args.live:
            if args.against or args.both_ways:
                raise SystemExit("--live compares with the live read: no --against or --both-ways")
            _print(miss_audit.write(conn, folder, candidate, eligibility_audit.live_read(x), None, args.seed,
                                    args.size, live=True))
            return
        ref_model, _, ref_effort = (args.against or f"{x.model}:{x.effort}").partition(":")
        ref_effort = ref_effort or x.effort
        _print(miss_audit.write(conn, folder, candidate,
                                (trial.version(ref_model, ref_effort, system, schema=schema), ref_model),
                                None if args.both_ways else
                                (trial.version(ref_model, ref_effort, system, 2, schema), ref_model),
                                args.seed, args.size))


def cmd_bench_match(args) -> None:
    """B1: Opus's logged match decisions replayed through Jev, then scored against Opus."""
    from pathlib import Path

    from im import match_bench

    folder = Path(args.folder)
    if args.action == "score":
        _print(match_bench.score(folder))
        return
    from im import jev

    cfg = config.load()
    cap = cfg.extract.monthly_cap_usd if args.monthly_cap_usd is None else args.monthly_cap_usd
    with db.connect(db.pipeline_dsn()) as conn:
        _print(match_bench.run(conn, jev.Jev(), folder, args.limit, cap))


def cmd_analogy_spike(args) -> None:
    """Rank each `analogy:` pair's partner by structure and by topic (sends to Claude; writes into the folder)."""
    from pathlib import Path

    from im import analogy_spike, llm
    from im.lattice import SentenceEmbedder

    cfg = config.load()
    with db.connect(db.pipeline_dsn()) as conn:
        out = analogy_spike.run(conn, llm.client_for(analogy_spike.MODEL, cfg.egress.zdr),
                                SentenceEmbedder(cfg.lattice.embedding_model),
                                Path(args.folder), args.others, monthly_cap_usd=cfg.extract.monthly_cap_usd)
    _print({k: v for k, v in out.items() if k != "pairs_detail"} | {"pairs": [
        {k: r[k] for k in ("pair", "rank_by_structure", "rank_by_topic", "note", "structure_a", "structure_b")}
        for r in out["pairs_detail"]]})


def cmd_audit_dedupe(args) -> None:
    """B5: a blind sheet of merged captures and near misses for me to mark, then the false merges in my marks."""
    from pathlib import Path

    from im import dedupe_audit

    if args.action == "score":
        _print(dedupe_audit.score(Path(args.folder)))
        return
    with db.connect(db.pipeline_dsn()) as conn:
        _print(dedupe_audit.write(conn, Path(args.folder), args.seed))


def cmd_import_chatgpt(args) -> None:
    """My messages to the ChatGPT Idea Machine, as the sheet marks them, read by extraction v5.3 a session at a time
    ([extract]'s model and effort). Shows each session as it would go out and the plan; --write sends and
    stores. A session already read is never sent again."""
    from im import archive_observations as ao

    cfg = config.load()
    p = ao.plan(Path(args.export).read_text(), Path(args.sheet).read_text(), args.tz)
    with db.connect(db.pipeline_dsn()) as conn:
        if args.write:
            import anthropic

            x = cfg.extract
            _print(ao.import_sessions(conn, anthropic.Anthropic(), p, x.model, x.effort, x.monthly_cap_usd,
                                      x.max_tokens))
            return
        read = ao.already_read(conn, p)
    for k, session in enumerate(p["sessions"], 1):
        ref = ao.source_ref(p["conversation"], session)
        print(f"## Session {k}: {ref}{' (already read)' if ref in read else ''}\n")
        print(ao.payload(session).text + "\n")   # as it goes out
    _print(ao.report(p, read))


def cmd_web(args) -> None:
    """Serve the neutral view as its own role (IM_WEB_DATABASE_URL), never as im_pipeline."""
    from im import web

    web.serve(args.host, args.port, args.demo)


def cmd_lattice(args) -> None:
    from im import lattice

    with db.connect(db.pipeline_dsn()) as conn:
        _print(lattice.status(conn))


def cmd_feedback(args) -> None:
    """--reasons: the cards I dismissed, counted by the reason I gave (feedback.dismiss_reasons)."""
    from im import feedback

    with db.connect(db.pipeline_dsn()) as conn:
        _print(feedback.dismiss_reasons(conn))


def cmd_connect(args) -> None:
    """The connection judge, on demand: Jev screens the nominated pairs not judged yet, Terra judges the rest, and
    their verdicts become connections, or remove the pair's stand-in. --limit 0 only counts. `im run` does the same,
    at most [connect] max_pairs_per_run a run."""
    from im import connect

    cfg = config.load()
    with db.connect(db.pipeline_dsn()) as conn:
        judge, screen = _judges(cfg) if args.limit != 0 else (None, None)
        _print(connect.stage(conn, judge, screen, cfg.extract.monthly_cap_usd, args.limit, cfg.connect))


def cmd_merge(args) -> None:
    """Merge cluster OTHER into KEEP (im.merge): KEEP's title and statement stay; OTHER's go into the record. With
    --distinct, only mark the two as distinct points, which the merge screen then leaves out."""
    from im import merge

    with db.connect(db.pipeline_dsn()) as conn:
        try:
            _print((merge.distinct if args.distinct else merge.merge)(conn, args.keep, args.other))
        except ValueError as e:
            raise SystemExit(f"im merge: {e}") from None


def cmd_merges(args) -> None:
    """The merge screen (im.merge.screen): asks Jev about the pairs due, at most --limit (0: none, needing no key),
    then prints the likeliest duplicates first. Writes nothing but Jev's answers; never merges."""
    from im import jev, merge

    cfg = config.load()
    with db.connect(db.pipeline_dsn()) as conn:
        rows, stats = merge.screen(conn, jev.Jev() if args.limit != 0 else None, args.limit,
                                   cfg.extract.monthly_cap_usd)
        print(merge.listing(rows, stats, args.top))


def cmd_idea(args) -> None:
    """One card: show its text, quotes and what was merged into it, or correct its title, statement or note."""
    from im import idea

    with db.connect(db.pipeline_dsn()) as conn:
        try:
            print(idea.show(conn, args.id) if args.action == "show"
                  else idea.edit(conn, args.id, args.title, args.statement, args.note))
        except ValueError as e:
            raise SystemExit(f"im idea {args.action}: {e}") from None


def cmd_ideas(args) -> None:
    from im import extract

    with db.connect(db.pipeline_dsn()) as conn:
        if args.review:
            n = _review_items(conn, extract)
            print(f"\ndecided {n}; {len(extract.review_items(conn))} still undecided")
            return
        if args.discards:
            rows = extract.discards(conn)
            for who, quote, gist, at, note, model, pv in rows:
                print(f"{at.astimezone():%Y-%m-%d %H:%M}  {who:<16}  {model} prompt {pv}")
                print(f"    \u201c{quote}\u201d")
                print(f"    {gist}")
                print(f"    why discarded: {note or '(no note)'}\n")
            print(f"{len(rows)} discarded")
            return
        rows = conn.execute(
            """SELECT w.said_by, i.quote, i.gist, e.started_at,
                      (SELECT v.verdict FROM im.item_verdicts v WHERE v.item_id = i.item_id
                       ORDER BY v.decided_at DESC LIMIT 1)
               FROM im.items i JOIN im.item_said_by w USING (item_id) JOIN im.episodes e USING (episode_id)
               WHERE e.current ORDER BY e.started_at DESC, i.item_id""").fetchall()
        shown = 0
        for who, quote, gist, at, verdict in rows:
            if verdict == "discard" and not args.all:
                continue
            mark = {"keep": "kept", "star": "\u2605", "discard": "discarded", None: "new"}[verdict]
            print(f"{at.astimezone():%Y-%m-%d %H:%M}  {who:<16}  [{mark}]")
            print(f"    \u201c{quote}\u201d")
            print(f"    {gist}\n")
            shown += 1
        print(f"{shown} shown ({len(rows)} captured on current episodes)")


def _choose(read, write, prompt: str, options: dict):
    while True:
        got = read(prompt).strip().lower()
        if got in options:
            return options[got]
        write(f"  one of: {', '.join(options)}")


def _review_items(conn, extract, read=input, write=print) -> int:
    n = 0
    for item_id, who, quote, gist, _, at, _ in extract.review_items(conn):
        write("\n" + "─" * 72)
        write(f"{at.astimezone():%A %Y-%m-%d %H:%M}  said by {who}")
        write(f"  \u201c{quote}\u201d")
        write(f"  {gist}")
        got = _choose(read, write, "[k]eep · [*] star · [d]iscard · [s]kip · [q]uit > ",
                      {"k": "keep", "*": "star", "d": "discard", "s": "skip", "q": "quit"})
        if got == "quit":
            break
        if got == "skip":
            continue
        # Only a discard asks why: that's what tuning the extraction prompt needs (im ideas --discards).
        note = (read("why? (optional; helps tune the prompt) > ").strip() or None) if got == "discard" else None
        extract.decide(conn, item_id, got, note)
        n += 1
    return n


def cmd_forgotten(args) -> None:
    from im import extract

    with db.connect(db.pipeline_dsn()) as conn:
        rows = extract.forgotten_sent(conn)
    if not rows:
        print("no forgotten segment was ever sent to Claude")
    for seg, forgotten_at, first_sent, n in rows:
        print(f"{seg}  forgotten {forgotten_at:%Y-%m-%d}, sent {n}x, first {first_sent:%Y-%m-%d %H:%M}")


def cmd_reset(args) -> None:
    with db.connect(db.pipeline_dsn()) as conn:
        _print(pipeline.reset(conn, args.stage))


def cmd_show(args) -> None:
    with db.connect(db.pipeline_dsn()) as conn:
        print(show(conn, args.episode))


def cmd_queue(args) -> None:
    """The attention queue (im.attention): what I tapped or called a note to self, newest first; or remove one."""
    from im import attention

    with db.connect(db.pipeline_dsn()) as conn:
        if args.action == "remove":
            if args.id is None:
                raise SystemExit("im queue remove takes an item's id, e.g. im queue remove 3")
            if not attention.remove(conn, args.id):
                raise SystemExit(f"im queue: no open item #{args.id}")
            print(f"removed #{args.id}")
            return
        print(attention.listing(attention.open_items(conn)))


def cmd_check(args) -> None:
    with db.connect(db.pipeline_dsn()) as conn:
        failures = {k: v for k, v in run_checks(conn).items() if v}
    _print({k: len(v) for k, v in failures.items()} or "all invariants hold")
    if failures:
        raise SystemExit(1)


def main(argv=None) -> None:
    p = argparse.ArgumentParser(prog="im", description="Idea Machine")
    sub = p.add_subparsers(required=True, metavar="command")
    sub.add_parser("migrate", help="apply im schema migrations").set_defaults(func=cmd_migrate)
    f = sub.add_parser("load-fixtures", help="write a synthetic Hearsay stream (dev only)")
    f.add_argument("--dir", help="where to write it (default: $IM_STREAM_DIR)")
    f.add_argument("--scenario", choices=fixtures.SCENARIOS, default="base",
                   help="base, or base plus every correction and a forget")
    f.set_defaults(func=cmd_load_fixtures)
    sub.add_parser("run", help="import and segment, queue taps and notes to self, then send new speech to Claude to "
                   "extract, match, file, score, tag and digest it; idempotent").set_defaults(func=cmd_run)
    r = sub.add_parser("reset", help="drop a stage's derived rows so the next run rebuilds them")
    r.add_argument("--stage", required=True, choices=pipeline.STAGES)
    r.set_defaults(func=cmd_reset)
    s = sub.add_parser("show", help="print an episode")
    s.add_argument("episode", help="episode id or unique prefix")
    s.set_defaults(func=cmd_show)
    pe = sub.add_parser("pending", help="what the next run would send, and what the re-read gate holds per stage, "
                        "without sending anything")
    pe.add_argument("--expect-none-resent", action="store_true",
                    help="exit 1 if any already-extracted episode would be sent again (use after a move)")
    pe.add_argument("--prompt", help="as if extract.prompt were this one (e.g. v5.2): what it would read and hold")
    pe.set_defaults(func=cmd_pending)
    rr = sub.add_parser("reread", help="the re-read gate: send a stage's held work, what a version change would send "
                        "again, now (sends it, under the monthly cap)")
    rr.add_argument("stage", choices=reread.STAGES)
    rr.add_argument("--limit", type=int, help="send at most this many items (0: only print how many are held and the "
                    "estimate, sending nothing)")
    rr.set_defaults(func=cmd_reread)
    dg = sub.add_parser("digest", help="the weekly digest: what the lattice gained, synthesized by Claude")
    dg.add_argument("--week", help="any date in the week (default: the latest digest, or last week with --write)")
    dg.add_argument("--write", action="store_true", help="write the week's digest if it isn't written (sends to Claude)")
    dg.add_argument("--again", action="store_true", help="rewrite the week's digest (sends to Claude)")
    dg.set_defaults(func=cmd_digest)
    tr = sub.add_parser("trial", help="try an extraction model or prompt on a sample of episodes, against a reference "
                        "(sends the episodes to the models)")
    tr.add_argument("prompt", help="a name in extract.PROMPTS, e.g. v5.3")
    tr.add_argument("--limit", type=int, help="read at most this many episodes this time")
    tr.add_argument("--report", action="store_true", help="only report on what's been read; send nothing")
    tr.add_argument("--model", help="the candidate model: claude-sonnet-5-5, or through OpenRouter, e.g. "
                    "openai/gpt-6.1-sol (default: the configured one)")
    tr.add_argument("--against", help="the reference, model[:effort] (default: the configured model and effort)")
    tr.add_argument("--sample", type=int, default=150, help="how many episodes I speak in, the same for every model")
    tr.add_argument("--seed", type=int, help="another order to draw the sample in (default: the trials' usual one)")
    tr.add_argument("--exclude", nargs="+", metavar="FOLDER", help="leave out every episode a capture on these "
                    "audit sheets came from (their key.json): a held-out sample")
    tr.add_argument("--replicate", type=int, default=1,
                    help="a further independent read by the same model (2, 3...): the reference against its own "
                         "replicate is the ceiling for every candidate's recall")
    tr.add_argument("--effort", help="the candidate's effort (default: the configured one)")
    tr.add_argument("--max-tokens", type=int, help="each request's output budget, reasoning included; not part of "
                    "the trial's version, so a failed read can be retried with more (default: [extract] max_tokens, "
                    "else 32000)")
    tr.add_argument("--monthly-cap-usd", type=float, help="send nothing once the month's spend reaches this "
                    "(default: config)")
    tr.add_argument("--same-point", action="store_true",
                    help="also count as found a capture the other model quoted from different lines, where Jev says "
                         "they make the same point (sends those pairs to Jev, even with --report; answers are kept)")
    tr.set_defaults(func=cmd_trial)
    mh = sub.add_parser("mishearings", help="the transcription's mishearings, as hints for later reads: list them, "
                        "or confirm (or add) or reject a pair")
    mh.add_argument("action", choices=["list", "confirm", "reject"])
    mh.add_argument("heard", nargs="?", help="confirm, reject: the words as transcribed")
    mh.add_argument("meant", nargs="?", help="confirm, reject: what was meant")
    mh.set_defaults(func=cmd_mishearings)
    tu = sub.add_parser("tune", help="preview weight and proposal settings, e.g. recurrence_minutes=45 (writes nothing)")
    tu.add_argument("knobs", nargs="*", metavar="knob=value")
    tu.add_argument("--top", type=int, default=10)
    tu.set_defaults(func=cmd_tune)
    ad = sub.add_parser("audit-dedupe", help="B5: a blind sheet of merged captures and near misses; score my marks")
    ad.add_argument("action", choices=["sheet", "score"])
    ad.add_argument("folder", help="where sheet.md and key.json go (it quotes people: keep it in saved/)")
    ad.add_argument("--seed", type=int, default=5, help="the sample and shuffle's seed")
    ad.set_defaults(func=cmd_audit_dedupe)
    ae = sub.add_parser("audit-edges", help="B4: a blind sheet of pairs of clusters to type; tally my marks")
    ae.add_argument("action", choices=["sheet", "score", "draft", "review"],
                    help="draft: a model marks the same sheet blind (sends to it: Claude, or OpenRouter for a "
                         "provider/model name); score then compares it with me; review: apply my answers on "
                         "review.md, then list the pairs where a chat model's draft disagrees with me, with its case")
    ae.add_argument("folder", nargs="+", help="where sheet.md and key.json go (it quotes people: keep it in saved/); "
                    "score and review take several, as one")
    ae.add_argument("--seed", type=int, default=4, help="the sample and shuffle's seed")
    ae.add_argument("--size", type=int, default=50, help="how many pairs")
    ae.add_argument("--exclude", nargs="*", default=[], help="sheet: leave out pairs on these earlier sheets")
    ae.add_argument("--all", action="store_true", help="review: list the models' marks I haven't reviewed too, to "
                    "spot-check (a blank answer leaves a mark)")
    ae.add_argument("--model", default="claude-opus-5-5",
                    help="draft: the candidate model, e.g. openai/gpt-6.1-terra, or jev-1.14.0 (TypeSafe's Jev)")
    ae.add_argument("--effort", default="low", help="draft: the candidate's effort")
    ae.set_defaults(func=cmd_audit_edges)
    ael = sub.add_parser("audit-eligibility", help="capture eligibility: a random sample of live captures, labeled "
                         "by two models under a frozen rubric; I mark where they split, and some where they agree, "
                         "blind (sheet, review and score send nothing; draft sends to the model)")
    ael.add_argument("action", choices=["sheet", "draft", "review", "score"],
                     help="sheet: draw the sample; draft: a model labels every item; review: write review.md for "
                          "my marks, or read them back; score: agreement, validation and the ineligible rate")
    ael.add_argument("folder", help="where sheet.md and key.json go (it quotes people: keep it in saved/)")
    ael.add_argument("--model", help="draft: the model labeling, not Anthropic's, e.g. openai/gpt-6.1-terra")
    ael.add_argument("--effort", default="low", help="draft: the model's effort")
    ael.add_argument("--size", type=int, default=100, help="sheet: how many captures")
    ael.add_argument("--seed", type=int, default=10, help="sheet: the sample's seed (review draws its agreed items "
                     "from it too)")
    ael.add_argument("--monthly-cap-usd", type=float, help="draft: send nothing once the month's spend reaches this "
                     "(default: config)")
    ael.set_defaults(func=cmd_audit_eligibility)
    alp = sub.add_parser("audit-lens", help="the lens pilot: Jev answers lens wordings (lenses.json in the folder, "
                         "never in the repo) about every card; report says what each lets in (draft sends to Jev)")
    alp.add_argument("action", choices=["draft", "report"],
                     help="draft: ask Jev about every card not answered yet; report: per lens, what passes its cut, "
                          "and near-cut.md (sends nothing)")
    alp.add_argument("folder", help="where lenses.json is and the draft goes (it quotes me: keep it in saved/)")
    alp.add_argument("--limit", type=int, help="draft: ask at most this many cards (a smoke run)")
    alp.add_argument("--monthly-cap-usd", type=float, help="draft: send nothing once the month's spend reaches this "
                     "(default: config)")
    alp.set_defaults(func=cmd_audit_lens)
    for name, question, what in (
            ("audit-analogies", "analogy", "a blind sheet of possible analogies between unrelated ideas, ranked by "
                                           "Jev beside random pairs (sends to Jev; --only facets to Claude); score "
                                           "my marks"),
            ("audit-tension", "tension", "a blind sheet of possible tensions, proposed several ways (sends to "
                                         "Claude); score my marks"),
            ("audit-summaries", "summary", "a blind sheet of extraction trials' summaries: do they say more than "
                                           "what was said? (the sheet sends nothing; draft sends to the model); "
                                           "score my marks")):
        pa = sub.add_parser(name, help=what)
        pa.add_argument("action", choices=["sheet", "draft", "score"])
        pa.add_argument("folder", help="where sheet.md and key.json go (it quotes people: keep it in saved/)")
        pa.add_argument("--seed", type=int, default=9, help="the sample and shuffle's seed")
        pa.add_argument("--model", default="claude-opus-5-5",
                        help="draft: the model answering, e.g. z-ai/glm-5.3, or jev-1.14.0 (TypeSafe's Jev)")
        pa.add_argument("--effort", default="low", help="draft: its effort")
        pa.add_argument("--quotes-only", action="store_true", help="draft: show the model what was said, no summaries")
        pa.add_argument("--probes", action="store_true",
                        help="draft, Jev: also ask the probe questions (jev.PROBES), scored beside the deciding ones")
        pa.add_argument("--only", help="sheet: draw every pair from one set, e.g. model or jev (or facets, no longer "
                                       "on an analogy sheet by default)")
        pa.add_argument("--pool", type=int, help="analogy sheet: how many unrelated pairs Jev ranks to propose from "
                                                 "(default 3000)")
        pa.add_argument("--size", type=int, help="sheet, with --only: how many pairs (default 30)")
        pa.add_argument("--exclude", nargs="*", default=[], help="sheet: leave out pairs on these earlier sheets")
        pa.set_defaults(func=cmd_audit_probe, question=question)
    am = sub.add_parser("audit-misses", help="a blind sheet of the reference's solid captures in a trial, missed or "
                        "found by a cheaper model: are the misses worth having? Or --both-ways: whose bar is right? "
                        "(sends nothing); score my marks")
    am.add_argument("action", choices=["sheet", "score"])
    am.add_argument("folder", help="where sheet.md and key.json go (it quotes people: keep it in saved/)")
    am.add_argument("prompt", nargs="?", help="sheet: the trial's prompt, a name in extract.PROMPTS, e.g. v5.3")
    am.add_argument("--model", help="sheet: the candidate, as its trial named it, e.g. openai/gpt-6.1-terra")
    am.add_argument("--effort", help="sheet: the candidate's effort in its trial (default: the configured one)")
    am.add_argument("--against", help="sheet: the reference, model[:effort], read twice (--replicate 2) unless "
                    "--both-ways (default: the configured model and effort)")
    am.add_argument("--both-ways", action="store_true",
                    help="sheet: captures only the candidate made, only the reference made, and both made, side by "
                         "side; needs no second read")
    am.add_argument("--live", action="store_true",
                    help="sheet: the trial against the live read (what `im run` keeps), not a trial read: every "
                         "capture only the live read made, every one only the trial made, and --size (default 20) "
                         "both made, marked under the eligibility rubric")
    am.add_argument("--seed", type=int, default=13, help="sheet: the sample and shuffle's seed")
    am.add_argument("--size", type=int, help="sheet: how many misses (default: all), beside as many found; "
                    "--both-ways: how many from each set (default 20)")
    am.set_defaults(func=cmd_audit_misses)
    hs = sub.add_parser("hard-set", help="a deliberate hard set of episodes (irony, fragments, relayed thoughts, short "
                        "thoughts, ones I only acknowledge, ones answering someone), each as extraction would send "
                        "it (sends nothing)")
    hs.add_argument("folder", help="where the episodes and index.json go (it quotes people: keep it in saved/)")
    hs.add_argument("--per-case", type=int, default=6, help="episodes per case (default 6)")
    hs.add_argument("--controls", type=int, default=0, help="also this many episodes from the trials' random sample")
    hs.add_argument("--seed", type=int, default=1, help="the shuffle's seed")
    hs.set_defaults(func=cmd_hard_set)
    bm = sub.add_parser("bench-match", help="B1: replay Opus's logged match decisions through Jev (sends to Jev), "
                        "then score its agreement with Opus")
    bm.add_argument("action", choices=["run", "score"])
    bm.add_argument("folder", help="where Jev's answers go (it quotes people: keep it in saved/), e.g. /saved/b1-1")
    bm.add_argument("--limit", type=int, help="run: only the first N decisions")
    bm.add_argument("--monthly-cap-usd", type=float, help="run: send nothing once the month's spend reaches this "
                    "(default: config)")
    bm.set_defaults(func=cmd_bench_match)
    sp = sub.add_parser("analogy-spike", help="can structure find my analogy pairs where topic can't? (sends to Claude)")
    sp.add_argument("folder", help="a marked B4 sheet's folder, e.g. /saved/b4-1")
    sp.add_argument("--others", type=int, default=300, help="other clusters to rank the partners among")
    sp.set_defaults(func=cmd_analogy_spike)
    ic = sub.add_parser("import-chatgpt", help="my messages to the ChatGPT Idea Machine, as a curation sheet marks "
                        "them, read by extraction v5.3 into archive captures (sends to Claude with --write)")
    ic.add_argument("export", help="the conversation's export (Markdown), e.g. /saved/chatgpt-idea-machine.md")
    ic.add_argument("sheet", help="the curation sheet (keep, drop or edit each message), e.g. "
                    "/saved/chatgpt-curate-sheet.md")
    ic.add_argument("--tz", default="America/New_York", help="the time zone the export's times are in")
    ic.add_argument("--write", action="store_true", help="send and store (without it, only show what would be sent)")
    ic.set_defaults(func=cmd_import_chatgpt)
    sub.add_parser("lattice", help="counts: ideas, evidence, connections, unmatched items").set_defaults(
        func=cmd_lattice)
    fb = sub.add_parser("feedback", help="summaries of my feedback")
    fb.add_argument("--reasons", action="store_true", required=True,
                    help="the cards I dismissed, each counted once by the latest reason I gave; unknown where I gave "
                    "none")
    fb.set_defaults(func=cmd_feedback)
    cn = sub.add_parser("connect", help="the connection judge: Jev screens pairs of nearby clusters, Terra judges "
                        "the rest, and its connections replace the embedding stand-ins (sends to Jev and Terra; "
                        "`im run` does this too, at most [connect] max_pairs_per_run pairs a run)")
    cn.add_argument("--limit", type=int, help="ask about at most this many pairs this time (0: send nothing, only "
                    "count what's nominated and due)")
    cn.set_defaults(func=cmd_connect)
    mg = sub.add_parser("merge", help="merge cluster OTHER into KEEP: its captures, observations and connections move "
                        "to KEEP, whose title and statement stay; OTHER is no card or matcher candidate from then on")
    mg.add_argument("keep", help="the cluster to keep (its id)")
    mg.add_argument("other", help="the cluster merged into it (its id)")
    mg.add_argument("--distinct", action="store_true", help="don't merge: mark the two as distinct points, so "
                    "`im merges` stops listing them")
    mg.set_defaults(func=cmd_merge)
    ms = sub.add_parser("merges", help="the merge screen: pairs of clusters that may be one point, likeliest first by "
                        "Jev's same_v2 (sends to Jev for pairs not asked about their current text; writes nothing "
                        "else and never merges)")
    ms.add_argument("--top", type=int, default=20, help="how many pairs to print (default 20)")
    ms.add_argument("--limit", type=int, help="ask Jev about at most this many pairs this time (0: send nothing)")
    ms.set_defaults(func=cmd_merges)
    ie = sub.add_parser("idea", help="one card: show it, or correct its title, statement or note")
    iea = ie.add_subparsers(dest="action", required=True, metavar="action")
    ish = iea.add_parser("show", help="its text, whether I corrected it, its quotes, and the text of the clusters "
                         "merged into it")
    ish.add_argument("id", help="the cluster's id or a unique prefix")
    ied = iea.add_parser("edit", help="correct it: only the fields given change; an empty value reverts to Claude's "
                         "(no note, for the note)")
    ied.add_argument("id", help="the cluster's id or a unique prefix")
    ied.add_argument("--title", help="at most 120 characters")
    ied.add_argument("--statement", help="at most 1000 characters")
    ied.add_argument("--note", help="my note, at most 1000 characters")
    ie.set_defaults(func=cmd_idea)
    th = sub.add_parser("themes", help="list themes; pin, unpin, rename or reject one")
    th.add_argument("action", nargs="?", default="list", choices=["list", "pin", "unpin", "rename", "reject"])
    th.add_argument("theme", nargs="?", help="theme name (or its start), or id prefix")
    th.add_argument("name", nargs="?", help="the new name, for rename")
    th.set_defaults(func=cmd_themes)
    ia = sub.add_parser("ideas", help="what Claude captured; --review to keep or discard each item")
    ia.add_argument("--review", action="store_true", help="keep or discard each undecided item, most confident first")
    ia.add_argument("--all", action="store_true", help="also list discarded items")
    ia.add_argument("--discards", action="store_true",
                    help="discarded items with why: material for revising the extraction prompt")
    ia.set_defaults(func=cmd_ideas)
    fg = sub.add_parser("forgotten", help="forgotten segments that had already been sent to Claude")
    fg.add_argument("--sent", action="store_true", required=True)
    fg.set_defaults(func=cmd_forgotten)
    qu = sub.add_parser("queue", help="the attention queue: what I tapped the pendant for or called a note to self, "
                        "with its lines, newest first; remove ID takes one off (and changes nothing else)")
    qu.add_argument("action", nargs="?", default="list", choices=["list", "remove"])
    qu.add_argument("id", nargs="?", type=int, help="remove: the item's id, as listed")
    qu.set_defaults(func=cmd_queue)
    sub.add_parser("check", help="run invariant queries; exit 1 on failure").set_defaults(func=cmd_check)
    wb = sub.add_parser("web", help="serve the neutral view (every cluster, newest first, no score)")
    wb.add_argument("--host", default="127.0.0.1")
    wb.add_argument("--port", type=int, default=8792)
    wb.add_argument("--demo", action="store_true", help="demo mode on every page, with no way out of it: nothing "
                    "sensitive is shown (the demo views)")
    wb.set_defaults(func=cmd_web)
    args = p.parse_args(argv)
    args.func(args)
