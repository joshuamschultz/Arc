#!/usr/bin/env bash
# =============================================================================
# Attach the arc-internal overlay onto a fresh `arc` checkout.
#
# The internal repo (specs, ADRs, steering, brainstorms, .claude knowledge)
# shares its worktree with the public `arc` repo but uses a SEPARATE git dir
# (`.git-internal`) so the two never collide. The public `.gitignore` already
# hides every internal path, so nothing internal ever leaks into `arc`.
#
# Usage (run from anywhere inside your `arc` clone):
#     bash .claude/internal-repo/attach.sh
#
# You need read access to git@github.com:joshuamschultz/arc-internal.git
# =============================================================================
set -euo pipefail

REMOTE="git@github.com:joshuamschultz/arc-internal.git"

ROOT="$(git rev-parse --show-toplevel)"
GITDIR="$ROOT/.git-internal"

echo "Repo root: $ROOT"

# 1. Global alias so `git internal <cmd>` drives the overlay from anywhere
#    inside the arc repo (resolves the root dynamically — machine-agnostic).
if ! git config --global --get alias.internal >/dev/null 2>&1; then
  git config --global alias.internal \
    '!git --git-dir="$(git rev-parse --show-toplevel)/.git-internal" --work-tree="$(git rev-parse --show-toplevel)"'
  echo "Added global git alias: 'git internal'"
else
  echo "Global 'git internal' alias already present."
fi

# 2. Initialise the overlay git dir if it isn't there yet.
if [ ! -d "$GITDIR" ]; then
  git --git-dir="$GITDIR" init -b main >/dev/null
  git --git-dir="$GITDIR" config core.bare false
  git --git-dir="$GITDIR" config core.worktree "$ROOT"
  git --git-dir="$GITDIR" remote add origin "$REMOTE"
  echo "Initialised $GITDIR"
else
  echo "$GITDIR already exists — reusing."
fi

# 3. Fetch and check out the internal files in place (develop = day-to-day line).
git --git-dir="$GITDIR" --work-tree="$ROOT" fetch origin
git --git-dir="$GITDIR" --work-tree="$ROOT" checkout -f develop
git --git-dir="$GITDIR" --work-tree="$ROOT" branch --set-upstream-to=origin/develop develop >/dev/null 2>&1 || true

echo ""
echo "Done. The internal knowledge is now in place."
echo "Drive it with:  git internal status | git internal add -f <file> | git internal commit | git internal push"
