#!/usr/bin/env bash
# scripts/deploy-node.sh — single-command bootstrap for a fresh Arc node.
#
# Automates docs/runbooks/deploy/local.md: install the runtime, nats-server +
# platform adapter, arc init, config overlays, agent create, and a
# systemd --user unit running the embedded-gateway `arc ui start` pattern
# (SPEC-023) — one process, no standalone arcgateway daemon, no per-agent
# `arc agent serve`. See that doc for the architecture note on why this is
# NOT the same pattern as scripts/arc-stack.sh.
#
# Run this FROM the synced repo root on the target node (i.e. after
# `rsync ... host:~/arc/`, ssh in and run `~/arc/scripts/deploy-node.sh`).
# Idempotent — safe to re-run; every step checks before acting and never
# overwrites a value you set by hand unless you re-pass the matching flag.
#
# THE CHECKOUT IS NOT THE INSTALL. This script copies the checkout into
# ~/.arc/runtime/<version>/ and builds that copy's venv there, then flips the
# `current` symlink onto it. The checkout you ran this from is only a source
# tarball afterwards: nothing executes from it, nothing durable lives in it,
# and deleting it costs nothing. That is what makes an update an atomic symlink
# flip (`arc runtime activate <new>`) and a rollback the same verb with the
# previous version — and what keeps a `git pull` in the checkout away from the
# fleet, which lives at ~/arc/team beside it and is never executed from.
#
# Fails closed: aborts before touching systemd if a required secret is
# missing, naming exactly which one, rather than starting a service that
# will crash-loop.
#
# Usage:
#   scripts/deploy-node.sh [agent_name ...]
#   With no agent names, creates a single "josh_agent". Every named agent
#   gets the same model/eval/skills config — for per-agent personas, roles,
#   and team/channel setup see docs/deploy/team-building.md (a manual
#   follow-up step; not automated here since it's a one-time roster
#   decision, not a repeatable bootstrap action).
#
# Env overrides:
#   ARC_AGENT_MODEL               default: anthropic/claude-sonnet-5
#   ARC_PROVIDER                  default: anthropic
#   ARC_TIER                      default: personal
#   ARC_UI_PORT                   default: 8420
#   ARC_ENABLE_TELEGRAM           default: 0 (set 1 to install + wire the adapter)
#   ARC_TELEGRAM_ALLOWED_USER_IDS space-separated Telegram user ids (empty = deny all)
#   ARC_ENV_FILE                  source of ANTHROPIC_API_KEY / ARCAGENT_TELEGRAM_BOT_TOKEN
#                                  default: $REPO_ROOT/.env
#   ARC_RUNTIME_VERSION           name of the runtime/<version> dir to install into
#                                  default: <pyproject version>-<git short sha|UTC stamp>
#   ARC_TEAM_ROOT                 relocates the FLEET only (parent of team/)
#                                  default: $ARC_CONFIG_DIR — never the checkout
#
# KNOWN LIMITATION (not fixable from this script): `arc ui start` only
# accepts --viewer-token/--operator-token as CLI flags, no env var
# fallback (packages/arccli/commands/ui.py). The systemd unit's
# EnvironmentFile substitutes them into ExecStart, so both tokens are
# visible to any local user via `ps aux`/`systemctl status`. Fine on a
# single-user, network-gated host; not appropriate for a shared multi-user
# host as-is. Real fix needs `arc ui start` to read VIEWER_TOKEN/
# OPERATOR_TOKEN from the environment when the flags are omitted —
# tracked as a follow-up, out of scope for docs/deploy/scripts.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

