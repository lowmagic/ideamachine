# Shared helpers for install scripts. Source this; don't run it.

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# NAS layout (TrueNAS SCALE). Data on the NVMe pool "fast", so the spinning disks in "storage" can idle; it's
# snapshotted nightly at 4:00 and replicated to storage. Clones live in /mnt/fast/repos, owned by admin.
NAS_POOL_DATASET="fast/ideamachine"
NAS_ROOT="/mnt/fast/ideamachine"
NAS_CONFIG="$NAS_ROOT/config"
NAS_PG="$NAS_ROOT/pg"
NAS_BACKUPS="$NAS_ROOT/backups"
NAS_SAVED="$NAS_ROOT/saved"   # outputs kept by hand, e.g. `im trial` reports (they quote people: root and apps only)
NAS_ACCESS="$NAS_ROOT/access"   # connection files for other programs on the NAS (root and apps only)
NAS_READER_ENV="$NAS_ACCESS/reader.env"   # the read-only role's (im_reader) database URL
NAS_DB_ENV="$NAS_CONFIG/db.env"
NAS_PIPELINE_ENV="$NAS_CONFIG/pipeline.env"
NAS_WEB_ENV="$NAS_CONFIG/web.env"   # the web view's database URL and login token
NAS_MOVE_DUMP="$NAS_BACKUPS/from-eeyore.dump"
# Before 2026-10-15 everything was on storage. install/nas.sh moves it once and never deletes it.
OLD_POOL_DATASET="storage/ideamachine"
OLD_ROOT="/mnt/storage/ideamachine"
# Hearsay's stream, on whichever pool Hearsay is on (pick_hearsay_stream).
HEARSAY_STREAM_FAST="/mnt/fast/hearsay/stream"
HEARSAY_STREAM_STORAGE="/mnt/storage/hearsay/stream"
WEB_HTTPS_PORT=8445   # the neutral view (im web)
# Postgres on the NAS's loopback, for the read-only role: never the LAN or the tailnet. Policy C's names job had it
# until 2026-11-11; 55432 is the dev database's.
DB_LOOPBACK_PORT=55433
DB_CONTAINER=ideamachine-db
RUN_LOCK=/run/ideamachine-run.lock
# Corral's NAS worker runs from here (deployed from the infra repo).
NAS_CORRAL_DIR="/mnt/fast/corral-worker"
# Dedicated NAS user/groups managed by infra (ADR-025).
IM_UID=3104
IM_GID=3104
HEARSAY_STREAM_GID=3102
COMPOSE=(docker compose -f "$REPO_DIR/install/compose.yaml")

say() { printf '\n==> %s\n' "$*"; }
die() { printf '\nERROR: %s\n' "$*" >&2; exit 1; }

require_root() {
    [ "$(id -u)" -eq 0 ] || die "run as root: sudo $0"
}

# Env files are plain KEY=VALUE lines, readable by both bash and compose env_file.
env_get() {
    local file="$1" key="$2"
    [ -f "$file" ] || return 0
    sed -n "s/^${key}=//p" "$file" | tail -n 1
}

# Appends KEY only if it's absent, so re-runs never rotate existing values.
env_ensure() {
    local file="$1" key="$2" value="$3"
    [ -n "$(env_get "$file" "$key")" ] && return 0
    [ -n "$value" ] || die "empty value for $key"
    (umask 077; touch "$file")
    printf '%s=%s\n' "$key" "$value" >> "$file"
    chmod 600 "$file"
}

# Renames KEY to NEW, keeping its value, unless NEW is already there. Does nothing once it's done.
env_rename() {
    local file="$1" key="$2" new="$3"
    [ -n "$(env_get "$file" "$key")" ] && [ -z "$(env_get "$file" "$new")" ] || return 0
    sed -i "s/^${key}=/${new}=/" "$file"
    echo "renamed $key to $new in $file"
}

# Removes KEY, if it's there: a secret nothing uses any more.
env_delete() {
    local file="$1" key="$2"
    [ -n "$(env_get "$file" "$key")" ] || return 0
    sed -i "/^${key}=/d" "$file"
    echo "removed $key from $file"
}

# Creates or updates a TrueNAS cron job (shown in System -> Advanced -> Cron Jobs). The middleware appends its own
# redirect to the command, so the caller's redirect sits inside an outer subshell.
cron_id() {
    midclt call cronjob.query "[[\"description\", \"=\", \"$1\"]]" \
        | python3 -c 'import json, sys; jobs = json.load(sys.stdin); print(jobs[0]["id"] if jobs else "")'
}

cron_ensure() {
    local description="$1" minute="$2" hour="$3" command="$4" payload id
    payload="$(python3 -c '
import json, sys
print(json.dumps({"user": "root", "command": sys.argv[1], "description": sys.argv[2],
                  "schedule": {"minute": sys.argv[3], "hour": sys.argv[4], "dom": "*", "month": "*", "dow": "*"},
                  "enabled": True, "stdout": True, "stderr": True}))' "$command" "$description" "$minute" "$hour")"
    id="$(cron_id "$description")"
    if [ -z "$id" ]; then
        midclt call cronjob.create "$payload" >/dev/null
        echo "created: $description"
    else
        midclt call cronjob.update "$id" "$payload" >/dev/null
        echo "up to date: $description (job $id)"
    fi
}

cron_delete() {
    local id
    id="$(cron_id "$1")"
    [ -n "$id" ] || return 0
    midclt call cronjob.delete "$id" >/dev/null
    echo "removed: $1 (job $id)"
}

