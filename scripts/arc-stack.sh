#!/usr/bin/env bash
# arc-stack.sh — clean start/stop/status of ArcUI + agents.
#
# Order matters: ArcUI must answer /api/health BEFORE any agent is
# started. The agent's ui_reporter runs a one-shot probe at startup
# (packages/arcagent/src/arcagent/modules/ui_reporter/_runtime.py).
# If the probe fails, the agent runs UI-blind for its whole lifetime —
# no retry, no reconnect to the dashboard, no operator messaging.
#
# Usage:
#   scripts/arc-stack.sh start [agent_name ...]
#   scripts/arc-stack.sh stop
#   scripts/arc-stack.sh restart [agent_name ...]
#   scripts/arc-stack.sh status
#
# With no agent names, "start" / "restart" launches every <name>_agent
# directory found under TEAM_ROOT.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TEAM_ROOT="${ARC_TEAM_ROOT:-$REPO_ROOT/team}"
LOG_DIR="${ARC_LOG_DIR:-$REPO_ROOT/.arc-logs}"
PID_DIR="$LOG_DIR/pids"
UI_PORT="${ARC_UI_PORT:-8420}"
UI_HEALTH="http://127.0.0.1:${UI_PORT}/api/health"
HEALTH_TIMEOUT_S="${ARC_HEALTH_TIMEOUT_S:-20}"
AGENT_PROBE_TIMEOUT_S="${ARC_AGENT_PROBE_TIMEOUT_S:-15}"

mkdir -p "$LOG_DIR" "$PID_DIR"

# Stable token file. arcgateway derives the viewer DID by hashing the
# viewer token (packages/arcgateway/src/arcgateway/identity.py:25). The
# chat history filename is sha256(agent_did + user_did)[:16]. So if the
# viewer token rotates between restarts, the (agent, user) pair maps to
# a NEW chat_id, and yesterday's <workspace>/sessions/<old_chat_id>.jsonl
# is stranded — the file still exists, but no UI session ever asks for
# it again. Pinning the tokens here is the difference between
# "Slack-style persistent history" and "history disappears every restart".
TOKENS_FILE="${ARC_TOKENS_FILE:-$HOME/.arcagent/arc-stack.tokens}"
mkdir -p "$(dirname "$TOKENS_FILE")"

ensure_stable_tokens() {
  if [ -f "$TOKENS_FILE" ]; then
    return 0
  fi
  echo "→ Generating stable UI tokens (first run): $TOKENS_FILE"
  local v o a
  v="$(openssl rand -hex 32)"
  o="$(openssl rand -hex 32)"
  a="$(openssl rand -hex 32)"
  # umask 077 ensures the file is born at 0600 — no chmod race.
  ( umask 077
    printf 'VIEWER_TOKEN=%s\nOPERATOR_TOKEN=%s\nAGENT_TOKEN=%s\n' \
      "$v" "$o" "$a" >"$TOKENS_FILE"
  )
}

load_stable_tokens() {
  ensure_stable_tokens
  # shellcheck disable=SC1090
  . "$TOKENS_FILE"
  if [ -z "${VIEWER_TOKEN:-}" ] || [ -z "${OPERATOR_TOKEN:-}" ] || [ -z "${AGENT_TOKEN:-}" ]; then
    echo "✗ $TOKENS_FILE is malformed (missing VIEWER/OPERATOR/AGENT)."
    exit 1
  fi
}

# Resolve `arc` once. We can't rely on `command -v arc` — it's a shell
# alias in this project, not a PATH entry, and nohup execs the literal
# command name without alias expansion. Lookup order:
#   1. ARC_BIN env override
#   2. `type -p arc` (real PATH binary, ignores aliases)
#   3. $REPO_ROOT/.venv/bin/arc (project default — the alias target)
ARC_BIN="${ARC_BIN:-}"
if [ -z "$ARC_BIN" ]; then
  ARC_BIN="$(type -p arc 2>/dev/null || true)"
fi
if [ -z "$ARC_BIN" ] && [ -x "$REPO_ROOT/.venv/bin/arc" ]; then
  ARC_BIN="$REPO_ROOT/.venv/bin/arc"
fi
if [ -z "$ARC_BIN" ] || [ ! -x "$ARC_BIN" ]; then
  echo "✗ Could not locate the 'arc' executable."
  echo "  Tried: type -p arc, $REPO_ROOT/.venv/bin/arc"
  echo "  Set ARC_BIN to its absolute path and re-run."
  exit 1
fi
echo "  arc binary: $ARC_BIN"

