# arcgateway Security Reference

arcgateway ships three tiers with distinct isolation, credential, and audit postures. Set `[security] tier` in `~/.arc/gateway.toml`.

## Tier Matrix

| Aspect | personal | enterprise | federal |
|---|---|---|---|
| Executor (`arcgateway.executor`) | `AsyncioExecutor` (in-process) | `AsyncioExecutor` | `SubprocessExecutor` (subprocess per chat) |
| Credentials (`arcagent.modules.vault.resolver`) | file or env | vault preferred, env fallback w/ warn | vault required; hard error on unreachable |
| DM pairing approver | CLI | CLI | CLI + DID signature (TODO: signature verify stub) |
| Cross-session reads | N/A | per-session ACL | per-session ACL, default `private` |
| Audit events | optional | required | required (all `gateway.*` events) |
| Subprocess resource limits | n/a | n/a | `resource.setrlimit` RLIMIT_AS/CPU/NOFILE |
| NL cron LLM fallback | enabled | enabled + audit per resolve | disabled (deterministic only) |
| File perms on local creds | 0600 | 0600 | vault-only |

## Pre-Await Race Guard (Hermes PR #4926)

`SessionRouter.handle()` sets `_active_sessions[session_key] = asyncio.Event()` **synchronously before any `await`**, then `asyncio.create_task(...)`. Without this guard, 20 concurrent messages to the same session spawn 20 agent tasks. Integration test `tests/integration/test_race_regression.py` + stress `test_race_regression_stress.py` verify at n=5/10/20/50 concurrent messages for 100 consecutive runs. Zero flakes observed.

```python
# arcgateway/session.py::SessionRouter.handle()
if session_key in self._active_sessions:
    self._queue_for_session(session_key, event)
    return
self._active_sessions[session_key] = asyncio.Event()   # synchronous
asyncio.create_task(self._process_event(session_key, event))
```

## SubprocessExecutor (Federal)

Each `(user, agent)` session runs in its own OS subprocess via `arc-agent-worker` (console script registered by arccli). Parent and child communicate via JSON-lines IPC over stdin/stdout.

Resource limits (`arcgateway.executor.ResourceLimits`):

| Limit | Default | Syscall |
|---|---|---|
| `memory_mb` | 512 | `RLIMIT_AS` |
| `cpu_seconds` | 60 | `RLIMIT_CPU` |
| `file_descriptors` | 256 | `RLIMIT_NOFILE` |

Applied via `preexec_fn` on `asyncio.create_subprocess_exec`. macOS partial support (RLIMIT_AS is ignored by Darwin; per-limit exception handling keeps the other two working). Linux full support (federal target).

## DM Pairing

`arcgateway.pairing.PairingStore` — SQLite-backed:
- 8-char codes from `ABCDEFGHJKLMNPQRSTUVWXYZ23456789` (no `0/O/1/I`) via `secrets.choice()`
- 1h TTL, max 3 pending per platform
- 1 code request per user per 10 min → `PairingRateLimited`
- 5 failed approvals within 1h → `PairingPlatformLocked` (platform locked 1h)
- Raw `platform_user_id` never stored — only SHA-256-16 hash
- At federal tier, `verify_and_consume(code, approver_did=<DID>)` signature required (stub passes if DID provided; real verify is M2 work)
- Federal multi-instance: Postgres backend (`PostgresPairingStore` stub exists; SQLite default for single-instance)

## Vault Resolver

`arcagent.modules.vault.resolver.resolve_secret(name, tier, backend, env_fallback_var)`:

| Tier | Missing vault | Vault unreachable | Env fallback |
|---|---|---|---|
| federal | hard error at startup | hard error, no fallback | not attempted |
| enterprise | vault required, graceful degrade | warn + audit + env | permitted |
| personal | env → file (0600 enforced) → error | env | always |

Backends: `AzureKeyVaultBackend` (re-exported from `arcagent.modules.vault.backends.azure`), `EnvBackend`, `FileBackend` (0600 mode enforced). HashiCorp Vault is planned.

Legacy `arcagent.modules.vault_azure` still imports; emits `DeprecationWarning` pointing to `arcagent.modules.vault.backends.azure`.

## Audit Event Catalog

All emitted via `arcagent.core.telemetry` to OTel + structured logger. Required at federal + enterprise.

