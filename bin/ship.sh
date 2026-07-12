#!/usr/bin/env bash
# Ship changes as a PR without hand-managing branch context.
#
# Two modes, mirroring worktree-clean's positional-name pattern:
#
#   make ship m="message"      No name: from main, cut a fresh ship/<stamp>
#                              branch off HEAD, commit the working changes onto
#                              it, push, open a PR — and leave you sitting on
#                              that branch. (Already on a branch? Use it as-is.)
#
#   make ship <name>           Name given: push + open a PR for the EXISTING
#                              branch <name> (accepts a worktree name too →
#                              worktree-<name>). No commit; it's already made.
#
# After the PR merges, `make worktree-clean` (no name) returns you to an
# up-to-date main and deletes the branch.
#
# SHIP_DRYRUN=1 echoes the push/PR commands instead of running them.
set -uo pipefail

NAME="${1:-}"
MSG="${2:-}"

run() {  # execute, or just print under SHIP_DRYRUN
  if [ "${SHIP_DRYRUN:-}" = "1" ]; then echo "  [dry-run] $*"; else "$@"; fi
}

open_pr() { run gh pr create --fill --base main --head "$1"; }

# ---- mode 1: ship an existing named branch --------------------------------
if [ -n "$NAME" ]; then
  BRANCH="$NAME"
  if ! git rev-parse --verify "$BRANCH" >/dev/null 2>&1 \
     && git rev-parse --verify "worktree-$NAME" >/dev/null 2>&1; then
    BRANCH="worktree-$NAME"    # accept the short worktree name
  fi
  git rev-parse --verify "$BRANCH" >/dev/null 2>&1 \
    || { echo "ship: no such branch '$NAME' (nor worktree-$NAME)"; exit 2; }
  if [ "$BRANCH" = "main" ]; then echo "ship: refusing to ship main"; exit 1; fi
  echo "[ship] existing branch $BRANCH → push + PR"
  run git push -u origin "$BRANCH"
  open_pr "$BRANCH"
  exit $?
fi

# ---- mode 2: no name — cut ship/<stamp> from HEAD, commit, push, PR --------
cur="$(git rev-parse --abbrev-ref HEAD)"
if [ "$cur" = "main" ]; then
  BRANCH="ship/$(date +%Y%m%d-%H%M%S)"
  run git checkout -b "$BRANCH" || { echo "ship: could not create $BRANCH"; exit 1; }
  echo "[ship] cut $BRANCH from main HEAD"
else
  BRANCH="$cur"                 # already on a branch — ship that one
  echo "[ship] on branch $BRANCH"
fi

# Commit working changes if there are any (needs a message).
if ! git diff --quiet || ! git diff --cached --quiet; then
  [ -n "$MSG" ] || { echo 'ship: uncommitted changes — pass m="message"'; exit 2; }
  run git add -A
  run git commit -m "$MSG"
elif git merge-base --is-ancestor HEAD origin/main 2>/dev/null; then
  echo "ship: nothing to ship — HEAD already in origin/main"; exit 2
fi

run git push -u origin "$BRANCH"
open_pr "$BRANCH"
echo "[ship] done — you are on $BRANCH while the PR is open."
