#!/usr/bin/env bash
# ============================================================================
# Arc demo — VM setup
# ============================================================================
#
# Runs ON the Lightsail VM after `deploy.sh` provisioned the infra and you
# rsync'd the codebase to /home/ubuntu/arc/. Mirrors deploy/azure/setup-vm.sh.
#
# Usage:
#   bash ~/arc/deploy/aws/setup-vm.sh <domain>
#
# Args:
#   $1   domain to serve (e.g. demo.blackarcsystems.com)
#
# Which agents are started is controlled by deploy/aws/agents.enabled (one
# agent directory name per line, # comments allowed). If the file is missing
# the script falls back to the three NLIT demo agents and prints a warning.
# If the file exists but is empty (misconfiguration) the script errors out.
#
# What it does:
#   1. Installs system deps (Python 3.11, build tools, Caddy)
#   2. Installs uv and runs `uv sync`
#   3. Verifies .env has at least one provider key set
#   4. Pre-warms ~/.arcagent/arc-stack.tokens (stable demo URL)
#   5. Applies agents.enabled manifest — removes unlisted agent dirs
#   6. Installs systemd unit (arc-stack.service)
#   7. Installs Caddyfile with your domain, reloads Caddy (Let's Encrypt auto)
#   8. Starts arc-stack.service and prints the demo URL
#
# ============================================================================
set -euo pipefail

DOMAIN="${1:-}"

if [ -z "${DOMAIN}" ]; then
  cat >&2 <<EOF
usage: bash deploy/aws/setup-vm.sh <domain>
   e.g.   bash deploy/aws/setup-vm.sh agent.blackarcsystems.com
EOF
  exit 1
fi

