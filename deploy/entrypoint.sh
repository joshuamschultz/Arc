#!/usr/bin/env bash
# Container bootstrap for Arc — the containerized form of
# scripts/deploy-node.sh steps 5-8, minus everything systemd owned.
#
# Idempotent: every step checks before acting, so restarting a container or
# re-pulling a newer image against an existing /data volume preserves the
# agent, its memory, its identity keys, and its access tokens.
#
# Env:
#   <PROVIDER>_API_KEY              required for cloud providers — fails closed if absent
#   ARC_AGENTS                      space-separated agent names (default: arc_agent)
#   ARC_AGENT_MODEL                 default: anthropic/claude-sonnet-5
#   ARC_PROVIDER                    default: anthropic
#   ARC_BLUEPRINT                   optional — applied to each agent at creation
#   ARC_TIER                        default: personal
#   ARC_UI_PORT                     default: 8420
#   ARC_ENABLE_TELEGRAM             default: 0
#   ARC_TELEGRAM_ALLOWED_USER_IDS   space-separated Telegram user ids (empty = deny all)
#   TELEGRAM_BOT_TOKEN              required when ARC_ENABLE_TELEGRAM=1
#   VIEWER_TOKEN / OPERATOR_TOKEN   optional — minted here when a provisioner
#                                   has not already handed them to the customer

set -euo pipefail

REPO_ROOT=/opt/arc
ARC_BIN="$REPO_ROOT/.venv/bin/arc"
VENV_PY="$REPO_ROOT/.venv/bin/python"
OVERLAYS="$REPO_ROOT/scripts/deploy_node_overlays.py"

ARC_CONFIG_DIR="${ARC_CONFIG_DIR:-$HOME/.arc}"
TEAM_ROOT="${ARC_TEAM_ROOT:-$HOME/team}"
AGENT_MODEL="${ARC_AGENT_MODEL:-anthropic/claude-sonnet-5}"
PROVIDER="${ARC_PROVIDER:-anthropic}"
TIER="${ARC_TIER:-personal}"
UI_PORT="${ARC_UI_PORT:-8420}"
ENABLE_TELEGRAM="${ARC_ENABLE_TELEGRAM:-0}"
TELEGRAM_ALLOWED_USER_IDS="${ARC_TELEGRAM_ALLOWED_USER_IDS:-}"
BLUEPRINT="${ARC_BLUEPRINT:-}"
read -r -a AGENT_NAMES <<< "${ARC_AGENTS:-arc_agent}"

log()  { echo "→ $*"; }
ok()   { echo "  ✓ $*"; }
warn() { echo "  ! $*" >&2; }
fail() { echo "  ✗ $*" >&2; exit 1; }

# --- 1. preflight: degrade loudly, never silently -------------------------
# A missing embedder makes semantic recall and consolidation dedup a no-op
# that looks exactly like "working" from the outside. Say so at startup.
if "$VENV_PY" -c "import sentence_transformers" >/dev/null 2>&1; then
  ok "local embedder present (all-MiniLM-L6-v2)"
else
  warn "sentence-transformers MISSING — semantic recall and memory dedup will degrade to keyword-only"
fi
if "$VENV_PY" -c "
import sys
from arcmemory.db import sqlite_vec_loadable
sys.exit(0 if sqlite_vec_loadable() else 1)
" >/dev/null 2>&1; then
  ok "sqlite-vec loadable (vector recall enabled)"
else
  warn "sqlite-vec NOT loadable in this Python build — recall falls back to BM25 + graph"
fi

# --- 2. secrets: fail closed before anything is written -------------------
# Which key is required follows from the provider. `arc init` owns that mapping;
# reading it here keeps one source of truth and lets an unknown provider fail
# with its own name rather than a misleading complaint about Anthropic.
KEY_VAR="$("$VENV_PY" -c '
import sys
from arccli.commands.init import PROVIDER_ENV_VARS
provider = sys.argv[1]
if provider not in PROVIDER_ENV_VARS:
    sys.exit(2)
print(PROVIDER_ENV_VARS[provider])
' "$PROVIDER")" || fail "ARC_PROVIDER=$PROVIDER is not a provider Arc knows"
# Local providers (ollama, lmstudio) map to an empty var name — no key needed.
if [ -n "$KEY_VAR" ] && [ -z "${!KEY_VAR:-}" ]; then
  fail "$KEY_VAR is not set — required for ARC_PROVIDER=$PROVIDER; pass it via --env-file or -e"
fi
if [ "$ENABLE_TELEGRAM" = "1" ] && [ -z "${TELEGRAM_BOT_TOKEN:-}" ]; then
  fail "ARC_ENABLE_TELEGRAM=1 but TELEGRAM_BOT_TOKEN is not set"
fi

mkdir -p "$ARC_CONFIG_DIR" "$TEAM_ROOT"
ARC_ENV="$ARC_CONFIG_DIR/arc.env"

