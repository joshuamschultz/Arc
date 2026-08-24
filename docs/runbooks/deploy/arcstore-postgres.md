# ArcStore PostgreSQL provisioning

ArcStore uses PostgreSQL for its durable operational store. The local path and
Supabase path use the same `PostgresBackend`; only the connection URL changes.
The deployment script is safe to run repeatedly: it reuses the named container,
named volume, runtime environment file, and migrated schema.

## Local first-alpha node

`scripts/deploy-node.sh` provisions a dedicated `arcstore-postgres` container
with the `arcstore-pg-data` named volume, binds PostgreSQL to loopback, waits
for `pg_isready`, runs an authenticated query, and starts the ArcStore backend
once to apply and verify the packaged schema. The database and role are both
named `arcstore` by default.

The deploy creates `~/arc/config/arc.env` with mode `0600`. The complete
`ARCSTORE_DATABASE_URL` is written only there and is passed to the runtime via
its environment; it is never put in TOML, command output, or the repository.
Provide a password in the source environment file or let the deploy generate a
random one:

```dotenv
ANTHROPIC_API_KEY=...
# Optional; deploy-node.sh generates one when omitted.
ARCSTORE_DATABASE_PASSWORD=...
```

Run the normal deployment:

```bash
scripts/deploy-node.sh
```

The provisioning script is independently re-runnable for release checks. Set
`ARCSTORE_PYTHON` to the installed runtime Python to require the application
schema smoke check; set `ARCSTORE_REQUIRE_SCHEMA=1` when a migrated schema must
already exist.

```bash
set -a
. "$HOME/arc/config/arc.env"
set +a
ARCSTORE_PYTHON="$HOME/.arc/runtime/current/.venv/bin/python" \
  "$HOME/.arc/runtime/current/scripts/install-postgres.sh"
```

The command output contains health and schema status only. It does not print a
DSN or a password. Do not use `docker compose down -v` on this volume unless
destroying ArcStore data is intentional.

If the optional arcmemory pgvector index is enabled with
`ARC_MEMORY_INDEX_BACKEND=postgres`, deployment uses
`scripts/install-memory-postgres.sh` separately (port `5433`, database
`arcmemory`, and volume `arc-memory-pg-data`). It never reuses the ArcStore
container or DSN.

## Supabase direct and transaction pooler URLs

Put one of these values in the protected source environment file before running
`deploy-node.sh`; the deploy copies it to `arc.env` with mode `0600`:

```dotenv
# Direct connection: session features are available.
ARCSTORE_DATABASE_URL=postgresql://postgres:<password>@db.<project-ref>.supabase.co:5432/postgres?sslmode=require

# Transaction pooler: use the Supavisor transaction host and port 6543.
ARCSTORE_DATABASE_URL=postgresql://postgres.<project-ref>:<password>@aws-0-us-east-1.pooler.supabase.com:6543/postgres?sslmode=require
```

External PostgreSQL hosts require TLS. The ArcStore config rejects `sslmode`
values that disable or weaken TLS, and the adapter sets `statement_cache_size=0`
for port `6543` because prepared statements do not survive transaction pooling.
Direct connections retain the normal statement cache. URL-encode reserved
characters in the username or password.

For a vault-backed deployment, provide a coordinate instead of a URL:

```dotenv
ARCSTORE_DATABASE_CREDENTIAL_REF=vault://arcstore/database
```

The coordinate is written to the non-secret `[arcstore]` config block. The vault
resolver supplies the URL at runtime; the deploy does not attempt a managed
database smoke check without a resolved secret.

## Release smoke checks

The focused deterministic checks are:

```bash
bash -n scripts/install-postgres.sh scripts/deploy-node.sh
uv run pytest packages/arcstore/tests/unit/test_provisioning.py
```

For a live database, `ARCSTORE_PYTHON=... scripts/install-postgres.sh` proves
connectivity and migration, then the script verifies that `arcstore_schema` is
present. The command is idempotent and leaves credentials in the protected
runtime environment only.
