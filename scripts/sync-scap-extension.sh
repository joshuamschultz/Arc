#!/usr/bin/env bash
# Pull SCAP extension changes from ~/.arc/ back into the repo before commit.
# This is the inverse of install-scap-extension.sh — live → repo.
#
# Usage: scripts/sync-scap-extension.sh [--dry-run]

set -euo pipefail

DRY_RUN=""
if [[ "${1:-}" == "--dry-run" ]]; then
  DRY_RUN="--dry-run"
fi

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SRC_CAP="$HOME/.arc/capabilities/scap/"
SRC_SKILL="$HOME/.arc/skills/scap/"
DST_CAP="$REPO_ROOT/demo-extensions/scap/"
DST_SKILL="$REPO_ROOT/demo-extensions/skill-scap/"

mkdir -p "$DST_CAP" "$DST_SKILL"

echo "Syncing SCAP extension back to repo:"
echo "  $SRC_CAP   →   $DST_CAP"
rsync -a $DRY_RUN --delete \
  --exclude '__pycache__' --exclude '*.pyc' \
  --exclude 'data/sanitize_map.toml' \
  "$SRC_CAP" "$DST_CAP"

echo "  $SRC_SKILL →   $DST_SKILL"
rsync -a $DRY_RUN --delete \
  --exclude '__pycache__' --exclude '*.pyc' \
  "$SRC_SKILL" "$DST_SKILL"

if [[ -z "$DRY_RUN" ]]; then
  echo "Done. Run 'git status' to review changes."
fi
