#!/usr/bin/env bash
# scripts/deploy-node.sh — single-command bootstrap for a fresh Arc node.
#
# Automates docs/runbooks/deploy/local.md: install the runtime, nats-server +
# platform adapter, arc init, config overlays, agent create, and a
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
# THE CHECKOUT IS NOT THE INSTALL. This script copies the checkout into
# ~/.arc/runtime/<version>/ and builds that copy's venv there, then flips the
# `current` symlink onto it. The checkout you ran this from is only a source
# tarball afterwards: nothing executes from it, nothing durable lives in it,
# and deleting it costs nothing. That is what makes an update an atomic symlink
# flip (`arc runtime activate <new>`) and a rollback the same verb with the
# previous version — and what keeps a `git pull` in the checkout away from the
# fleet, which lives at ~/arc/team beside it and is never executed from.
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
#   ARC_ENABLE_TELEGRAM           default: 0 (set 1 to verify + wire the adapter)
#   ARC_TELEGRAM_ALLOWED_USER_IDS space-separated Telegram user ids (empty = deny all)
#   ARC_ENV_FILE                  source of ANTHROPIC_API_KEY / ARCAGENT_TELEGRAM_BOT_TOKEN
#                                  default: $REPO_ROOT/.env
#   ARC_RUNTIME_VERSION           name of the runtime/<version> dir to install into
#                                  default: <pyproject version>-<git short sha|UTC stamp>
#   ARC_TEAM_ROOT                 relocates the FLEET only (parent of team/)
#                                  default: $ARC_CONFIG_DIR — never the checkout
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
export ARC_CONFIG_DIR

# arc_team() falls back to ARC_CONFIG_DIR before ~/arc, so exporting the line
# above is itself enough to move the fleet into the hidden home — every `arc`
# call below would answer ~/.arc/team while the unit serves ~/arc/team. Pin the
# fleet explicitly so the resolver and the unit cannot disagree.
ARC_TEAM_ROOT="${ARC_TEAM_ROOT:-$HOME/arc}"
export ARC_TEAM_ROOT

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

# --- 2. install the runtime beside its siblings, then flip `current` ------
# The version names a directory under ~/.arc/runtime/. It carries the commit so
# two deploys of different code are two installs you can flip between; a redeploy
# of the SAME commit lands in the same directory, which is what keeps re-running
# this script idempotent instead of accumulating identical trees.
PROJECT_VERSION="$(sed -n 's/^version = "\(.*\)"$/\1/p' "$REPO_ROOT/pyproject.toml" | head -1)"
[ -n "$PROJECT_VERSION" ] || fail "could not read version from $REPO_ROOT/pyproject.toml"

# The stamp must describe the SOURCE, not the checkout it arrived from. Asking
# git here reads the deploy target's own .git — which the documented rsync
# excludes, so it answers with whatever commit that box was last cloned at. A
# DGX deploy installed 536ff25e's code into a directory named for 1658fe71: the
# name silently collides on every later deploy, so `current` flips between two
# names that are the same directory and rollback quietly does nothing.
#
# Hashing the source removes the question. Identical code lands in the same
# directory (still idempotent); different code gets a different one (rollback
# works again), with no dependence on a .git that is not shipped.
# scripts/ and deploy/ are in scope because they are consumed FROM the installed
# runtime, not from the checkout: the unit is copied out of
# runtime/current/deploy/systemd/arc.service and install-nats.sh runs from
# runtime/current/scripts/. Fingerprinting only packages/ left a change confined
# to either of them computing the same name, so the deploy would rsync --delete
# into the ACTIVE runtime in place — the same hazard through a narrower door.
BUILD_STAMP="$(
  find "$REPO_ROOT/packages" "$REPO_ROOT/scripts" "$REPO_ROOT/deploy" \
       "$REPO_ROOT/pyproject.toml" \
    -type f \( -name '*.py' -o -name '*.toml' -o -name '*.sh' -o -name '*.service' \) \
    2>/dev/null |
    LC_ALL=C sort | xargs shasum 2>/dev/null | shasum | cut -c1-8
)"
[ -n "$BUILD_STAMP" ] || fail "could not fingerprint the source tree at $REPO_ROOT"
RUNTIME_VERSION="${ARC_RUNTIME_VERSION:-$PROJECT_VERSION-$BUILD_STAMP}"
RUNTIME_DIR="$ARC_CONFIG_DIR/runtime/$RUNTIME_VERSION"

