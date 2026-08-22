#!/usr/bin/env bash
# Provision the optional arcmemory pgvector index without sharing ArcStore's
# database, role, container, or volume.

set -euo pipefail

log()  { echo "→ $*"; }
ok()   { echo "  ✓ $*"; }
fail() { echo "  ✗ $*" >&2; exit 1; }

PG_USER="${POSTGRES_USER:-arc}"
PG_DB="${POSTGRES_DB:-arcmemory}"
PG_CONTAINER="${ARC_MEMORY_PG_CONTAINER:-arc-memory-postgres}"
PG_VOLUME="${ARC_MEMORY_PG_VOLUME:-arc-memory-pg-data}"
PG_PORT="${ARC_MEMORY_PG_PORT:-5433}"
PG_IMAGE="${ARC_MEMORY_PG_IMAGE:-pgvector/pgvector:pg16}"

[ -n "${POSTGRES_PASSWORD:-}" ] || fail \
  "POSTGRES_PASSWORD is unset — refusing to start the optional memory index without a password"
command -v docker >/dev/null 2>&1 || fail "docker not found — install Docker for the optional memory index"

if docker ps --filter "name=^/${PG_CONTAINER}$" --filter "status=running" --format '{{.Names}}' |
  grep -qx "$PG_CONTAINER"; then
  ok "memory pgvector container already running: $PG_CONTAINER"
elif docker ps -a --filter "name=^/${PG_CONTAINER}$" --format '{{.Names}}' | grep -qx "$PG_CONTAINER"; then
  log "starting existing memory pgvector container $PG_CONTAINER..."
  docker start "$PG_CONTAINER" >/dev/null
else
  log "creating memory pgvector container $PG_CONTAINER ($PG_IMAGE)..."
  docker run -d --name "$PG_CONTAINER" --restart unless-stopped \
    -e POSTGRES_USER="$PG_USER" -e POSTGRES_PASSWORD="$POSTGRES_PASSWORD" \
    -e POSTGRES_DB="$PG_DB" -p "127.0.0.1:${PG_PORT}:5432" \
    -v "${PG_VOLUME}:/var/lib/postgresql/data" "$PG_IMAGE" >/dev/null
  ok "memory pgvector container created with durable volume $PG_VOLUME"
fi

log "waiting for the memory index PostgreSQL readiness..."
for _ in $(seq 1 60); do
  if docker exec "$PG_CONTAINER" pg_isready -U "$PG_USER" -d "$PG_DB" >/dev/null 2>&1; then
    break
  fi
  sleep 1
done
docker exec "$PG_CONTAINER" pg_isready -U "$PG_USER" -d "$PG_DB" >/dev/null 2>&1 \
  || fail "memory index PostgreSQL did not become ready within 60s"
docker exec -e PGPASSWORD="$POSTGRES_PASSWORD" "$PG_CONTAINER" \
  psql -U "$PG_USER" -d "$PG_DB" -v ON_ERROR_STOP=1 -c \
  "CREATE EXTENSION IF NOT EXISTS vector;" >/dev/null
ok "memory pgvector extension present in $PG_DB"
