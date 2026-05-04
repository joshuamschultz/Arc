#!/usr/bin/env bash
# Install the SCAP extension from this repo into ~/.arc/capabilities/scap/
# and ~/.arc/skills/scap/ — the dev-mode install path per D-372.
#
# This is a one-way sync: repo → live.  After implementation work in
# ~/.arc/, run scripts/sync-scap-extension.sh to push changes back into
# the repo before committing.
#
# Usage: scripts/install-scap-extension.sh [--dry-run]

set -euo pipefail

DRY_RUN=""
if [[ "${1:-}" == "--dry-run" ]]; then
  DRY_RUN="--dry-run"
fi

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SRC_CAP="$REPO_ROOT/demo-extensions/scap/"
SRC_SKILL="$REPO_ROOT/demo-extensions/skill-scap/"
DST_CAP="$HOME/.arc/capabilities/scap/"
DST_SKILL="$HOME/.arc/skills/scap/"

mkdir -p "$DST_CAP" "$DST_SKILL"

echo "Installing SCAP extension:"
echo "  $SRC_CAP   →   $DST_CAP"
rsync -a $DRY_RUN --delete \
  --exclude '__pycache__' --exclude '*.pyc' \
  "$SRC_CAP" "$DST_CAP"

echo "  $SRC_SKILL →   $DST_SKILL"
rsync -a $DRY_RUN --delete \
  --exclude '__pycache__' --exclude '*.pyc' \
  "$SRC_SKILL" "$DST_SKILL"

if [[ -z "$DRY_RUN" ]]; then
  echo "Done. Extension installed."
fi
