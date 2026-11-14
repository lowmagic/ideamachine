# Deploying to the NAS

Idea Machine runs on TrueNAS SCALE next to Hearsay (ROADMAP §1 stack assumptions):
- Postgres, the pipeline run and the web view run in Docker. The run is a kind on
  [corral](https://github.com/lowmagic/corral)'s NAS worker: Hearsay enqueues it at the end of its pipeline, once
  the stream has new turns. Set up corral's server and NAS worker (in `/mnt/fast/corral-worker`, deployed from the
  infra repo) before `install/nas.sh`.
- The data lives in the `fast/ideamachine` dataset, on the NVMe pool, so the spinning disks in `storage` can idle.
  `fast` is snapshotted nightly at 4:00 and replicated to `storage/fast-replica`.
- The clones live in `/mnt/fast/repos` (dataset `fast/repos`), owned by `admin`. Clone and pull as admin, never
  as root; run the install script with sudo from there.
- Hearsay's stream is mounted read-only, from whichever pool Hearsay is on (below).
- The neutral view (`im web`, container `ideamachine-web`) is on the tailnet at
  `https://truenas.<tailnet>.ts.net:8445/` via `tailscale serve`, with its own token (`IM_WEB_TOKEN`).
- Other programs on the NAS read the database's `pub` schema as role `im_reader`, through Postgres on the NAS's
  loopback, `127.0.0.1:55433` (below).
- Logs go to the journal: `journalctl -t ideamachine-run`, `-t ideamachine-backup`, `-t ideamachine-db`,
  `-t ideamachine-web`.

eeyore keeps the dev and test databases.

## First install, moving from eeyore

On the NAS, as admin:

```sh
git clone https://github.com/lowmagic/ideamachine.git /mnt/fast/repos/ideamachine
sudo /mnt/fast/repos/ideamachine/install/nas.sh
```

It creates the datasets and secrets (asking once for the Anthropic API key), builds the images and starts
Postgres. Then it stops and asks for the database.

On eeyore, as your user:

```sh
install/move-to-nas.sh
```

This stops eeyore's hourly run, dumps the database and copies it to the NAS. eeyore's copy is left as a fallback.

On the NAS again: `sudo /mnt/fast/repos/ideamachine/install/nas.sh`. It does the rest:
- restores the database;
- checks that nothing already sent to Claude would be sent again (`im pending --expect-none-resent`; this catches
  a time zone that differs from eeyore's);
- starts the neutral view and publishes it with `tailscale serve`;
- adds the run to corral's NAS worker and registers nightly backups;
- checks that it all works (below) and prints the view's address and login token.

On the phone, open that address in Safari, log in, then Share → Add to Home Screen. Finally, on eeyore:
`systemctl --user disable --now lattice.service`.

## Moving from `storage` to `fast` (2026-10-15)

Until 2026-10-15 everything lived in `storage/ideamachine`, with root-owned clones inside it. `install/nas.sh` moves
it once, when `fast/ideamachine` has no database yet and `storage/ideamachine` has one:
1. switches off both cron jobs and waits for a run in progress;
2. counts the rows of every table in the running database (`/mnt/fast/ideamachine/rowcounts-before-move.txt`);
3. stops the web view and Postgres, snapshots `config`, `backups` and `pg` together (`@to-fast-<time>`), and copies
   each with `zfs send -p | zfs recv`, so their properties come along;
4. copies any file in `logs/` that isn't a `*.log` into `saved/` (there were two, kept by hand: an
   `im trial` report and a text export). The old logs stay on `storage`;
5. starts Postgres from `fast`, checks every table has the same row count before anything writes, then carries on
   as an update, switching the cron jobs back on.

If a step fails it stops with nothing scheduled; re-running picks up where it left off. Nothing on `storage` is
deleted. Once everything works from `fast`, the script's last lines are the commands that remove the old copy.

On the NAS, as admin:

```sh
git clone https://github.com/lowmagic/ideamachine.git /mnt/fast/repos/ideamachine   # if not there yet
sudo /mnt/fast/repos/ideamachine/install/nas.sh
```

## Hearsay's stream