# The fleet directory name is the layout's, not this script's. Everything below
# that has to name it derives it here, so renaming the fleet can never leave a
# guard watching a directory that no longer exists.
FLEET_DIR="$(basename "${ARC_TEAM_ROOT:-$HOME/arc}/team")"

# GUARD 1 (before rsync, the only one that PREVENTS rather than reports).
# `rsync --delete` is about to run inside $RUNTIME_DIR, so nothing durable may
# sit under it. ARC_TEAM_ROOT is the only way a fleet could, and a fleet deleted
# here is agent memory, identity keys and workspaces gone with no copy anywhere.
case "${ARC_TEAM_ROOT:-}/" in
  "$ARC_CONFIG_DIR/runtime"/*)
    fail "ARC_TEAM_ROOT points inside $ARC_CONFIG_DIR/runtime — an install would delete the fleet" ;;
esac

log "Installing runtime $RUNTIME_VERSION into $RUNTIME_DIR..."
mkdir -p "$RUNTIME_DIR"
# --delete so a redeploy of the same version cannot leave a file the new code no
# longer ships. rsync does not delete an EXCLUDED destination path, which is what
# each exclude below is for: .venv is rebuilt in place (a copied venv has absolute
# paths baked into its shebangs), /modules holds bundles `arc install` already
# materialized into this runtime, and .env holds secrets that live in
# ~/.arc/config/arc.env instead. /modules is anchored so package-internal
# modules/ directories still ship.
#
# $FLEET_DIR is the load-bearing one: ~/arc is BOTH the rsync source and the
# fleet's parent, so without it every deploy rakes each agent's memory, identity
# keys, tools, skills and workspace into a runtime the next deploy deletes.
rsync -a --delete \
  --exclude '.git/' --exclude '.venv/' --exclude '/modules/' \
  --exclude 'team/' --exclude '.env' \
  --exclude '__pycache__/' --exclude '.pytest_cache/' --exclude '.ruff_cache/' \
  --exclude '.mypy_cache/' --exclude '.arc-logs/' --exclude 'dist/' \
  "$REPO_ROOT/" "$RUNTIME_DIR/"

# GUARD 2 (after rsync, before anything is activated). The exclusion above is one
# word in a long command. Check the OUTCOME instead of trusting the spelling: a
# dropped flag, a renamed flag and a mis-anchored pattern all land here, and the
# deploy stops with `current` still pointing at the runtime that was working.
[ ! -e "$RUNTIME_DIR/$FLEET_DIR" ] || fail \
  "the runtime install captured $RUNTIME_DIR/$FLEET_DIR — the rsync no longer excludes the fleet. Nothing was activated; delete that copy and restore the exclude."

log "uv sync (building $RUNTIME_DIR/.venv)..."
"$UV" sync --project "$RUNTIME_DIR"

# The flip is atomic: a reader sees the old runtime or the new one, never a gap.
# A pre-symlink install leaves a real `current/` directory here; activate_runtime
# moves it aside rather than deleting it, so its modules are recoverable.
"$RUNTIME_DIR/.venv/bin/arc" runtime activate "$RUNTIME_VERSION" \
  || fail "could not point $ARC_CONFIG_DIR/runtime/current at $RUNTIME_VERSION"

ARC_BIN="$ARC_CONFIG_DIR/runtime/current/.venv/bin/arc"
VENV_PY="$ARC_CONFIG_DIR/runtime/current/.venv/bin/python"
RUNTIME_ROOT="$ARC_CONFIG_DIR/runtime/current"
[ -x "$ARC_BIN" ] || fail "$ARC_BIN is not executable after activation"
ok "runtime $RUNTIME_VERSION active"

# --- 2c. prune old runtimes so the disk cannot fill -----------------------
# Each deploy installs a full runtime tree (~1.5 GB with the venv) side by side;
# unpruned they accumulate until the disk fills mid-rsync (a box hit 13 runtimes
# / 18 GB at 100%). Keep the ACTIVE runtime plus the few most recent for
# rollback, delete the rest. Module bundles are materialized read-only, so make
# them writable before removing. Never touches config/ or state/ (siblings).
KEEP_RUNTIMES="${ARC_KEEP_RUNTIMES:-3}"
_active="$(basename "$(readlink "$ARC_CONFIG_DIR/runtime/current" 2>/dev/null)")"
# shellcheck disable=SC2010  # need mtime order (ls -t); runtime names are controlled `<ver>-<hex>`
ls -1dt "$ARC_CONFIG_DIR"/runtime/[0-9]* 2>/dev/null |
  grep -v "/${_active}\$" |
  tail -n "+${KEEP_RUNTIMES}" |
  while read -r _old; do
    log "Pruning old runtime $(basename "$_old")..."
    chmod -R u+w "$_old" 2>/dev/null || true
    rm -rf "$_old"
  done
ok "runtimes pruned (kept active + $((KEEP_RUNTIMES - 1)) recent)"

# --- 2b. split a flat home BEFORE any stage reads a config path -----------
# A deployment older than the lifecycle split has its TOML flat at ~/.arc. Every
# stage below resolves a config path under ~/.arc/config, and `arc init` CREATES
# what it does not find — so running it first would write a fresh config beside
# the real one, and the migration could then only refuse, because both are real.
# The fleet is never in scope here: it lives at ~/arc/team, outside this home.
log "Splitting the Arc home if it is still flat (idempotent)..."
"$ARC_BIN" install --migrate-only || fail "layout migration refused — see above; nothing was moved"

# The fleet root comes from the resolver, never from a literal here: a script
# that carries its own answer is how the deploy and the service unit came to
# disagree about which directory holds the agents, and starting from the wrong
# empty root loads ZERO agents while reporting healthy.
TEAM_ROOT="$("$VENV_PY" -c 'from arctrust.paths import arc_team; print(arc_team())')"
# Compare the FULL path, not the basename. Both sides spell the last component
# "team", so a basename check passes while the parent is wrong — which is exactly
# the failure this guard exists to catch, and it could not see it.
EXPECTED_TEAM_ROOT="${ARC_TEAM_ROOT:-$HOME/arc}/$FLEET_DIR"
[ "$TEAM_ROOT" = "$EXPECTED_TEAM_ROOT" ] || fail \
  "the resolver says the fleet is '$TEAM_ROOT' but this deploy targets '$EXPECTED_TEAM_ROOT' — \
the unit would serve one and agents would be created in the other"
ok "fleet root: $TEAM_ROOT"

# --- 3. nats-server (not a Python dep — arcteam auto-spawns it, needs PATH) --
# Delegated to scripts/install-nats.sh, which install.sh also calls. The
# checksum verification lives in exactly one place on purpose: a second copy
# of a verification routine is the copy that quietly stops verifying.
"$RUNTIME_ROOT/scripts/install-nats.sh"

# --- 4. platform adapter (telegram) --------------------------------------
# The Telegram adapter lives INSIDE core arcgateway (arcgateway.adapters.telegram)
# — `uv sync` installs it with every deploy, so there is no separate package to
# add. Verify the import fail-closed rather than reach systemd with a config that
# enables a platform the runtime cannot serve.
if [ "$ENABLE_TELEGRAM" = "1" ]; then
  "$VENV_PY" -c "import arcgateway.adapters.telegram" >/dev/null 2>&1 \
    || fail "ARC_ENABLE_TELEGRAM=1 but arcgateway.adapters.telegram is not importable in the new runtime"
  ok "telegram adapter present (arcgateway.adapters.telegram)"
fi

# --- 5. secrets: fail-closed if anything required is missing -------------
[ -f "$ENV_FILE" ] || fail "ARC_ENV_FILE not found: $ENV_FILE"
ANTHROPIC_API_KEY="$(grep -m1 '^ANTHROPIC_API_KEY=' "$ENV_FILE" | cut -d= -f2-)"
[ -n "$ANTHROPIC_API_KEY" ] || fail "ANTHROPIC_API_KEY missing from $ENV_FILE"

ARCSTORE_URL_INPUT="$(grep -m1 '^ARCSTORE_DATABASE_URL=' "$ENV_FILE" | cut -d= -f2- || true)"
ARCSTORE_REF_INPUT="$(grep -m1 '^ARCSTORE_DATABASE_CREDENTIAL_REF=' "$ENV_FILE" | cut -d= -f2- || true)"
ARCSTORE_PASSWORD_INPUT="$(grep -m1 '^ARCSTORE_DATABASE_PASSWORD=' "$ENV_FILE" | cut -d= -f2- || true)"
ARCSTORE_PORT="$(grep -m1 '^ARCSTORE_PG_PORT=' "$ENV_FILE" | cut -d= -f2- || true)"
ARCSTORE_PORT="${ARCSTORE_PORT:-5432}"

TELEGRAM_BOT_TOKEN=""
if [ "$ENABLE_TELEGRAM" = "1" ]; then
  TELEGRAM_BOT_TOKEN="$(grep -m1 '^ARCAGENT_TELEGRAM_BOT_TOKEN=' "$ENV_FILE" | cut -d= -f2-)"
  [ -n "$TELEGRAM_BOT_TOKEN" ] || fail "ARC_ENABLE_TELEGRAM=1 but ARCAGENT_TELEGRAM_BOT_TOKEN missing from $ENV_FILE"
fi

mkdir -p "$ARC_CONFIG_DIR/config"
ARC_ENV="$ARC_CONFIG_DIR/config/arc.env"
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
      [ -n "$ARCSTORE_URL_INPUT" ] && printf 'ARCSTORE_DATABASE_URL=%s\n' "$ARCSTORE_URL_INPUT"
      [ -n "$ARCSTORE_REF_INPUT" ] && printf 'ARCSTORE_DATABASE_CREDENTIAL_REF=%s\n' "$ARCSTORE_REF_INPUT"
    } > "$ARC_ENV"
  )
  chmod 600 "$ARC_ENV"
  ok "$ARC_ENV written (0600)"
fi
# Preserve an operator-managed database URL or vault coordinate when the
# protected runtime environment already exists from an earlier deploy.
if [ -n "$ARCSTORE_URL_INPUT" ] && ! grep -q '^ARCSTORE_DATABASE_URL=' "$ARC_ENV"; then
  ( umask 077; printf 'ARCSTORE_DATABASE_URL=%s\n' "$ARCSTORE_URL_INPUT" >> "$ARC_ENV" )
fi
if [ -n "$ARCSTORE_REF_INPUT" ] && ! grep -q '^ARCSTORE_DATABASE_CREDENTIAL_REF=' "$ARC_ENV"; then
  ( umask 077; printf 'ARCSTORE_DATABASE_CREDENTIAL_REF=%s\n' "$ARCSTORE_REF_INPUT" >> "$ARC_ENV" )
fi
chmod 600 "$ARC_ENV"
set -a
# shellcheck disable=SC1090
. "$ARC_ENV"
set +a

# --- 5b. ArcStore PostgreSQL ----------------------------------------------
# A managed URL (for example Supabase) is validated by the same ArcStore
# adapter. Without a URL or vault coordinate, provision the dedicated local
# database and keep its DSN only in arc.env (0600).
if [ -z "${ARCSTORE_DATABASE_URL:-}" ] && [ -z "${ARCSTORE_DATABASE_CREDENTIAL_REF:-}" ]; then
  ARCSTORE_PASSWORD_INPUT="${ARCSTORE_PASSWORD_INPUT:-$(openssl rand -hex 32)}"
  ARCSTORE_DATABASE_URL="postgresql://arcstore:${ARCSTORE_PASSWORD_INPUT}@127.0.0.1:${ARCSTORE_PORT}/arcstore"
  ( umask 077
    printf 'ARCSTORE_DATABASE_URL=%s\n' "$ARCSTORE_DATABASE_URL" >> "$ARC_ENV"
  )
  ok "ArcStore DSN stored in $ARC_ENV (0600)"
fi

if [ -n "${ARCSTORE_DATABASE_URL:-}" ]; then
  ARCSTORE_DATABASE_PASSWORD_RUNTIME="$("$VENV_PY" -c '
import sys
from urllib.parse import unquote, urlparse

parsed = urlparse(sys.argv[1])
print(unquote(parsed.password or ""))
' "$ARCSTORE_DATABASE_URL")"
  if [[ "$ARCSTORE_DATABASE_URL" == *"@127.0.0.1:"* || "$ARCSTORE_DATABASE_URL" == *"@localhost:"* ]]; then
    ARCSTORE_DATABASE_PASSWORD="$ARCSTORE_DATABASE_PASSWORD_RUNTIME" \
    ARCSTORE_DATABASE_URL="$ARCSTORE_DATABASE_URL" ARCSTORE_PG_PORT="$ARCSTORE_PORT" \
      ARCSTORE_PYTHON="$VENV_PY" ARCSTORE_REQUIRE_SCHEMA=1 \
      "$RUNTIME_ROOT/scripts/install-postgres.sh"
  else
    ARCSTORE_DATABASE_URL="$ARCSTORE_DATABASE_URL" "$VENV_PY" -c '
import asyncio

from arcstore.backends import open_backend


async def main() -> None:
    backend = open_backend()
    await backend.start()
    await backend.stop()


asyncio.run(main())
'
    ok "managed ArcStore PostgreSQL schema and connection smoke check passed"
  fi
elif [ -n "${ARCSTORE_DATABASE_CREDENTIAL_REF:-}" ]; then
  ok "ArcStore database resolved by vault coordinate ${ARCSTORE_DATABASE_CREDENTIAL_REF}"
else
  fail "ArcStore requires ARCSTORE_DATABASE_URL or ARCSTORE_DATABASE_CREDENTIAL_REF"
fi

# --- 5c. optional arcmemory pgvector index -------------------------------
# This legacy opt-in remains separate from ArcStore: it gets a different
# container, named volume, port, database, and DSN. Enabling it never changes
# the ArcStore URL above.
MEMORY_INDEX_BACKEND="$(grep -m1 '^ARC_MEMORY_INDEX_BACKEND=' "$ENV_FILE" | cut -d= -f2- || true)"
if [ "$MEMORY_INDEX_BACKEND" = "postgres" ]; then
  MEMORY_PASSWORD="$(grep -m1 '^POSTGRES_PASSWORD=' "$ENV_FILE" | cut -d= -f2- || true)"
  [ -n "$MEMORY_PASSWORD" ] || fail \
    "ARC_MEMORY_INDEX_BACKEND=postgres but POSTGRES_PASSWORD missing from $ENV_FILE"
  MEMORY_USER="$(grep -m1 '^POSTGRES_USER=' "$ENV_FILE" | cut -d= -f2- || true)"
  MEMORY_USER="${MEMORY_USER:-arc}"
  MEMORY_DB="$(grep -m1 '^POSTGRES_DB=' "$ENV_FILE" | cut -d= -f2- || true)"
  MEMORY_DB="${MEMORY_DB:-arcmemory}"
  MEMORY_PORT="$(grep -m1 '^ARC_MEMORY_PG_PORT=' "$ENV_FILE" | cut -d= -f2- || true)"
  MEMORY_PORT="${MEMORY_PORT:-5433}"
  POSTGRES_PASSWORD="$MEMORY_PASSWORD" POSTGRES_USER="$MEMORY_USER" \
    POSTGRES_DB="$MEMORY_DB" ARC_MEMORY_PG_PORT="$MEMORY_PORT" \
    "$RUNTIME_ROOT/scripts/install-memory-postgres.sh"
  if ! grep -q '^ARC_MEMORY_PG_DSN=' "$ARC_ENV"; then
    ( umask 077
      printf 'ARC_MEMORY_PG_DSN=postgresql://%s:%s@127.0.0.1:%s/%s\n' \
        "$MEMORY_USER" "$MEMORY_PASSWORD" "$MEMORY_PORT" "$MEMORY_DB" >> "$ARC_ENV" )
    ok "memory index DSN stored in $ARC_ENV (0600)"
  fi
  set -a
  # shellcheck disable=SC1090
  . "$ARC_ENV"
  set +a
fi

# --- 6. arc init -----------------------------------------------------------
if [ -f "$ARC_CONFIG_DIR/config/gateway.toml" ]; then
  ok "arc init already run — leaving ~/.arc/config/*.toml as-is"
else
  log "arc init --tier $TIER --provider $PROVIDER..."
  "$ARC_BIN" init --tier "$TIER" --provider "$PROVIDER"
fi

# --- 7. config overlays (idempotent — safe to re-run every time) -----------
OVERLAYS="$RUNTIME_ROOT/scripts/deploy_node_overlays.py"
log "Applying user-wide config overlays..."
"$VENV_PY" "$OVERLAYS" agent-config \
  "$ARC_CONFIG_DIR/config/arcagent.toml" --provider "$PROVIDER" --model "${AGENT_MODEL#*/}"

