# SPEC-023 — Implementation Plan

> Status: COMPLETE · Tasks crossed off as completed · Pillar priority: Simplicity → Modularity → Security → Scalability
> Estimated total: ~32 hours core + ~16 hours Federal Track (parallel, non-blocking)

---

## Status Legend

- `[ ]` PENDING — not started
- `[~]` IN PROGRESS
- `[x]` COMPLETE — implementation done, tests passing
- `[V]` VERIFIED — `/validate` confirmed
- `[B]` BLOCKED — waiting on something (note in description)

## Completion Tracking

**Core build:** 38 / 38 tasks complete (100%)
**Federal Track:** 0 / 14 tasks complete (0%)
**Total:** 38 / 52 tasks complete

---

## Phase 0 — Identity Helper (single source of truth)

> Smallest unit. Zero dependencies. Sets the seam for everything else. *(Modularity)*

- [x] **T0.1** Create `packages/arcgateway/src/arcgateway/identity.py` with `derive_viewer_did(viewer_token: str) -> str`. Acceptance: function returns `"did:arc:viewer:" + sha256(token).hexdigest()[:16]`. *(Modularity, Simplicity)*
- [x] **T0.2** Create `packages/arcgateway/tests/unit/test_identity.py` with three tests: determinism, format regex `^did:arc:viewer:[0-9a-f]{16}$`, distinct inputs produce distinct outputs. *(Simplicity)*
- [x] **T0.3** Run `pytest tests/unit/test_identity.py`, `ruff check`, `mypy --strict identity.py`. All clean. *(Quality)*

**Phase 0 done when:** Phase 0 tests are green and `derive_viewer_did` is importable from arcgateway.

---

## Phase 1 — WebPlatformAdapter (TDD)

> Implement `BasePlatformAdapter`. Test-first. *(Simplicity, Modularity)*

### 1.1 Skeleton

- [x] **T1.1** Create `packages/arcgateway/src/arcgateway/adapters/web.py` with `WebPlatformAdapter` class skeleton — constructor signature, all attribute slots initialized, all public methods raising `NotImplementedError`. *(Modularity)*
- [x] **T1.2** Create `packages/arcgateway/tests/unit/test_web_adapter.py` with the 17 unit-test stubs from SDD §8.1. Each stub asserts `pytest.fail("not implemented")` initially. *(Simplicity)*

### 1.2 Connect / disconnect / state

- [x] **T1.3** Implement `connect()` and `disconnect()`. Acceptance: `test_connect_and_disconnect_are_noops` passes. `disconnect()` cancels all per-socket tasks and closes all sockets cleanly. *(Simplicity)*

### 1.3 Register / unregister

- [x] **T1.4** Implement `register_socket(ws, agent_did, user_did, chat_id)` — store socket, create queue (maxsize=100), spawn drain + idle tasks, increment `_n_connections`. Acceptance: `test_register_socket_returns_stable_chat_id`, `test_register_socket_different_tokens_different_chat_ids` (signature compliance — chat_id is computed externally), `test_register_socket_does_not_accept_viewer_token`. *(Security, Modularity)*
- [x] **T1.5** Implement `unregister_socket(ws)` — remove from all maps, cancel tasks, close socket if open. Acceptance: `test_unregister_socket_removes_from_set`, `test_unregister_last_socket_removes_chat_id_key`. *(Simplicity)*

### 1.4 Ingest

- [x] **T1.6** Implement `ingest(chat_id, text, client_seq=None)` — validate text length, validate `client_seq` monotonicity, build `InboundEvent`, call `on_message`. Acceptance: `test_ingest_builds_correct_inbound_event`, `test_ingest_empty_text_rejected`, `test_ingest_oversized_text_rejected`, `test_client_seq_replay_rejected`. *(Security)*

### 1.5 Send + drain loop

