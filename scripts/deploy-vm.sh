#!/usr/bin/env bash
# scripts/deploy-vm.sh — one-command, deterministic redeploy of an Arc VM node
# over SSH. Covers the DGX Spark and the Azure VM: both run the SAME model — a
# host runtime under ~/.arc/runtime/<version>/ served by a `systemd --user`
# arc.service. (Docker is a separate, in-development online-deploy path — NOT
# how these machines run. See scripts/deploy-azure.sh for that lane.)
#
# Runs FROM your laptop. Does everything a code update needs, end to end:
#   1. sync the node's source checkout to origin/<branch> (hard reset — the
#      checkout is a source tarball; nothing durable lives in it)
#   2. install that source as a NEW runtime, build its venv, flip `current`,
#      materialize modules, refresh the unit, and restart the service — all via
#      scripts/deploy-node.sh, which owns runtime management on the box
#   3. prove the new code is actually serving: health, fleet count, the Telegram
#      adapter import, and the arcui bundle hash the browser will load.
#
# THE CHECKOUT IS NOT THE INSTALL. `git pull` in ~/arc never changes what runs —
# the service executes from ~/.arc/runtime/current/.venv, a frozen snapshot. Only
# installing a new runtime and flipping `current` ships code. This script does
# that; a bare pull+restart does not, and silently serves the old bundle.
#
# Deterministic: same origin/<branch> commit → same source fingerprint → same
# runtime/<version> directory. Re-running is idempotent and safe on a live fleet;
# agent configs, gateway.toml, arc.env, and the fleet at ~/arc/team are untouched.
#
# GOLDEN RULE — DEPLOY FROM main. Merge every change to main FIRST, then deploy
# (the default branch here is main). What runs in production is always an
# origin/main commit, so main is the single source of truth for the fleet. The
# `--branch` escape hatch below exists only for throwaway pre-merge testing on a
# scratch node; it must never be how a real change reaches dgx or azure.
#
# Usage:
#   scripts/deploy-vm.sh dgx                 # sync main → install runtime → restart → verify
#   scripts/deploy-vm.sh azure               # same, against the Azure VM
#   scripts/deploy-vm.sh --host user@ip      # any bootstrapped systemd-user node
#   scripts/deploy-vm.sh dgx --branch feat/x # deploy a different branch
#
# Env overrides:
#   ARC_DGX_HOST     ssh target for `dgx`     default: dgx
#   ARC_AZURE_HOST   ssh target for `azure`   default: az-resolved (rg-arcagent/vm-josh-agent)
#   ARC_VM_REPO      remote source tree       default: ~/arc
#   ARC_VM_BRANCH    branch to deploy         default: main
#   ARC_UI_PORT      health/UI port           default: 8420

set -euo pipefail

REPO="${ARC_VM_REPO:-\$HOME/arc}"   # expanded on the REMOTE shell, not here
BRANCH="${ARC_VM_BRANCH:-main}"
UI_PORT="${ARC_UI_PORT:-8420}"
TARGET=""
HOST=""

log()  { echo "→ $*"; }
ok()   { echo "  ✓ $*"; }
fail() { echo "  ✗ $*" >&2; exit 1; }

while [ $# -gt 0 ]; do
  case "$1" in
    dgx|azure) TARGET="$1" ;;
    --host)    HOST="${2:-}"; shift; [ -n "$HOST" ] || { echo "--host needs a value" >&2; exit 2; } ;;
    --branch)  BRANCH="${2:-}"; shift; [ -n "$BRANCH" ] || { echo "--branch needs a value" >&2; exit 2; } ;;
    -h|--help) sed -n '2,40p' "$0"; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
  shift
done

# --- resolve the ssh target ----------------------------------------------
# A named target carries its own host resolution; --host is the escape hatch for
# any other bootstrapped node. Azure's IP is not pinned in the script — it is
# read from Azure (same resolution scripts/deploy-azure.sh uses) so a VM restart
# that changes the public IP does not silently deploy nowhere.
if [ -z "$HOST" ]; then
  case "$TARGET" in
    dgx)   HOST="${ARC_DGX_HOST:-dgx}" ;;
    azure)
      HOST="${ARC_AZURE_HOST:-}"
      if [ -z "$HOST" ]; then
        command -v az >/dev/null || fail "azure target needs the az CLI to resolve the VM IP (or set ARC_AZURE_HOST)"
        IP="$(az vm list-ip-addresses -g "${ARC_AZ_RG:-rg-arcagent}" -n "${ARC_AZ_VM:-vm-josh-agent}" \
              --query "[0].virtualMachine.network.publicIpAddresses[0].ipAddress" -o tsv 2>/dev/null)"
        [ -n "$IP" ] || fail "could not resolve the Azure VM public IP — set ARC_AZURE_HOST or run 'az login'"
        HOST="azureuser@$IP"
      fi ;;
    "") fail "pick a target: dgx | azure  (or pass --host user@ip)" ;;
  esac
fi

command -v ssh >/dev/null || fail "ssh not found"

# One login shell per remote step so ~/.local/bin (uv, nats) is on PATH and the
# script under test is the one that just landed. The command is fed over stdin so
# it can contain any quoting without a nested-quote fight; the caller's $VARS are
# expanded locally before sending, remote $HOME/~ expand on the far side.
remote() { ssh "$HOST" 'bash -l -s' <<<"$1"; }