AGENT_NAMES=("$@")
if [ ${#AGENT_NAMES[@]} -eq 0 ]; then
  AGENT_NAMES=("josh_agent")
fi

AGENT_MODEL="${ARC_AGENT_MODEL:-anthropic/claude-sonnet-5}"
PROVIDER="${ARC_PROVIDER:-anthropic}"
TIER="${ARC_TIER:-personal}"
UI_PORT="${ARC_UI_PORT:-8420}"
ENABLE_TELEGRAM="${ARC_ENABLE_TELEGRAM:-0}"
TELEGRAM_ALLOWED_USER_IDS="${ARC_TELEGRAM_ALLOWED_USER_IDS:-}"
ENV_FILE="${ARC_ENV_FILE:-$REPO_ROOT/.env}"
ARC_CONFIG_DIR="${ARC_CONFIG_DIR:-$HOME/.arc}"
export ARC_CONFIG_DIR

# arc_team() falls back to ARC_CONFIG_DIR before ~/arc, so exporting the line
# above is itself enough to move the fleet into the hidden home — every `arc`
# call below would answer ~/.arc/team while the unit serves ~/arc/team. Pin the
# fleet explicitly so the resolver and the unit cannot disagree.
ARC_TEAM_ROOT="${ARC_TEAM_ROOT:-$HOME/arc}"
export ARC_TEAM_ROOT

log()  { echo "→ $*"; }
ok()   { echo "  ✓ $*"; }
fail() { echo "  ✗ $*" >&2; exit 1; }

# --- 1. uv -------------------------------------------------------------
if [ -x "$HOME/.local/bin/uv" ]; then
  UV="$HOME/.local/bin/uv"
elif command -v uv >/dev/null 2>&1; then
  UV="$(command -v uv)"
else
  log "Installing uv..."
  curl -LsSf https://astral.sh/uv/install.sh | sh
  UV="$HOME/.local/bin/uv"
fi
ok "uv: $("$UV" --version)"
export PATH="$HOME/.local/bin:$PATH"

# --- 2. install the runtime beside its siblings, then flip `current` ------
# The version names a directory under ~/.arc/runtime/. It carries the commit so
# two deploys of different code are two installs you can flip between; a redeploy
# of the SAME commit lands in the same directory, which is what keeps re-running
# this script idempotent instead of accumulating identical trees.
PROJECT_VERSION="$(sed -n 's/^version = "\(.*\)"$/\1/p' "$REPO_ROOT/pyproject.toml" | head -1)"
[ -n "$PROJECT_VERSION" ] || fail "could not read version from $REPO_ROOT/pyproject.toml"
BUILD_STAMP="$(git -C "$REPO_ROOT" rev-parse --short HEAD 2>/dev/null || date -u +%Y%m%dT%H%M%SZ)"
RUNTIME_VERSION="${ARC_RUNTIME_VERSION:-$PROJECT_VERSION-$BUILD_STAMP}"
RUNTIME_DIR="$ARC_CONFIG_DIR/runtime/$RUNTIME_VERSION"

# The fleet directory name is the layout's, not this script's. Everything below
# that has to name it derives it here, so renaming the fleet can never leave a
# guard watching a directory that no longer exists.
FLEET_DIR="$(basename "${ARC_TEAM_ROOT:-$HOME/arc}/team")"

# GUARD 1 (before rsync, the only one that PREVENTS rather than reports).
# `rsync --delete` is about to run inside $RUNTIME_DIR, so nothing durable may
# sit under it. ARC_TEAM_ROOT is the only way a fleet could, and a fleet deleted
# here is agent memory, identity keys and workspaces gone with no copy anywhere.
case "${ARC_TEAM_ROOT:-}/" in
  "$ARC_CONFIG_DIR/runtime"/*)
    fail "ARC_TEAM_ROOT points inside $ARC_CONFIG_DIR/runtime — an install would delete the fleet" ;;
esac

log "Installing runtime $RUNTIME_VERSION into $RUNTIME_DIR..."
mkdir -p "$RUNTIME_DIR"
# --delete so a redeploy of the same version cannot leave a file the new code no
# longer ships. rsync does not delete an EXCLUDED destination path, which is what
# each exclude below is for: .venv is rebuilt in place (a copied venv has absolute
# paths baked into its shebangs), /modules holds bundles `arc install` already
# materialized into this runtime, and .env holds secrets that live in
# ~/.arc/config/arc.env instead. /modules is anchored so package-internal
# modules/ directories still ship.
#
# $FLEET_DIR is the load-bearing one: ~/arc is BOTH the rsync source and the
# fleet's parent, so without it every deploy rakes each agent's memory, identity
# keys, tools, skills and workspace into a runtime the next deploy deletes.
rsync -a --delete \
  --exclude '.git/' --exclude '.venv/' --exclude '/modules/' \
  --exclude 'team/' --exclude '.env' \
  --exclude '__pycache__/' --exclude '.pytest_cache/' --exclude '.ruff_cache/' \
  --exclude '.mypy_cache/' --exclude '.arc-logs/' --exclude 'dist/' \
  "$REPO_ROOT/" "$RUNTIME_DIR/"

# GUARD 2 (after rsync, before anything is activated). The exclusion above is one
# word in a long command. Check the OUTCOME instead of trusting the spelling: a
# dropped flag, a renamed flag and a mis-anchored pattern all land here, and the
# deploy stops with `current` still pointing at the runtime that was working.
[ ! -e "$RUNTIME_DIR/$FLEET_DIR" ] || fail \
  "the runtime install captured $RUNTIME_DIR/$FLEET_DIR — the rsync no longer excludes the fleet. Nothing was activated; delete that copy and restore the exclude."

log "uv sync (building $RUNTIME_DIR/.venv)..."
"$UV" sync --project "$RUNTIME_DIR"

# The flip is atomic: a reader sees the old runtime or the new one, never a gap.
# A pre-symlink install leaves a real `current/` directory here; activate_runtime
# moves it aside rather than deleting it, so its modules are recoverable.
"$RUNTIME_DIR/.venv/bin/arc" runtime activate "$RUNTIME_VERSION" \
  || fail "could not point $ARC_CONFIG_DIR/runtime/current at $RUNTIME_VERSION"

ARC_BIN="$ARC_CONFIG_DIR/runtime/current/.venv/bin/arc"
VENV_PY="$ARC_CONFIG_DIR/runtime/current/.venv/bin/python"
RUNTIME_ROOT="$ARC_CONFIG_DIR/runtime/current"
[ -x "$ARC_BIN" ] || fail "$ARC_BIN is not executable after activation"
ok "runtime $RUNTIME_VERSION active"

# --- 2b. split a flat home BEFORE any stage reads a config path -----------
# A deployment older than the lifecycle split has its TOML flat at ~/.arc. Every
# stage below resolves a config path under ~/.arc/config, and `arc init` CREATES
# what it does not find — so running it first would write a fresh config beside
# the real one, and the migration could then only refuse, because both are real.
# The fleet is never in scope here: it lives at ~/arc/team, outside this home.
log "Splitting the Arc home if it is still flat (idempotent)..."
"$ARC_BIN" install --migrate-only || fail "layout migration refused — see above; nothing was moved"

# The fleet root comes from the resolver, never from a literal here: a script
# that carries its own answer is how the deploy and the service unit came to
# disagree about which directory holds the agents, and starting from the wrong
# empty root loads ZERO agents while reporting healthy.
TEAM_ROOT="$("$VENV_PY" -c 'from arctrust.paths import arc_team; print(arc_team())')"
# Compare the FULL path, not the basename. Both sides spell the last component
# "team", so a basename check passes while the parent is wrong — which is exactly
# the failure this guard exists to catch, and it could not see it.
EXPECTED_TEAM_ROOT="${ARC_TEAM_ROOT:-$HOME/arc}/$FLEET_DIR"
[ "$TEAM_ROOT" = "$EXPECTED_TEAM_ROOT" ] || fail \
  "the resolver says the fleet is '$TEAM_ROOT' but this deploy targets '$EXPECTED_TEAM_ROOT' — \
the unit would serve one and agents would be created in the other"
ok "fleet root: $TEAM_ROOT"

# --- 3. nats-server (not a Python dep — arcteam auto-spawns it, needs PATH) --
# Delegated to scripts/install-nats.sh, which install.sh also calls. The
# checksum verification lives in exactly one place on purpose: a second copy
# of a verification routine is the copy that quietly stops verifying.
"$RUNTIME_ROOT/scripts/install-nats.sh"

# --- 4. platform adapter plugin (telegram) -------------------------------
# arcgateway-telegram is a uv workspace member but, as of this writing, NOT
# declared in root pyproject.toml's [project.dependencies] — `uv sync`
# neither installs it nor keeps a manually pip-installed copy (it actively
# UNINSTALLS one on the next `uv sync`, confirmed against the DGX deploy).
# Detect whether the root dependency has landed; if so `uv sync` alone
# already handled it and this whole step is a no-op.
if [ "$ENABLE_TELEGRAM" = "1" ]; then
  if grep -q '"arcgateway-telegram"' "$RUNTIME_ROOT/pyproject.toml"; then
    ok "arcgateway-telegram is a declared root dependency — uv sync already installed it"
  elif "$VENV_PY" -c "import arcgateway_telegram" >/dev/null 2>&1; then
    ok "arcgateway-telegram already importable"
  else
    log "Installing arcgateway-telegram (workspace member, not yet a root dependency — see comment above)..."
    "$VENV_PY" -m pip install -e "$RUNTIME_ROOT/packages/arcgateway-telegram" --no-deps
    ok "arcgateway-telegram installed"
  fi
fi

# --- 5. secrets: fail-closed if anything required is missing -------------
[ -f "$ENV_FILE" ] || fail "ARC_ENV_FILE not found: $ENV_FILE"
ANTHROPIC_API_KEY="$(grep -m1 '^ANTHROPIC_API_KEY=' "$ENV_FILE" | cut -d= -f2-)"
[ -n "$ANTHROPIC_API_KEY" ] || fail "ANTHROPIC_API_KEY missing from $ENV_FILE"

TELEGRAM_BOT_TOKEN=""
if [ "$ENABLE_TELEGRAM" = "1" ]; then
  TELEGRAM_BOT_TOKEN="$(grep -m1 '^ARCAGENT_TELEGRAM_BOT_TOKEN=' "$ENV_FILE" | cut -d= -f2-)"
  [ -n "$TELEGRAM_BOT_TOKEN" ] || fail "ARC_ENABLE_TELEGRAM=1 but ARCAGENT_TELEGRAM_BOT_TOKEN missing from $ENV_FILE"
fi

mkdir -p "$ARC_CONFIG_DIR/config"
ARC_ENV="$ARC_CONFIG_DIR/config/arc.env"
if [ -f "$ARC_ENV" ]; then
  ok "$ARC_ENV already present — leaving viewer/operator tokens pinned"
else
  log "Writing $ARC_ENV..."
  VIEWER_TOKEN="$(openssl rand -hex 32)"
  OPERATOR_TOKEN="$(openssl rand -hex 32)"
  ( umask 077
    {
      printf 'ANTHROPIC_API_KEY=%s\n' "$ANTHROPIC_API_KEY"
      [ -n "$TELEGRAM_BOT_TOKEN" ] && printf 'TELEGRAM_BOT_TOKEN=%s\n' "$TELEGRAM_BOT_TOKEN"
      printf 'VIEWER_TOKEN=%s\n' "$VIEWER_TOKEN"
      printf 'OPERATOR_TOKEN=%s\n' "$OPERATOR_TOKEN"
    } > "$ARC_ENV"
  )
  chmod 600 "$ARC_ENV"
  ok "$ARC_ENV written (0600)"
fi
set -a
# shellcheck disable=SC1090
. "$ARC_ENV"
set +a

# --- 6. arc init -----------------------------------------------------------
if [ -f "$ARC_CONFIG_DIR/config/gateway.toml" ]; then
  ok "arc init already run — leaving ~/.arc/config/*.toml as-is"
else
  log "arc init --tier $TIER --provider $PROVIDER..."
  "$ARC_BIN" init --tier "$TIER" --provider "$PROVIDER"
fi

# --- 7. config overlays (idempotent — safe to re-run every time) -----------
OVERLAYS="$RUNTIME_ROOT/scripts/deploy_node_overlays.py"
log "Applying user-wide config overlays..."
"$VENV_PY" "$OVERLAYS" agent-config \
  "$ARC_CONFIG_DIR/config/arcagent.toml" --provider "$PROVIDER" --model "${AGENT_MODEL#*/}"

GATEWAY_ARGS=(gateway-config "$ARC_CONFIG_DIR/config/gateway.toml")
if [ "$ENABLE_TELEGRAM" = "1" ]; then
  GATEWAY_ARGS+=(--enable-telegram)
  if [ -n "$TELEGRAM_ALLOWED_USER_IDS" ]; then
    # shellcheck disable=SC2206
    IDS=($TELEGRAM_ALLOWED_USER_IDS)
    GATEWAY_ARGS+=(--allowed-user-ids "${IDS[@]}")
  fi
fi
"$VENV_PY" "$OVERLAYS" "${GATEWAY_ARGS[@]}"

# --- 8. agent create — one or more, first one wins gateway routing --------
mkdir -p "$TEAM_ROOT"
for AGENT_NAME in "${AGENT_NAMES[@]}"; do
  if [ -d "$TEAM_ROOT/$AGENT_NAME" ]; then
    ok "$TEAM_ROOT/$AGENT_NAME already exists"
  else
    log "Creating agent $AGENT_NAME ($AGENT_MODEL)..."
    "$ARC_BIN" agent create "$AGENT_NAME" --dir "$TEAM_ROOT" --model "$AGENT_MODEL"
  fi
  "$VENV_PY" "$OVERLAYS" agent-config \
    "$TEAM_ROOT/$AGENT_NAME/arcagent.toml" --provider "$PROVIDER" --model "${AGENT_MODEL#*/}"
  "$ARC_BIN" agent build "$TEAM_ROOT/$AGENT_NAME" --check
done

# gateway.toml routes remote-platform DMs (Telegram etc.) to ONE agent_did.
# The first agent named on the command line wins; add more agents with
# docs/runbooks/operate/teams.md if you need a multi-agent roster with
# per-agent channels.
PRIMARY_AGENT="${AGENT_NAMES[0]}"
AGENT_DID="$("$VENV_PY" -c '
import sys, tomllib
with open(sys.argv[1], "rb") as f:
    print(tomllib.load(f).get("identity", {}).get("did", ""))
' "$TEAM_ROOT/$PRIMARY_AGENT/arcagent.toml")"
[ -n "$AGENT_DID" ] || fail "could not read minted DID from $TEAM_ROOT/$PRIMARY_AGENT/arcagent.toml"
"$VENV_PY" "$OVERLAYS" gateway-config \
  "$ARC_CONFIG_DIR/config/gateway.toml" --agent-did "$AGENT_DID"
ok "agent_did wired into gateway.toml: $AGENT_DID ($PRIMARY_AGENT)"

# --- 8b. modules ----------------------------------------------------------
# Modules do NOT ship in the wheel. Each is a separately signed bundle that
# `arc install` verifies and materializes under $ARC_CONFIG_DIR/runtime/current/modules. Skip
# this and every agent boots, answers chat, and looks healthy while its
# scheduler never fires and its tasks never dispatch — nothing crashes, so
# nothing is noticed. Idempotent, and non-zero when a config asks for a
# capability this box cannot deliver, so a hollow node never reaches systemd.
log "Installing modules for every agent..."
"$ARC_BIN" install --team-root "$TEAM_ROOT" \
  || fail "arc install could not deliver every module the configs enable (see above)"

# --- 9. systemd user unit -------------------------------------------------
UNIT_DIR="$HOME/.config/systemd/user"
mkdir -p "$UNIT_DIR"
if [ "$UI_PORT" != "8420" ]; then
  sed "s/--port 8420/--port $UI_PORT/" "$RUNTIME_ROOT/deploy/systemd/arc.service" \
    > "$UNIT_DIR/arc.service"
else
  cp "$RUNTIME_ROOT/deploy/systemd/arc.service" "$UNIT_DIR/arc.service"
fi
ok "wrote $UNIT_DIR/arc.service"

systemctl --user daemon-reload
systemctl --user enable arc.service
# `enable --now` STARTS a stopped unit but leaves a running one on its old
# runtime, so a redeploy would report success while the box still served the
# previous version. `restart` starts a stopped unit too, so it is correct for
# both a first deploy and an update.
systemctl --user restart arc.service
loginctl enable-linger "$USER" 2>/dev/null || echo "  ! enable-linger failed (may need sudo — service still runs while logged in)"

# --- 10. wait for health, print URL ---------------------------------------
log "Waiting for health check..."
for _ in $(seq 1 30); do
  if curl -sSf "http://127.0.0.1:$UI_PORT/api/health" >/dev/null 2>&1; then
    ok "healthy"
    break
  fi
  sleep 1
done

echo
echo "=== Arc is up ==="
echo "  Agents:    ${AGENT_NAMES[*]}"
echo "  Dashboard: http://<this-host>:$UI_PORT/#auth=$VIEWER_TOKEN"
echo "  (fragment form — the frontend reads window.location.hash, strips it after storing"
echo "   the token to localStorage; paste-token login also works if you open the bare URL)"
