#!/usr/bin/env bash
# ============================================================================
# Arc demo — VM setup
# ============================================================================
#
# Runs ON the Lightsail VM after `deploy.sh` provisioned the infra and you
# rsync'd the codebase to /home/ubuntu/arc/. Mirrors deploy/azure/setup-vm.sh.
#
# Usage:
#   bash ~/arc/deploy/aws/setup-vm.sh <domain> [agent_name ...]
#
# Args:
#   $1   domain to serve (e.g. demo.blackarcsystems.com)
#   $2+  agent names to keep enabled (default: my_agent)
#
# What it does:
#   1. Installs system deps (Python 3.11, build tools, Caddy)
#   2. Installs uv and runs `uv sync`
#   3. Verifies .env has at least one provider key set
#   4. Pre-warms ~/.arcagent/arc-stack.tokens (stable demo URL)
#   5. Disables agents you didn't pass (rename-based, reversible)
#   6. Installs systemd unit (arc-stack.service)
#   7. Installs Caddyfile with your domain, reloads Caddy (Let's Encrypt auto)
#   8. Starts arc-stack.service and prints the demo URL
#
# ============================================================================
set -euo pipefail

DOMAIN="${1:-}"
shift || true
AGENTS=("$@")
if [ ${#AGENTS[@]} -eq 0 ]; then
  AGENTS=("my_agent")
fi

if [ -z "${DOMAIN}" ]; then
  cat >&2 <<EOF
✗ usage: bash deploy/aws/setup-vm.sh <domain> [agent_name ...]
   e.g.   bash deploy/aws/setup-vm.sh demo.blackarcsystems.com my_agent
EOF
  exit 1
fi

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "${REPO_ROOT}"

echo "=== Arc Demo VM Setup ==="
echo "Repo:    ${REPO_ROOT}"
echo "Domain:  ${DOMAIN}"
echo "Agents:  ${AGENTS[*]}"
echo ""

# --- 1. System packages ---
echo "[1/8] Installing system packages..."
sudo DEBIAN_FRONTEND=noninteractive apt-get update -qq
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq \
  python3.11 python3.11-venv python3.11-dev \
  build-essential pkg-config libssl-dev libffi-dev \
  curl ca-certificates gnupg jq \
  debian-keyring debian-archive-keyring apt-transport-https

# --- 2. Caddy ---
echo "[2/8] Installing Caddy..."
if ! command -v caddy >/dev/null 2>&1; then
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' \
    | sudo gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' \
    | sudo tee /etc/apt/sources.list.d/caddy-stable.list >/dev/null
  sudo apt-get update -qq
  sudo apt-get install -y -qq caddy
fi

# --- 3. uv ---
echo "[3/8] Installing uv..."
if ! command -v uv >/dev/null 2>&1 && [ ! -x "${HOME}/.local/bin/uv" ]; then
  curl -LsSf https://astral.sh/uv/install.sh | sh
fi
export PATH="${HOME}/.local/bin:${PATH}"

# --- 4. Python deps ---
echo "[4/8] Building venv (uv sync — first run is 5-10 min on small_3_0)..."
uv sync
if [ ! -x "${REPO_ROOT}/.venv/bin/arc" ]; then
  echo "✗ ${REPO_ROOT}/.venv/bin/arc missing after uv sync — bailing." >&2
  exit 1
fi
# `uv sync` only installs packages declared as deps of the root project
# (arc-agent, arccmd, arcgateway). The remaining workspace members are
# siblings, not transitive deps, so `arc ui start` would fail with
# "No module named 'arcui'". Install every package/ subdir explicitly.
echo "  Installing remaining workspace packages..."
PACKAGES=()
for p in "${REPO_ROOT}/packages"/*/; do
  [ -f "${p}pyproject.toml" ] || continue
  PACKAGES+=(-e "${p%/}")
done
"${REPO_ROOT}/.venv/bin/pip" install --quiet "${PACKAGES[@]}"
"${REPO_ROOT}/.venv/bin/python" -c "import arcui" 2>/dev/null \
  || { echo "✗ arcui still not importable — bailing." >&2; exit 1; }

# --- 5. .env ---
echo "[5/8] Checking .env..."
if [ ! -f "${REPO_ROOT}/.env" ]; then
  cat >"${REPO_ROOT}/.env" <<'EOF'
# Fill in at least one provider key the demo agent needs.
# my_agent uses Anthropic; josh/brad/mosa/brian agents use Azure OpenAI.
ANTHROPIC_API_KEY=
OPENAI_API_KEY=
EOF
  chmod 600 "${REPO_ROOT}/.env"
  echo "✗ Wrote stub ${REPO_ROOT}/.env — fill it in:" >&2
  echo "    nano ${REPO_ROOT}/.env" >&2
  echo "  then re-run this script." >&2
  exit 1
fi
if ! grep -qE '^[A-Z_]+_API_KEY=[^[:space:]]+' "${REPO_ROOT}/.env"; then
  echo "✗ ${REPO_ROOT}/.env has no non-empty *_API_KEY lines. Fill it in then re-run." >&2
  exit 1
fi
chmod 600 "${REPO_ROOT}/.env"

# --- 6. Stable tokens (demo URL stays stable across restarts) ---
echo "[6/8] Pre-warming stable tokens..."
mkdir -p "${HOME}/.arcagent"
TOKENS_FILE="${HOME}/.arcagent/arc-stack.tokens"
if [ ! -f "${TOKENS_FILE}" ]; then
  v="$(openssl rand -hex 32)"
  o="$(openssl rand -hex 32)"
  a="$(openssl rand -hex 32)"
  printf 'VIEWER_TOKEN=%s\nOPERATOR_TOKEN=%s\nAGENT_TOKEN=%s\n' "${v}" "${o}" "${a}" \
    >"${TOKENS_FILE}"
  chmod 600 "${TOKENS_FILE}"
fi

# --- 7. Restrict which agents arc-stack starts ---
# arc-stack.sh starts every <name>_agent dir under TEAM_ROOT when no args
# are passed. Disable the rest by renaming → cheap, reversible, survives `uv sync`.
echo "[7/8] Disabling agents not in: ${AGENTS[*]}"
shopt -s nullglob
for d in "${REPO_ROOT}/team"/*_agent; do
  name="$(basename "${d}")"
  keep=0
  for want in "${AGENTS[@]}"; do
    if [ "${name}" = "${want}" ]; then keep=1; break; fi
  done
  if [ "${keep}" -eq 0 ]; then
    mv "${d}" "${d}.disabled"
    echo "  disabled: ${name}"
  fi
done
shopt -u nullglob

# --- 8. systemd + Caddy ---
echo "[8/8] Installing systemd unit and Caddyfile..."

sudo install -m 0644 "${REPO_ROOT}/deploy/aws/arc-stack.service" \
  /etc/systemd/system/arc-stack.service
sudo systemctl daemon-reload
sudo systemctl enable arc-stack.service

sudo mkdir -p /var/log/caddy
sudo chown caddy:caddy /var/log/caddy
sed "s/DEMO_DOMAIN/${DOMAIN}/g" "${REPO_ROOT}/deploy/aws/Caddyfile" \
  | sudo tee /etc/caddy/Caddyfile >/dev/null
sudo systemctl enable caddy
sudo systemctl restart caddy

sudo systemctl restart arc-stack.service
sleep 8

echo ""
echo "── arc-stack status ──"
sudo systemctl --no-pager --lines=0 status arc-stack.service || true
echo ""

# shellcheck disable=SC1091
. "${TOKENS_FILE}"
cat <<EOF
============================================
  SETUP COMPLETE
============================================

Demo URL:
  https://${DOMAIN}/#auth=${VIEWER_TOKEN}

Logs:
  journalctl -u arc-stack -f
  tail -f ${REPO_ROOT}/.arc-logs/ui.log
  sudo tail -f /var/log/caddy/arc-access.log

Restart everything:
  sudo systemctl restart arc-stack caddy

Re-enable an agent later:
  mv ${REPO_ROOT}/team/<name>_agent.disabled ${REPO_ROOT}/team/<name>_agent
  sudo systemctl restart arc-stack
EOF
