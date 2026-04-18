# Changelog

All notable changes to ArcTeam will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Changed

- **File store** — Updated file handling internals.
- **Public API exports** — Updated `__init__` exports.
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