| Event | Emitted by | Fields |
|---|---|---|
| `gateway.runner.{started,stopping,stopped}` | `GatewayRunner` | adapter_count |
| `gateway.adapter.{connected,disconnected,fail}` | `BasePlatformAdapter._emit_*` | platform, adapter_id, reason |
| `gateway.adapter.auth_rejected` | `TelegramAdapter`, `SlackAdapter` | platform, user_id_hash |
| `gateway.session.{create,executor_choice}` | `SessionRouter` | session_id, executor_type |
| `gateway.pairing.{minted,approved,denied,expired,locked_out}` | `PairingStore` | platform, code_id (hash), expires_at, approver_did |
| `gateway.identity.{link,unlink}` | `IdentityGraph` | user_did, platform, user_hash, linked_by_did |
| `gateway.message.{received,deduped,sent}` | adapters, `SlackAdapter` dedup | platform, event_id, session_key |
| `session.search.queried` | `arcagent.modules.session.search` | query_hash, limit, result_count |
| `session.acl.{veto,cross_session_read}` | `arcagent.modules.memory_acl` | caller_did, target_user_did, classification |
| `cron.{parsed,session.disabled_tools,delivered,skipped_silent}` | `CronRunner` | job_name, next_run, disabled_toolsets |

## NIST Control Mapping

| Control | Implementation |
|---|---|
| **AU-2** Audit events | Every state-changing op emits gateway.*/session.*/cron.* |
| **AU-3** Audit content | Fields above + ts + session/agent DIDs |
| **AU-9** Tamper-evident logs | JSONL append-only session store; FTS5 index is derived, rebuildable |
| **AU-10** Non-repudiation | DID-signed audit entries (M2 for gateway; exists in arcagent.core.telemetry) |
| **AC-3** Access enforcement | `allowed_user_ids` allowlist; pairing gate; memory_acl ACL on cross-session reads |
| **AC-4** Information flow | Tier-gated cross-session ACL; per-tenant httpx pool (M2) |
| **IA-3** Device identity | Ed25519 DIDs per agent + per subprocess child |
| **IA-5** Authenticator mgmt | Vault resolver at federal/enterprise; 0600 file fallback at personal |
| **SC-3** Security function isolation | `SubprocessExecutor` per session (federal) |
| **SC-8** Transmission encryption | TLS 1.2+ to platform APIs; mTLS to NATS (M3) |
| **SC-28** Data at rest | SQLite SEE/SQLCipher optional for `index.db` (federal) |
| **SC-39** Process isolation | `resource.setrlimit` in `preexec_fn` |

## Threat Surface Covered

- **LLM01 Prompt Injection** — caller DID bound at transport; description-injection scan (skills hub M4)
- **LLM02 Sensitive Disclosure** — ACL filter at retrieval; PII-safe audit (user_id hashed)
- **LLM06 Excessive Agency** — cron sessions strip `cronjob` tool from registry (tool-registry-layer enforcement, not policy flag); delegation tool allowlist intersection
- **ASI01 Goal Hijack** — identity.md immutable
- **ASI02 Tool Misuse** — DELEGATE_BLOCKED_TOOLS frozenset; cron disabled_toolsets
- **ASI03 Identity Abuse** — per-child DID via HKDF (M3); per-user session ACL
- **ASI06 Memory Poisoning** — memory_acl bus veto at priority 10 (M2); skills hub covert-config-write block (M4)
- **ASI08 Cascading Failures** — per-adapter asyncio.TaskGroup; spawn timeout + budget

## Known Gaps (M2)

- Enterprise tier: `SessionRouter.handle()` applies pre-await guard but does NOT acquire a session lock through the full response pipeline. Treat enterprise as single-tenant per-session-id until M2 adds per-session lock.
- StreamBridge → adapter.send() wiring is stubbed (`arcgateway/stream_bridge.py`); responses are logged as deltas but not yet flushed back to the platform for the user. E2E tests verify the delta stream; full platform delivery needs completion.
- DM pairing DID signature verification at federal tier is a stub (returns True if DID provided). Real Ed25519 verify is M2.
- Tenant-partitioned httpx pool at federal tier: per-adapter client exists (no shared pool across adapters); per-session subprocess isolation provides the federal-tier boundary.
