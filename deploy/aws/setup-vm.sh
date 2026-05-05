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
  # Default trio for the NLIT 2026 demo:
  #   nlit_cora_agent   — STIG cross-reference, POA&M validation, gap reports
  #   nlit_soc_agent    — SOC threat hunter, entity capture, incident triage
  #   scap_isso_agent   — ISSO assistant: ATO evidence, FedRAMP gap analysis,
  #                       drift detection, threat-informed compliance
  # All three use Anthropic so the only key the .env needs is ANTHROPIC_API_KEY.
  AGENTS=("nlit_cora_agent" "nlit_soc_agent" "scap_isso_agent")
fi

if [ -z "${DOMAIN}" ]; then
  cat >&2 <<EOF
✗ usage: bash deploy/aws/setup-vm.sh <domain> [agent_name ...]
   e.g.   bash deploy/aws/setup-vm.sh agent.blackarcsystems.com
   (with no agent args, defaults to: ${AGENTS[*]})
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

Re-enable an agent later:
  mv ${REPO_ROOT}/team/<name>_agent.disabled ${REPO_ROOT}/team/<name>_agent
  sudo systemctl restart arc-stack
EOF
