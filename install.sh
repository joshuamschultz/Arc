#!/usr/bin/env bash
# install.sh — one command from a fresh clone to ready-to-start.
#
#   git clone https://github.com/joshuamschultz/Arc.git && cd Arc
#   ./install.sh analyst        # or ./install.sh with agents already in team/
#   arc up
#
# This is a shell script for exactly one reason: it has to create the Python
# environment that provides the `arc` binary. A process cannot rebuild the
# environment it is running inside, so the bootstrap cannot itself be an `arc`
# subcommand. Everything AFTER the environment exists is `arc install`, which
# this script ends by calling — there is no second implementation of any of it.
#
# Finds its own tooling. `uv` and `nats-server` are looked for in ~/.local/bin
# and the other usual install directories before $PATH is trusted, because the
# PATH a non-interactive `ssh host './install.sh'` gets is not the PATH of a
# login shell — and that difference alone has blocked a real deploy.
#
# Idempotent at every step: an existing uv, broker, config, agent, or module is
# left exactly as it is. Re-running after a partial failure is the normal case.
#
# Env overrides:
#   ARC_TIER        personal | enterprise | federal   (default: personal)
#   ARC_PROVIDER    default LLM provider              (default: anthropic)
#   ARC_MODEL       model for agents created here     (default: anthropic/claude-sonnet-5)
#   ARC_CONFIG_DIR  config root                       (default: ~/.arc)
#
# For a full node — systemd unit, secrets, Telegram/Slack — use
# scripts/deploy-node.sh, which does everything here plus the service wiring.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$REPO_ROOT"

TIER="${ARC_TIER:-personal}"
PROVIDER="${ARC_PROVIDER:-anthropic}"
MODEL="${ARC_MODEL:-anthropic/claude-sonnet-5}"
ARC_CONFIG_DIR="${ARC_CONFIG_DIR:-$HOME/.arc}"
AGENT_NAMES=("$@")

log()  { echo "→ $*"; }
ok()   { echo "  ✓ $*"; }
fail() { echo "  ✗ $*" >&2; exit 1; }

# --- 1. uv ----------------------------------------------------------------
# ~/.local/bin first: that is where the official installer puts it, and it is
# the directory most likely to be missing from a non-interactive PATH.
if [ -x "$HOME/.local/bin/uv" ]; then
  UV="$HOME/.local/bin/uv"
elif command -v uv >/dev/null 2>&1; then
  UV="$(command -v uv)"
elif [ -x /usr/local/bin/uv ]; then
  UV=/usr/local/bin/uv
elif [ -x /opt/homebrew/bin/uv ]; then
  UV=/opt/homebrew/bin/uv
else
  log "Installing uv..."
  curl -LsSf https://astral.sh/uv/install.sh | sh
  UV="$HOME/.local/bin/uv"
fi
[ -x "$UV" ] || fail "uv is still not executable at $UV"
ok "uv: $("$UV" --version)"

# --- 2. dependencies ------------------------------------------------------
# A bare `uv sync` is complete on purpose: the workspace root pins arcui,
# arcmemory[local], arcskill, and arcgateway[telegram] explicitly, so no
# deployment needs to remember a flag to get a working stack.
log "Syncing dependencies (this is the slow step)..."
"$UV" sync
ARC_BIN="$REPO_ROOT/.venv/bin/arc"
[ -x "$ARC_BIN" ] || fail "uv sync finished but $ARC_BIN does not exist"
ok "venv synced"

# --- 3. nats-server -------------------------------------------------------
"$REPO_ROOT/scripts/install-nats.sh"

# --- 4. config + operator key --------------------------------------------
# `arc init` prompts before overwriting, so it is only run when there is
# nothing to overwrite. It is what mints the operator key that every module
# signature on this box is verified against.
if [ -f "$ARC_CONFIG_DIR/arcagent.toml" ]; then
  ok "config already present in $ARC_CONFIG_DIR"
else
  log "Writing config ($TIER tier, $PROVIDER)..."
  "$ARC_BIN" init --tier "$TIER" --provider "$PROVIDER" >/dev/null
  ok "config written to $ARC_CONFIG_DIR"
fi

# --- 5. agents ------------------------------------------------------------
mkdir -p "$REPO_ROOT/team"
for AGENT_NAME in ${AGENT_NAMES[@]+"${AGENT_NAMES[@]}"}; do
  if [ -d "$REPO_ROOT/team/$AGENT_NAME" ]; then
    ok "team/$AGENT_NAME already exists"
  else
    log "Creating agent $AGENT_NAME ($MODEL)..."
    "$ARC_BIN" agent create "$AGENT_NAME" --dir team --model "$MODEL" --tier "$TIER" >/dev/null
    ok "team/$AGENT_NAME created"
  fi
done

# Naming a fleet is a decision only the operator can make, so this reports the
# one command rather than inventing an agent nobody asked for.
if [ -z "$(ls -A "$REPO_ROOT/team" 2>/dev/null)" ]; then
  echo
  echo "No agents yet. Create one and re-run:"
  echo "    ./install.sh <name>"
  exit 1
fi

# --- 6. modules -----------------------------------------------------------
# Everything from here is `arc install`: it builds and signs a bundle for every
# module the fleet's configs enable, installs them for every agent, and exits
# non-zero if a capability a config asked for could not be delivered.
echo
"$ARC_BIN" install --team-root "$REPO_ROOT/team"

echo
echo "=== Ready ==="
echo "  Start it with:   $ARC_BIN up --team-root $REPO_ROOT/team"
echo "  Set an API key:  $ARC_BIN keys set $PROVIDER"