"$VENV_PY" "$OVERLAYS" arcstore-config \
  "$ARC_CONFIG_DIR/config/arcagent.toml" --credential-ref "${ARCSTORE_DATABASE_CREDENTIAL_REF:-}"
if [ "$MEMORY_INDEX_BACKEND" = "postgres" ]; then
  "$VENV_PY" "$OVERLAYS" memory-config \
    "$ARC_CONFIG_DIR/config/arcagent.toml" --index-backend postgres
fi

GATEWAY_ARGS=(gateway-config "$ARC_CONFIG_DIR/config/gateway.toml")
if [ "$ENABLE_TELEGRAM" = "1" ]; then
  GATEWAY_ARGS+=(--enable-telegram)
  if [ -n "$TELEGRAM_ALLOWED_USER_IDS" ]; then
    # shellcheck disable=SC2206
    IDS=($TELEGRAM_ALLOWED_USER_IDS)
    GATEWAY_ARGS+=(--allowed-user-ids "${IDS[@]}")
  fi
fi
"$VENV_PY" "$OVERLAYS" "${GATEWAY_ARGS[@]}"

# --- 8. agent create — one or more, first one wins gateway routing --------
mkdir -p "$TEAM_ROOT"
for AGENT_NAME in "${AGENT_NAMES[@]}"; do
  if [ -d "$TEAM_ROOT/$AGENT_NAME" ]; then
    ok "$TEAM_ROOT/$AGENT_NAME already exists"
  else
    log "Creating agent $AGENT_NAME ($AGENT_MODEL)..."
    "$ARC_BIN" agent create "$AGENT_NAME" --dir "$TEAM_ROOT" --model "$AGENT_MODEL"
  fi
  "$VENV_PY" "$OVERLAYS" agent-config \
    "$TEAM_ROOT/$AGENT_NAME/arcagent.toml" --provider "$PROVIDER" --model "${AGENT_MODEL#*/}"
  "$VENV_PY" "$OVERLAYS" arcstore-config \
    "$TEAM_ROOT/$AGENT_NAME/arcagent.toml" --credential-ref "${ARCSTORE_DATABASE_CREDENTIAL_REF:-}"
  "$ARC_BIN" agent build "$TEAM_ROOT/$AGENT_NAME" --check
