# ADR-020: arcgateway Owns the Data Plane; arcui Is a Pure Consumer

**Status**: Accepted
**Date**: 2026-04-29
**Spec**: SPEC-022 ArcUI Agents + Agent Detail with Live Updates (D-001)
**Supersedes**: implicit pre-SPEC-022 pattern of arcui touching `team/` directly

## Context

SPEC-022 introduced UI surfaces (Agents fleet page, Agent Detail with 9 tabs, Tasks/Tools/Security/Policy fleet pages) that need to read agent state from disk: `arcagent.toml`, `workspace/policy.md`, `workspace/skills/*.md`, `workspace/sessions/*.jsonl`, etc.

Two architectural choices were on the table:

1. **arcui reads `team/<agent>/` directly** via `pathlib`/`watchfiles`. Cheapest to ship, but creates a direct file-IO surface in the presentation layer. Audit, classification, and read sandboxing would need to be re-implemented (or skipped) inside arcui. The "arcui never writes to `team/`" invariant would be a comment, not a structural guarantee.

2. **arcgateway owns the read API**; arcui calls it as a consumer. Adds a small in-process module boundary, but:
   - Centralizes path validation, size caps, traversal blocking in one place
   - Existing audit infra (`arcgateway.audit.emit_event`, NIST AU-2 fields) ships every read for free
   - Makes the "no-team-writes" invariant **structurally enforceable** — arcui has no `Path("team/...")` constructor in the entire package; we can grep-prove it
   - Enables the future `scope: agent | team | shared` semantic (team-shared knowledge) without any arcui change

The cost of (2) is one extra module crossing per request, all in-process, all stateless. The benefit is a security boundary that compounds.

## Decision

**arcgateway is the single source of truth for the agent data plane.** Every read of `team/<agent>/...` flows through `arcgateway.fs_reader.read_file()` / `arcgateway.fs_reader.list_tree()`. `arcgateway.fs_watcher` is the only watcher; it emits `FileChangeEvent` through `arcgateway.file_events.FileEventBus`. arcui imports gateway *functions* (pure, stateless) but never touches the filesystem under `team/`.

Two CI-enforced static guards make this load-bearing:

1. `tests/test_arcui_no_team_imports.py` — parametrized grep over every `packages/arcui/src/arcui/**/*.py`. Forbidden patterns: `Path("team/...")`, `os.path.*("team/...")`, `open("team/...")`, `import watchfiles`, `from watchfiles import`. Each pattern is its own test row so a regression message names the offending pattern.

2. `tests/integration/test_no_team_writes.py` — boots arcui, exercises every read endpoint via TestClient, snapshots SHA-256 + mtime + size of every file under a synthetic `team/` before and after, asserts byte-identical.

`fs_reader` takes a `scope: agent | team | shared` argument from day one. Only `agent` is wired today; `team` and `shared` raise `NotImplementedError`. This is forward-compat for team-shared knowledge without an API churn — the call shape that arcui already issues today is the same call shape it will issue when shared scope lands.

## Consequences

### Positive

- **Single chokepoint** for path validation, size caps, classification-aware logic. One file to audit, one regression test surface.
- **Free audit** on every fs op — `gateway.fs.read`, `gateway.fs.tree`, `gateway.fs.changed` events emit through the existing NIST AU-2 sink without arcui being aware.
- **Structurally enforced invariant**: "arcui never writes under `team/`" is a `git grep` away from being a CI failure, not a code review hope.
- **Lazy + ref-counted watchers**: `WatcherManager.subscribe(agent_id)` lazy-starts a watcher on the first subscription and tears down at refcount zero. No idle CPU when nobody's looking. (D-004)
- **Polling fallback**: when `watchfiles` is unavailable (or determinism is needed in tests), the same dispatch path runs from a stdlib mtime poll loop. Same event contract; lower frequency. (D-007)

### Negative

- **One extra module boundary per request**. Negligible — same Python interpreter, no network hop, no serialization tax.
- **arcui depends on arcgateway** at the package level (`arcgateway>=0.2` in `arcui/pyproject.toml`). Anyone vending arcui standalone would have to vend gateway too. Acceptable: the use cases that motivated SPEC-022 (multi-agent observability) fundamentally require both.

### Out of scope

- An HTTP boundary between arcui and arcgateway. They run in the same process today (D-022-C). If we ever split them, the routes already only call gateway *functions*, so the seam is small.
- Write paths through gateway. `fs_reader` is read-only by structure — there is no `write_file`, no `mkdir`, no API for mutation. Writers (the agent itself, `arccli`, etc.) own their writes; gateway is the read-side observer.

## References

- SPEC-022 §README "Hard Architectural Invariants"
- SPEC-022 SDD §2 "Module Boundaries"
- SPEC-022 SDD §4.1–4.6 (`fs_reader`, `fs_watcher`, `policy_parser`, `team_roster`, `agent_config`, `file_events`)
- SPEC-022 README §"Decision Log" entries D-022-A, D-022-B, D-022-C, D-022-D
- ADR-019 (Four Pillars) — the audit pillar is what makes this boundary structurally enforce-able for free
