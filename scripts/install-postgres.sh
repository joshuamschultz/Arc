#!/usr/bin/env bash
# scripts/install-postgres.sh — ensure a pgvector Postgres is running on this box
# for arcmemory's optional postgres index backend (SPEC-073 COMP-007).
#
# OPT-IN: deploy-node.sh calls this ONLY when the deploy .env sets
# ARC_MEMORY_INDEX_BACKEND=postgres. The default deployment uses the built-in
# sqlite + sqlite-vec index and never runs this script — nothing here touches a
# default deploy.
#
# Runs pgvector as a Docker container (the reliable, OS-agnostic way to get
# Postgres 16 + the `vector` extension), mirroring install-nats.sh's role:
# "ensure the service dependency is present," idempotent, safe to re-run. A
# container already healthy is left alone. The connection secret is NOT written
# here — deploy-node.sh writes ARC_MEMORY_PG_DSN into ~/.arc/config/arc.env
# (the same fail-closed secret path as ANTHROPIC_API_KEY).
#
# Env in:
#   POSTGRES_PASSWORD   (required)  — the arc DB role's password
#   POSTGRES_USER       (default arc)
#   POSTGRES_DB         (default arcmemory)
#   ARC_PG_CONTAINER    (default arc-postgres)
#   ARC_PG_PORT         (default 5432)     — host port the container binds
#   ARC_PG_IMAGE        (default pgvector/pgvector:pg16)

set -euo pipefail

log()  { echo "→ $*"; }
ok()   { echo "  ✓ $*"; }
fail() { echo "  ✗ $*" >&2; exit 1; }

PG_USER="${POSTGRES_USER:-arc}"
PG_DB="${POSTGRES_DB:-arcmemory}"
PG_CONTAINER="${ARC_PG_CONTAINER:-arc-postgres}"
PG_PORT="${ARC_PG_PORT:-5432}"
PG_IMAGE="${ARC_PG_IMAGE:-pgvector/pgvector:pg16}"

[ -n "${POSTGRES_PASSWORD:-}" ] || fail "POSTGRES_PASSWORD is unset — refusing to start Postgres without a password"
command -v docker >/dev/null 2>&1 || fail "docker not found — the postgres index backend is provisioned via Docker; install docker or run Postgres+pgvector yourself and set ARC_MEMORY_PG_DSN"

# Already running? Leave it alone (idempotent), just re-assert the extension.
if docker ps --filter "name=^/${PG_CONTAINER}$" --filter "status=running" --format '{{.Names}}' | grep -qx "$PG_CONTAINER"; then
  ok "postgres container already running: $PG_CONTAINER"
else
  # A stopped container of the same name: start it rather than recreate (keeps the volume).
  if docker ps -a --filter "name=^/${PG_CONTAINER}$" --format '{{.Names}}' | grep -qx "$PG_CONTAINER"; then
    log "starting existing postgres container $PG_CONTAINER..."
    docker start "$PG_CONTAINER" >/dev/null
  else
    log "creating pgvector container $PG_CONTAINER ($PG_IMAGE)..."
    docker run -d --name "$PG_CONTAINER" --restart unless-stopped \
      -e POSTGRES_USER="$PG_USER" \
      -e POSTGRES_PASSWORD="$POSTGRES_PASSWORD" \
      -e POSTGRES_DB="$PG_DB" \
      -p "127.0.0.1:${PG_PORT}:5432" \
      -v arc-pg-data:/var/lib/postgresql/data \
      "$PG_IMAGE" >/dev/null
  fi
  ok "postgres container up: $PG_CONTAINER"
fi

# Wait for readiness, then ensure the pgvector extension exists (idempotent).
log "waiting for postgres to accept connections..."
for _ in $(seq 1 30); do
  if docker exec "$PG_CONTAINER" pg_isready -U "$PG_USER" -d "$PG_DB" >/dev/null 2>&1; then
    break
  fi
  sleep 1
done
docker exec "$PG_CONTAINER" pg_isready -U "$PG_USER" -d "$PG_DB" >/dev/null 2>&1 \
  || fail "postgres did not become ready within 30s"

docker exec -e PGPASSWORD="$POSTGRES_PASSWORD" "$PG_CONTAINER" \
  psql -U "$PG_USER" -d "$PG_DB" -v ON_ERROR_STOP=1 -c "CREATE EXTENSION IF NOT EXISTS vector;" >/dev/null \
  || fail "could not create the 'vector' extension in $PG_DB"
ok "pgvector 'vector' extension present in $PG_DB"

# The DSN the runtime uses. deploy-node.sh persists this to ~/.arc/config/arc.env;
# printed here (password redacted) so the deploy log shows what was provisioned.
echo "  → DSN: postgresql://${PG_USER}:***@127.0.0.1:${PG_PORT}/${PG_DB}"
ok "postgres index backend ready"
