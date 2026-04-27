# Changelog

All notable changes to arcskill will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.1.0] - 2026-04-26

First public release of the skill management hub. Validates, installs, scans, and locks skills for use by agents, run loops, and LLM contexts.

### Added

- **Signed install pipeline** (`hub.install`, `uninstall`) — Fetch to quarantine → Sigstore + Rekor verification → CRL check → regex/AST/semgrep/bandit scan → Firecracker/Docker dry-run sandbox → atomic activation → lock-file entry.
- **Static analysis scanner** (`hub.scan`, `ScanResult`) — regex patterns + AST inspection + optional semgrep/bandit. Returns structured verdict.
- **CRL lifecycle** (`hub.check_revocation_on_boot`, `quarantine_skill`) — Boot-time revocation check; quarantine on revocation.
- **Hub configuration** (`HubConfig`, `TierPolicy`, `HubPolicy`, `SkillSource`) — Inert unless `[skills.hub] enabled = true`.
- **Typed error hierarchy** — `HubDisabled`, `SourceNotAllowed`, `SignatureInvalid`, `CRLUnreachable`, `SandboxRequired`, `ScanVerdictFailed`, `HubLockFileCorrupted`.
- **Atomic lock file** (`HubLockFile`) — Records every installed skill with content hash, Rekor UUID, SLSA level, scan verdict, and install path.
- **Test suite** — 342 tests / 86% coverage. New: `test_dry_run_extended.py`, `test_lifecycle_extended.py`, `test_no_tier_bypass.py`, `test_scanner_extended.py`, `test_verify_internal.py`.
- **README** — Layer position, public surface, scope, deferred-features list.

### Security

- **No tier bypass** — `test_no_tier_bypass.py` enforces ADR-019: skill verification cannot be skipped at any tier. `UnsafeNoOp` eliminated.
- **Default deny** — Hub is inert by default; explicit opt-in required to install skills.

### Deferred (wave-3)

- GEPA improvement loop relocation from arcagent
- Eval harness for automated skill quality scoring
- Three-target skill loaders (LLM context / arcrun loop / arcagent workspace)
- Upgrade workflow (`arc skill upgrade`)
- Full version-control beyond lock file (semver history, rollback)

## [0.0.1] - prior

Initial scaffolding. No public API.
