#!/usr/bin/env bash
# Set up the Reddit channel: the Devvit app (reddit-intg/soljet-postiz-intg)
# that the Cloud Run reddit-poster calls to post/reply on Reddit.
#
#   ops/reddit/deploy.sh [--devvit-only|--upload-only|--key-only|--install-only|all]
#
# Devvit apps run on Reddit's infrastructure, not ours — `devvit upload` ships
# the bundle to Reddit and `devvit install` turns it on per subreddit. The app
# is a thin pass-through (see src/shared/api.ts); all posting logic lives in
# the poster.
#
#   --upload-only   build + `devvit upload --bump patch` (new app version)
#   --key-only      set the app's secret `posterKey` setting from REDDIT_POSTER_KEY
#   --install-only  install/upgrade the app on every sub in REDDIT_SUBREDDITS
#   --devvit-only   all three, in that order
#   all             --devvit-only (+ the Cloud Run poster, once it exists)
#
# CONFIG (explicit env var > repo-root .env):
#   REDDIT_SUBREDDITS   comma-separated subs you moderate, eg "arboryx,facades"
#   REDDIT_POSTER_KEY   shared secret the poster sends as X-Poster-Key
#                       (generate: openssl rand -hex 32)
#
# AUTH: whoever `npx devvit login` is logged in as (must moderate each sub).
# DRY=1 prints every command instead of running it.
set -uo pipefail

MODE="${1:-all}"
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$DIR/../.." && pwd)"
APP="$ROOT/reddit-intg/soljet-postiz-intg"
cd "$ROOT"

# Same helper shape as ops/reflex/deploy.sh::envget.
envget(){ [ -f .env ] || return 0; grep -E "^$1=" .env 2>/dev/null | tail -1 | cut -d= -f2- \
          | sed -e 's/^"\(.*\)"$/\1/' -e "s/^'\(.*\)'\$/\1/" -e 's/\r$//' -e 's/[[:space:]]*$//'; }

SUBREDDITS="${REDDIT_SUBREDDITS:-$(envget REDDIT_SUBREDDITS)}"
POSTER_KEY="${REDDIT_POSTER_KEY:-$(envget REDDIT_POSTER_KEY)}"

red(){ printf '\033[31m%s\033[0m\n' "$*"; }
grn(){ printf '\033[32m%s\033[0m\n' "$*"; }
run(){ if [ -n "${DRY:-}" ]; then echo "+ (cd $APP && $*)"; else (cd "$APP" && "$@"); fi; }

need_node(){
  command -v node >/dev/null || { red "node not found (need >=22; .nvmrc pins 24)"; exit 1; }
  if [ ! -x "$APP/node_modules/.bin/devvit" ]; then
    run npm ci || { red "npm ci failed in $APP"; exit 1; }
  fi
}

need_login(){
  [ -n "${DRY:-}" ] && return 0
  local who; who="$(cd "$APP" && npx devvit whoami 2>&1 | tail -1)"
  case "$who" in
    *"Logged in as"*) grn "devvit: $who" ;;
    *) red "devvit: not logged in — run: (cd $APP && npx devvit login)"; exit 1 ;;
  esac
}

do_upload(){
  need_node; need_login
  run npm run upload || { red "devvit upload failed"; exit 1; }
  grn "uploaded new app version"
}

do_key(){
  need_node; need_login
  [ -n "$POSTER_KEY" ] || { red "REDDIT_POSTER_KEY is empty in .env (openssl rand -hex 32)"; exit 1; }
  if [ -n "${DRY:-}" ]; then echo "+ (cd $APP && <REDDIT_POSTER_KEY> | npx devvit settings set posterKey)"; return 0; fi
  # The CLI only prompts for the value; feed it on stdin.
  if (cd "$APP" && printf '%s\n' "$POSTER_KEY" | npx devvit settings set posterKey >/dev/null 2>&1); then
    grn "posterKey set"
  else
    red "couldn't set posterKey non-interactively — run it by hand and paste REDDIT_POSTER_KEY:"
    echo "    (cd $APP && npx devvit settings set posterKey)"
    exit 1
  fi
}

do_install(){
  need_node; need_login
  [ -n "$SUBREDDITS" ] || { red "REDDIT_SUBREDDITS is empty in .env (eg \"arboryx,facades\")"; exit 1; }
  local sub rc=0
  IFS=',' read -ra subs <<< "$SUBREDDITS"
  for sub in "${subs[@]}"; do
    sub="$(echo "$sub" | tr -d '[:space:]')"; sub="${sub#r/}"
    [ -n "$sub" ] || continue
    # Re-running on an installed sub upgrades it to the latest version.
    if run npx devvit install "$sub"; then grn "installed on r/$sub"; else red "install failed on r/$sub"; rc=1; fi
  done
  return $rc
}

case "$MODE" in
  --upload-only)  do_upload ;;
  --key-only)     do_key ;;
  --install-only) do_install ;;
  --devvit-only|all)
    do_upload && do_key && do_install || exit 1
    if [ "$MODE" = all ]; then echo "note: Cloud Run reddit-poster isn't built yet — Devvit app only."; fi
    ;;
  *) red "unknown part: $MODE"; exit 2 ;;
esac