# Tokens are generated exactly once and pinned for the life of the volume.
# The UI derives its (agent, user) chat-session id from the viewer token, so
# regenerating on restart strands every prior conversation. A provisioner may
# supply both up front, which is what lets it show a customer the dashboard
# link at checkout instead of waiting for the box to finish booting.
mint() { "$VENV_PY" -c 'import secrets; print(secrets.token_hex(32))'; }
if [ -f "$ARC_ENV" ]; then
  ok "$ARC_ENV present — viewer/operator tokens stay pinned"
else
  log "Writing viewer/operator tokens..."
  ( umask 077
    {
      printf 'VIEWER_TOKEN=%s\n' "${VIEWER_TOKEN:-$(mint)}"
      printf 'OPERATOR_TOKEN=%s\n' "${OPERATOR_TOKEN:-$(mint)}"
    } > "$ARC_ENV"
  )
  chmod 600 "$ARC_ENV"
  ok "$ARC_ENV written (0600)"
fi
set -a
# shellcheck disable=SC1090
. "$ARC_ENV"
set +a

# --- 3. arc init ----------------------------------------------------------
if [ -f "$ARC_CONFIG_DIR/gateway.toml" ]; then
  ok "arc init already run — leaving $ARC_CONFIG_DIR/*.toml as-is"
else
  log "arc init --tier $TIER --provider $PROVIDER..."
  "$ARC_BIN" init --tier "$TIER" --provider "$PROVIDER"
fi

# --- 4. config overlays (idempotent) --------------------------------------
log "Applying config overlays..."
"$VENV_PY" "$OVERLAYS" agent-config \
  "$ARC_CONFIG_DIR/arcagent.toml" --provider "$PROVIDER" --model "${AGENT_MODEL#*/}"

GATEWAY_ARGS=(gateway-config "$ARC_CONFIG_DIR/gateway.toml")
if [ "$ENABLE_TELEGRAM" = "1" ]; then
  GATEWAY_ARGS+=(--enable-telegram)
  if [ -n "$TELEGRAM_ALLOWED_USER_IDS" ]; then
    read -r -a TELEGRAM_IDS <<< "$TELEGRAM_ALLOWED_USER_IDS"
    GATEWAY_ARGS+=(--allowed-user-ids "${TELEGRAM_IDS[@]}")
  fi
fi
"$VENV_PY" "$OVERLAYS" "${GATEWAY_ARGS[@]}"

# --- 5. agents ------------------------------------------------------------
for AGENT_NAME in "${AGENT_NAMES[@]}"; do
  if [ -d "$TEAM_ROOT/$AGENT_NAME" ]; then
    ok "$TEAM_ROOT/$AGENT_NAME already exists"
  else
    log "Creating agent $AGENT_NAME ($AGENT_MODEL, tier=$TIER)..."
    "$ARC_BIN" agent create "$AGENT_NAME" --dir "$TEAM_ROOT" --model "$AGENT_MODEL" --tier "$TIER"
    # Blueprints are materialized only at creation. Re-applying on every restart
    # would overwrite the persona, prompt overlays, and schedules the operator
    # has tuned since — the whole point of "pick a blueprint, then make it yours".
    if [ -n "$BLUEPRINT" ]; then
      log "Applying blueprint $BLUEPRINT to $AGENT_NAME..."
      "$ARC_BIN" blueprint apply "$BLUEPRINT" --agent "$TEAM_ROOT/$AGENT_NAME"
    fi
  fi
  "$VENV_PY" "$OVERLAYS" agent-config \
    "$TEAM_ROOT/$AGENT_NAME/arcagent.toml" --provider "$PROVIDER" --model "${AGENT_MODEL#*/}"
  "$ARC_BIN" agent build "$TEAM_ROOT/$AGENT_NAME" --check
done

# gateway.toml routes remote-platform DMs to exactly one agent_did; the first
# name in ARC_AGENTS wins.
PRIMARY_AGENT="${AGENT_NAMES[0]}"
AGENT_DID="$("$VENV_PY" -c '
import sys, tomllib
with open(sys.argv[1], "rb") as f:
    print(tomllib.load(f).get("identity", {}).get("did", ""))
' "$TEAM_ROOT/$PRIMARY_AGENT/arcagent.toml")"
[ -n "$AGENT_DID" ] || fail "could not read minted DID from $TEAM_ROOT/$PRIMARY_AGENT/arcagent.toml"
"$VENV_PY" "$OVERLAYS" gateway-config "$ARC_CONFIG_DIR/gateway.toml" --agent-did "$AGENT_DID"
ok "agent_did wired into gateway.toml: $AGENT_DID ($PRIMARY_AGENT)"

# --- 6. serve -------------------------------------------------------------
echo
echo "=== Arc is starting ==="
echo "  Agents:    ${AGENT_NAMES[*]}"
echo "  Dashboard: http://<host>:$UI_PORT/#auth=$VIEWER_TOKEN"
echo

exec "$ARC_BIN" ui start \
  --host 0.0.0.0 \
  --port "$UI_PORT" \
  --team-root "$TEAM_ROOT" \
  --gateway-config "$ARC_CONFIG_DIR/gateway.toml" \
  --no-browser \
  --viewer-token "$VIEWER_TOKEN" \
  --operator-token "$OPERATOR_TOKEN"
