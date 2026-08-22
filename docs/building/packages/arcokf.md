# arcokf — Open Knowledge Format

`arcokf` is the standalone typed Markdown contract for Arc knowledge artifacts.
It is intentionally below agent/runtime packages, so stores can validate before
writing without importing `arcagent` or any operational storage surface.

The package README contains the v0.2 schema and examples:
[packages/arcokf/README.md](../../../packages/arcokf/README.md).

Use `parse()` when invalid input should fail closed, or `validate()`/`lint()`
when a caller needs structured diagnostics. Operational TOML, JSON, JSONL,
signature, and audit files do not use OKF.