- [x] **T1.7** Implement `send(target, message, *, reply_to=None)` — compute `audit_hash`, enqueue to per-socket queues with drop-oldest on `QueueFull`, emit single audit event with `breakdown`. Acceptance: `test_send_fans_out_to_all_sockets`, `test_send_no_sockets_is_noop`, `test_send_audit_hash_in_payload`, `test_send_under_backpressure_drops_oldest`, `test_audit_emit_breakdown_under_partial_fanout`. *(Simplicity, Security)*
- [x] **T1.8** Implement `_drain_loop(ws, queue)` — await queue, send_json, catch disconnect → unregister. Acceptance: `test_drain_loop_handles_websocket_disconnect_cleanly`, `test_send_removes_dead_socket`. *(Simplicity)*
- [x] **T1.9** Implement `_inactivity_monitor(ws)` — periodic check, close on idle timeout. Acceptance: `test_inactivity_monitor_closes_idle_socket`. *(Simplicity)*

### 1.6 Limits

- [x] **T1.10** Implement `max_connections` enforcement → raise `WebAdapterFull` from `register_socket`. Acceptance: `test_max_connections_rejects_overflow`. *(Scalability)*
- [x] **T1.11** Implement `max_frame_bytes` enforcement in `ingest`. Acceptance: `test_oversized_inbound_frame_rejected`. *(Security)*

### 1.7 Tool-call frames

- [x] **T1.12** Wire `Delta(kind="tool_call")` → `tool_call` outbound frame (separate from `message` frame). Acceptance: `test_tool_call_delta_sends_tool_call_frame`. *(Simplicity)*

### 1.8 Quality gate

- [x] **T1.13** `pytest tests/unit/test_web_adapter.py` — all 17 tests green, coverage ≥95% on `adapters/web.py`. *(Quality)*
- [x] **T1.14** `ruff check`, `mypy --strict` on `adapters/web.py` — clean. *(Quality)*

**Phase 1 done when:** all 17 unit tests are green, coverage ≥95%, lint clean.

---

## Phase 2 — Config + CLI Wiring

- [x] **T2.1** Add `WebPlatformConfig` to `packages/arcgateway/src/arcgateway/config.py`. Add `web: WebPlatformConfig = WebPlatformConfig()` to `PlatformsConfig`. Acceptance: pytest the config parses a `[platforms.web]` block correctly. *(Modularity)*
- [x] **T2.2** Add web-adapter wiring to `_wire_adapters()` in `packages/arcgateway/src/arcgateway/cli.py` — mirrors Slack/Telegram blocks at lines 124–193. Acceptance: with `[platforms.web].enabled=true`, `arc gateway start` registers a `WebPlatformAdapter` with the runner. *(Modularity)*
- [x] **T2.3** `pytest packages/arcgateway/tests/unit/test_config.py::test_web_platform_config` — green. *(Quality)*

---

## Phase 3 — Bootstrap (composition root)

- [x] **T3.1** Create `packages/arcgateway/src/arcgateway/bootstrap.py` with `async def build_for_embedded(team_root, gateway_config) -> EmbeddedGateway`. Acceptance: signature matches SDD §3.3; returns named tuple. *(Modularity)*
- [x] **T3.2** Implement `agent_factory` — async callable that loads `ArcAgent` from `team_root/<name>_agent/arcagent.toml`. Mirrors `arccli._load_arcagent`. Acceptance: factory called with valid agent_did returns running ArcAgent. *(Modularity)*
- [x] **T3.3** Implement composition: `executor = AsyncioExecutor(agent_factory)`, `session_router = SessionRouter(executor)`, `stream_bridge = StreamBridge(session_router)`. Iterate over `[platforms.X]` blocks; instantiate each enabled adapter; register with `session_router`. Acceptance: with all three platforms enabled, all three adapters are returned. *(Modularity)*
- [x] **T3.4** Tests in `packages/arcgateway/tests/unit/test_bootstrap.py`: bootstrap with web only, web + slack, web + telegram + slack, federal tier (`SubprocessExecutor`). *(Modularity)*
- [x] **T3.5** `mypy --strict` and `ruff check` clean on bootstrap.py. *(Quality)*

---

## Phase 4 — arcui Lifespan Integration

