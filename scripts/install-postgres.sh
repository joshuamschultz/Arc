#!/usr/bin/env bash
# Ensure the dedicated local PostgreSQL service used by ArcStore is running.
#
# The container and named volume are deliberately ArcStore-specific. This
# script never writes a DSN and never prints a password; deploy-node.sh keeps
# the DSN in the protected runtime environment instead.
#
# Required environment:
#   ARCSTORE_DATABASE_PASSWORD (or POSTGRES_PASSWORD) — role password
#
# Optional environment:
#   ARCSTORE_PG_USER       default arcstore
#   ARCSTORE_PG_DATABASE   default arcstore
#   ARCSTORE_PG_CONTAINER  default arcstore-postgres
#   ARCSTORE_PG_VOLUME     default arcstore-pg-data
#   ARCSTORE_PG_PORT       default 5432
#   ARCSTORE_PG_IMAGE      default postgres:16
#   ARCSTORE_PYTHON        runtime Python used to apply/check the ArcStore schema
#   ARCSTORE_REQUIRE_SCHEMA=1 to fail when the schema has not been migrated

set -euo pipefail

log()  { echo "→ $*"; }
ok()   { echo "  ✓ $*"; }
fail() { echo "  ✗ $*" >&2; exit 1; }

PG_USER="${ARCSTORE_PG_USER:-arcstore}"
PG_DB="${ARCSTORE_PG_DATABASE:-arcstore}"
PG_CONTAINER="${ARCSTORE_PG_CONTAINER:-arcstore-postgres}"
PG_VOLUME="${ARCSTORE_PG_VOLUME:-arcstore-pg-data}"
PG_PORT="${ARCSTORE_PG_PORT:-5432}"
PG_IMAGE="${ARCSTORE_PG_IMAGE:-postgres:16}"
PG_PASSWORD="${ARCSTORE_DATABASE_PASSWORD:-${POSTGRES_PASSWORD:-}}"
if [ -z "$PG_PASSWORD" ] && [ -n "${ARCSTORE_DATABASE_URL:-}" ]; then
  PYTHON_BIN="${ARCSTORE_PYTHON:-python3}"
  command -v "$PYTHON_BIN" >/dev/null 2>&1 || fail \
    "python is required to recover the local ArcStore password from ARCSTORE_DATABASE_URL"
  PG_PASSWORD="$("$PYTHON_BIN" -c '
import sys
from urllib.parse import unquote, urlparse

parsed = urlparse(sys.argv[1])
print(unquote(parsed.password or ""))
' "$ARCSTORE_DATABASE_URL")"
fi

[ -n "$PG_PASSWORD" ] || fail \
  "ARCSTORE_DATABASE_PASSWORD is unset — refusing to start ArcStore PostgreSQL without a password"
command -v docker >/dev/null 2>&1 || fail \
  "docker not found — install Docker or provide ARCSTORE_DATABASE_URL for managed PostgreSQL"

if docker ps --filter "name=^/${PG_CONTAINER}$" --filter "status=running" --format '{{.Names}}' |
  grep -qx "$PG_CONTAINER"; then
  ok "ArcStore PostgreSQL container already running: $PG_CONTAINER"
elif docker ps -a --filter "name=^/${PG_CONTAINER}$" --format '{{.Names}}' | grep -qx "$PG_CONTAINER"; then
  log "starting existing ArcStore PostgreSQL container $PG_CONTAINER..."
  docker start "$PG_CONTAINER" >/dev/null
  ok "ArcStore PostgreSQL container started: $PG_CONTAINER"
else
  log "creating ArcStore PostgreSQL container $PG_CONTAINER ($PG_IMAGE)..."
  docker run -d --name "$PG_CONTAINER" --restart unless-stopped \
    -e POSTGRES_USER="$PG_USER" \
    -e POSTGRES_PASSWORD="$PG_PASSWORD" \
    -e POSTGRES_DB="$PG_DB" \
    -p "127.0.0.1:${PG_PORT}:5432" \
    -v "${PG_VOLUME}:/var/lib/postgresql/data" \
    "$PG_IMAGE" >/dev/null
  ok "ArcStore PostgreSQL container created with durable volume $PG_VOLUME"
fi

log "waiting for ArcStore PostgreSQL readiness..."
for _ in $(seq 1 60); do
  if docker exec "$PG_CONTAINER" pg_isready -U "$PG_USER" -d "$PG_DB" >/dev/null 2>&1; then
    break
  fi
  sleep 1
done
docker exec "$PG_CONTAINER" pg_isready -U "$PG_USER" -d "$PG_DB" >/dev/null 2>&1 \
  || fail "ArcStore PostgreSQL did not become ready within 60s"

docker exec -e PGPASSWORD="$PG_PASSWORD" "$PG_CONTAINER" \
  psql -U "$PG_USER" -d "$PG_DB" -v ON_ERROR_STOP=1 -Atqc "SELECT 1" | grep -qx 1 \
  || fail "ArcStore PostgreSQL health query failed"
ok "ArcStore PostgreSQL accepts authenticated queries"

# The application owns the schema migration. When the deployed runtime is
# available, start/stop its backend once so the check proves both connectivity
# and the packaged schema. A standalone install can opt into the same strict
# check after the runtime has migrated the database.
if [ -n "${ARCSTORE_PYTHON:-}" ]; then
  ARCSTORE_DATABASE_URL="${ARCSTORE_DATABASE_URL:-postgresql://${PG_USER}:$PG_PASSWORD@127.0.0.1:${PG_PORT}/${PG_DB}}" \
    "$ARCSTORE_PYTHON" -c '
import asyncio

from arcstore.backends import open_backend


async def main() -> None:
    backend = open_backend()
    await backend.start()
    await backend.stop()


asyncio.run(main())
'
  ok "ArcStore schema migration and connection smoke check passed"
fi

SCHEMA_PRESENT="$(docker exec -e PGPASSWORD="$PG_PASSWORD" "$PG_CONTAINER" \
  psql -U "$PG_USER" -d "$PG_DB" -Atqc \
  "SELECT EXISTS (SELECT 1 FROM pg_catalog.pg_tables WHERE schemaname = 'public' AND tablename = 'arcstore_schema');" \
  2>/dev/null || true)"
if [ "$SCHEMA_PRESENT" = "t" ]; then
  ok "ArcStore schema table present"
elif [ "${ARCSTORE_REQUIRE_SCHEMA:-0}" = "1" ]; then
  fail "ArcStore schema table is missing — run the ArcStore runtime migration first"
else
  log "ArcStore schema migration pending (the runtime applies it on startup)"
fi

ok "ArcStore PostgreSQL is ready"
