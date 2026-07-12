#!/usr/bin/env bash
# Safely tear down a worktree everywhere: verify its branch is fully merged into
# origin/main, then remove (1) the worktree, (2) the local branch, (3) the
# remote branch. Refuses if the branch has commits not yet in origin/main, so
# you can't delete unmerged work by accident.
#
# Usage: make worktree-clean <name>          (name = dir under .claude/worktrees/)
#        make worktree-clean <name> FORCE=1  (skip the merged check)
set -uo pipefail

NAME="${1:-}"
FORCE="${2:-}"

# --- no name: "return to main" — the partner of `make ship` -----------------
# After a ship/<stamp> PR merges, put HEAD back on an up-to-date main and delete
# the branch you were on. Refuses if the branch isn't merged (unless FORCE=1) or
# the tree is dirty, so no work is lost.
if [ -z "$NAME" ]; then
  cur="$(git rev-parse --abbrev-ref HEAD)"
  if [ "$cur" = "main" ]; then
    echo "[return-to-main] already on main — updating"; git pull --ff-only 2>/dev/null || true
    exit 0
  fi
  if ! git diff --quiet || ! git diff --cached --quiet; then
    echo "  ✗ REFUSING: uncommitted changes on $cur — commit or stash first"; exit 1
  fi
  git fetch origin --quiet || true
  if [ "${FORCE#FORCE=}" = "1" ]; then
    echo "  FORCE set — skipping merged check"
  elif git merge-base --is-ancestor "$cur" origin/main; then
    echo "  ✓ $cur is merged into origin/main"
  else
    ahead="$(git rev-list --count origin/main.."$cur" 2>/dev/null || echo '?')"
    echo "  ✗ REFUSING: $cur has $ahead commit(s) not in origin/main."
    echo "    Merge its PR first (squash-merges look unmerged here — use FORCE=1 then)."
    exit 1
  fi
  git checkout main || exit 1
  git pull --ff-only 2>/dev/null || true
  git rev-parse --verify "$cur" >/dev/null 2>&1 && git branch -D "$cur" && echo "  deleted local branch $cur"
  if git ls-remote --exit-code --heads origin "$cur" >/dev/null 2>&1; then
    git push origin --delete "$cur" && echo "  deleted remote branch $cur"
  fi
  echo "[return-to-main] on $(git rev-parse --abbrev-ref HEAD), up to date."
  exit 0
fi

# Always operate from the MAIN working tree, never from inside the target worktree.
MAIN="$(git worktree list --porcelain | awk '/^worktree /{print $2; exit}')"
cd "$MAIN" || { echo "can't locate main worktree"; exit 1; }

WT_DIR=".claude/worktrees/$NAME"
BRANCH="worktree-$NAME"
# If the worktree exists, trust the branch it actually has checked out.
if [ -d "$WT_DIR" ]; then
  b="$(git -C "$WT_DIR" rev-parse --abbrev-ref HEAD 2>/dev/null || true)"
  [ -n "${b:-}" ] && [ "$b" != "HEAD" ] && BRANCH="$b"
fi
echo "[worktree-clean] dir=$WT_DIR branch=$BRANCH"

# --- Safety: branch must be fully merged into origin/main -------------------
git fetch origin --quiet || true
if [ "${FORCE#FORCE=}" = "1" ]; then
  echo "  FORCE set — skipping merged check"
elif ! git rev-parse --verify "$BRANCH" >/dev/null 2>&1; then
  echo "  local branch $BRANCH not found — will just clean up dir/remote"
elif git merge-base --is-ancestor "$BRANCH" origin/main; then
  echo "  ✓ $BRANCH is fully merged into origin/main"
else
  ahead="$(git rev-list --count origin/main.."$BRANCH" 2>/dev/null || echo '?')"
  echo "  ✗ REFUSING: $BRANCH has $ahead commit(s) not in origin/main."
  echo "    Merge its PR first (squash-merges look unmerged here — use FORCE=1 then)."
  exit 1
fi

# --- 1. worktree dir + registration -----------------------------------------
if [ -d "$WT_DIR" ]; then
  git worktree remove "$WT_DIR" && echo "  removed worktree $WT_DIR"
fi
git worktree prune

# --- 2. local branch --------------------------------------------------------
if git rev-parse --verify "$BRANCH" >/dev/null 2>&1; then
  git branch -D "$BRANCH" && echo "  deleted local branch $BRANCH"
fi

# --- 3. remote branch -------------------------------------------------------
if git ls-remote --exit-code --heads origin "$BRANCH" >/dev/null 2>&1; then
  git push origin --delete "$BRANCH" && echo "  deleted remote branch $BRANCH"
else
  echo "  remote branch $BRANCH already gone"
fi
echo "[worktree-clean] done."