- [x] **T4.1** In `packages/arcui/src/arcui/server.py`, add `gateway_config: GatewayConfig | None = None` parameter to `create_app()`. *(Modularity)*
- [x] **T4.2** In the lifespan async context manager, call `arcgateway.bootstrap.build_for_embedded(team_root, gateway_config)` when `gateway_config` is non-None. Store `executor`, `session_router`, `web_adapter`, `stream_bridge` on `app.state`. *(Modularity)*
- [x] **T4.3** On lifespan shutdown, call `await web_adapter.disconnect()` and other adapters' `disconnect()` cleanly. *(Simplicity)*
- [x] **T4.4** Add lifespan integration test `tests/integration/test_arcui_lifespan_with_gateway.py` — start app with embedded config, verify `app.state.web_adapter` is a `WebPlatformAdapter`. *(Modularity)*

---

## Phase 5 — Chat WebSocket Route

- [x] **T5.1** Create `packages/arcui/src/arcui/routes/chat_ws.py` per SDD §3.4. Auth check, agent resolution, derive `user_did` and `chat_id`, register socket, ingest loop, unregister on disconnect. *(Security, Simplicity)*
- [x] **T5.2** Register the route in `arcui/server.py` route table. *(Modularity)*
- [x] **T5.3** Integration tests in `packages/arcui/tests/integration/test_chat_ws.py`:
  - `test_browser_message_reaches_echo_executor` (echo executor)
  - `test_reconnect_preserves_session_key`
  - `test_concurrent_messages_queue_correctly`
  - `test_send_after_ws_disconnect_is_noop`
  - `test_broadcast_to_two_sockets`
  - `test_auth_required_on_ws_upgrade`
  - `test_invalid_token_rejected`
*(Security, Modularity)*
- [x] **T5.4** `pytest tests/integration/test_chat_ws.py` green; route lint + types clean. *(Quality)*

---

## Phase 6 — Self-Hosted Fonts

> Air-gap blocker. Must land before any new page references `@font-face`. *(Security)*

- [x] **T6.1** Download Inter (regular/medium/semibold/bold) and JetBrains Mono (regular/medium) WOFF2 from official OFL/SIL distributions. License files committed. *(Security)*
- [x] **T6.2** Place files in `packages/arcui/src/arcui/static/assets/fonts/`. *(Simplicity)*
- [x] **T6.3** Replace `<link rel="stylesheet" href="https://fonts.googleapis.com/...">` in any served HTML/CSS with local `@font-face` declarations. Verify no `googleapis.com` reference in `static/` or `demo/` (when copied into static). *(Security)*
- [x] **T6.4** Add E2E test `tests/e2e/test_self_hosted_fonts_load_offline` — request `assets/fonts/inter.woff2` returns 200 with correct content-type. Grep all served HTML for `googleapis` — must return zero matches. *(Security)*

---

## Phase 7 — Messages Page

- [x] **T7.1** Add `messages` entry to `PAGES` in `arc-shell.js` with sidebar icon. *(Simplicity)*
- [x] **T7.2** Add `<div data-page-content="messages" class="hidden">…</div>` block to `index.html` modeled on `demo/messages.html` but trimmed: DM list (left), chat pane (center), thread/refs panel (right). Use local `@font-face`. *(Simplicity)*
- [x] **T7.3** Create `packages/arcui/src/arcui/static/assets/messages-page.js`:
  - `loadAgents()` — `fetch /api/agents`, render DM list.
  - `openChat(agent_id)` — open WebSocket `/ws/chat/{agent_id}`.
  - `onIncoming(frame)` — dispatch on `type` (status/tool_call/message/error).
  - `sendMessage(text)` — send `{"type":"message","text":...,"client_seq":n++}`.
  - `localMessages: Map<turn_id, MessageEntry>` for reconnect history.
*(Simplicity)*
- [x] **T7.4** Manual smoke test: `arc ui start` with embedded config → open browser → click Messages → click an agent → send a prompt → see response. *(Acceptance)*
- [x] **T7.5** Visual cross-check against `demo/messages.html` — colors, spacing, badges consistent. *(Simplicity)*

---

## Phase 8 — Knowledge Page