# Writes stdin to FILE only if that changes it, and says whether it did, so a re-run doesn't restart a worker for
# nothing.
write_if_changed() {
    local file="$1" content
    content="$(cat)"
    [ -f "$file" ] && [ "$(cat "$file")" = "$content" ] && return 1
    printf '%s\n' "$content" > "$file"
}

cron_disable() {  # leaves the job in place, switched off; cron_ensure switches it back on
    local id
    id="$(cron_id "$1")"
    [ -n "$id" ] || return 0
    midclt call cronjob.update "$id" '{"enabled": false}' >/dev/null
    echo "disabled: $1 (job $id)"
}

# Hearsay's stream: on fast once Hearsay has moved there, else on storage. The importer reads a conversation missing
# from index.json as gone and drops its segments, so a stream on fast that doesn't yet list every conversation the
# one on storage does (half set up, or a fresh empty Hearsay) is refused rather than read.
# Sets HEARSAY_STREAM and HEARSAY_STREAM_WHY. An explicit HEARSAY_STREAM in the environment wins.
pick_hearsay_stream() {
    if [ -n "${HEARSAY_STREAM:-}" ]; then
        HEARSAY_STREAM_WHY="set by hand (HEARSAY_STREAM=$HEARSAY_STREAM)"
    elif [ -f "$HEARSAY_STREAM_FAST/index.json" ]; then
        if [ -f "$HEARSAY_STREAM_STORAGE/index.json" ]; then
            local missing
            missing="$(python3 - "$HEARSAY_STREAM_STORAGE/index.json" "$HEARSAY_STREAM_FAST/index.json" <<'PY'
import json, sys
old, new = ({c["conversation_id"] for c in json.load(open(p))["conversations"]} for p in sys.argv[1:])
print(len(old - new))
PY
)"
            [ "$missing" = 0 ] || die "Hearsay's stream on fast lacks $missing conversations that the one on storage has.
    Reading it would delete them here. If Hearsay is still moving, finish that first. If they were forgotten on
    purpose, choose by hand: sudo HEARSAY_STREAM=$HEARSAY_STREAM_FAST $0"
        fi
        HEARSAY_STREAM="$HEARSAY_STREAM_FAST"
        HEARSAY_STREAM_WHY="Hearsay is on fast"
    elif [ -f "$HEARSAY_STREAM_STORAGE/index.json" ]; then
        HEARSAY_STREAM="$HEARSAY_STREAM_STORAGE"
        HEARSAY_STREAM_WHY="no stream in $HEARSAY_STREAM_FAST yet; re-run this after Hearsay moves to switch"
    else
        die "no Hearsay stream (no index.json in $HEARSAY_STREAM_FAST or $HEARSAY_STREAM_STORAGE). Install Hearsay first."
    fi
    [ -f "$HEARSAY_STREAM/index.json" ] || die "no index.json in $HEARSAY_STREAM"
}

# Exact row counts of every table, one "schema.table|count" per line, sorted. Compares a database before and after
# a move.
row_counts() {
    docker exec -i "$DB_CONTAINER" psql -v ON_ERROR_STOP=1 -U postgres -d im -tA <<'SQL'
SELECT format('SELECT %L, count(*) FROM %I.%I', n.nspname || '.' || c.relname, n.nspname, c.relname)
FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE c.relkind IN ('r', 'p') AND n.nspname NOT IN ('pg_catalog', 'information_schema')
  AND n.nspname NOT LIKE 'pg_toast%'
ORDER BY 1 \gexec
SQL
}

db_wait() {  # until the database container answers
    for _ in $(seq 60); do
        docker exec "$DB_CONTAINER" pg_isready -q -U postgres -d im 2>/dev/null && return 0
        sleep 1
    done
    die "$DB_CONTAINER isn't answering"
}

db_sql() {  # runs SQL as the database superuser, prints unaligned tuples
    "${COMPOSE[@]}" exec -T db psql -v ON_ERROR_STOP=1 -U postgres -d im -tAc "$1"
}

im() {  # runs one `im` command in the pipeline image
    "${COMPOSE[@]}" run --rm -T run "$@" </dev/null
}

# Logs in to the web view at BASE (ending in /) with its token, then loads the feed; prints the feed's HTTP status.
# Any more arguments go to both curls. The token goes on stdin, not the command line.
web_feed() {
    local base="$1" jar code
    shift
    jar="$(mktemp)"
    printf %s "$(env_get "$NAS_WEB_ENV" IM_WEB_TOKEN)" \
        | curl -s -o /dev/null -c "$jar" --data-urlencode token@- "$@" "${base}login" || true
    code="$(curl -s -o /dev/null -w '%{http_code}' -b "$jar" "$@" "$base" || true)"
    rm -f "$jar"
    echo "$code"
}

# Runs one statement as the read-only role, as another program on the NAS would: its URL from access/reader.env,
# psql from Postgres's own image on the host's network, through the loopback port. Prints unaligned tuples. Any more
# arguments go to `docker run` (e.g. -e IM_READER_DATABASE_URL=... for another URL).
reader_sql() {
    local sql="$1"
    shift
    docker run --rm --network host --user "$IM_UID:$IM_GID" --env-file "$NAS_READER_ENV" "$@" --entrypoint sh \
        "$(docker inspect -f '{{.Config.Image}}' "$DB_CONTAINER")" \
        -c 'exec psql "$IM_READER_DATABASE_URL" -X -v ON_ERROR_STOP=1 -tAc "$1"' sh "$sql"
}
