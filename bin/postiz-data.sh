#!/usr/bin/env bash
# Export/import everything that makes this machine "the" Postiz machine, so a
# move to new hardware doesn't cost you the channel connections.
#
# A git clone + .env does NOT carry any of this. The Postgres volume holds the
# Integration table = every connected channel and its OAuth access/refresh
# tokens; losing it means redoing the X + LinkedIn OAuth dance by hand. The
# poster's own data/posted_log.sqlite is separate again, and it — not Postiz —
# is what stops a fresh machine re-posting the whole backlog.
#
# Usage: make postiz-export [UPLOADS=1]
#        make postiz-import [FILE=<path>] [UPLOADS=1] [FORCE=1]
set -uo pipefail

CMD="${1:-}"
shift || true
# Make's KEY=val knobs arrive as literal words, and may be empty/absent. Parse by
# name rather than position so order and gaps don't matter.
FILE_ARG=""; UPLOADS=""; FORCE=""
for arg in "$@"; do
  case "$arg" in
    FILE=*)    FILE_ARG="${arg#FILE=}" ;;
    UPLOADS=*) UPLOADS="${arg#UPLOADS=}" ;;
    FORCE=*)   FORCE="${arg#FORCE=}" ;;
  esac
done

EXPORT_DIR="data/postiz_export"
PG_CONTAINER="postiz-postgres"
UPLOADS_VOLUME="soljet-postiz_postiz-uploads"
POSTED_LOG="data/posted_log.sqlite"

# Validate the subcommand before anything environmental, so a typo reports the
# usage rather than a misleading "no .env here".
case "$CMD" in
  export|import) ;;
  *) echo "usage: $0 {export|import} [FILE=..] [UPLOADS=..] [FORCE=..]"; exit 2 ;;
esac

[ -f .env ] || { echo "  ✗ REFUSING: no .env here — run this from the repo root"; exit 1; }
PGUSER="$(grep -E '^POSTGRES_USER=' .env | head -1 | cut -d= -f2-)"
PGDB="$(grep -E '^POSTGRES_DB=' .env | head -1 | cut -d= -f2-)"
[ -n "$PGUSER" ] && [ -n "$PGDB" ] || {
  echo "  ✗ REFUSING: POSTGRES_USER / POSTGRES_DB missing from .env"; exit 1; }

# --- postgres lifecycle ------------------------------------------------------
# Only postgres is needed, not the whole stack. If we start it ourselves we stop
# it again on the way out, so this leaves the machine as it found it.
WE_STARTED_PG=0

pg_running() { [ -n "$(docker ps --filter "name=^${PG_CONTAINER}$" --filter status=running -q)" ]; }

pg_up() {
  if pg_running; then return 0; fi
  echo "  starting $PG_CONTAINER (stack stays down)"
  docker compose up -d "$PG_CONTAINER" >/dev/null 2>&1 || {
    echo "  ✗ could not start $PG_CONTAINER"; exit 1; }
  WE_STARTED_PG=1
  for _ in $(seq 1 30); do
    docker exec "$PG_CONTAINER" pg_isready -U "$PGUSER" -d "$PGDB" >/dev/null 2>&1 && return 0
    sleep 1
  done
  echo "  ✗ $PG_CONTAINER never became ready"; exit 1
}

pg_down() {
  [ "$WE_STARTED_PG" = "1" ] || return 0
  echo "  stopping $PG_CONTAINER again (we started it)"
  docker compose stop "$PG_CONTAINER" >/dev/null 2>&1 || true
}
trap pg_down EXIT

psql_q() { docker exec "$PG_CONTAINER" psql -U "$PGUSER" -d "$PGDB" -tAc "$1" 2>/dev/null; }

# Row count for a table, or "" when the table doesn't exist yet (fresh DB).
count_of() { psql_q "SELECT count(*) FROM \"$1\";" | tr -d '[:space:]'; }