done

# gateway.toml routes remote-platform DMs (Telegram etc.) to ONE agent_did.
#
# An EXISTING value is left alone. Deriving it from ${AGENT_NAMES[0]} made the
# roster's argument order load-bearing configuration: re-running a live box with
# an alphabetical roster silently repointed the Telegram bot from josh_agent to
# coder_agent, and every other check stayed green — fleet count, health, and the
# per-platform block that carries its own override. Caught on the DGX only
# because the value was recorded beforehand and diffed after.
#
# Set ARC_GATEWAY_AGENT=<name> to change it deliberately. Order never decides.
EXISTING_DID="$("$VENV_PY" -c '
import sys, tomllib
from pathlib import Path
p = Path(sys.argv[1])
if not p.exists():
    print("")
else:
    with p.open("rb") as f:
        print(tomllib.load(f).get("gateway", {}).get("agent_did", ""))
' "$ARC_CONFIG_DIR/config/gateway.toml")"

if [ -n "$EXISTING_DID" ] && [ -z "${ARC_GATEWAY_AGENT:-}" ]; then
  ok "agent_did left as-is: $EXISTING_DID (set ARC_GATEWAY_AGENT to change it)"
else
  PRIMARY_AGENT="${ARC_GATEWAY_AGENT:-${AGENT_NAMES[0]}}"
  [ -d "$TEAM_ROOT/$PRIMARY_AGENT" ] || fail \
    "ARC_GATEWAY_AGENT='$PRIMARY_AGENT' is not an agent in $TEAM_ROOT"
  AGENT_DID="$("$VENV_PY" -c '
