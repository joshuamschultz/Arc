#!/usr/bin/env bash
# scripts/deploy-azure.sh — push the current checkout to the Azure VM.
#
# Build in ACR, pull on the VM, recreate the stack. Idempotent and re-runnable:
# agent state lives in a Docker named volume on the attached data disk, so
# recreating the container keeps memory, identity keys, sessions, and the pinned
# access tokens. Only `docker compose down -v` on the box destroys them.
#
# The image is built by ACR Tasks rather than locally: the VM is x86_64 and most
# dev machines are arm64, so a local build would cross-compile torch — minutes
# against seconds, for a byte-identical result.
#
# Registry auth uses a ~3h ACR refresh token minted from the operator's own `az`
# session and piped to the VM's `docker login` over stdin. Nothing long-lived is
# written to the box, and the token never appears in a command line or in the
# process table. Between deploys the VM needs no registry access at all —
# `restart: unless-stopped` restarts from the local image.
#
# Usage:
#   scripts/deploy-azure.sh              # build, push, deploy, health-check
#   scripts/deploy-azure.sh --skip-build # redeploy the tag already in ACR
#
# Env overrides (defaults target the deployment this script was written for):
#   ARC_AZ_RG          resource group          default: rg-arcagent
#   ARC_AZ_VM          VM name                 default: vm-josh-agent
#   ARC_AZ_ACR         registry name           default: acrarcctg
#   ARC_AZ_HOST        ssh target              default: azureuser@<VM public ip>
#   ARC_DOMAIN         hostname for the health check; falls back to the public IP

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

RG="${ARC_AZ_RG:-rg-arcagent}"
VM="${ARC_AZ_VM:-vm-josh-agent}"
ACR="${ARC_AZ_ACR:-acrarcctg}"
REMOTE_DIR=/opt/arc
SKIP_BUILD=0
[ "${1:-}" = "--skip-build" ] && SKIP_BUILD=1

log()  { echo "→ $*"; }
ok()   { echo "  ✓ $*"; }
fail() { echo "  ✗ $*" >&2; exit 1; }

command -v az >/dev/null || fail "az CLI not found"
az account show >/dev/null 2>&1 || fail "not logged in — run 'az login'"

# --- 1. what are we shipping ---------------------------------------------
# The tag is the commit, so `docker image ls` on the box answers "what is
# actually running" without trusting a deploy log.
SHA="$(git rev-parse --short HEAD)"
BRANCH="$(git rev-parse --abbrev-ref HEAD)"
if [ -n "$(git status --porcelain --untracked-files=no)" ]; then
  echo "  ! working tree has uncommitted changes — they WILL ship (build context is the working tree, not the commit)" >&2
fi
log "Deploying $BRANCH @ $SHA"

HOST="${ARC_AZ_HOST:-}"
if [ -z "$HOST" ]; then
  IP="$(az vm list-ip-addresses -g "$RG" -n "$VM" \
        --query "[0].virtualMachine.network.publicIpAddresses[0].ipAddress" -o tsv)"
  [ -n "$IP" ] || fail "could not resolve a public IP for $VM in $RG"
  HOST="azureuser@$IP"
else
  IP="${HOST#*@}"
fi
ok "target $HOST"

# --- 2. build in ACR ------------------------------------------------------
# TARGETARCH is a buildx variable. ACR Tasks uses the classic builder, which does
# not populate it, so the nats-server download in the Dockerfile would fail on an
# unset parameter. Pass it explicitly rather than weakening the Dockerfile, which
# is still correct for the documented multi-arch buildx path.
if [ "$SKIP_BUILD" = "1" ]; then
  ok "skipping build (--skip-build)"
else
  log "Building in ACR $ACR (linux/amd64) — several minutes on a cold cache..."
  az acr build \
    --registry "$ACR" \
    --image "arc:latest" \
    --image "arc:$SHA" \
    --platform linux/amd64 \
    --build-arg TARGETARCH=amd64 \
    --file Dockerfile \
    . >/dev/null
  ok "pushed $ACR.azurecr.io/arc:$SHA and :latest"
fi

