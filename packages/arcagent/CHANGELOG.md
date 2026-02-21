# Changelog

All notable changes to ArcAgent will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.2.0] - 2026-02-21

### Added

- **Biological memory module** — Long-term identity-aware memory system (`bio_memory/`). Tracks agent identity, episodic memory, and working memory across sessions. Includes:
  - `IdentityManager` — Persistent agent identity with traits, preferences, and behavioral patterns.
  - `WorkingMemory` — Session-scoped scratchpad for in-progress reasoning and intermediate state.
  - `Consolidator` — Promotes working memory to long-term episodic storage with relevance scoring.
  - `Retriever` — Context-aware memory retrieval with recency, relevance, and importance weighting.
  - `MODULE.yaml` — Declarative module manifest for Module Bus registration.
- **Shared text sanitizer** — `utils/sanitizer.py` provides `sanitize_text()` with NFKC normalization, zero-width character stripping, and control character removal. Centralizes ASI-06 (Memory & Context Poisoning) defense across all modules.
- **Bio memory CLI commands** — `arc agent bio_memory status|identity|episodes|working` for inspecting biological memory state.
- **Bio memory integration tests** — End-to-end tests for memory lifecycle (write, consolidate, retrieve) and retrieval accuracy.
- **Bio memory unit tests** — Component-level tests for identity manager, working memory, consolidator, retriever, and config.

### Changed

- **Entity extractor** — Refactored `_sanitize_fact_text()` to use shared `sanitize_text()` utility instead of inline implementation. Same defense, less duplication.
- **CLI agent commands** — Registered `bio_memory` as a lazy module group with `status`, `identity`, `episodes`, `working` subcommands.

### Security

- Centralized text sanitization prevents memory poisoning (OWASP ASI-06) with consistent NFKC normalization across entity extraction and biological memory.
- Biological memory validates all writes through the shared sanitizer before storage.

## [0.1.0] - 2026-02-01

### Added

- Initial release with core agent nucleus.
- Ed25519 cryptographic identity with W3C DID format.
- TOML-based configuration with Pydantic validation.
- OpenTelemetry traces, metrics, and structured audit events.
- Token-budgeted context manager with tiered compaction.
- Tool registry with schema validation, policy enforcement, and timeout guards.
- Event-driven module bus for extensibility.
- JSONL session persistence with retention policies.
- Markdown skill discovery and registration.
- Hot-loadable Python extensions.
- Runtime-mutable settings manager.
- Memory module with hybrid search (BM25 + vector), entity extraction, and policy engine.
- Sandboxed filesystem tools (bash, read, write, edit, ls, find, grep).
