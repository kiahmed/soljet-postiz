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

# Bring local `main` in line with origin/main. A genuinely divergent local
# main (someone committed straight to it outside the worktree/PR flow — this
# happened once already: `git pull` afterward refused with "divergent
# branches, need to specify how to reconcile") is a decision only a human
# should make, so this only auto-handles the unambiguous cases: already even,
# or a plain fast-forward. Prints what it did/found either way so a stale
# main is never silently left stale.
sync_main() {
  git fetch origin --quiet || true
  local local_head origin_head
  local_head="$(git rev-parse main 2>/dev/null || true)"
  origin_head="$(git rev-parse origin/main 2>/dev/null || true)"
  [ -z "$origin_head" ] && return 0          # no remote reachable — nothing to sync
  if [ "$local_head" = "$origin_head" ]; then
    echo "  main: already up to date with origin/main"
    return 0
  fi
  if git merge-base --is-ancestor main origin/main 2>/dev/null; then
    if [ "$(git rev-parse --abbrev-ref HEAD)" = "main" ]; then
      git merge --ff-only origin/main --quiet && echo "  main: fast-forwarded to origin/main"
    else
      echo "  ⚠ main is behind origin/main but isn't checked out here — pull it yourself"
    fi
  elif git merge-base --is-ancestor origin/main main 2>/dev/null; then
    echo "  main: already ahead of origin/main (local commits not yet pushed) — nothing to pull"
  else
    echo "  ✗ main has DIVERGED from origin/main — needs a human to reconcile"
    echo "    (a local commit exists that origin doesn't, alongside origin's own new commits —"
    echo "     someone committed straight to main outside the worktree/PR flow)"
    echo "    From the main checkout, pick one:"
    echo "      git pull --no-rebase   # merge — preserves both histories (recommended default)"
    echo "      git pull --rebase      # replay local commits on top of origin's"
    return 1
  fi
}

# --- no name: "return to main" — the partner of `make ship` -----------------
# After a ship/<stamp> PR merges, put HEAD back on an up-to-date main and delete
# the branch you were on. Refuses if the branch isn't merged (unless FORCE=1) or
# the tree is dirty, so no work is lost.
if [ -z "$NAME" ]; then
  cur="$(git rev-parse --abbrev-ref HEAD)"
  if [ "$cur" = "main" ]; then
    echo "[return-to-main] already on main — updating"
    sync_main || exit 1
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
  sync_main || exit 1
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

# Keep local main from going stale here too — the merged-check below already
# fetches and compares against origin/main regardless, so this isn't needed
# for correctness, but skipping it is exactly how you end up running `git
# pull` yourself right after and hitting a divergence this script could have
# caught. Non-fatal: the worktree/branch teardown below is independent of
# main's state, so a genuine divergence here warns but doesn't block it.
sync_main || true

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
  # A worktree created by Claude Code's EnterWorktree is LOCKED by default —
  # plain `git worktree remove` refuses on a locked worktree (distinct from
  # its separate refusal on uncommitted changes, which the merged-check above
  # already ruled out) and git's own error names the exact fix: `-f -f`.
  # Unlock explicitly first (harmless no-op if it was never locked) rather
  # than relying on double --force, so the reason is visible in the log.
  # Getting this wrong is exactly how this used to fail SILENTLY: the plain
  # `remove` call above errored out, the script kept going (no `set -e`),
  # and `git branch -D` two steps down then failed too with "used by
  # worktree" — while the remote-branch step after IT still succeeded and
  # printed "done.", making a half-finished cleanup look complete.
  git worktree unlock "$WT_DIR" 2>/dev/null || true
  git worktree remove --force "$WT_DIR" && echo "  removed worktree $WT_DIR" \
    || { echo "  ✗ could not remove worktree $WT_DIR — a session may still be using it"; exit 1; }
fi
git worktree prune

# --- 2. local branch --------------------------------------------------------
if git rev-parse --verify "$BRANCH" >/dev/null 2>&1; then
  git branch -D "$BRANCH" && echo "  deleted local branch $BRANCH"
fi

# --- 3. remote branch -------------------------------------------------------
# Best-effort: some shells (an unattended Claude Code session, say) have no
# git credential helper for a write, even though `ls-remote` (read) above
# worked fine unauthenticated. Capture stderr instead of letting a raw
# `fatal: could not read Username...` leak out looking like a real failure —
# it isn't one; the worktree + local branch are already gone either way, and
# GitHub auto-deletes the remote branch on merge in the normal case anyway.
if git ls-remote --exit-code --heads origin "$BRANCH" >/dev/null 2>&1; then
  if push_err="$(git push origin --delete "$BRANCH" 2>&1 >/dev/null)"; then
    echo "  deleted remote branch $BRANCH"
  elif echo "$push_err" | grep -qiE 'could not read username|permission denied|authentication failed|unable to access|403'; then
    echo "  remote branch $BRANCH left behind — no push credentials in this shell."
    echo "    Run this from a shell that can push: git push origin --delete $BRANCH"
  else
    echo "  ✗ could not delete remote branch $BRANCH: $push_err"
  fi
else
  echo "  remote branch $BRANCH already gone"
fi
echo "[worktree-clean] done."