# --- 3. sync the stack definition ----------------------------------------
# compose + Caddyfile are versioned here and copied every deploy, so a config
# change ships like a code change. .env is NOT copied: it holds the provider key
# and is the one file that lives only on the box.
log "Syncing stack definition to $REMOTE_DIR..."
# shellcheck disable=SC2029  # $REMOTE_DIR must expand locally — it is this script's constant.
ssh "$HOST" "sudo mkdir -p $REMOTE_DIR && sudo chown azureuser:azureuser $REMOTE_DIR"
scp -q deploy/cloud/docker-compose.yml deploy/cloud/Caddyfile "$HOST:$REMOTE_DIR/"
# shellcheck disable=SC2029  # same: local constant, deliberately expanded here.
ssh "$HOST" "test -f $REMOTE_DIR/.env" \
  || fail "$REMOTE_DIR/.env missing on the VM — it holds the provider key; create it before the first deploy"
ok "compose + Caddyfile synced"

# --- 4. pull + recreate ---------------------------------------------------
# Login and orchestration are two separate ssh calls on purpose. `docker login`
# needs the token on stdin (so it never becomes an argv entry visible to `ps`),
# and a heredoc would take stdin away from the pipe. One call per stdin.
log "Minting a short-lived ACR token..."
TOKEN="$(az acr login --name "$ACR" --expose-token --output tsv --query accessToken)"
[ -n "$TOKEN" ] || fail "could not mint an ACR token"

# shellcheck disable=SC2029  # $ACR must expand locally — it names the registry we just built in.
printf '%s' "$TOKEN" | ssh "$HOST" \
  "sudo docker login '$ACR.azurecr.io' --username 00000000-0000-0000-0000-000000000000 --password-stdin" \
  >/dev/null || fail "docker login to $ACR.azurecr.io failed on the VM"
ok "registry login accepted"

log "Pulling and recreating on $HOST..."
# shellcheck disable=SC2029  # $ARC_IMAGE/$REMOTE_DIR must expand HERE — they are this deploy's values.
ssh "$HOST" "ARC_IMAGE='$ACR.azurecr.io/arc:$SHA' REMOTE_DIR='$REMOTE_DIR' ACR='$ACR' bash -s" <<'REMOTE'
set -euo pipefail
cd "$REMOTE_DIR"
# Pin the commit-tagged image for this deploy so a restart cannot silently drift
# onto a newer :latest that was pushed in the meantime.
if grep -q '^ARC_IMAGE=' .env; then
  sudo sed -i "s|^ARC_IMAGE=.*|ARC_IMAGE=$ARC_IMAGE|" .env
else
  echo "ARC_IMAGE=$ARC_IMAGE" | sudo tee -a .env >/dev/null
fi
sudo docker compose pull
sudo docker compose up -d --remove-orphans
sudo docker logout "$ACR.azurecr.io" >/dev/null 2>&1 || true
sudo docker image prune -f >/dev/null 2>&1 || true
REMOTE
ok "stack recreated"

# --- 5. prove it ----------------------------------------------------------
# /api/health is auth-exempt (arcui/auth.py) so this needs no token. Poll rather
# than sleep: first boot pays for agent scaffolding and model load.
TARGET="${ARC_DOMAIN:+https://$ARC_DOMAIN}"
TARGET="${TARGET:-http://$IP:8420}"
log "Waiting for $TARGET/api/health ..."
for i in $(seq 1 60); do
  if curl -fsS --max-time 5 "$TARGET/api/health" >/dev/null 2>&1; then
    ok "healthy after ~$((i * 5))s"
    echo
    echo "  Running: $ACR.azurecr.io/arc:$SHA"
    echo "  Dashboard: $TARGET"
    echo "  Token:     ssh $HOST \"sudo docker compose -f $REMOTE_DIR/docker-compose.yml exec -T arc sh -c 'grep VIEWER_TOKEN \\\$HOME/.arc/arc.env'\""
    exit 0
  fi
  sleep 5
done

echo "  ✗ not healthy after 5 minutes — recent logs:" >&2
# shellcheck disable=SC2029  # same: local constant, deliberately expanded here.
ssh "$HOST" "cd $REMOTE_DIR && sudo docker compose logs --tail 40 arc" >&2
exit 1
