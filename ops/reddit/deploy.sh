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
#   --allow-only    set the app's `allowedSubreddits` setting (the only subs it
#                   will post to) from every product's REDDIT_SUBREDDIT
#   --install-only  refresh allowedSubreddits, then install/upgrade the app on
#                   every product's REDDIT_SUBREDDIT
#   --devvit-only   upload, key, allow, install — in that order
#   all             --devvit-only (+ the Cloud Run poster, once it exists)
#
# CONFIG:
#   REDDIT_SUBREDDIT   per product, in products/**/*tier.config (empty = no
#                      Reddit). Every non-empty one gets an install.
#   REDDIT_POSTER_KEY  repo-root .env. Our own shared secret (not from Reddit)
#                      the poster sends as X-Poster-Key; generated and appended
#                      to .env on first run if missing.
#
# AUTH: whoever `npx devvit login` is logged in as (must moderate each sub).
# DRY=1 prints every command instead of running it.
set -uo pipefail

MODE="${1:-all}"
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$DIR/../.." && pwd)"
APP="$ROOT/reddit-intg/soljet-postiz-intg"
cd "$ROOT"

# WSL: node loading the CLI's ~8k module files from /mnt/c (the Windows drive)
# takes minutes per devvit command. Run from a mirror on the Linux filesystem
# instead (seconds); the repo copy stays the source of truth.
SRC_APP="$APP"
case "$SRC_APP" in
  /mnt/*) APP="${REDDIT_DEVVIT_WORKDIR:-$HOME/.cache/soljet-postiz/devvit-app}" ;;
esac

# Same helper shape as ops/reflex/deploy.sh::envget.
envget(){ [ -f .env ] || return 0; grep -E "^$1=" .env 2>/dev/null | tail -1 | cut -d= -f2- \
          | sed -e 's/^"\(.*\)"$/\1/' -e "s/^'\(.*\)'\$/\1/" -e 's/\r$//' -e 's/[[:space:]]*$//'; }

POSTER_KEY="${REDDIT_POSTER_KEY:-$(envget REDDIT_POSTER_KEY)}"

# Every product's REDDIT_SUBREDDIT, r/-stripped, lowercased, de-duplicated.
subreddits(){
  find products -name '*tier.config' -exec grep -hE '^REDDIT_SUBREDDIT=' {} + 2>/dev/null \
    | cut -d= -f2- | sed -e 's/[[:space:]]*#.*$//' -e 's/^"\(.*\)"$/\1/' -e "s/^'\(.*\)'\$/\1/" \
    | tr -d ' \t\r' | sed 's#^[rR]/##' | tr '[:upper:]' '[:lower:]' | grep -v '^$' | sort -u
}

red(){ printf '\033[31m%s\033[0m\n' "$*"; }
grn(){ printf '\033[32m%s\033[0m\n' "$*"; }
run(){ if [ -n "${DRY:-}" ]; then echo "+ (cd $APP && $*)"; else (cd "$APP" && "$@"); fi; }

SYNCED=""
need_node(){
  command -v node >/dev/null || { red "node not found (need >=22; .nvmrc pins 24)"; exit 1; }
  [ -n "$SYNCED" ] && return 0
  if [ "$APP" != "$SRC_APP" ]; then
    if [ -n "${DRY:-}" ]; then echo "+ rsync $SRC_APP/ -> $APP/ (Linux-FS mirror)"
    else
      mkdir -p "$APP" && rsync -a --delete --exclude node_modules --exclude dist "$SRC_APP"/ "$APP"/ \
        || { red "couldn't mirror the app to $APP"; exit 1; }
    fi
  fi
  # (Re)install when deps are missing or package-lock.json changed since last install.
  if [ ! -x "$APP/node_modules/.bin/devvit" ] || ! cmp -s "$APP/package-lock.json" "$APP/node_modules/.deploy-lock"; then
    run npm ci --no-audit --no-fund || { red "npm ci failed in $APP"; exit 1; }
    [ -n "${DRY:-}" ] || cp "$APP/package-lock.json" "$APP/node_modules/.deploy-lock"
  fi
  SYNCED=1
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
  if [ -z "$POSTER_KEY" ]; then
    POSTER_KEY="$(openssl rand -hex 32)"
    if [ -n "${DRY:-}" ]; then echo "+ append generated REDDIT_POSTER_KEY to .env"
    else printf '\nREDDIT_POSTER_KEY=%s\n' "$POSTER_KEY" >> .env; grn "generated REDDIT_POSTER_KEY -> .env"; fi
  fi
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

do_allow(){
  need_node; need_login
  local allowed
  allowed="$(subreddits | paste -sd, -)"
  if [ -z "$allowed" ]; then
    red "every product's REDDIT_SUBREDDIT is empty (products/**/*tier.config) — nothing would be allowed"
    exit 1
  fi
  if [ -n "${DRY:-}" ]; then echo "+ (cd $APP && echo '$allowed' | npx devvit settings set allowedSubreddits)"; return 0; fi
  # The CLI only prompts for the value; feed it on stdin, never on argv.
  if (cd "$APP" && printf '%s\n' "$allowed" | npx devvit settings set allowedSubreddits >/dev/null 2>&1); then
    grn "allowedSubreddits set: $allowed"
  else
    red "couldn't set allowedSubreddits non-interactively — run it by hand and paste: $allowed"
    echo "    (cd $APP && npx devvit settings set allowedSubreddits)"
    exit 1
  fi
}

do_install(){
  need_node; need_login
  local sub rc=0 subs
  subs="$(subreddits)"
  [ -n "$subs" ] || { red "no product has REDDIT_SUBREDDIT set (products/**/*tier.config)"; exit 1; }
  for sub in $subs; do
    # Re-running on an installed sub upgrades it to the latest version.
    if run npx devvit install "$sub"; then grn "installed on r/$sub"; else red "install failed on r/$sub"; rc=1; fi
  done
  return $rc
}

case "$MODE" in
  --upload-only)  do_upload ;;
  --key-only)     do_key ;;
  --allow-only)   do_allow ;;
  --install-only) do_allow && do_install ;;
  --devvit-only|all)
    do_upload && do_key && do_allow && do_install || exit 1
    if [ "$MODE" = all ]; then echo "note: Cloud Run reddit-poster isn't built yet — Devvit app only."; fi
    ;;
  *) red "unknown part: $MODE"; exit 2 ;;
esac
