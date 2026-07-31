# SPEC-019 Code Review — Verdict: **FAIL** (initial) → blockers resolved

**Resolution status (2026-04-27 follow-up):** All 14 blocker tasks landed.
arcui 344 passed, arccli 321 passed, ui_reporter 55 passed. Wave 2 (arch
review, simplifier, performance, tech debt) ready to run on re-review.



**Date**: 2026-04-27
**Branch**: `feature/SPEC-019-arcui-zero-config`
**Rubric**: principled-coder (Simplicity → Modularity → Security → Scalability)
**Wave 1 reviewers**: security, coverage, QA, code-quality (all 4 blocking)
**Wave 2**: skipped per workflow short-circuit (every Wave 1 reviewer failed)

## Headline

The implementation is functionally working — server returns CSS/JS correctly, traces appear via the API, audit events fire, the federation reads N stores, the probe-and-connect flow works. **Architecture is sound.** But the implementation has **two CRITICAL security defects, four HIGH security defects, and substantive coverage gaps on the new launcher path** that block merge under the principled-coder rubric.

## Blocking findings

### Pillar 3 — Security (CRITICAL)

| ID | File:Line | What | Why it blocks |
|---|---|---|---|
| C-1 | `arccli/commands/ui.py:44-48` (`_persist_agent_token`) | `Path.write_text(token)` then `chmod(0o600)` creates the file with the process umask, narrows perms after. Concurrent reader on a multi-user host can open the file in the gap. | SR-1's threat model assumes 0600 from creation. NFS/tmpfs make the post-hoc chmod non-atomic. |
| C-2 | `arccli/commands/ui.py:128-135` (`_maybe_open_browser`) | When `webbrowser.open()` returns False or raises, fallback `_write`s the full URL **including `#auth=<viewer_token>`** to stdout — captured by systemd journal, tmux scrollback, CI log shippers. | SR-4 forbids logging URL with token. `_write` is not a tty-only channel. |

### Pillar 3 — Security (HIGH, must fix this PR or as immediate follow-up before non-loopback deployment)

| ID | File:Line | What |
|---|---|---|
| H-1 | `arcui/auth.py:30-79` (`SessionTracker`) | `_sessions` and `_bootstrap_token_hashes` grow unbounded — NAT/CGNAT clients accrete entries; long-running deployments OOM. Replace with `cachetools.TTLCache(maxsize=10_000, ttl=86400)`. |
| H-3 | `arcagent/modules/ui_reporter/__init__.py:127-138` | `_should_auto_enable` validates 0600+UID; `_resolve_token` re-reads via `_TOKEN_FILE.read_text()` without re-checking. TOCTOU. Open once via `os.open(..., O_RDONLY|O_NOFOLLOW)`, fstat, pass bytes onward. |
| H-4 | `arcagent/modules/ui_reporter/__init__.py:108-117` | Probe HEAD targets whatever URL the agent's TOML configures. Tampered config (ASI06) can redirect autoconnect to attacker host; agent token then exfiltrated via `WebSocketTransport`. Pin autoconnect URL to `_LOOPBACK_HOSTS`; non-loopback requires explicit `enabled=true` + signed-config attestation. |

### Pillar 1 — Simplicity (BLOCKING per CLAUDE.md)

| ID | File:Line | What |
|---|---|---|
| S-1 | `arccli/commands/ui.py:225-229` | Monkey-patches `server.startup` with `# type: ignore[method-assign]` (no justification comment). CLAUDE.md explicitly bans monkey-patching. Use Starlette `on_startup` callback (already used for `event_buffer.start`) or uvicorn lifespan. |
| S-2 | `arcui/federated_store.py:95-117` | Comment-explained control flow ("Simpler: just emit the next_sub if the store reported one, else there are unemitted records but no more to fetch — let next page rebuild"). Per repo standard, that's a refactor signal. |
| S-3 | `arcagent/modules/ui_reporter/__init__.py:80-117` | `_should_auto_enable` mixes file-perm probe (SR-1) with HTTP probe — two concerns, two reasons to fail. Split into `_token_file_ok(path)` and `_server_reachable(url)`. |

### Pillar 2 — Modularity (BLOCKING)

| ID | File:Line | What |
|---|---|---|
| M-1 | `arcui/federated_store.py:137` | `from arcui.aggregator import _merge_by_timestamp` — reaches into a peer module's underscore-private. Promote to `arcui/_merge.py` (shared) or duplicate the small heap-merge here. |

### Coverage (BLOCKING — under 80% on net-new)

| File | Line cov | Branch cov | Threshold |
|---|---|---|---|
| `arccli/commands/ui.py` | **27%** | 98% | 80% |
| `arccli/commands/agent.py` | **0%** | n/a | 80% (file in PR, no test imports it) |
| `arcui/federated_store.py` | **67%** | 91% | 80% (cursor resume path untested — the whole reason cursors exist) |
| `arcllm/trace_store.py` | **31%** | 99% | 80% (likely cov-attribution miscount; rerun with `--cov=arcllm.trace_store`) |

`_start()` (the entire zero-config launcher) is uncovered. No test exercises:
- `mark_bootstrap_issued` on viewer_token
- Loopback-only `webbrowser.open` gate
- Non-loopback warning + tokens-to-stdout (FR-8 / SR-4)
- `warm_start_multi` wiring
- Lifespan-hook firing of browser open

