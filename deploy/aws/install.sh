#!/usr/bin/env bash
# install.sh — finishes Arc demo deploy AFTER the repo is cloned.
#
# Run as the `ubuntu` user from the repo root:
#   cd ~/arc
#   bash deploy/aws/install.sh demo.blackarcsystems.com my_agent
#
# Args:
#   $1  domain to serve (e.g. demo.blackarcsystems.com)
#   $2+ agent names to start (default: my_agent)

set -euo pipefail

DOMAIN="${1:-}"
shift || true
AGENTS=("$@")
if [ ${#AGENTS[@]} -eq 0 ]; then
  AGENTS=("my_agent")
fi

if [ -z "$DOMAIN" ]; then
  echo "✗ usage: bash deploy/aws/install.sh <domain> [agent_name ...]" >&2
  exit 1
fi

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$REPO_ROOT"

echo "→ Installing system packages"
sudo DEBIAN_FRONTEND=noninteractive apt-get update -qq
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq \
  python3.11 python3.11-venv python3.11-dev \
  build-essential pkg-config libssl-dev \
  git curl ca-certificates gnupg debian-keyring debian-archive-keyring apt-transport-https

echo "→ Installing Caddy"
if ! command -v caddy >/dev/null; then
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' \
    | sudo gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' \
    | sudo tee /etc/apt/sources.list.d/caddy-stable.list >/dev/null
  sudo apt-get update -qq
  sudo apt-get install -y -qq caddy
fi

echo "→ Installing uv"
if ! command -v uv >/dev/null; then
  curl -LsSf https://astral.sh/uv/install.sh | sh
  export PATH="$HOME/.local/bin:$PATH"
fi

echo "→ Syncing Python deps (uv sync)"
uv sync

if [ ! -x "$REPO_ROOT/.venv/bin/arc" ]; then
  echo "✗ .venv/bin/arc missing after uv sync — bailing." >&2
  exit 1
fi

echo "→ Checking .env"
if [ ! -f "$REPO_ROOT/.env" ]; then
  cat >"$REPO_ROOT/.env" <<'EOF'
# Fill in at least one provider key the demo agent needs.
ANTHROPIC_API_KEY=
OPENAI_API_KEY=
EOF
  chmod 600 "$REPO_ROOT/.env"
  echo "✗ Wrote stub $REPO_ROOT/.env — edit it (nano $REPO_ROOT/.env) then re-run this script." >&2
  exit 1
fi
if ! grep -qE '^[A-Z_]+=[^[:space:]]+' "$REPO_ROOT/.env"; then
  echo "✗ $REPO_ROOT/.env has no non-empty keys. Edit it then re-run." >&2
  exit 1
fi
chmod 600 "$REPO_ROOT/.env"

echo "→ Pre-warming stable tokens"
mkdir -p "$HOME/.arcagent"
if [ ! -f "$HOME/.arcagent/arc-stack.tokens" ]; then
  v="$(openssl rand -hex 32)"
  o="$(openssl rand -hex 32)"
  a="$(openssl rand -hex 32)"
  printf 'VIEWER_TOKEN=%s\nOPERATOR_TOKEN=%s\nAGENT_TOKEN=%s\n' "$v" "$o" "$a" \
    >"$HOME/.arcagent/arc-stack.tokens"
  chmod 600 "$HOME/.arcagent/arc-stack.tokens"
fi

echo "→ Restricting which agents arc-stack starts"
# arc-stack.sh starts every <name>_agent under TEAM_ROOT when no args
# are passed. We don't want all 7 — disable the ones we aren't running
# by renaming their dirs (cheap, reversible).
for d in "$REPO_ROOT/team"/*_agent; do
  [ -d "$d" ] || continue
  name="$(basename "$d" _agent)_agent"
  keep=0
  for want in "${AGENTS[@]}"; do
    if [ "$name" = "$want" ]; then keep=1; break; fi
  done
  if [ "$keep" -eq 0 ]; then
    mv "$d" "${d}.disabled" 2>/dev/null || true
  fi
done

echo "→ Installing systemd unit"
sudo install -m 0644 "$REPO_ROOT/deploy/aws/arc-stack.service" /etc/systemd/system/arc-stack.service
sudo systemctl daemon-reload
sudo systemctl enable arc-stack.service

echo "→ Installing Caddyfile for $DOMAIN"
sudo mkdir -p /var/log/caddy
sudo chown caddy:caddy /var/log/caddy
sed "s/DEMO_DOMAIN/${DOMAIN}/g" "$REPO_ROOT/deploy/aws/Caddyfile" \
  | sudo tee /etc/caddy/Caddyfile >/dev/null
sudo systemctl enable caddy
sudo systemctl restart caddy

echo "→ Starting arc-stack"
sudo systemctl restart arc-stack.service
sleep 8

echo
echo "── Status ──"
sudo systemctl --no-pager --lines=0 status arc-stack.service || true
echo

# shellcheck disable=SC1091
. "$HOME/.arcagent/arc-stack.tokens"
echo "──────────────────────────────────────────────"
echo " Demo URL:"
echo "   https://${DOMAIN}/#auth=${VIEWER_TOKEN}"
echo "──────────────────────────────────────────────"
echo " Logs:"
echo "   journalctl -u arc-stack -f"
echo "   tail -f $REPO_ROOT/.arc-logs/ui.log"
echo "   sudo tail -f /var/log/caddy/arc-access.log"
echo "──────────────────────────────────────────────"