# --- export ------------------------------------------------------------------
do_export() {
  pg_up
  mkdir -p "$EXPORT_DIR"
  local stamp dump
  stamp="$(date +%Y%m%d-%H%M%S)"
  dump="$EXPORT_DIR/postiz-db-$stamp.sql"

  echo "[postiz-export]"
  # Whole database: Integration (channels + tokens), Post (publish history),
  # Media, Notifications, Customer, Organization and User (the Postiz login).
  # pg_dump of one DB never includes pg_catalog/information_schema, so this is
  # already "app data only, no system tables".
  if ! docker exec "$PG_CONTAINER" pg_dump -U "$PGUSER" -d "$PGDB" --clean --if-exists > "$dump" 2>/dev/null; then
    echo "  ✗ pg_dump failed"; rm -f "$dump"; exit 1
  fi
  [ -s "$dump" ] || { echo "  ✗ dump came out empty"; rm -f "$dump"; exit 1; }
  echo "  db          $dump ($(du -h "$dump" | cut -f1))"
  echo "              Integration $(count_of Integration)   Post $(count_of Post)   Media $(count_of Media)"

  # posted_log.sqlite via the sqlite backup API, not cp: the poster may be
  # mid-write, and a torn copy would let the new machine repost.
  if [ -f "$POSTED_LOG" ]; then
    if python3 - "$POSTED_LOG" "$EXPORT_DIR/posted_log.sqlite" <<'PY'
import sqlite3, sys
src, dst = sys.argv[1], sys.argv[2]
s = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
d = sqlite3.connect(dst)
s.backup(d); d.close(); s.close()
PY
    then
      rows="$(python3 -c "import sqlite3;print(sqlite3.connect('$EXPORT_DIR/posted_log.sqlite').execute('SELECT count(*) FROM posted').fetchone()[0])" 2>/dev/null || echo '?')"
      echo "  posted_log  $EXPORT_DIR/posted_log.sqlite ($rows rows)"
    else
      echo "  ✗ posted_log.sqlite backup failed — do NOT rely on this export to prevent reposts"
      exit 1
    fi
  else
    echo "  posted_log  (none on this machine — nothing to carry)"
  fi

  if [ "$UPLOADS" = "1" ]; then
    docker run --rm -v "${UPLOADS_VOLUME}:/v" -v "$PWD/$EXPORT_DIR:/out" alpine \
      tar czf "/out/postiz-uploads.tar.gz" -C /v . >/dev/null 2>&1 \
      && echo "  uploads     $EXPORT_DIR/postiz-uploads.tar.gz ($(du -h "$EXPORT_DIR/postiz-uploads.tar.gz" | cut -f1))" \
      || echo "  ! uploads archive failed (media only — channels work without it)"
  else
    echo "  uploads     skipped (UPLOADS=1 to include ~87M of media)"
  fi

  echo
  echo "  These files hold LIVE OAuth tokens and the Postiz login hash."
  echo "  $EXPORT_DIR/ is gitignored — move them by hand (scp/USB), never commit."
  echo "  Still copy separately: .env, auth/, products/arboryx.ai/handles.yaml"
}

# --- import ------------------------------------------------------------------
do_import() {
  local dump="$FILE_ARG"
  if [ -z "$dump" ]; then
    dump="$(ls -1t "$EXPORT_DIR"/postiz-db-*.sql 2>/dev/null | head -1)"
  fi
  [ -n "$dump" ] && [ -f "$dump" ] || {
    echo "  ✗ REFUSING: no dump found — put one in $EXPORT_DIR/ or pass FILE=<path>"; exit 1; }

  pg_up
  echo "[postiz-import] $dump"

  # Guard: the dump is --clean --if-exists, so it DROPS what's there. If this
  # machine has already published, importing an older dump silently discards it.
  local existing
  existing="$(count_of Post)"
  if [ -n "$existing" ] && [ "$existing" != "0" ]; then
    if [ "$FORCE" != "1" ]; then
      echo "  ✗ REFUSING: this machine's Postiz DB already has $existing posts."
      echo "    Importing would DROP them (the dump is --clean --if-exists)."
      echo "    Two machines posting to the same channels also double-posts."
      echo "    If you're sure this box should be replaced: make postiz-import FORCE=1"
      exit 1
    fi
    echo "  ! FORCE=1 — replacing an existing DB that has $existing posts"
  fi

  if ! docker exec -i "$PG_CONTAINER" psql -U "$PGUSER" -d "$PGDB" -q < "$dump" >/dev/null 2>&1; then
    echo "  ! psql reported errors (DROP ... IF EXISTS on a fresh DB is normally harmless)"
  fi

  # posted_log: never silently overwrite a longer local history.
  local incoming="$EXPORT_DIR/posted_log.sqlite"
  if [ -f "$incoming" ]; then
    local have want
    have=0
    # Guard on existence: sqlite3.connect() would CREATE an empty file here.
    [ -f "$POSTED_LOG" ] && have="$(python3 -c "import sqlite3;print(sqlite3.connect('$POSTED_LOG').execute('SELECT count(*) FROM posted').fetchone()[0])" 2>/dev/null || echo 0)"
    want="$(python3 -c "import sqlite3;print(sqlite3.connect('$incoming').execute('SELECT count(*) FROM posted').fetchone()[0])" 2>/dev/null || echo 0)"
    if [ "$have" -gt "$want" ] && [ "$FORCE" != "1" ]; then
      echo "  ! keeping local $POSTED_LOG ($have rows > incoming $want) — FORCE=1 to overwrite"
    else
      [ -f "$POSTED_LOG" ] && cp "$POSTED_LOG" "$POSTED_LOG.bak.$(date +%Y%m%d-%H%M%S)"
      cp "$incoming" "$POSTED_LOG"
      echo "  posted_log  restored ($want rows; previous kept as .bak)"
    fi
  fi

  if [ "$UPLOADS" = "1" ] && [ -f "$EXPORT_DIR/postiz-uploads.tar.gz" ]; then
    docker run --rm -v "${UPLOADS_VOLUME}:/v" -v "$PWD/$EXPORT_DIR:/in" alpine \
      tar xzf /in/postiz-uploads.tar.gz -C /v >/dev/null 2>&1 \
      && echo "  uploads     restored" || echo "  ! uploads restore failed"
  fi

  echo
  echo "  channels now in the DB:"
  docker exec "$PG_CONTAINER" psql -U "$PGUSER" -d "$PGDB" -c \
    'SELECT name, "providerIdentifier", "tokenExpiration" FROM "Integration" WHERE "deletedAt" IS NULL;' 2>/dev/null \
    | sed 's/^/  /'
  echo "  Post rows: $(count_of Post)"
  echo
  echo "  Next: make deploy   (then check the UI shows the channels)"
}

case "$CMD" in
  export) do_export ;;
  import) do_import ;;
esac
