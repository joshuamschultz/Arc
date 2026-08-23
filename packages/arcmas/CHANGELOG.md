# Changelog

All notable changes to arcmas (the Arc meta-install package) will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.5.0] - 2026-08-22

- Meta-install aligned with the 0.4 alpha stack: PostgreSQL ArcStore, streaming,
  durable inbox/approvals, ArcFlow reliability, attachments and shared knowledge.

## [0.4.0] - 2026-08-19

### Added

- **`arcmemory` added to the full-stack install.** Scaffolded agents now default to
  `brain = "arcmemory"`, so the whole-stack meta-package ships the memory substrate — a fresh
  `pip install arcmas` agent has a working Brain (daily-log + episodic index + entity graph) out
  of the box. `arc-agent` keeps arcmemory an optional import (clean layering); arcmas provides it.

### Changed

- **README made honest about the install closure.** The "What's in the Box" table previously
  listed `arcgateway`, `arcui`, and `arcskill` as installed by `arcmas`. They are **not** in the
  dependency graph (`arcmas` → `arccmd` + `arcmemory`, and the CLI pulls `arcllm`, `arcrun`,
  `arc-agent`, `arcbundle`, `arcteam`, plus their transitive `arctrust`/`arcstore`/`arcprompt`).
  Those three surfaces are now documented as separate installs. Package docstring updated to match.

## [0.3.0] - 2026-04-26

Refreshed package map for the post-refactor monorepo. README rewritten around the four pillars (ADR-019).

### Changed

- **Package map updated** — Now lists the full post-refactor stack: `arctrust` (leaf), `arcllm`, `arcrun`, `arcagent`, `arcgateway`, `arcskill`, `arcteam`, `arcui`, `arccli`. Adds explicit layer-position commentary per package.
- **Four Pillars (ADR-019) made explicit** — README opens with Identity / Sign / Authorize / Audit guarantees that hold at every tier.
- **README** — Marketing prose tightened; replaces the older "what you get" section with a layered architecture overview.

### Notes

- The "arcstack" rename (referenced in the prior README) is now `arcmas`. Users should `pip install arcmas` going forward.

## [0.2.0] - prior

Initial multi-package install meta-package (named "arcstack" at the time).
