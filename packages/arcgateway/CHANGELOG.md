# Changelog

All notable changes to arcgateway will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.2.0] - 2026-04-26

Audit hardening, pairing-signature enforcement at every tier, and broader executor coverage.

### Added

- **`audit.py`** — Canonical audit emission via arctrust. Wraps `arctrust.audit.emit` with a module-level sink (default `NullSink`); operators wire a real sink (`JsonlSink` for compliance, `SignedChainSink` for tamper-evident federal deployments) at startup via `configure_sink()`. All gateway modules now call `emit_event()` rather than constructing `AuditEvent` directly — keeps schema central, prevents drift.
- **Adapter test coverage** — `test_adapters_slack_send_with_id.py`, `test_adapters_telegram_send_with_id.py`, `test_adapters_telegram_coverage.py` exercise the per-platform send paths with audit-event assertions.
- **Delivery-sender test** — `test_delivery_sender.py` covers `DeliveryTarget` parsing and dispatch.
- **Executor tests** — `test_executor_nats.py` and `test_executor_subprocess_module.py` cover both transports.
- **Pairing-signature personal-tier test** — `test_pairing_signature_personal_tier_pillar.py` enforces ADR-019 four-pillar Sign requirement at every tier (no `UnsafeNoOp` bypass).

### Changed

- **README rewritten** — Marketing prose replaced with a focused layer-position + public-surface reference. Highlights pairing controls and audit emission.
- **All security-relevant operations emit through arctrust** — pairing lifecycle, runner start/stop, adapter auth, delivery, execution. Single-point-of-emission per ADR-019.

### Security

- **Pairing signature required at every tier** — Personal tier still verifies; only the keyset and stringency differ from federal. `UnsafeNoOp` bypass eliminated.
- **Default `NullSink`** — Gateway never crashes if the audit sink is misconfigured at startup; operators wire a real sink before going live.

## [0.1.0] - prior

Initial gateway release: `GatewayRunner`, `SessionRouter`, `AsyncioExecutor`, platform adapters (Telegram, Slack), pairing signature primitives, NATS executor stub, subprocess executor.