log "Target: ${TARGET:-$HOST}  →  $HOST  ($BRANCH)"
remote "true" || fail "cannot ssh to $HOST"

# --- 1. deterministic git sync to origin/<branch> -------------------------
# reset --hard, not pull: pull can conflict or create a merge, and the checkout
# has no work worth preserving (team/ and .env are gitignored, and nothing runs
# from here). This makes the SOURCE the exact origin commit, every time.
log "Syncing $REPO to origin/$BRANCH ..."
COMMIT="$(remote "cd $REPO && git fetch --quiet origin && git checkout --quiet $BRANCH && git reset --hard --quiet origin/$BRANCH && git rev-parse --short HEAD")"
[ -n "$COMMIT" ] || fail "git sync failed on $HOST"
ok "source at origin/$BRANCH @ $COMMIT"

# --- 2. discover the fleet, install the runtime, restart ------------------
# The roster is read from the node's own fleet so the deploy adapts to whoever
# actually lives there — no hardcoded agent list to drift. deploy-node.sh is
# idempotent on an existing node: agents "already exist", configs are left as
# they are, and only the runtime is rebuilt and activated.
ROSTER="$(remote "for d in $REPO/team/*/; do [ -f \"\${d}arcagent.toml\" ] && basename \"\$d\"; done | tr '\n' ' '")"
[ -n "$ROSTER" ] || fail "no agents found under $REPO/team — is this a bootstrapped node?"
ok "fleet: $ROSTER"

# BEFORE hash: what the browser loads right now, to prove the deploy changed it.
BUNDLE_BEFORE="$(remote "curl -s http://localhost:$UI_PORT/ 2>/dev/null | grep -oE 'assets/index-[A-Za-z0-9_-]+\.js' | head -1" || true)"

log "Installing runtime + restarting service (deploy-node.sh)..."
# Telegram ships inside core arcgateway (uv sync installs it) and gateway.toml is
# already wired, so the redeploy needs no ARC_ENABLE_TELEGRAM — leaving it off
# also skips the first-boot single-bot token check that does not fit a multi-bot
# node. The adapter's presence is asserted in step 3 regardless.
remote "cd $REPO && ./scripts/deploy-node.sh $ROSTER" || fail "deploy-node.sh failed — see output above; if it stopped before 'runtime active', nothing new was activated"

# --- 3. prove the new code is serving -------------------------------------
log "Verifying deployment..."

ACTIVE="$(remote "readlink \$HOME/.arc/runtime/current | xargs basename")"
ok "active runtime: $ACTIVE"

# Application startup (six agents, memory backends) takes well over the old
# single-probe window — poll until the lifespan finishes or two minutes pass.
remote "for _ in {1..24}; do curl -fsS --max-time 5 http://localhost:$UI_PORT/api/health >/dev/null && exit 0; sleep 5; done; exit 1" \
  || fail "health check failed on :$UI_PORT after 120s"
ok "health: 200"

# Fleet: deploy-node.sh already gates a hollow node — it runs `arc agent build
# --check` per agent and fails closed if `arc install` cannot deliver every
# module a config enables — so reaching here means the rostered fleet is intact.
# (No HTTP re-check here: /api/agents needs a viewer token this script does not
# carry, and an unauthenticated probe only ever answers 401.)
ok "fleet gated by deploy-node.sh ($(echo "$ROSTER" | wc -w | tr -d ' ') agents)"

# Telegram adapter must be importable in the ACTIVE runtime (it lives in core
# arcgateway; a missing import means chat delivery is silently dead).
if remote "\$HOME/.arc/runtime/current/.venv/bin/python -c 'import arcgateway.adapters.telegram'"; then
  ok "telegram adapter importable"
else
  echo "  ! telegram adapter NOT importable — Telegram delivery is down" >&2
fi

# arcui bundle: the JS the browser loads must match what origin/<branch> ships,
# and must differ from what was served before (proves the flip took effect).
BUNDLE_AFTER="$(remote "curl -s http://localhost:$UI_PORT/ 2>/dev/null | grep -oE 'assets/index-[A-Za-z0-9_-]+\.js' | head -1" || true)"
BUNDLE_REPO="$(remote "grep -oE 'assets/index-[A-Za-z0-9_-]+\.js' \$HOME/.arc/runtime/current/packages/arcui/src/arcui/static/index.html | head -1" || true)"
if [ -n "$BUNDLE_AFTER" ] && [ "$BUNDLE_AFTER" = "$BUNDLE_REPO" ]; then
  ok "arcui bundle live: $BUNDLE_AFTER"
  if [ "$BUNDLE_BEFORE" != "$BUNDLE_AFTER" ]; then
    ok "bundle changed from ${BUNDLE_BEFORE:-none}"
  else
    echo "  (bundle unchanged — this commit did not touch the web build)"
  fi
else
  fail "served bundle ($BUNDLE_AFTER) does not match the runtime's index.html ($BUNDLE_REPO)"
fi

echo
echo "=== ${TARGET:-$HOST} deploy complete ==="
echo "  Commit:  $COMMIT ($BRANCH)"
echo "  Runtime: $ACTIVE"
echo "  Health:  http://$HOST:$UI_PORT/api/health"