`install/nas.sh` reads the stream from `/mnt/fast/hearsay/stream` if Hearsay has moved there, else from
`/mnt/storage/hearsay/stream`, says which, and writes it to `install/.env` (which `compose.yaml` reads). After
Hearsay moves, re-run `install/nas.sh` to switch.

The importer reads a conversation missing from `index.json` as removed and drops its segments, so a stream on
`fast` that doesn't yet list every conversation the one on `storage` does is refused rather than read. If
conversations were forgotten on purpose in between, choose by hand:
`sudo HEARSAY_STREAM=/mnt/fast/hearsay/stream /mnt/fast/repos/ideamachine/install/nas.sh`.

## Updates

As admin, `git pull` in the clone, then `sudo /mnt/fast/repos/ideamachine/install/nas.sh`. It's idempotent:
existing secrets are kept, images are rebuilt, migrations applied, the worker's kind and the backup cron job
rewritten.

Every time, it ends by checking that it all works: Postgres is up; one run finishes; one backup, the same way,
writes a readable dump to `backups/`; the neutral view, logged in with its token, loads its feed (as its own
role) on 127.0.0.1:8792 and, through `tailscale serve`, at the tailnet address on 8445; and the read-only role,
from `access/reader.env`, reads `pub.cards` on 127.0.0.1:55433, while a wrong password, a write to `pub` and a read
of `im` are refused. So each re-run also does a run and a backup.

## Reading the database from another program

A program on the NAS run by `admin`, or any member of the `apps` group, can read Idea Machine's database without
root or Docker:
- Postgres is published on the NAS's loopback only, `127.0.0.1:55433` (`DB_LOOPBACK_PORT` in `install/lib.sh`,
  passed to `compose.yaml` through `install/.env`): never the LAN, the tailnet or `0.0.0.0`. Every role needs its
  password there. Policy C's names job used the same port until 2026-11-11.
- Role `im_reader` reads all of schema `pub` and nothing else: `SELECT` on its views and on
  `pub.feedback_events`, and its functions `pub.card_neighbours()` and `pub.embedding_model()`. Nothing in `im`
  (not `im.egress_log`, not the transcript in `im.source_segments`), and no write anywhere. Its grants are by
  schema (`dev/init/bootstrap-db.sql.in`), so what a migration adds to `pub` later is readable too.
- `pub` is what the web view reads: the ideas and their cards (title, statement, my note, counts, maturity),
  themes, connections and lineage, digests, proposals, sensitivity marks, map positions, my feedback events, and
  the evidence behind each idea: each capture's quote and gist, the speaker's label as Hearsay gave it, when, and
  in which conversation. The quotes include other people's words. There are no whole transcripts and no record of
  what was sent to models.
- `/mnt/fast/ideamachine/access/reader.env` (directory and file root:apps, 750 and 640) holds
  `IM_READER_DATABASE_URL` and the same connection as `PGHOST`, `PGPORT`, `PGDATABASE`, `PGUSER` and
  `PGPASSWORD`. `install/nas.sh` makes the password once (`IM_READER_PASSWORD` in `config/db.env`) and rewrites
  the file on every run.

As admin:

```sh
set -a; . /mnt/fast/ideamachine/access/reader.env; set +a
psql "$IM_READER_DATABASE_URL"
```

A program can take `IM_READER_DATABASE_URL` from that file, or, with libpq (psycopg, psql with no arguments), the
`PG*` variables.

## Lattice, removed (2026-11-11)

