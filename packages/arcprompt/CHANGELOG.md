# Changelog

All notable changes to arcprompt will be documented in this file.

## [Unreleased]

## [0.1.0] - 2026-08-19

### Added

- **Editable, signed, inspectable system prompts (SPEC-047).** Full storage +
  resolution API, replacing the scaffolding.
  - `PromptCatalog` — discover every stock prompt across installed packages
    (`DEFAULT_PROMPT_PACKAGES` = arcrun, arcagent, arcmemory, arcskill).
  - `PromptResolver` — two-layer overlay-over-stock resolution, first-match-wins,
    with mandatory Ed25519 signature pinning (unpinned key fails closed).
  - `PromptSnapshot` / `snapshot()` — freeze every resolved prompt once per run
    and emit one prompt-provenance audit event (package, name, source, sha256,
    resolved overlay signer DID; tier from the resolver's posture).
  - `SignatureVerifier` / `TrustPosture` — pinned-key verification, unconditional
    across personal/enterprise/federal tiers.
  - `PromptDocument` / `PromptFrontmatter`, `parse_prompt` / `render_prompt`,
    `load_stock` / `load_stock_document` — document parse/author/load helpers.
  - Fail-loud errors: `PromptError`, `PromptMissing`, `PromptUnparseable`,
    `PromptUnsigned` — a broken overlay never silently falls back to stock.
- **Wheel packaging guarantee** — stock prompt markdown ships in the wheel via
  `artifacts = ["src/arcprompt/**/*.md"]` (REQ-140).

### Changed

- Version identity is the `sha256` of raw file bytes, never an authored field.
- Docs refreshed for the shipped API (2026-08-19).

## [0.0.2] - 2026-04-26

### Added

- **README** — Layer position; reserved as the future home of the strategy prompt provider. Strategy-prompt logic currently lives in `arcrun.prompts` (`get_strategy_prompts`); arcprompt exposes no public API beyond `__version__`.

### Status

- Scaffolding with minimal implementation. Public API not yet stable.
