# Changelog

All notable changes to ArcTeam will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Fixed

- **`MessagingService.create_channel` overwrote an existing channel's membership on a name
  collision** instead of refusing. Any caller bypassing the CLI's own pre-check could silently
  clobber membership on a duplicate name. The service now reads first and raises `ValueError`,
  matching `TeamStore.create`'s duplicate-team convention — a defense-in-depth fix for the
  check-then-write race the CLI's pre-check alone couldn't close.
- **`NatsBackend.connect` bounds the initial connect (F9)** — `asyncio.wait_for` (3s) so an
  unreachable server fails fast instead of looping nats-py's ~2min reconnect budget, plus a quiet
  `error_cb` so nats-py async errors log at debug rather than surfacing as a raw stderr traceback.
  The caller (arcagent messaging bootstrap) degrades to the in-memory bus with a single warning.

### Security

- **`TeamFileStore` path-traversal hardening** — every resolved path is checked against the
  team root (`path.resolve().is_relative_to(root.resolve())`) before a read or write, closing
  an entity-ID/filename-controlled path-traversal / symlink-escape gap.
- **Poison-message guard now covers parsing**, not just post-parse validation.

### Changed

- **Registry resolve caching completed** — `mentions` now resolves against a **per-send
  entity snapshot** from the registry cache instead of re-querying per mention.
- **BM25 memory-search index reuse** — `memory/search_engine.py` no longer rebuilds the index
  on every query.

### Removed

- **Unwired `Roster` / presence surface deleted** (`roster.py`, `test_roster.py`,
  `test_presence.py`) — dead code with no live caller; entity/role tracking already lives in
  `registry.py`.

## [0.5.0] - 2026-07-06

SPEC-038 sub-scope C: the messenger enforces Bell-LaPadula "no write down", and the duplicate classification ladder is consolidated into arctrust.

### Added
- `Message.classification` (defaults `UNCLASSIFIED`); `Entity.clearance` / `Channel.clearance`.
- `MessagingService.send` refuses delivery when the recipient/channel clearance does not dominate the message classification (`message.classification_refused` + audit + DLQ). `MessagingService(strict_classification=...)` fails closed on an unresolvable recipient clearance / unknown label at federal.

### Changed
- `arcteam.memory` now imports the canonical `Classification` from arctrust; the duplicate IntEnum in `arcteam.memory.types` is deleted (single lattice, no divergence).

## [0.4.0] - 2026-07-06

SPEC-037: the audit chain's chained HMAC is replaced by a per-record asymmetric signature.

### Changed
- **`AuditLogger(backend, signer: Signer)`** — each record is signed over `prev_signature || canonical(record)` with an arctrust `Signer` (Ed25519). `verify_chain` verifies each record against the operator public key (not the key embedded in the record), so a record re-signed under a substituted key fails. `_compute_record_hmac`, `load_hmac_key`, the `hmac`/`hashlib` imports, and the `ARCTEAM_HMAC_KEY` fallback are **deleted** (REQ-002). Legitimate non-signing HMAC is unaffected (there is none in arcteam signing paths).
- **`AuditRecord`** drops `hmac_sha256`; adds `signature`, `public_key`, `algorithm`, `key_ref`.
- `TeamConfig.hmac_key_env` removed (vestigial — the audit chain no longer uses a shared secret).

## [0.3.0] - 2026-04-26

Audit migration to arctrust, UI reporter wiring, and README refresh.

### Added

- **UI reporter wiring test** — `tests/unit/test_uireporter_wiring.py` regression-tests team event delivery to the live ArcUI dashboard.

### Changed

- **Audit emission migrated to arctrust** — `AuditLogger` now wraps `arctrust.audit.emit(AuditEvent, sink)`. Single canonical schema across all team operations (entity registration, channel creation, message send, memory mutation). HMAC-signed local stream still available; signed-chain and UI-bridge sinks now plug in without code changes.
- **Public API exports** — Updated `__init__` exports for the new audit interface.
- **README rewritten** — Marketing prose replaced with a focused layer-position + public-surface reference.
- **PyPI packaging** — GitHub Actions publish workflow.

## [0.2.0] - 2026-02-21

### Added

- **Team memory subsystem** — Institutional knowledge management for multi-agent teams (`memory/`):
  - `TeamMemoryConfig` — Pydantic configuration with tier-based defaults and entity directory management.
  - `MemoryStorage` — Async entity file storage with markdown frontmatter parsing and atomic writes.
  - `IndexManager` — Entity index with dirty-state tracking, incremental updates, and full rebuild capability.
  - `SearchEngine` — BM25 text search with wiki-link traversal for graph-based knowledge discovery. Multi-hop results with relevance scoring.
  - `TeamMemoryService` — High-level service facade for entity CRUD, search, and index management.
  - `PromotionGate` — Quality gate for promoting agent-local memory to team-shared knowledge. Confidence thresholds, deduplication, and review workflows.
  - `Classification` — Data classification types for CUI/FOUO/Unclassified memory entries.
  - Type definitions for `EntityEntry`, `SearchResult`, `MemoryTier`, and related models.
  - Standalone CLI via `arc-memory` entry point.
- **Public API exports** — `TeamMemoryConfig` and `TeamMemoryService` added to `arcteam.__init__.__all__`.

### Changed

- **Dependencies** — Added `python-frontmatter>=1.0` and `rank-bm25>=0.2.2` for markdown entity parsing and text search.
- **Entry points** — Added `arc-memory` CLI entry point for standalone team memory management.
- **Ruff config** — Added lint exception for `src/arcteam/memory/cli.py` (T201 — print in CLI).

## [0.1.0] - 2026-02-01

### Added

- Initial release with four collaboration primitives.
- Async messaging with structured envelopes, channels, roles, threading, and lifecycle tracking.
- Task engine with status workflow, subtask delegation, and output linking.
- Knowledge base with bidirectional linking, typed entries, and backlink traversal.
- File store with searchable manifest and cross-references.
- Universal addressing via typed URIs (`agent://`, `task://`, `kb://`, `file://`, `msg://`, `channel://`, `role://`).
- Entity registry with role-based access control.
- Append-only audit trail (NIST 800-53 AU-2/AU-3/AU-9 compliant).
- `StorageBackend` protocol with `FileBackend` and `MemoryBackend` implementations.
- CLI via `arc-team` entry point.