Lattice, the old presentation app, left the deploy on 2026-11-11, and with it its MCP endpoint for claude.ai. `im web`
serves the MCP server again (M5): on the tailnet at `/mcp` on the web view's address (8445), with `IM_WEB_TOKEN` as a
bearer header. The claude.ai connector needs the public listener behind OAuth, which comes next (M5's slice 2).

The next run of `install/nas.sh` after the change, and every one after it, does nothing once these are gone:
- removes the `lattice` and `lattice-tunnel` (cloudflared) containers and their images, and Lattice's address on
  the tailnet (`tailscale serve` on 8444);
- moves `config/lattice.env` to `config/web.env`, renaming `LATTICE_DATABASE_URL` and `LATTICE_TOKEN` to
  `IM_WEB_DATABASE_URL` and `IM_WEB_TOKEN` (same values: the token on the phone still works) and deleting
  `LATTICE_PUBLIC_URL`, `LATTICE_MCP_ALLOW` and `LATTICE_DEMO_HIDE`; renames `LATTICE_APP_PASSWORD` in `db.env`
  to `IM_WEB_PASSWORD`; deletes `config/cloudflared.env`, the tunnel's token;
- migration 058 drops what only Lattice read: `pub.idea_neighbours`, `pub.ideas_near()`, `pub.idea_positions`,
  `pub.review_items`, `pub.item_context`, `pub.runs`, `pub.spend`, and the columns of `pub.ideas` only it showed,
  along with `archive_ref` and the theme origin `archive`.

The web view's role keeps its name, `lattice_app`: migrations 008 to 052 grant to it by name, so a new database
needs that name while they run, and the pipeline role that runs migrations can't rename roles.

By hand, once: delete the Cloudflare tunnel and its public hostname (and the two WAF rules) in the Cloudflare
dashboard, remove the connector in claude.ai's settings, and, as admin, `rm -rf /mnt/fast/repos/lattice` (the
script says so while the clone is there). On a dev box, rename the same three lines in `.env` (`.env.example`).

## Policy C, retired (2026-11-11)

Names said go out as said (decision 0008, amended), so nothing looks for names any more. Each run of
`install/nas.sh` drops role `im_names` if it's still there, deletes `IM_NAME_KEY` and `IM_NAMES_PASSWORD` from the
config and the names job's connections in `gpu/`, and Postgres stopped listening on the NAS's loopback (until the
read-only role, above, brought port 55433 back). `[egress] names`, `public_names` and `my_names` are ignored, with a
warning, until deleted from `im.toml`.
TrueNAS's SSH "Allow TCP Port Forwarding", turned on for the names job's tunnel, can be turned off if nothing else
uses it.

The dev box runs nothing for Idea Machine now. Remove the `ideamachine.names` kinds from its corral worker (delete
`~/.config/corral/kinds.d/ideamachine.toml`, then `systemctl --user restart corral-worker`), and the environment
and connections `install/gpu.sh` made: `~/.local/share/ideamachine/gpu-venv`, `~/.config/ideamachine/gpu.env` and
`gpu-rebuild.env`.

## The archived database, `im_v1`

Decision 0012 rebuilt the archive from Hearsay's stream into a second database, which replaced `im` at the cutover
(2026-11-11). The database from before the rebuild was renamed `im_v1`, in the same Postgres. It's archived: dumped
once at the cutover (`backups/im_v1-2026-11-11.dump`), never migrated again, and not in the nightly backup, which
covers only `im`. Nothing connects to it. To look at it, use the psql command below with `-d im_v1`.

To drop it later, once that dump is kept somewhere safe, run the same command with `-d postgres -c 'DROP DATABASE
im_v1'`. The rebuild's leftovers can go now: `config/im/rebuild.toml`, and the last `backups/im_rebuild-*.dump` (the
nightly job no longer prunes them).

## Where things are

| What | Where |
|---|---|
| Secrets | `/mnt/fast/ideamachine/config/` (`db.env`, `pipeline.env`, `web.env`; root only) |
| Config | `/mnt/fast/ideamachine/config/im/im.toml` (e.g. `extract.prompt`; read by the run container) |
| Database | `/mnt/fast/ideamachine/pg` |
| Nightly dumps (14 days) | `/mnt/fast/ideamachine/backups/` |
| Kept by hand (trial reports, exports) | `/mnt/fast/ideamachine/saved/` (root and apps only: they quote people) |
| Read-only access | `/mnt/fast/ideamachine/access/reader.env` (root and apps only; role `im_reader`) |
| Clone | `/mnt/fast/repos/ideamachine` (admin) |
| Logs | `journalctl -t ideamachine-run` (and `-t ideamachine-backup`, `-t ideamachine-db`, `-t ideamachine-web`) |
| Run now | `im` as root (`install/bin/im`; `install/nas.sh` puts it on root's PATH), or `docker compose -f /mnt/fast/repos/ideamachine/install/compose.yaml run --rm run` |
| Any im command | `im check`, `im lattice`, ...: the command runs in the pipeline image, with `saved/` at `/saved` |
| psql | `docker compose -f /mnt/fast/repos/ideamachine/install/compose.yaml exec db psql -U postgres -d im` |
