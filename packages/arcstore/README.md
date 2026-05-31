# arcstore

Operational / observability data plane for Arc.

`arcstore` is **ambient infrastructure**: any `arcllm`, `arcrun`, or `arcagent`
usage is auto-recorded to an always-on local **spool** the moment it happens —
independent of whether any store, server, DB, or UI is running. A later-started
store layer backfills from the spool (and the `arctrust` WORM) into a queryable
backend (SQLite by default) that the UIs read.

Two layers:

- **`arcstore.spool`** — always-on, server-independent, dependency-light
  (stdlib + pydantic) append-only recorder. Pulls no DB driver. The guarantee:
  *call a setup arcllm from the CLI now, spin up arcstore later, see the call.*
- **store/query layer** (later phases) — `StorageBackend` Protocol + default
  `SqliteBackend` that backfills + tails the spool and the WORM.

See `SPEC-026` and `ADR-022` for the full design.

Import direction (never reversed): `arctrust ← arcstore ← {arcllm, arcrun, arcagent, arcui}`.