- [x] **T8.1** Create `packages/arcui/src/arcui/routes/knowledge.py` per SDD §3.5. Returns the JSON shape from SDD §5.2. Uses `arcgateway.fs_reader` for memory + workspace. Calls `code-review-graph.list_graph_stats_tool()` with 500ms timeout via the project's MCP client. *(Modularity)*
- [x] **T8.2** Register `GET /api/knowledge/{agent_id}` route in `server.py`. *(Modularity)*
- [x] **T8.3** Tests in `packages/arcui/tests/integration/test_knowledge_route.py`:
  - `test_knowledge_api_returns_correct_shape`
  - `test_knowledge_endpoint_404_when_agent_missing`
  - `test_knowledge_endpoint_graph_unavailable_returns_200`
*(Security, Simplicity)*
- [x] **T8.4** Add `knowledge` entry to `PAGES` in `arc-shell.js`. Visible only when an agent is selected (`route.agent` non-null). *(Simplicity)*
- [x] **T8.5** Add `<div data-page-content="knowledge">…</div>` to `index.html`: context budget meter, memory entries table, workspace tree (lazy-load), graph stats card, recent events timeline. *(Simplicity)*
- [x] **T8.6** Create `knowledge-page.js`:
  - `loadKnowledge(agent_id)` → render five panels.
  - Subscribe to existing arcui `/ws` with `subscribe: agent:{id}:memory` for `FileChangeEvent` updates.
  - Lazy-expand directories on click.
*(Simplicity)*
- [x] **T8.7** Manual smoke test: navigate to Knowledge for a known agent → see memory entries → edit a memory file → see it appear in real time. *(Acceptance)*

---

## Phase 9 — Architecture Tests

- [x] **T9.1** Create `tests/architecture/test_imports.py` enforcing module boundaries from SDD §2:
  - `test_arcui_does_not_import_arcagent`
  - `test_arcgateway_does_not_import_arcui`
  - `test_adapters_do_not_import_arcui_or_arcagent`
  - `test_web_adapter_does_not_import_bootstrap`
*(Modularity)*

---

## Phase 10 — E2E + Air-Gap Verification

- [x] **T10.1** Create `tests/e2e/test_web_chat_e2e.py` (`@pytest.mark.e2e`):
  - `test_full_chat_turn_with_real_agent` (requires `team/`)
  - `test_air_gap_no_external_calls_during_chat_turn` — mock external DNS to fail; verify no outbound HTTP except to local LLM.
*(Security)*
- [x] **T10.2** Run `ARC_E2E=1 pytest tests/e2e/` — green. *(Acceptance)*

---

## Phase 11 — Demo Tunnel

- [x] **T11.1** Create `scripts/demo-tunnel.sh`:
  - Start arcui (`arc ui start --config gateway.toml`).
  - Start ttyd (`ttyd --port 7681 --interface 127.0.0.1 --credential "arc:$ARC_OPERATOR_TOKEN" --writable bash`).
  - Start `cloudflared tunnel run arc-demo`.
  - Print public URLs.
- [x] **T11.2** `~/.cloudflared/config.yml` routes:
  - `demo.blackarcsystems.com` → `http://localhost:8765`
  - `term.blackarcsystems.com` → `http://localhost:7681`
- [x] **T11.3** Smoke test the live URLs end-to-end. *(Acceptance)*

---

## Phase 12 — Final Quality Gate

- [x] **T12.1** Full repo `ruff check` — 0 errors. *(Quality)*
- [x] **T12.2** `mypy --strict packages/arcgateway/ packages/arcui/` — 0 errors. *(Quality)*
- [x] **T12.3** `pytest --cov=packages/arcgateway --cov=packages/arcui` — coverage thresholds met (line ≥80%, branch ≥75%, new files ≥90%, web adapter ≥95%). *(Quality)*
- [x] **T12.4** `pip-audit` — 0 critical/high vulnerabilities. *(Security)*
- [x] **T12.5** Manual demo dress rehearsal: open prod URL, run through the eight Success Criteria from PRD §7. *(Acceptance)*

---

## Federal Track (Parallel; Not Blocking Core)

> These are FedRAMP-compliance fixes mapped in the deepening. Each is independently shippable; complete in priority order when time permits.

### F1 — OS user binding (FedRAMP Low gate, 1h)