### QA — Acceptance criteria not yet covered

| AC | Status | Gap |
|---|---|---|
| AC1 | PARTIAL | No assertion that `ui.session_start` emits with `auth_method: "browser_bootstrap"` and required fields. |
| AC2 | PARTIAL | No 50ms latency budget assertion (NFR-5); no `ui.agent_autoconnect` field assertion; no LLM-call-to-browser-within-1s integration test. |
| AC4 | PARTIAL | Token-print branch (`--host 0.0.0.0`) untested; no `auth_method: "manual_token"` audit assertion. |
| AC7 | MISSING | mypy/ruff/coverage gates not asserted in test suite (CI gate only). |

**3 HIGH-likelihood edge cases missing:**
1. Registry corruption (malformed JSON entity record) — `_resolve_trace_stores` resilience.
2. `webbrowser.open()` returns False (headless / no DISPLAY) — fallback path leaks token (see C-2).
3. Multiple agents with same name in registry — backfill match-by-name behavior undefined.

**Spec-coverage misses:**
- SR-2 (history.replaceState strips hash before any external request) — JS-side, no jsdom/Playwright test.
- SR-5 (loopback does NOT bypass token validation) — no negative test.
- SR-7 (federal default override via `~/.arc/arcagent.toml`) — no test.
- FR-5 multi-store interleaved-by-timestamp merge — `warm_start_multi` heap ordering not asserted.

## Non-blocking findings (worth fixing soon)

### Pillar 1 / Code quality

- `arccli/commands/ui.py:67-113` `_resolve_trace_stores` does discovery + validation + I/O + warning print — split into `_load_entities` and `_entities_to_stores`.
- `arcui/aggregator.py:486-490` three-level nested closure with default-arg loop-capture — extract `_percentile(samples, p)` to module level.
- `arcui/server.py:131` local import of `SessionTracker` mid-function; move to top with the other auth imports.
- `arcui/server.py:52-69` `_index` reads file on every request — cache bytes at app startup.
- `arcui/auth.py:83` `_EXEMPT_PATHS = {"/api/health"}` set-of-one — use direct equality at line 140 (already done) and drop the constant.
- `arcllm/trace_store.py:231` `except (json.JSONDecodeError, Exception)` is bare-except in disguise — repo standard forbids.
- `arcllm/trace_store.py:327, 372, 390` repeats `read_text().strip().split("\n")` 3× — extract `_read_lines(path)`.
- `arccli/commands/ui.py:128` magic URL-fragment `#auth={token}` — promote to a named constant shared with `index.html` (or document the shared contract in one place).
- `arcagent/modules/ui_reporter/__init__.py:131` duplicate `import os` inside `_resolve_token` (already at line 11).
- `arccli/commands/ui.py:253-272` `_build_subscribe_message` has one caller — inline.

### Pillar 3 (MEDIUM)

- M-1: No CSP header on dashboard — `localStorage` viewer token is XSS-exfiltratable. Add `Content-Security-Policy: default-src 'self'; script-src 'self' 'unsafe-inline'` and `X-Content-Type-Options: nosniff` to `_index` response (alongside existing `Cache-Control`).
- M-2: `mark_bootstrap_issued` never expires — token rotation will mislabel new manual tokens as `browser_bootstrap`. Tie marker to current `AuthConfig.viewer_token`; clear when config rotates.
- M-3: Audit redaction regex misses `auth=...` in URL values. Add value-side pass for `auth=[A-Fa-f0-9]{32,}` and `Bearer\s+\S+`.
- M-4: `_backfill_workspaces` doesn't run TOML-supplied workspace path through `_resolve_workspace` (SR-6 validation skipped).
- M-5: WebSocket first-message auth in `arcui/routes/agent_ws.py` not in this review set — verify it doesn't log the first-message token at debug level.

### Notable patterns worth keeping

- `SessionTracker` at-most-once observation via `(token_hash, remote_addr)` key, SHA-256 hashing tokens before storage.
- `_should_auto_enable` returns `(bool, reason)` — auditor-friendly; reason becomes audit metadata, not a thrown-away log line.
- `FederatedTraceStore.append` raises `NotImplementedError` with explicit message — fail-loud over silent-drop.
- `trace_store.iter_records` uses `asyncio.to_thread` — correct async hygiene.
- `Cache-Control: no-store` on `/` (added in this session) prevents stale-tab dead-token resurrection.

## Required to flip to PASS

In strict pillar order:

1. **Simplicity** — fix S-1 (monkey-patch), S-2 (federated query control flow), S-3 (split probe).
2. **Modularity** — fix M-1 (private cross-module import).
3. **Security** — fix C-1 and C-2 (criticals); H-1, H-3, H-4 should land in the same PR.
4. **Scalability** — H-1 (bounded session tracker) is also Pillar 4.
5. **Coverage** — add tests so `arccli/commands/ui.py` clears 80% line and `arcui/federated_store.py` clears 80% line. Audit-field assertions and the 3 HIGH edge cases close QA partials.

Tasks #7–#15 cover blockers; #16–#20 cover the highs.

## Wave 2 status

Not run. Workflow short-circuits when any Wave 1 reviewer fails. Architecture review, simplifier auto-fix, performance audit, tech-debt assessment will run on the re-review after blockers land.