# Filter the Caddy hostname list to only those that ALREADY resolve to
# this VM's public IP. Hostnames whose DNS hasn't propagated yet would
# trigger ACME challenges that resolve to the wrong server, and Caddy
# would fall back to the Let's Encrypt staging issuer (whose certs the
# browser rejects with ERR_SSL_PROTOCOL_ERROR). Cleaning the staging
# fallback later requires deleting Caddy's cert state and restarting —
# a real footgun. So: only feed Caddy hostnames it can validate today.
#
# `<public-ip>.nip.io` is always added because nip.io's DNS is by
# construction correct. The operator's real domain is included only if
# its current A-record matches this VM. If it doesn't, we print a clear
# message and the operator can re-run setup-vm.sh once DNS is updated.
PUBLIC_IP=$(curl -fsSL --max-time 3 http://169.254.169.254/latest/meta-data/public-ipv4 2>/dev/null || true)
if [ -z "${PUBLIC_IP}" ]; then
  echo "✗ could not read public IP from instance metadata — bailing." >&2
  exit 1
fi

VALID_HOSTS=()
IFS=',' read -r -a _RAW_HOSTS <<< "${DOMAIN}"
for raw in "${_RAW_HOSTS[@]}"; do
  host=$(echo "${raw}" | tr -d '[:space:]')
  [ -z "${host}" ] && continue
  if [[ "${host}" == *".nip.io" ]]; then
    VALID_HOSTS+=("${host}")
    continue
  fi
  resolved=$(getent hosts "${host}" 2>/dev/null | awk '{print $1; exit}')
  if [ "${resolved}" = "${PUBLIC_IP}" ]; then
    VALID_HOSTS+=("${host}")
    echo "  ✓ DNS for ${host} → ${PUBLIC_IP} (will be served)"
  else
    echo "  ✗ DNS for ${host} → ${resolved:-unresolved} (NOT this VM ${PUBLIC_IP})"
    echo "    Skipping until DNS propagates. Re-run this script after the A-record updates."
  fi
done

# Always include the IP-based nip.io so the URL works regardless of DNS.
if [[ ! " ${VALID_HOSTS[*]} " =~ " ${PUBLIC_IP}.nip.io " ]]; then
  VALID_HOSTS+=("${PUBLIC_IP}.nip.io")
fi
DOMAIN=$(IFS=, ; echo "${VALID_HOSTS[*]}")

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "${REPO_ROOT}"

# ---------------------------------------------------------------------------
# Read the per-host agent manifest (deploy/aws/agents.enabled).
# ---------------------------------------------------------------------------
# Returns ENABLED_AGENTS as a newline-separated string of agent dir names.
# SPEC-025 §TD-4 — when (not if) deploy/azure/setup-vm.sh adopts the same
# manifest, extract this function to deploy/lib/agent-manifest.sh and
# source it from both. Don't duplicate.
_read_agent_manifest() {
  local manifest="${REPO_ROOT}/deploy/aws/agents.enabled"
  if [ ! -f "${manifest}" ]; then
    echo "[WARN] deploy/aws/agents.enabled not found — falling back to default demo agents." >&2
    printf 'nlit_cora_agent\nnlit_soc_agent\nscap_isso_agent\n'
    return
  fi
  local enabled
  enabled=$(grep -v '^#' "${manifest}" | grep -v '^[[:space:]]*$' || true)
  if [ -z "${enabled}" ]; then
    echo "✗ deploy/aws/agents.enabled exists but is empty — misconfiguration, aborting." >&2
    exit 1
  fi
  # Defense-in-depth (SPEC-025 §M3): every line must match a strict shape
  # so a tampered manifest cannot smuggle path-traversal, shell metachars,
  # or whitespace tricks into the rm -rf loop downstream.
  while IFS= read -r line; do
    if [[ ! "${line}" =~ ^[a-z0-9_]+_agent$ ]]; then
      echo "✗ agents.enabled contains malformed entry: '${line}' (must match ^[a-z0-9_]+_agent$)" >&2
      exit 1
    fi
  done <<< "${enabled}"
  printf '%s\n' "${enabled}"
}

ENABLED_AGENTS=$(_read_agent_manifest)

echo "=== Arc Demo VM Setup ==="
echo "Repo:    ${REPO_ROOT}"
echo "Domain:  ${DOMAIN}"
echo "Agents:  $(echo "${ENABLED_AGENTS}" | tr '\n' ' ')"
echo ""

# --- 1. System packages ---
echo "[1/8] Installing system packages..."
sudo DEBIAN_FRONTEND=noninteractive apt-get update -qq
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq \
  python3.11 python3.11-venv python3.11-dev \
  build-essential pkg-config libssl-dev libffi-dev \
  curl ca-certificates gnupg jq \
  debian-keyring debian-archive-keyring apt-transport-https \
  libpango-1.0-0 libpangoft2-1.0-0 libharfbuzz0b \
  libcairo2 libgdk-pixbuf-2.0-0
  # Last 5 are needed by WeasyPrint (used by the SCAP evidence-pack
  # tool to render PDFs). Without them, scap_evidence_pack errors at
  # call time. Cheap to install up-front.

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

# SCAP demo extension dependencies. The scap_tools.py shim imports
# bs4/lxml/jinja2/weasyprint/pypdf transitively; if any are missing,
# the entire shim fails to load and ALL 6 SCAP @tool functions stay
# unregistered (the agent then says "the SCAP tools haven't been
# created yet" when the user asks it to ingest scans).
echo "  Installing SCAP extension dependencies..."
"${REPO_ROOT}/.venv/bin/pip" install --quiet \
  lxml beautifulsoup4 jinja2 tomli-w weasyprint pypdf

# AWS Secrets Manager backend (arcllm.backends.aws_secrets). Required
# for production secret resolution via IAM Instance Profile. Pinned to
# a recent stable boto3 — bump as needed during dependency audits.
echo "  Installing AWS SDK (boto3) for Secrets Manager backend..."
"${REPO_ROOT}/.venv/bin/pip" install --quiet 'boto3>=1.40,<2.0'

# --- 4.5. SCAP extension install (dev-mode rsync into ~/.arc/) ---
# Mirrors scripts/install-scap-extension.sh — the loader scans
# ~/.arc/capabilities/*.py for flat shim files (not subdirs), so the
# scap/ package needs to land at ~/.arc/capabilities/scap/ and the
# scap_tools.py shim at ~/.arc/capabilities/scap_tools.py.
if [ -d "${REPO_ROOT}/demo-extensions/scap" ]; then
  echo "  Installing SCAP extension into ~/.arc/..."
  mkdir -p "${HOME}/.arc/capabilities" "${HOME}/.arc/skills"
  rsync -a --delete --exclude '__pycache__' --exclude '*.pyc' \
    "${REPO_ROOT}/demo-extensions/scap/" "${HOME}/.arc/capabilities/scap/"
  if [ -d "${REPO_ROOT}/demo-extensions/skill-scap" ]; then
    rsync -a --delete --exclude '__pycache__' --exclude '*.pyc' \
      "${REPO_ROOT}/demo-extensions/skill-scap/" "${HOME}/.arc/skills/scap/"
  fi
  cp "${REPO_ROOT}/demo-extensions/scap_tools.py" \
     "${HOME}/.arc/capabilities/scap_tools.py"
fi

# --- 4.6. knowledge skill install (rsync into ~/.arc/skills/) ---
# extract_knowledge is the universal "save this turn to memory" tool.
# It lives in the agent's workspace/.capabilities/ (per-agent, ships
# inside the team/ directory rsync), but its skill ships globally so
# any agent on this VM can adopt it.
if [ -d "${REPO_ROOT}/demo-extensions/skill-knowledge" ]; then
  echo "  Installing knowledge skill into ~/.arc/skills/..."
  mkdir -p "${HOME}/.arc/skills"
  rsync -a --delete --exclude '__pycache__' --exclude '*.pyc' \
    "${REPO_ROOT}/demo-extensions/skill-knowledge/" "${HOME}/.arc/skills/knowledge/"
fi

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

# --- 7. Apply agents.enabled manifest — remove unlisted agent dirs ---
# This runs BEFORE systemd start so arc-stack only sees the right agents.
# Idempotent: re-running with the same manifest is a no-op.
echo "[7/8] Applying agent manifest (removing unlisted agent dirs)..."
shopt -s nullglob
for d in "${REPO_ROOT}/team"/*_agent; do
  [ -d "${d}" ] || continue
  name="$(basename "${d}")"
  if echo "${ENABLED_AGENTS}" | grep -qx "${name}"; then
    echo "  kept:    ${name}"
  else
    rm -rf "${d}"
    echo "  removed: ${name} (not in agents.enabled)"
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

# scap_evidence_pack writes its PDFs + POA&M CSVs to /tmp/scap-out
# (the path the agent's tool-use args pick by default — derived from
# the SCAP skill's example invocations). Caddy's stock systemd unit
# sets PrivateTmp=yes, which gives Caddy its own /tmp namespace
# isolated from the agent's. With that on, /artifacts/ returns 404
# even though files exist.
# Override: disable PrivateTmp so Caddy and the agent share /tmp.
sudo mkdir -p /etc/systemd/system/caddy.service.d
echo -e "[Service]\nPrivateTmp=false" \
  | sudo tee /etc/systemd/system/caddy.service.d/private-tmp.conf >/dev/null
sudo systemctl daemon-reload

# Make sure the artifact dir exists at boot so /artifacts/ renders an
# (empty) directory listing instead of 404 before any tool has run.
mkdir -p /tmp/scap-out
chmod 755 /tmp/scap-out
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

Add/remove agents:
  Edit ${REPO_ROOT}/deploy/aws/agents.enabled
  then re-run this script.
EOF
