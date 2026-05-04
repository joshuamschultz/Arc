#!/usr/bin/env bash
# SPEC-023 Phase 11 — demo tunnel.
#
# Starts arcui (web chat enabled), ttyd (terminal fallback), and
# cloudflared on the cluster's named tunnel. Public URLs print to
# stderr at the end so the operator can copy them into the demo.
#
# Required:
#   ARC_OPERATOR_TOKEN — the operator-tier viewer token. ttyd uses
#                        it for HTTP basic auth.
#
# Optional:
#   ARC_TUNNEL_NAME    — cloudflared tunnel name (default: arc-demo).
#   ARC_UI_PORT        — bind port for arcui (default: 8765).
#   ARC_TTYD_PORT      — bind port for ttyd (default: 7681).
#   ARC_GATEWAY_CONFIG — path to gateway.toml (default: ~/.arc/gateway.toml).
#
# Cloudflared route mapping is operator-managed via
# ~/.cloudflared/config.yml. Suggested entries:
#   - hostname: demo.blackarcsystems.com → http://localhost:8765
#   - hostname: term.blackarcsystems.com → http://localhost:7681
set -euo pipefail

if [[ -z "${ARC_OPERATOR_TOKEN:-}" ]]; then
  echo "demo-tunnel.sh: ARC_OPERATOR_TOKEN must be set" >&2
  exit 1
fi

UI_PORT="${ARC_UI_PORT:-8765}"
TTYD_PORT="${ARC_TTYD_PORT:-7681}"
TUNNEL_NAME="${ARC_TUNNEL_NAME:-arc-demo}"
GATEWAY_CONFIG="${ARC_GATEWAY_CONFIG:-$HOME/.arc/gateway.toml}"

if ! command -v ttyd >/dev/null 2>&1; then
  echo "demo-tunnel.sh: ttyd not found in PATH (https://github.com/tsl0922/ttyd)" >&2
  exit 1
fi
if ! command -v cloudflared >/dev/null 2>&1; then
  echo "demo-tunnel.sh: cloudflared not found in PATH" >&2
  exit 1
fi

# Track child PIDs so trap can clean them up on Ctrl-C.
PIDS=()
cleanup() {
  echo "demo-tunnel.sh: shutting down children…" >&2
  for pid in "${PIDS[@]}"; do
    kill "$pid" 2>/dev/null || true
  done
}
trap cleanup EXIT INT TERM

# 1. arcui — production web chat.
arc ui start --config "$GATEWAY_CONFIG" --port "$UI_PORT" >/tmp/arc-ui.log 2>&1 &
PIDS+=($!)
echo "demo-tunnel.sh: arcui started (pid=${PIDS[-1]}, port=$UI_PORT)" >&2

# 2. ttyd — terminal fallback if the chat UI hits trouble live.
ttyd \
  --port "$TTYD_PORT" \
  --interface 127.0.0.1 \
  --credential "arc:$ARC_OPERATOR_TOKEN" \
  --writable \
  bash >/tmp/arc-ttyd.log 2>&1 &
PIDS+=($!)
echo "demo-tunnel.sh: ttyd started (pid=${PIDS[-1]}, port=$TTYD_PORT)" >&2

# 3. cloudflared — public ingress on the operator-configured tunnel.
cloudflared tunnel run "$TUNNEL_NAME" >/tmp/arc-cloudflared.log 2>&1 &
PIDS+=($!)
echo "demo-tunnel.sh: cloudflared started (pid=${PIDS[-1]}, tunnel=$TUNNEL_NAME)" >&2

cat <<EOF >&2

✅ Demo tunnel up.

Public URLs (from ~/.cloudflared/config.yml):
  - Dashboard:     https://demo.blackarcsystems.com
  - Terminal:      https://term.blackarcsystems.com (basic auth: arc:\$ARC_OPERATOR_TOKEN)

Logs:
  - arcui:         /tmp/arc-ui.log
  - ttyd:          /tmp/arc-ttyd.log
  - cloudflared:   /tmp/arc-cloudflared.log

Ctrl-C to stop all three processes.
EOF

# Wait on all background jobs so the trap fires correctly on signal.
wait