import sys, tomllib
with open(sys.argv[1], "rb") as f:
    print(tomllib.load(f).get("identity", {}).get("did", ""))
' "$TEAM_ROOT/$PRIMARY_AGENT/arcagent.toml")"
  [ -n "$AGENT_DID" ] || fail "could not read minted DID from $TEAM_ROOT/$PRIMARY_AGENT/arcagent.toml"
  "$VENV_PY" "$OVERLAYS" gateway-config \
    "$ARC_CONFIG_DIR/config/gateway.toml" --agent-did "$AGENT_DID"
  ok "agent_did wired into gateway.toml: $AGENT_DID ($PRIMARY_AGENT)"
fi

# --- 8b. modules ----------------------------------------------------------
# Modules do NOT ship in the wheel. Each is a separately signed bundle that
# `arc install` verifies and materializes under $ARC_CONFIG_DIR/runtime/current/modules. Skip
# this and every agent boots, answers chat, and looks healthy while its
# scheduler never fires and its tasks never dispatch — nothing crashes, so
# nothing is noticed. Idempotent, and non-zero when a config asks for a
# capability this box cannot deliver, so a hollow node never reaches systemd.
# A code change to a module ships in the runtime SOURCE, but agents load each
# module from a SIGNED BUNDLE staged in state/bundles and materialized per agent.
# `arc install` only materializes a module it finds ABSENT and only from the
# already-staged bundle, so without this a changed capabilities.py keeps running
# the old bundle forever — the module looks deployed and silently is not.
#
# On a personal box we hold the operator key, so rebuild every already-staged
# bundle from the fresh source and drop the per-agent copies; `arc install` below
# then re-materializes every module from the rebuilt bundles. Higher tiers
# receive bundles pre-signed through the supply chain and must NOT rebuild on the
# box, so they skip this and rely on a newer staged bundle being delivered.
if [ "$TIER" = "personal" ]; then
  log "Rebuilding staged module bundles from fresh source (personal tier)..."
  export ARC_MODULE_SOURCE="$RUNTIME_ROOT/packages/arcagent/src/arcagent/modules"
  shopt -s nullglob
  for bundle in "$ARC_CONFIG_DIR"/state/bundles/*.arcbundle; do
    name="$(basename "$bundle" .arcbundle)"
    "$ARC_BIN" module bundle "$name" --force \
      || fail "could not rebuild module bundle '$name' from source"
  done
  shopt -u nullglob
  unset ARC_MODULE_SOURCE
  # The per-agent capability copy is create-if-absent, so drop it to force
  # `arc install` to re-copy the freshly rebuilt bundle. Only capabilities/modules
  # is removed — skills and other capability roots are left untouched.
  for agent_dir in "$TEAM_ROOT"/*/; do
    rm -rf "${agent_dir}capabilities/modules"
  done
  ok "rebuilt module bundles and cleared per-agent copies for re-materialization"