discover_agents() {
  local d
  for d in "$TEAM_ROOT"/*_agent; do
    [ -d "$d" ] || continue
    basename "$d"
  done
}

stop_all() {
  echo "→ Stopping any running arc processes..."
  pkill -TERM -f "arc agent serve" 2>/dev/null || true
  pkill -TERM -f "arc ui start"    2>/dev/null || true

  # Agent shutdown can take a few seconds (close WS, flush audit chain).
  # Poll up to STOP_GRACE_S, then SIGKILL anything still alive.
  local grace="${ARC_STOP_GRACE_S:-8}"
  local deadline=$((SECONDS + grace))
  while (( SECONDS < deadline )); do
    if ! pgrep -f "arc agent serve" >/dev/null \
       && ! pgrep -f "arc ui start"  >/dev/null; then
      break
    fi
    sleep 0.3
  done
  pkill -KILL -f "arc agent serve" 2>/dev/null || true
  pkill -KILL -f "arc ui start"    2>/dev/null || true

  # Belt-and-suspenders: if the UI port is still bound, drop the listener.
  if lsof -nP -iTCP:"$UI_PORT" -sTCP:LISTEN >/dev/null 2>&1; then
    lsof -nP -iTCP:"$UI_PORT" -sTCP:LISTEN -t \
      | xargs -r kill -9 2>/dev/null || true
  fi
}

start_ui() {
  load_stable_tokens
  echo "→ Starting ArcUI (team-root=$TEAM_ROOT, port=$UI_PORT)"
  nohup "$ARC_BIN" ui start \
    --team-root  "$TEAM_ROOT" \
    --port       "$UI_PORT" \
    --viewer-token   "$VIEWER_TOKEN" \
    --operator-token "$OPERATOR_TOKEN" \
    --agent-token    "$AGENT_TOKEN" \
    --no-browser \
    >"$LOG_DIR/ui.log" 2>&1 &
  echo $! >"$PID_DIR/ui.pid"
}

# With pinned tokens, the bootstrap URL is stable — bookmark it.
bootstrap_url() {
  if [ -f "$TOKENS_FILE" ]; then
    # shellcheck disable=SC1090
    . "$TOKENS_FILE"
    echo "http://127.0.0.1:${UI_PORT}/#auth=${VIEWER_TOKEN}"
  else
    echo "http://127.0.0.1:${UI_PORT}/"
  fi
}

wait_for_ui() {
  local deadline=$((SECONDS + HEALTH_TIMEOUT_S))
  while (( SECONDS < deadline )); do
    if curl -sSf "$UI_HEALTH" >/dev/null 2>&1; then
      echo "✓ ArcUI healthy at $UI_HEALTH"
      return 0
    fi
    sleep 0.5
  done
  echo "✗ ArcUI did not become healthy within ${HEALTH_TIMEOUT_S}s"
  echo "  Tail of $LOG_DIR/ui.log:"
  tail -n 20 "$LOG_DIR/ui.log" | sed 's/^/    /'
  return 1
}

register_agents() {
  # Idempotent: already-registered is a no-op.
  #
  # The Agent Fleet page in arcui reads from $TEAM_ROOT/shared/messages/registry/.
  # `arc team register` writes there ONLY when --root points at $TEAM_ROOT/shared.
  # The default root resolves to ~/.arc/team/, which arcui never reads — so a
  # default-root register silently succeeds but the agent never appears in the
  # fleet. That's exactly the bug that left every prior deploy with an empty
  # dashboard.
  #
  # Entity ID format must be agent://<name> (the URI scheme arcui's registry
  # adapter expects). Display name comes from [agent].name in arcagent.toml,
  # falling back to the dir name.
  local a entity_id name reg_root
  reg_root="$TEAM_ROOT/shared"
  for a in "$@"; do
    [ -d "$TEAM_ROOT/$a" ] || continue
    entity_id="agent://$a"

    if "$ARC_BIN" team --root "$reg_root" entities 2>/dev/null \
         | awk 'NR>2 {print $1}' | grep -qx "$entity_id"; then
      echo "  ✓ already registered: $entity_id"
      continue
    fi

    name=$(awk -F'=' '
      /^\[/ { in_agent = ($0 == "[agent]") }
      in_agent && /^[[:space:]]*name[[:space:]]*=/ {
        gsub(/^[[:space:]"'\'']+|[[:space:]"'\'']+$/, "", $2); print $2; exit
      }
    ' "$TEAM_ROOT/$a/arcagent.toml" 2>/dev/null)
    : "${name:=$a}"

    echo "→ Registering $entity_id ($name)"
    if ! "$ARC_BIN" team --root "$reg_root" register "$entity_id" \
        --name "$name" --type agent --roles executor \
        --workspace "$TEAM_ROOT/$a/workspace"; then
      echo "  ✗ registration failed for $a — aborting startup."
      return 1
    fi
  done
}

start_agents() {
  local a
  for a in "$@"; do
    if [ ! -d "$TEAM_ROOT/$a" ]; then
      echo "  ! skipping $a (no $TEAM_ROOT/$a)"
      continue
    fi
    echo "→ Starting agent: $a"
    : >"$LOG_DIR/agent-$a.log"   # truncate so probe-result scan is fresh
    nohup "$ARC_BIN" agent serve "$TEAM_ROOT/$a" \
      >"$LOG_DIR/agent-$a.log" 2>&1 &
    echo $! >"$PID_DIR/agent-$a.pid"
  done
}

# Scan each agent's log for the probe result. Distinguishes four states
# so the operator knows what to do:
#   ok        — ui_reporter handshake succeeded (visible on dashboard)
#   probe     — UI probe ran but failed (UI unreachable from this agent)
#   crashed   — process exited during startup (config / key / vault error)
#   stalled   — alive but never emitted a ui_reporter line (likely no
#               [modules.ui_reporter] section in arcagent.toml, OR a
#               long boot path)
# Returns the count of agents not in state `ok`.
wait_for_agents() {
  local fails=0 a
  for a in "$@"; do
    [ -d "$TEAM_ROOT/$a" ] || continue
    local log="$LOG_DIR/agent-$a.log"
    local pid; pid="$(cat "$PID_DIR/agent-$a.pid" 2>/dev/null || true)"
    local deadline=$((SECONDS + AGENT_PROBE_TIMEOUT_S))
    local result=""
    while (( SECONDS < deadline )); do
      if grep -q "ui_reporter: connected"      "$log" 2>/dev/null; then
        result="ok"; break
      fi
      if grep -q "ui_reporter: not connecting" "$log" 2>/dev/null; then
        result="probe"; break
      fi
      # Process gone before any probe line landed = crashed during boot.
      if [ -n "$pid" ] && ! kill -0 "$pid" 2>/dev/null; then
        result="crashed"; break
      fi
      sleep 0.3
    done
    case "$result" in
      ok)
        echo "  ✓ $a connected to UI"
        ;;
      probe)
        echo "  ✗ $a UI probe failed (agent ran but couldn't reach UI):"
        grep "ui_reporter: not connecting" "$log" | tail -n 1 \
          | sed 's/^/      /'
        fails=$((fails+1))
        ;;
      crashed)
        echo "  ✗ $a crashed during startup:"
        # Last non-blank line is almost always the actionable error
        # ('arc: error in agent: ...').
        grep -v '^[[:space:]]*$' "$log" | tail -n 3 | sed 's/^/      /'
        fails=$((fails+1))
        ;;
      *)
        echo "  ! $a alive but no ui_reporter line in ${AGENT_PROBE_TIMEOUT_S}s"
        if ! grep -q "ui_reporter" "$log" 2>/dev/null; then
          echo "      (no [modules.ui_reporter] in $TEAM_ROOT/$a/arcagent.toml?)"
        fi
        fails=$((fails+1))
        ;;
    esac
  done
  return "$fails"
}

print_status() {
  echo
  echo "=== UI ==="
  if curl -sSf "$UI_HEALTH" >/dev/null 2>&1; then
    echo "  health   : OK  ($UI_HEALTH)"
  else
    echo "  health   : DOWN"
  fi
  if [ -f "$HOME/.arcagent/ui-token" ]; then
    echo "  token    : $(head -c 8 "$HOME/.arcagent/ui-token")…  (~/.arcagent/ui-token)"
  fi
  echo "  dashboard: $(bootstrap_url)"
  echo "    ^ stable URL (tokens pinned in $TOKENS_FILE) — bookmark it once"
  echo
  echo "=== Agents (arc agent serve) ==="
  if pgrep -f "arc agent serve" >/dev/null 2>&1; then
    pgrep -lf "arc agent serve" | sed 's/^/  /'
  else
    echo "  (none)"
  fi
  echo
  echo "Logs: $LOG_DIR/"
}

cmd="${1:-start}"; shift || true
agents=("$@")
if [ ${#agents[@]} -eq 0 ]; then
  mapfile -t agents < <(discover_agents)
fi

case "$cmd" in
  stop)
    stop_all
    echo "✓ stopped"
    ;;
  status)
    print_status
    ;;
  start|restart)
    stop_all
    start_ui
    wait_for_ui
    register_agents "${agents[@]}"
    start_agents    "${agents[@]}"
    if wait_for_agents "${agents[@]}"; then
      echo
      echo "✓ all agents connected"
    else
      echo
      echo "! some agents failed to connect to the UI"
      print_status
      exit 1
    fi
    print_status
    ;;
  *)
    echo "usage: $0 {start|stop|restart|status} [agent_name ...]"
    exit 2
    ;;
esac