- [ ] **F1.1** In `packages/arcui/src/arcui/auth.py`, extend `SessionStartFields` with `uid: int` (from `os.getuid()`) and `username: str` (from `pwd.getpwuid(uid).pw_name`). *(Security)*
- [ ] **F1.2** Update `gateway.session.start` audit event schema to include `uid` and `username`. *(Security)*
- [ ] **F1.3** Test: `test_session_start_includes_os_user_binding`. *(Security)*
- [ ] **F1.4** Documentation: note FedRAMP Low gate closed in README.md. *(Compliance)*

### F2 — Token TTL + revocation list (4h, FedRAMP Moderate gate)

- [ ] **F2.1** Add `viewer_token_ttl_seconds: int = 3600` to `AuthConfig`. Track `issued_at` per token. *(Security)*
- [ ] **F2.2** Add `_revoked_tokens: set[str]` to `AuthConfig`. Check on every request. *(Security)*
- [ ] **F2.3** Add `arc ui revoke <token>` CLI command. *(Simplicity)*
- [ ] **F2.4** Tests: token expires after TTL, revoked token rejected, valid token accepted. *(Security)*

### F3 — TOTP / U2F MFA (6h, FedRAMP High)

- [ ] **F3.1** Conditional MFA challenge at `tier=federal` before WebSocket upgrade. *(Security)*
- [ ] **F3.2** TOTP issuance + verification (use existing `pyotp`-style library). *(Security)*
- [ ] **F3.3** Cache MFA result in session tracker for 1h. *(Simplicity)*
- [ ] **F3.4** Tests: MFA required at federal, optional otherwise; cached for TTL. *(Security)*

### F4 — `localStorage` → `sessionStorage` (2h)

- [ ] **F4.1** In `packages/arcui/src/arcui/static/index.html:20`, swap `localStorage.setItem` → `sessionStorage.setItem`. *(Security)*
- [ ] **F4.2** Update bootstrap fallback to read from `Authorization` header on subsequent requests. *(Simplicity)*
- [ ] **F4.3** E2E test: token does not persist across browser tab close. *(Security)*

### F5 — `POST /api/auth/bootstrap` (3h)

- [ ] **F5.1** Create `packages/arcui/src/arcui/routes/bootstrap.py` — POST endpoint accepting token in JSON body, returning a signed session JWT. *(Security)*
- [ ] **F5.2** Update `index.html` bootstrap script — POST to `/api/auth/bootstrap` instead of reading URL hash. *(Simplicity)*
- [ ] **F5.3** Remove URL-hash mitigation (no longer needed). *(Simplicity)*
- [ ] **F5.4** Test: token never appears in URL bar; reverse-proxy log scan finds zero hits. *(Security)*

---

## Definition of Done (Whole Spec)

The spec is complete when:

1. All 38 core tasks are `[V]` VERIFIED.
2. Phase 12 quality gate passes.
3. PRD §7 Success Criteria all checked.
4. README.md `Learnings` section is populated with at least three findings discovered during implementation.
5. `git log --oneline | head -20` shows commits on a `feature/SPEC-023-arcui-web-platform-adapter` branch ready to merge.
6. `/review SPEC-023` post-implementation review is clean (security, performance, quality, ADRs, monitoring plan).

Federal Track tasks may complete on a different timeline. They are not gates on the core spec but their progress is tracked in the same plan.

---

## Risks (Mirror PRD §10)

If any of these materialize during implementation, return to `/deepen` for the affected section before proceeding:

- WS reconnect during a long agent turn loses turn output.
- `code-review-graph` MCP call shape changes upstream.
- arcgateway `BasePlatformAdapter` Protocol gains a method that `WebPlatformAdapter` must adopt.
- Multi-adapter coexistence reveals a SessionRouter behavior we did not predict.

---

## Notes for Implementer

- **TDD throughout.** Every task that adds production code starts with a failing test.
- **One commit per task.** Easy to bisect, easy to review.
- **Update task status as you go.** `[~]` when starting, `[x]` when test passes, `[V]` after `/validate`.
- **Brainstorm is the source of truth for detail.** When the SDD seems thin, the brainstorm has the rationale.
- **Never bypass `arcgateway.SessionRouter`.** That is the whole architectural point. If you find yourself importing arcagent from arcui, stop.