fi

log "Installing modules for every agent..."
"$ARC_BIN" install --team-root "$TEAM_ROOT" \
  || fail "arc install could not deliver every module the configs enable (see above)"

# --- 9. systemd user unit -------------------------------------------------
UNIT_DIR="$HOME/.config/systemd/user"
mkdir -p "$UNIT_DIR"
if [ "$UI_PORT" != "8420" ]; then
  sed "s/--port 8420/--port $UI_PORT/" "$RUNTIME_ROOT/deploy/systemd/arc.service" \
    > "$UNIT_DIR/arc.service"
else
  cp "$RUNTIME_ROOT/deploy/systemd/arc.service" "$UNIT_DIR/arc.service"
fi
ok "wrote $UNIT_DIR/arc.service"

# The CONNECT-proxy bridge is a second unit, and only refreshed when the box
# already runs it — installing it unasked would start a bridge with no target.
# It is refreshed rather than left alone because it ALSO executes out of the
# runtime: a DGX unit still naming %h/arc/deploy/connect-forward.py survived the
# migration pointing into the source tree, which the layout now says is
# deletable. Every agent on a proxied model dies the moment someone believes it.
if [ -f "$UNIT_DIR/arc-connect-forward.service" ]; then
  cp "$RUNTIME_ROOT/deploy/systemd/arc-connect-forward.service" \
     "$UNIT_DIR/arc-connect-forward.service"
  ok "refreshed $UNIT_DIR/arc-connect-forward.service"
  RESTART_FORWARD=1
else
  RESTART_FORWARD=0
fi

systemctl --user daemon-reload
systemctl --user enable arc.service
# `enable --now` STARTS a stopped unit but leaves a running one on its old
# runtime, so a redeploy would report success while the box still served the
# previous version. `restart` starts a stopped unit too, so it is correct for
# both a first deploy and an update.
systemctl --user restart arc.service
if [ "$RESTART_FORWARD" = "1" ]; then
  systemctl --user restart arc-connect-forward.service
  ok "restarted arc-connect-forward.service onto the new runtime"
fi
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
