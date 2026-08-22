# ArcStore SQLite to PostgreSQL

This is an operator-run, removable migration utility; runtime code never imports it.

```sh
ARCSTORE_DATABASE_URL="$VAULT_INJECTED_DSN" \
  uv run python tools/one_time/arcstore_sqlite_to_postgres.py \
  --sqlite /path/to/arcstore.sqlite --backup /path/to/arcstore.sqlite.bak \
  --dry-run --report /tmp/arcstore-migration-dry-run.json
```

Run again without `--dry-run` to write. Use `--resume` only after an interrupted
run or to validate an idempotent rerun. The utility accepts a destination DSN only
through `--postgres-env` (default `ARCSTORE_DATABASE_URL`), so a vault injector can
provide the secret without a CLI argument or report exposure.

It fails closed for unknown SQLite tables, invalid canonical inbox payloads, stale
checkpoints, key collisions, count/digest mismatches, or a destination not at
ArcStore schema v2. The JSON report records mapped and skipped source tables without
emitting credentials.
