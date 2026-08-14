#!/usr/bin/env bash
# scripts/deploy-node.sh — single-command bootstrap for a fresh Arc node.
#
# Automates docs/deploy/single-node.md: uv sync, nats-server + platform
# adapter install, arc init, config overlays, agent create, and a
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

# --- 2. uv sync ----------------------------------------------------------
log "uv sync..."
"$UV" sync
VENV_PY="$REPO_ROOT/.venv/bin/python"
ARC_BIN="$REPO_ROOT/.venv/bin/arc"
ok "venv synced"

# --- 3. nats-server (not a Python dep — arcteam auto-spawns it, needs PATH) --
# Delegated to scripts/install-nats.sh, which install.sh also calls. The
# checksum verification lives in exactly one place on purpose: a second copy
# of a verification routine is the copy that quietly stops verifying.
"$REPO_ROOT/scripts/install-nats.sh"

# --- 4. platform adapter plugin (telegram) -------------------------------
# arcgateway-telegram is a uv workspace member but, as of this writing, NOT
# declared in root pyproject.toml's [project.dependencies] — `uv sync`
# neither installs it nor keeps a manually pip-installed copy (it actively
# UNINSTALLS one on the next `uv sync`, confirmed against the DGX deploy).
# Detect whether the root dependency has landed; if so `uv sync` alone
# already handled it and this whole step is a no-op.
if [ "$ENABLE_TELEGRAM" = "1" ]; then
  if grep -q '"arcgateway-telegram"' pyproject.toml; then
    ok "arcgateway-telegram is a declared root dependency — uv sync already installed it"
  elif "$VENV_PY" -c "import arcgateway_telegram" >/dev/null 2>&1; then
    ok "arcgateway-telegram already importable"
  else
    log "Installing arcgateway-telegram (workspace member, not yet a root dependency — see comment above)..."
    "$VENV_PY" -m pip install -e packages/arcgateway-telegram --no-deps
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

mkdir -p "$ARC_CONFIG_DIR"
ARC_ENV="$ARC_CONFIG_DIR/arc.env"
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
if [ -f "$ARC_CONFIG_DIR/gateway.toml" ]; then
  ok "arc init already run — leaving ~/.arc/*.toml as-is"
else
  log "arc init --tier $TIER --provider $PROVIDER..."
  "$ARC_BIN" init --tier "$TIER" --provider "$PROVIDER"
fi

# --- 7. config overlays (idempotent — safe to re-run every time) -----------
log "Applying user-wide config overlays..."
"$VENV_PY" scripts/deploy_node_overlays.py agent-config \
  "$ARC_CONFIG_DIR/arcagent.toml" --provider "$PROVIDER" --model "${AGENT_MODEL#*/}"

GATEWAY_ARGS=(gateway-config "$ARC_CONFIG_DIR/gateway.toml")
if [ "$ENABLE_TELEGRAM" = "1" ]; then
  GATEWAY_ARGS+=(--enable-telegram)
  if [ -n "$TELEGRAM_ALLOWED_USER_IDS" ]; then
    # shellcheck disable=SC2206
    IDS=($TELEGRAM_ALLOWED_USER_IDS)
    GATEWAY_ARGS+=(--allowed-user-ids "${IDS[@]}")
  fi
fi
"$VENV_PY" scripts/deploy_node_overlays.py "${GATEWAY_ARGS[@]}"

# --- 8. agent create — one or more, first one wins gateway routing --------
mkdir -p team
for AGENT_NAME in "${AGENT_NAMES[@]}"; do
  if [ -d "team/$AGENT_NAME" ]; then
    ok "team/$AGENT_NAME already exists"
  else
    log "Creating agent $AGENT_NAME ($AGENT_MODEL)..."
    "$ARC_BIN" agent create "$AGENT_NAME" --dir team --model "$AGENT_MODEL"
  fi
  "$VENV_PY" scripts/deploy_node_overlays.py agent-config \
    "team/$AGENT_NAME/arcagent.toml" --provider "$PROVIDER" --model "${AGENT_MODEL#*/}"
  "$ARC_BIN" agent build "team/$AGENT_NAME" --check
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
' "team/$PRIMARY_AGENT/arcagent.toml")"
[ -n "$AGENT_DID" ] || fail "could not read minted DID from team/$PRIMARY_AGENT/arcagent.toml"
"$VENV_PY" scripts/deploy_node_overlays.py gateway-config \
  "$ARC_CONFIG_DIR/gateway.toml" --agent-did "$AGENT_DID"
ok "agent_did wired into gateway.toml: $AGENT_DID ($PRIMARY_AGENT)"

# --- 8b. modules ----------------------------------------------------------
# Modules do NOT ship in the wheel. Each is a separately signed bundle that
# `arc install` verifies and materializes under $ARC_CONFIG_DIR/modules. Skip
# this and every agent boots, answers chat, and looks healthy while its
# scheduler never fires and its tasks never dispatch — nothing crashes, so
# nothing is noticed. Idempotent, and non-zero when a config asks for a
# capability this box cannot deliver, so a hollow node never reaches systemd.
log "Installing modules for every agent..."
"$ARC_BIN" install --team-root "$REPO_ROOT/team" \
  || fail "arc install could not deliver every module the configs enable (see above)"

# --- 9. systemd user unit -------------------------------------------------
UNIT_DIR="$HOME/.config/systemd/user"
mkdir -p "$UNIT_DIR"
if [ "$UI_PORT" != "8420" ]; then
  sed "s/--port 8420/--port $UI_PORT/" "$REPO_ROOT/deploy/systemd/arc.service" \
    > "$UNIT_DIR/arc.service"
else
  cp "$REPO_ROOT/deploy/systemd/arc.service" "$UNIT_DIR/arc.service"
fi
ok "wrote $UNIT_DIR/arc.service"

systemctl --user daemon-reload
systemctl --user enable --now arc.service
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
