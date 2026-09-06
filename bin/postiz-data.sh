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
  export|import|uploads) ;;
  *) echo "usage: $0 {export|import|uploads} [FILE=..] [UPLOADS=..] [FORCE=..]"; exit 2 ;;
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

# --- uploads volume ----------------------------------------------------------
# Postiz serves /uploads off a named volume (STORAGE_PROVIDER=local). The DB
# carries the Media ROWS; without the files behind them every image 404s.
#
# Everything here runs INSIDE a container with the volume mounted, so it works
# the same on Docker Desktop/WSL2 where the volume's real path lives in the
# Docker VM and is not reachable from a normal shell. Each step is verified —
# the previous version hid tar's exit code and reported success on a no-op.
uploads_count() {
  docker run --rm -v "${UPLOADS_VOLUME}:/v" alpine sh -c 'find /v -type f | wc -l' 2>/dev/null | tr -d '[:space:]'
}

restore_uploads() {
  local tgz="$EXPORT_DIR/postiz-uploads.tar.gz" dir base
  [ -n "$FILE_ARG" ] && [ "${FILE_ARG%.tar.gz}" != "$FILE_ARG" ] && tgz="$FILE_ARG"
  if [ ! -f "$tgz" ]; then
    echo "  ✗ REFUSING: no $tgz — copy it over, or pass FILE=<path to .tar.gz>"; return 1
  fi
  # Absolute path: the bind mount is what silently produced an empty /in before.
  dir="$(cd "$(dirname "$tgz")" && pwd)"; base="$(basename "$tgz")"

  gzip -t "$tgz" 2>/dev/null || { echo "  ✗ REFUSING: $tgz is corrupt (gzip -t failed)"; return 1; }
  local want; want="$(tar tzf "$tgz" 2>/dev/null | grep -c '[^/]$')"
  echo "  archive     $base ($want files)"

  if ! docker volume inspect "$UPLOADS_VOLUME" >/dev/null 2>&1; then
    echo "  ✗ REFUSING: no docker volume named $UPLOADS_VOLUME"
    echo "    (the prefix follows the compose project = directory name)"
    docker volume ls --format '{{.Name}}' | grep -i upload | sed 's/^/    found: /'
    return 1
  fi

  local before; before="$(uploads_count)"
  echo "  volume      $UPLOADS_VOLUME had ${before:-?} files — wiping"
  docker run --rm -v "${UPLOADS_VOLUME}:/v" alpine \
    sh -c 'rm -rf /v/..?* /v/.[!.]* /v/* 2>/dev/null; exit 0' >/dev/null 2>&1

  if ! docker run --rm -v "${UPLOADS_VOLUME}:/v" -v "${dir}:/in:ro" alpine \
        tar xzf "/in/${base}" -C /v; then
    echo "  ✗ extract failed — volume is now EMPTY, re-run with a good archive"; return 1
  fi

  local after; after="$(uploads_count)"
  if [ "${after:-0}" -lt 1 ]; then
    echo "  ✗ extract reported success but the volume is still empty"; return 1
  fi
  echo "  uploads     restored — $after files in the volume"

  # nginx in the postiz image reads these as a non-root user; a fresh extract is
  # root-owned. Best effort: only matters when the container is up.
  if [ -n "$(docker ps --filter 'name=^postiz$' -q)" ]; then
    docker exec postiz sh -c 'chown -R www:www /uploads 2>/dev/null || chown -R nginx:nginx /uploads 2>/dev/null || true' >/dev/null 2>&1
    echo "  ownership   fixed inside the postiz container"
  else
    echo "  ! postiz container not running — start it, then: docker exec postiz chown -R www:www /uploads"
  fi
  return 0
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

  # What's already here? A lift-and-shift target may be empty, may be a half-built
  # fresh deploy, or may be a machine that has genuinely been posting.
  local existing_post existing_int
  existing_post="$(count_of Post)"; : "${existing_post:=}"
  existing_int="$(count_of Integration)"; : "${existing_int:=}"
  if [ -z "$existing_post" ]; then
    echo "  target      empty (no Postiz schema yet)"
  else
    echo "  target      Post $existing_post   Integration $existing_int"
  fi

  # Refuse only when the target has real published history — that's the case where
  # importing loses something nobody can get back.
  if [ -n "$existing_post" ] && [ "$existing_post" != "0" ] && [ "$FORCE" != "1" ]; then
    echo "  ✗ REFUSING: target already has $existing_post posts — importing replaces them."
    echo "    Two machines posting to the same channels also double-posts."
    echo "    If this box is being replaced: make postiz-import FORCE=1"
    exit 1
  fi
  [ -n "$existing_post" ] && [ "$existing_post" != "0" ] && \
    echo "  ! FORCE=1 — replacing a DB that has $existing_post posts"

  # Drop and recreate the database rather than leaning on the dump's
  # --clean --if-exists: that only drops objects the dump itself contains, so a
  # target on a different Postiz schema keeps orphan tables and you end up with a
  # mix of both. A lift-and-shift wants the source's schema exactly.
  echo "  nuking      DROP DATABASE $PGDB, recreating empty"
  if ! docker exec "$PG_CONTAINER" psql -U "$PGUSER" -d postgres -q \
        -c "DROP DATABASE IF EXISTS \"$PGDB\" WITH (FORCE);" \
        -c "CREATE DATABASE \"$PGDB\" OWNER \"$PGUSER\";" >/dev/null 2>&1; then
    echo "  ✗ could not recreate $PGDB (is something still connected?)"; exit 1
  fi

  if ! docker exec -i "$PG_CONTAINER" psql -U "$PGUSER" -d "$PGDB" -q -v ON_ERROR_STOP=1 < "$dump" >/dev/null 2>&1; then
    echo "  ✗ restore failed — $PGDB is now EMPTY. Re-run with a good dump."; exit 1
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

  if [ "$UPLOADS" = "1" ]; then
    restore_uploads || exit 1
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
  export)  do_export ;;
  import)  do_import ;;
  # Media only — no DB touched, so it's safe on a machine already running.
  uploads) echo "[postiz-uploads]"; restore_uploads ;;
esac
