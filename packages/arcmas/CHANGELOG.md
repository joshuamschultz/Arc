# Changelog

All notable changes to arcmas (the Arc meta-install package) will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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
