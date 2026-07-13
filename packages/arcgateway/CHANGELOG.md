# Changelog

All notable changes to arcgateway will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- **Cross-surface slash-command framework** — an in-chat slash-command a user types to the agent
  (Slack, Telegram, web) resolves through one shared `CommandRegistry`; a message is handled only
  when a *registered* command matches (an unknown `/foo` falls through to the agent as normal
  text). Adding a command is a one-liner (`registry.register(MyCommand())`).
- **`/new` (alias `/reset`) — session rotation.** Starts a fresh conversation by bumping a
  per-session **generation** folded into the deterministic session key (`SessionEpochStore`), so
  the new generation hashes to a new, empty session. No file is reset — minting a new key *is* the
  reset, and the old conversation stays resumable. Generations persist across gateway restarts (a
  db-backed store) so "New session" doesn't silently un-rotate on the next bounce.
- **`/help`** — lists the currently registered slash-commands, generated from the live registry
  (no hardcoded list to drift).
- **Slack slash-command intake** — the Slack adapter subscribes the registered command names as
  native slash-commands and re-injects them as inbound `InboundEvent`s, so the same registry drives
  both typed `/new` text and Slack's native command UI.

### Fixed

DM pairing and standalone-daemon fixes from a live single-node + four-agent-fleet deployment:

- **DM pairing was completely inert end-to-end.** `SessionRouter` never received a
  `PairingStore` (the `PairingInterceptor` was a permanent no-op) and `[security].require_pairing`
  had zero readers. `GatewayRunner.from_config` now builds the store from `[pairing].db_path` and
  wires it through; approvals persist to a `pairing_approvals` SQLite table so `arc gateway pair
  approve` in another process takes effect on the live gateway without IPC. `verify_and_consume`
  requires an Ed25519 operator signature at every tier, but nothing ever registered or signed as
  one — `arc identity init` now self-registers via `arctrust.trust_store.register_operator()`, and
  the approve handler signs with that authority. Also fixed: `PairingInterceptor` sent a raw
  `chat_id` where the adapter Protocol expects a `DeliveryTarget` (an `AttributeError` a broad
  `except` was swallowing, caught only by a new real-adapter E2E test).
- **Static `allowed_user_ids` were ignored even when configured correctly.** With
  `require_pairing=true`, an allowlisted user was still forced through the DM-pairing flow —
  neither `SessionRouter` construction site passed a `user_allowlist`. New
  `build_user_allowlist` maps each enabled platform's `allowed_user_ids` into that platform's own
  `user_did` scheme (Telegram `did:arc:telegram:{id}`; Slack `slack:{id}`; Mattermost is
  channel-based, N/A) and threads it through at both sites — only under `require_pairing`, and
  returning `None` (never an empty set) so unconfigured deployments keep default-open behavior.
- **Slack silently dropped unauthorized users** instead of forwarding them into the pairing flow
  (the same bug already fixed for Telegram) — `require_pairing` now forwards; `require_pairing`
  disabled preserves the drop, now with an audited `gateway.adapter.auth_rejected` event (Slack
  had none before). `arc gateway pair` CLI commands now correctly gate on
  `security.require_pairing` — `gateway.pairing.enabled` never existed in the schema.
- **Standalone `arcgateway start` now refuses at every tier**, not just personal/enterprise.
  Federal's `SubprocessExecutor` worker (`arc-agent-worker`) accepted `--did` but never used it to
  select a config — it always loaded from three fixed paths, so a federal standalone gateway would
  have silently served the *wrong* agent identity on any multi-agent deployment. `cmd_start` now
  unconditionally refuses, naming both failure modes and the correct embedded invocation
  (`arc ui start --team-root --gateway-config`); `[gateway].team_root` and `--team-root` are
  removed from the standalone path (dead once it doesn't serve). Superseded an earlier, narrower
  fix (95b7456) that had standalone fail closed only at personal/enterprise by building a real
  `agent_factory` from `team_root` — since the federal path is also unfixable without changing
  `arc-agent-worker` itself, standalone is blocked everywhere instead.
- **A shared `[gateway].agent_did` silently re-routed every platform on restart.** A fleet
  deployment rewriting the default `agent_did` and restarting changed routing for *every* platform
  sharing that default with no signal it had happened. `build_adapters` now emits a
  `gateway.adapter.shared_default_agent_did` warning per platform lacking an explicit
  per-block override.
- **Runtime/pairing paths ignored `ARC_CONFIG_DIR`.** `GatewaySection.runtime_dir` and
  `PairingSection.db_path` now resolve relative to `ARC_CONFIG_DIR` like every other subsystem
  instead of hardcoding `~/.arc`.

### Changed

- **Pairing/allowlist construction relocated out of the LOC-budget-gated core.** `from_config`'s
  `require_pairing` branch, `PairingStore` construction, and allowlist build moved to the
  non-gated `pairing_allowlist.build_pairing_wiring(config, tier)` — one call from the gated
  `runner.py`, behavior pinned by the existing test suite. No behavior change.
- **`agent_did` now threads through the Slack and Mattermost adapters**, mirroring what
  Telegram already did — `test_dual_adapter_chat` asserts the wired DID instead of the old
  hardcoded `""`.
- **Shared exponential-backoff helper** (`adapters/_backoff.py`) — the Telegram adapter's
  hand-rolled formula and `_reconnect.py` now share one implementation; distinct per-adapter
  policies stay parameterized, not collapsed into one.
- Ed25519 signature checks now go through `arctrust.verify` instead of a hand-rolled PyNaCl
  call.
- Telegram's message splitting now uses the shared `_text` splitter helper.

### Removed
- **Unreachable Telegram retry-escalation path and the dead `DeliverySenderImpl` plumbing**
  — no live caller; `delivery.py` shrank accordingly. `DeliveryTarget` (the parsed
  `"platform:chat_id[:thread_id]"` address) is unaffected and still exported.

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
