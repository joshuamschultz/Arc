# Arc Codebase Refactor & Cleanup Plan

> **Status:** PLAN — awaiting execution
> **Scope:** All 13 packages — `arcagent`, `arccli`, `arcgateway`, `arcllm`, `arcmas`, `arcmodel`, `arcprompt`, `arcrun`, `arcskill`, `arcteam`, `arctrust`, `arctui`, `arcui`
> **Constraint:** Behavior-preserving. No feature changes. No public API changes that break in-tree callers.
> **Mandate:** Local-only, unreleased — *delete* legacy/backward-compat code, do not preserve.
> **Verification:** Per-package `pytest` + `ruff check` + `mypy --strict` between every phase.

---

## 0. Reality Check

| Metric                 | Value          |
| ---------------------- | -------------- |
| Source files           | 461            |
| Source LOC             | ~89,400        |
| Test files             | 868            |
| Test LOC               | ~126,400       |
| Files ≥ 500 LOC        | 22             |
| Files ≥ 1000 LOC       | 6              |
| `arcagent/core/` LOC   | 5,610 (budget: 3,500 — **61% over**) |
| Stale `main_legacy` headers | 11 occurrences (file gone, comments remain) |
| `ARCUI_LEGACY_POLLING` gates | 9 routes + 1 helper + tests |
| Legacy module dirs in `arcagent/modules/` | ≥ 8 (`vault_azure`, `slack`, `pulse`, `scheduler`, `telegram` legacy bot, `voice/__init__.py`, `browser/__init__.py`, `web/__init__.py`) |
| `# TODO`/`FIXME` in src | 15 (most genuine open work; ~5 stale "(closes TODO …)" parentheticals) |

This plan executes over **6–8 sessions**, gated by per-package verification. Every phase is independently revertable.

---

## 1. Cross-cutting Themes

These are the patterns to delete consistently across all packages:

1. **Stale migration headers.** `arccli` migrated from Click → argparse (T1.1.5); `main_legacy.py` is *gone* but 11 command modules carry headers like `It MUST NOT import click or arccli.main_legacy.` Pure noise.
2. **Closed-TODO parentheticals.** `arcgateway/session_pairing.py` has `(closes TODO M1 T1.7 …)` annotations describing already-completed work. Delete the parentheticals; the code is the documentation now.
3. **Backward-compat function aliases.** `arctrust.identity.derive_child_identity` accepts both `parent_sk_bytes` (current) and `parent_sk` (legacy) keyword forms. No in-tree caller uses the legacy form.
4. **Legacy feature flags.** `ARCUI_LEGACY_POLLING` env var gates 9 polling endpoints whose WebSocket replacements (`/ws/dashboard`) already exist. Comments in the file say "delete in next release" — for an unreleased repo, this is now.
5. **Legacy module shims in `arcagent/modules/`.** Pre-capabilities-pattern modules (`vault_azure`, `slack`, legacy `telegram` bot, `pulse`, `scheduler`, `voice/__init__.py`, etc.) coexist with new capability-based replacements. Delete the legacy classes; keep the `capabilities.py` files.
6. **Oversized files.** 6 files ≥ 1000 LOC, 22 files ≥ 500 LOC. Split by clearly-separable concerns — never split for the sake of LOC alone.
7. **Inconsistent error swallowing.** `except Exception: logger.debug(...)` patterns silently hide real problems. Promote to `logger.warning` or narrow the exception class.

---

## 2. Phase Order & Risk Profile

| Phase | Title                                  | Risk       | Reversible | Touches |
| ----- | -------------------------------------- | ---------- | ---------- | ------- |
| 0     | Baseline verification                  | Zero       | n/a        | 0 files |
| 1     | Comment-only cleanup (noise sweep)     | Trivial    | Yes        | ~14 files |
| 2     | Legacy feature-flag removal            | Low        | Yes        | ~12 files |
| 3     | Backward-compat code-path removal      | Low–Medium | Yes        | ~15 files |
| 4     | Legacy module-shim deletion (`arcagent`) | Medium   | Yes        | ~25 files |
| 5     | Decomposition: oversized files         | Medium     | Yes        | ~80 files (creates ~50 new) |
| 6     | Refactor-only typing & doc cleanup     | Trivial    | Yes        | ~20 files |

Stop-the-world rule: **after every phase, the full per-package `pytest`+`ruff`+`mypy --strict` matrix must be green** before the next phase begins.

---

## 3. Phase 0 — Baseline Verification

**Goal:** prove the suite is green *now*, so any future red is caused by the refactor.

```bash
# Per-package matrix (run for each of the 13 packages)
for pkg in arcagent arccli arcgateway arcllm arcmas arcmodel arcprompt arcrun arcskill arcteam arctrust arctui arcui; do
  echo "=== $pkg ==="
  uv run --package "$pkg" pytest -q --no-header || echo "PYTEST FAIL: $pkg"
  uv run --package "$pkg" ruff check "packages/$pkg" || echo "RUFF FAIL: $pkg"
  uv run --package "$pkg" mypy --strict "packages/$pkg/src" || echo "MYPY FAIL: $pkg"
done
```

Capture the output as `.claude/specs/REFACTOR-CLEANUP/baseline-{pytest,ruff,mypy}.log` so we can diff after each phase.

**Exit criterion:** every package green on all three tools, *or* every existing failure recorded as "pre-existing" with file:line so we know what we inherited (and per CLAUDE.md, fix as we encounter them).

---

## 4. Phase 1 — Comment-only Cleanup (noise sweep)

**Risk:** Trivial. **No behavior change.** All edits are comment/docstring deletions.

### 4.1 `arccli` — stale migration headers (11 occurrences)

Delete the "T1.1.5 migration" preamble (typically lines 3–8 or just the single `MUST NOT import click` line). Migration is complete; the file `arccli/main_legacy.py` no longer exists.

| File                                | Lines to delete |
| ----------------------------------- | --------------- |
| `arccli/commands/skill.py`          | L9              |
| `arccli/commands/run.py`            | L3–8            |
| `arccli/commands/llm.py`            | L3–8            |
| `arccli/commands/team.py`           | L3–8            |
| `arccli/commands/init.py`           | L3, L12         |
| `arccli/commands/ext.py`            | L14             |
| `arccli/commands/agent.py`          | L10             |
| `arccli/commands/registry.py`       | L70, 77, 84, 91, 98, 105, 112, 119 — replace `T1.1.5 plain handler` docstrings with simple `Dispatch wrapper.` |

### 4.2 `arcgateway/session_pairing.py` — closed-TODO parentheticals (5)

| File:Line | Action |
| --------- | ------ |
| `session_pairing.py:170` | Strip ` (closes TODO M1 T1.7).` |
| `session_pairing.py:188` | Strip ` (closes TODO M1 T1.7 rate-limited branch).` |
| `session_pairing.py:202` | Strip ` (closes TODO M1 T1.7 full branch).` |
| `session_pairing.py:215` | Strip ` (closes TODO M1 T1.7 locked branch).` |
| `session_pairing.py:9–10, 131–132` | Rewrite module docstring to drop "Closes 5 TODO(M1 T1.7 integration) comments from the original SessionRouter" — those TODOs no longer exist; describe what the module *does*, not what it *closed*. |

### 4.3 `arcagent` — internal compatibility comments

| File:Line | Action |
| --------- | ------ |
| `arcagent/modules/web/_runtime.py:55` | Strip "matching legacy `WebModule.__init__` behaviour" — `_runtime` is the only init now. |
| `arcagent/modules/voice/_runtime.py:15` | Same treatment. |
| `arcagent/builtins/capabilities/_runtime.py:86` | Strip "Mirrors the legacy `ExtensionAPI.get_secret` resolution order …" — keep the *what*, drop the *why-it's-the-same-as-the-old-thing*. |

### 4.4 `arctui/app.py:326`

Comment reads `handlers (e.g. legacy CLI dispatch), they run in a worker thread`. Reword to `synchronous handlers run in a worker thread.`

### Verification (Phase 1)

`pytest`+`ruff`+`mypy` on `arccli`, `arcgateway`, `arcagent`, `arctui`. Should match Phase 0 baseline exactly (zero net behavior change).

---

## 5. Phase 2 — Legacy Feature-flag Removal

**Risk:** Low. Behavior change: the legacy code path is removed; the modern path was already the default.

### 5.1 `ARCUI_LEGACY_POLLING` (SPEC-025 Track E) — total removal

WebSocket replacement `/ws/dashboard` is live and topic-complete (verified: 9 polling routes' payloads are republished to the bus and consumed by `dashboard_ws.py:57–69` topic list).

**Delete:**

| File                                     | What to remove |
| ---------------------------------------- | -------------- |
| `arcui/server.py:52–58`                  | `_LEGACY_POLLING` env-var parse |
| `arcui/server.py:370–372`                | `app.state.legacy_polling = …` |
| `arcui/routes/stats.py:24–32`            | `_legacy_polling_enabled` helper + TODO comment block |
| `arcui/routes/stats.py:90, 112, 131, 143, 162, 181` | `if not _legacy_polling_enabled(request): return JSONResponse(...410)` guards |
| `arcui/routes/cost_efficiency.py:41–46`  | inline `legacy_polling` guard |
| `arcui/routes/schedules.py:62–67`        | inline `legacy_polling` guard |
| `arcui/tests/test_routes.py` `class TestLegacyPollingFlag` (~L792–840) | Delete the test class |

The 9 polling endpoints continue to work *and* publish to the bus — that behavior is preserved. We only remove the kill-switch that disabled them.

### 5.2 `arcllm` legacy `config.toml` fallback

| File:Line | Action |
| --------- | ------ |
| `arcllm/config.py:120–130` | Delete the `legacy = root / "config.toml"` fallback. Update `_user_config_path` docstring. |
| Pre-step | Grep the workspace for any caller still resolving `~/.arc/config.toml`; if found, migrate before deletion. |

### 5.3 `arcskill.run_dry_run` — drop `skip_sandbox` parameter

| File:Line | Action |
| --------- | ------ |
| `arcskill/hub/dry_run.py:644–692` | Remove `skip_sandbox` parameter from signature; remove the `raise` block; tests that exercise it should mock backend availability instead. |
| `arcskill/hub/dry_run.py:640–642` | Remove "backward-compatible entry point" docstring framing. |

### 5.4 `arctrust.derive_child_identity` — keyword-only signature

| File:Line | Action |
| --------- | ------ |
| `arctrust/identity.py:372–423` | Convert to keyword-only signature: `def derive_child_identity(*, parent_sk_bytes: bytes, spawn_id: str, wallclock_timeout_s: float \| None = None)`. Drop the `parent_sk`/`nonce`/`ttl_s` aliases and the three ternary lines (L407–409). Drop L406 comment. |
| Pre-step | Grep `derive_child_identity(` across all packages to confirm zero positional callers. |

### 5.5 `arcgateway/bootstrap.py` — legacy name-suffix fallback

| File:Line | Action |
| --------- | ------ |
| `arcgateway/bootstrap.py:113–127` | Delete the `did:arc:foo:bar/<name>` → `<name>_agent` suffix-match fallback. All agents now have `[identity].did` in their TOML. If a TOML lacks a DID, fail loudly rather than guessing. |

### 5.6 `arcagent/modules/session/index.py` — `insert_jsonl`

| File:Line | Action |
| --------- | ------ |
| `arcagent/modules/session/index.py:397` | Delete `insert_jsonl()` function. Confirm no in-tree caller (record-based API is canonical). |

### 5.7 `arcagent/modules/bio_memory/retriever.py:229` — `memory_dir` fallback

| File:Line | Action |
| --------- | ------ |
| `retriever.py:229` | Delete the "Try memory_dir first (backward compat)" fallback path. Enforce the canonical `bio_memory_dir` only. |

### 5.8 `arcui/event_buffer.py` — raw-dict legacy path

| File   | Action |
| ------ | ------ |
| `arcui/event_buffer.py:4, 43` | Audit every call to `event_buffer.push(…)` (grep). If all callers pass `UIEvent`, narrow the signature to `push(event: UIEvent)` and delete the dict-handling branch. If any caller still passes a dict, *that* caller is the migration target — fix it, then delete. |

### 5.9 `arcui/subscription.py:50` — "match-all" default

| File   | Action |
| ------ | ------ |
| `arcui/subscription.py:50` | Decision required: is "unregistered queues match everything" a *safety* default or a *legacy* tolerance? If legacy: register queues explicitly at registration time and remove the wildcard default. If safety: rephrase the comment to remove "(backward compat)" framing. |

### 5.10 `arcrun/backends/loader.py:493–571` — `_enforce_federal_manifest`

| File:Line | Action |
| --------- | ------ |
| `arcrun/backends/loader.py:493–571` | The function is comment-marked "no longer called from `load_backend`" but is preserved for unit tests. Move it to `tests/_legacy_manifest_helpers.py` or delete entirely; production code does not need it. |
| L571 docstring | Update or delete. |

### Verification (Phase 2)

`pytest`+`ruff`+`mypy` on `arcui`, `arcllm`, `arcskill`, `arctrust`, `arcgateway`, `arcagent`, `arcrun`. Several test files will need edits to drop assertions about the deleted feature flag and the deleted parameter forms. Expect ~50 LOC net reduction.

---

## 6. Phase 3 — Backward-compat Code-path Removal

This phase tackles the remaining "kept for backward-compat" markers that are *inside* live code (rather than feature flags or function aliases).

### 6.1 `arcagent/core/tool_registry.py:330`

Default value of `caller_did` parameter is `None` "for backward compatibility with existing tests." Per ADR-019, every tool dispatch carries `caller_did`. Decision:

- Make `caller_did` a **required** keyword-only parameter.
- Update every test that constructed a `ToolCall` without `caller_did` to pass the agent's DID explicitly.

This tightens the type contract and removes the silent-default attack surface (`ASI03 — Identity Privilege Abuse`).

### 6.2 `arcagent/orchestration/spawn.py:214–224`

Comment reads "The legacy `state` parameter is retained for … fall back to a state passed at construction (legacy path)."

- Drop the `state` parameter from the public spawn API.
- Inline the construction-time path; if any caller still passes `state`, migrate them to the canonical pattern (state derived from `RunState`).

### 6.3 `arcgateway/session.py:211–251` — test-hook counters

`agent_tasks_spawned`, `queued_events`, dual log emission. Audit:

- If tests use these counters as **assertions of behavior** (e.g., "1 task spawned"), they're fine — keep, but rename comments to "test instrumentation" rather than "backward-compat with existing tests."
- If tests just want to read state, expose a typed `SessionRouterStats` dataclass instead of bare dicts.

### 6.4 `arcteam/types.py:112` — `workspace_path` nullable

`workspace_path: str | None = None` defaults to `None` "for legacy records or non-agent entities."

- Audit the team store (NDJSON files) for any record with `workspace_path is None`.
- If none: make the field **required** (`workspace_path: str`) and delete `arc team backfill-workspaces` if such a command exists.
- If some: keep the optionality but rephrase the docstring to describe the *current* meaning (non-agent entities) and drop the "legacy records" branch.

### 6.5 `arcllm` provider re-exports

Audit which of these `arcllm/adapters/*.py` adapters are actually wired up to a registry entry and configured by any in-tree user: `deepseek`, `fireworks`, `groq`, `together`, `xai`, `ollama`, `vllm`, `huggingface`, `cohere`, `moonshot`. (See Phase 7 — this audit decides their fate; Phase 3 only removes adapters that are demonstrably dead — i.e., zero in-tree references and zero presence in `[providers]` config templates.)

### Verification (Phase 3)

Per-package `pytest`+`ruff`+`mypy`. Several test edits expected (esp. for `tool_registry.py:330` change). Net LOC reduction ~80.

---

## 7. Phase 4 — Legacy Module-shim Deletion in `arcagent/modules/`

**Risk:** Medium. We're deleting whole files. Each step is reversible (single revert), but the blast radius is larger because `arcagent` is the nucleus.

The pattern: each module has a legacy class (in `__init__.py` or `<name>module.py`) plus a new capabilities-based shim in `capabilities.py`. The legacy class is unused except by its own tests. We delete the legacy class and the tests that exercise it; the `capabilities.py` path remains.

### 7.1 Whole-directory deletions (zero callers expected)

| Path                                         | Verification before deletion |
| -------------------------------------------- | ---------------------------- |
| `arcagent/modules/vault_azure/`              | Grep `vault_azure` repo-wide. Should be self-references only. |
| `arcagent/modules/slack/`                    | Grep `arcagent.modules.slack`. Should be self-references only. |
| `arcagent/modules/pulse/`                    | Confirm `arcagent/modules/proactive/` is the replacement and is wired in agent templates. |
| `arcagent/modules/scheduler/`                | `arcgateway/delivery.py:22` already documents this as "deleted by SPEC-017 R-040." `arccli/commands/agent.py:138, 141, 803, 806` still emits `[modules.scheduler]` in scaffolds — **fix the scaffolds** to emit `[modules.proactive]` instead, then delete the directory. |

### 7.2 Class-only deletions (keep `capabilities.py`)

| Path                                       | Action |
| ------------------------------------------ | ------ |
| `arcagent/modules/voice/__init__.py`       | Delete the `VoiceModule` legacy class; keep `capabilities.py`. Update `__init__.py` to export the capability functions. |
| `arcagent/modules/browser/__init__.py`     | Same — drop `BrowserModule`. |
| `arcagent/modules/web/__init__.py`         | Same — drop `WebModule`. |
| `arcagent/modules/messaging/__init__.py`   | Same — drop legacy `MessagingModule`. |
| `arcagent/modules/telegram/`               | The *legacy bot* (`telegram/bot.py` 732 LOC) — `arcgateway/adapters/telegram.py` says "Ports the proven polling/reconnect/auth logic from arcagent.modules.telegram.bot." If the gateway adapter is the active path, the bot is dead. Verify, then delete. |

### 7.3 `_runtime.py` legacy compatibility behavior

| Path                                     | Action |
| ---------------------------------------- | ------ |
| `arcagent/modules/voice/_runtime.py`     | Strip the "match legacy VoiceModule.__init__" branch; keep the canonical init. |
| `arcagent/modules/web/_runtime.py`       | Same. |
| `arcagent/modules/browser/_runtime.py`   | Same. |

### 7.4 Update CLI scaffolds

| File:Line | Action |
| --------- | ------ |
| `arccli/commands/agent.py:138, 141, 803, 806` | Replace `[modules.scheduler]` with `[modules.proactive]` in agent-scaffold templates. |
| `arcllm/adapters/registry.py` (if applicable) | Remove any legacy module names from defaults. |

### Verification (Phase 4)

This is the highest-blast-radius cleanup phase. Run **all 13 packages'** `pytest`+`ruff`+`mypy` matrices. Expect:

- ~30 test files removed (the legacy-module test suites).
- ~3,000–5,000 LOC net deletion (counting tests).
- `arcagent/modules/` dir count drops from 21 → 13.

If anything fails outside the deleted modules' own tests, **stop, revert, investigate**. Most likely cause would be a config template or scaffolder still referencing the old module name.

---

## 8. Phase 5 — Decomposition of Oversized Files

**Risk:** Medium. Behavior must be 100% preserved. The strategy is **internal-API-only changes**: the public exports from each file's `__init__.py` (or top-level imports) stay identical; only the internal organization changes. All tests must continue to import from the same names.

### 8.1 `arcagent/core/agent.py` — 1,147 LOC, `ArcAgent` class 835 LOC

**Target:** keep `agent.py` ≤ 350 LOC; the `ArcAgent` class becomes a thin orchestrator.

| New file                              | Extracts |
| ------------------------------------- | -------- |
| `arcagent/core/agent_lifecycle.py`    | Startup phases (DID resolution, vault wiring, model loading, capability registration), shutdown, reload — currently 200+ lines inside `ArcAgent`. |
| `arcagent/core/agent_dispatch.py`     | `chat`, `run`, `follow_up`, `steer` — the public dispatch surface. |
| `arcagent/core/vault_resolver.py`     | `_create_vault_resolver`, `_validate_vault_backend`. |
| `arcagent/core/model_manager.py`      | `_ensure_model`, `_resolve_model_config`. |
| `arcagent/core/agent.py` (slim)       | `ArcAgent.__init__`, public method delegation, identity validation. |

**Hard requirement:** `from arcagent import ArcAgent` keeps working. Public surface unchanged.

### 8.2 `arcagent/orchestration/spawn.py` — 999 LOC

| New file                                     | Extracts |
| -------------------------------------------- | -------- |
| `arcagent/orchestration/token_budget.py`     | `RootTokenBudget`, `ChildIdentityBudget` (~250 LOC). |
| `arcagent/orchestration/spawn_handle.py`     | `SpawnHandle` dataclass + lifecycle helpers (~150 LOC). |
| `arcagent/orchestration/spawn.py` (slim)     | `spawn_tool` factory + `_run_child` core logic only. |

### 8.3 `arcagent/core/tool_registry.py` — 650 LOC

| New file                                     | Extracts |
| -------------------------------------------- | -------- |
| `arcagent/core/tool_transport.py`            | `ToolTransport` enum, `register_transport`, transport adapters. |
| `arcagent/core/tool_policy_bridge.py`        | Policy evaluation + audit emission for tool calls. |
| `arcagent/core/tool_registry.py` (slim)      | Registry + dispatch only. |

### 8.4 `arcagent/core/capability_registry.py` — 534 LOC, `capability_loader.py` — 470 LOC, `config.py` — 415 LOC

Smaller wins. Defer to a follow-up sweep unless the overall core-LOC budget is still over 3,500 after 8.1–8.3.

### 8.5 `arccli/commands/agent.py` — 1,553 LOC

Convert the single file into a subpackage. The argparse dispatch table (`_SUBCOMMAND_MAP`, ~L1512) maps each subcommand to a handler — extract handlers 1-to-1.

```
arccli/commands/agent/
├── __init__.py    # re-exports agent_handler for backward compat
├── _dispatch.py   # _build_parser, _SUBCOMMAND_MAP, agent_handler
├── _common.py     # _resolve_agent_dir, _load_env, _print_kv, _print_table, _safe_eval
├── create.py      # _create + _scaffold_workspace + _print_scaffold_summary
├── status.py      # _status + _run_validation
├── skills.py      # _skills + _iter_skill_folders
├── extensions.py  # _extensions + _iter_capability_files
├── sessions.py    # _sessions
├── build.py       # _build + _run_interactive_build
├── tools.py       # _tools + _discover_tools
├── config.py      # _config + _load_agent_config
├── reload.py      # _reload
├── strategies.py  # _strategies + _STRATEGIES
├── events.py      # _events
├── run.py         # _run + _agent_run_once
├── serve.py       # _serve + _serve_daemon
└── chat.py        # _chat + _chat_interactive
```

`arccli/commands/agent_handler` callers continue to work via `arccli/commands/agent/__init__.py` re-export.

### 8.6 `arcui/routes/agent_detail.py` — 953 LOC

```
arcui/routes/agent_detail/
├── __init__.py    # router assembly + re-export
├── _common.py     # _agent_root, _resolve_root_path, fs_reader wiring
├── config.py      # GET /config + _whitelist_config
├── skills.py      # GET /skills + _parse_skill + _parse_tool_blocks
├── tools.py       # GET /tools + _collect_module_tools
├── telemetry.py   # GET /stats, /traces, /audit
├── policy.py      # GET /policy + _read_policy + _bullet_to_dict
└── sessions.py    # GET /sessions + _parse_pagination + _parse_jsonl
```

### 8.7 `arcui/static/assets/agent-detail.js` — 1,409 LOC (frontend)

The file is currently loaded as a plain `<script src="assets/agent-detail.js">` (`index.html:811`) — an IIFE registering on `ARC.AgentDetail`, sibling to 18 other IIFE scripts (`arc-shell.js`, `formatters.js`, `dom-batcher.js`, `file-tree.js`, etc.).

**Constraint:** loading mechanism MUST NOT change. No `type="module"`. No build step. No bundler. Match the existing IIFE-script pattern.

Split into multiple sibling IIFE files, each registering helpers on the shared `ARC.AgentDetail` namespace:

```
static/assets/
├── agent-detail.js                  # slim entry: defines ARC.AgentDetail.mount; delegates to helpers
├── agent-detail-timeline.js         # ARC.AgentDetail._timeline = { render, update }
├── agent-detail-modules.js          # ARC.AgentDetail._modules = { render }
├── agent-detail-files.js            # ARC.AgentDetail._files = { render } (or reuse global file-tree.js)
├── agent-detail-audit.js            # ARC.AgentDetail._audit = { render, paginate }
├── agent-detail-policy.js           # ARC.AgentDetail._policy = { render }
└── agent-detail-shared.js           # ARC.AgentDetail._shared = { escText, kvRow, modelShort, ... }
```

Update `index.html` to add 6 new `<script src="...">` tags **before** the existing `<script src="assets/agent-detail.js">` line, following the existing convention with the `?v={{ARC_BUILD_ID}}` cache-buster. Script load order matters: `_shared` first, then specialty modules, then `agent-detail.js` last (it's the public entry).

**Behavior preserved:** same DOM output, same fetch calls, same WebSocket subscriptions, same global `ARC.AgentDetail.mount(panelEl, agentId) -> {dispose, refresh, setTab}` API.

### 8.8 `arcskill/hub/dry_run.py` — 921 LOC

| New file                                | Extracts |
| --------------------------------------- | -------- |
| `arcskill/hub/_firecracker.py`          | Firecracker microVM lifecycle. |
| `arcskill/hub/_docker.py`               | Docker container lifecycle. |
| `arcskill/hub/dry_run.py` (slim)        | Subprocess fallback + `run_dry_run` entry point. |

### 8.9 `arcskill/hub/scanner.py` — 919 LOC

| New file                                | Extracts |
| --------------------------------------- | -------- |
| `arcskill/hub/_secret_patterns.py`      | Regex patterns + secret detection. |
| `arcskill/hub/_ast_scanner.py`          | AST walker. |
| `arcskill/hub/scanner.py` (slim)        | Pipeline orchestration only. |

### 8.10 `arcgateway/pairing.py` — 901 LOC

| New file                                | Extracts |
| --------------------------------------- | -------- |
| `arcgateway/pairing_postgres.py`        | `PostgresPairingStore` stub. (Stays a stub per `TODO(T1.8.4)`; just isolated.) |
| `arcgateway/pairing.py` (slim)          | `PairingStore` (SQLite impl) + public API. |

### 8.11 `arcllm/modules/telemetry.py` — 620 LOC

| New file                                | Extracts |
| --------------------------------------- | -------- |
| `arcllm/modules/telemetry_budget.py`    | `BudgetAccumulator`, `BudgetChecker`, period helpers. |
| `arcllm/modules/telemetry_cost.py`      | Cost/token estimation. |
| `arcllm/modules/telemetry.py` (slim)    | OTel integration + module entry. |

### 8.12 `arcllm/trace_store.py` — 550 LOC

Extract query logic to `arcllm/trace_query.py`; keep schema + persistence in core.

### 8.13 `arcrun/backends/loader.py` — 666 LOC

| New file                                | Extracts |
| --------------------------------------- | -------- |
| `arcrun/backends/_verifier.py`          | `_verify_allowed_backends_signature`, `_canonical_json_payload`, `_verify_backend_content_hash`. |
| `arcrun/backends/_manifest.py`          | Manifest structure parsing. |
| `arcrun/backends/loader.py` (slim)      | Discovery + entry point. |

### 8.14 `arcteam/storage.py` — 491 LOC

Extract NDJSON tailing to `arcteam/_ndjson_reader.py`. Defer until after Phase 4 lands (storage layout may shift).

### 8.15 Smaller wins (defer to follow-up sweep)

`arccli/commands/ui.py` (637), `arccli/commands/team.py` (585), `arccli/commands/llm.py` (514), `arcgateway/adapters/telegram.py` (775), `arcagent/modules/bio_memory/{consolidator,deep_consolidator,bio_memory_module}.py` (843/833/655), `arcagent/modules/telegram/bot.py` (732 — *deleted in Phase 4 if confirmed dead*), `arcagent/tools/_dynamic_loader.py` (659 — security-sensitive; touch only with surgical review).

### Verification (Phase 5)

Each decomposition is **isolated** to its own commit and validated independently. After every split:

1. Confirm public imports still resolve from the same names (`from arcagent import ArcAgent`, `from arccli.commands.agent import agent_handler`, etc.).
2. Run the *full* test suite for that package (not just changed files).
3. `mypy --strict` on every file in the new subpackage.
4. `ruff check` clean.

If a decomposition crosses a package boundary (e.g., import cycles surface), revert and rethink the split.

Expected net change: zero LOC reduction (we're moving code, not deleting it), but average file size drops and complexity-per-file drops sharply.

---

## 9. Phase 6 — Refactor-only Typing & Doc Cleanup

**Behavior-preserving only.** No new validation, no new failure modes, no new exceptions caught/uncaught, no new fields, no new tamper detection. All edits in this phase produce **byte-identical wire output and identical control flow**.

### 9.1 `arcui` typed response models (wire-identical)

The current routes return `JSONResponse(dict)`. Switching to `JSONResponse(Model(...).model_dump(mode="json"))` produces the same JSON bytes — the model is just a typed shape over the same dict. Pure refactor.

| File | Action |
| ---- | ------ |
| `arcui/schemas.py` (new) | Pydantic response models that mirror the *current* dict shapes 1:1 — no extra fields, no rename, no validation tightening: `ConfigResponse`, `RosterResponse`, `TraceResponse`, `StatsResponse`, `BudgetResponse`, etc. Field defaults match what each route currently returns. |
| All `arcui/routes/*.py` | Replace `JSONResponse(dict)` with `JSONResponse(SomeModel(**dict_payload).model_dump(mode="json"))`. **Add a contract test per route** that asserts the new payload bytes-equal the previous payload for a fixed input. |

If any current route returns a *variant* shape under different conditions, the model captures both branches with `Optional[...]` fields rather than tightening.

### 9.2 `arcui` query-param parsing helpers (parsing-identical)

Pure refactor — extract repeated `try: int(qp.get("page", "1")) except ValueError: ...` blocks into helpers that produce the same outputs (including the same fallback values on parse failure).

| File | Action |
| ---- | ------ |
| `arcui/query_validators.py` (new) | `safe_int(value, default, min_, max_) -> int`, `safe_choice(value, choices, default) -> str`, `parse_pagination(qp) -> Pagination`. **Behavior matches the most-careful caller today** — anywhere a route is *less* careful, do not silently tighten; preserve current outputs. |
| All routes that read query strings | Use the centralized helpers. |

### 9.3 Document every surviving `# type: ignore`

Every `# type: ignore[...]` that survives Phases 1–5 must carry an inline `# reason: ...` explanation. No code change — comment additions only.

### 9.4 Document every surviving broad `except`

Every `except Exception:` that survives must carry an inline comment explaining the design: `# fail-open: ...` or `# test-instrumentation: ...`. No code change — comment additions only. Exception class is **not narrowed** in this phase.

### Verification (Phase 6)

Per-package `pytest`+`ruff`+`mypy`. The Pydantic response models add **byte-identity tests** (per route, fixed input → fixed output) that ride alongside existing tests.

---

## 10. New-feature work — explicitly NOT in this refactor

These were proposed in earlier drafts of this plan but are **new behavior**, not refactor. They are deferred to a separate spec — do not include them in the cleanup work.

| Item | Why it's not refactor |
| ---- | --------------------- |
| `ArcAgent.__init__` raises early on missing DID | Different failure point = behavior change. Today the failure happens at `startup()`; moving it to `__init__` changes when callers see the error. |
| HMAC integrity verification on `identity.jsonl` and `context.md` | New verification step. New audit event. New rejection path. New feature. |
| Narrowing `except Exception:` blocks to specific exception classes | Different exceptions are caught/uncaught vs. today = behavior change. |
| Frontend `.innerHTML` → `.textContent` audit | `agent-detail.js` already uses `escText`/`escAttr` to pre-escape values before insertion. Switching to `.textContent` would lose intentional HTML structure. This is a redesign, not a refactor. |
| CSRF middleware for `arcui` state-change routes | New security feature. |
| `arcllm` provider audit / "kill list" | All 18 providers have lazy exports in `arcllm/__init__.py`, config TOMLs in `arcllm/providers/`, and CLI references in `arccli/.claude/CLI.md` and `arccli/commands/init.py`. None are dead. There is no kill list. |

If any of the above become important, file them as new specs. They are not part of the cleanup contract.

---

## 11. Out of Scope / Deferred

These are *real* concerns surfaced during audit but are **not** included in this plan:

| Item                                                  | Why deferred |
| ----------------------------------------------------- | ------------ |
| `pairing.py:889` `TODO(T1.8.4)` Postgres pairing impl | New feature work, not cleanup. |
| `arcteam/memory/promotion_gate.py:166` messaging TODO | Genuinely open work (gates CUI+ promotion). |
| `arccli/commands/spec017.py` Click-based shim         | Intentional isolation for CI scripts; documented. |
| `arccli/agent_worker.py:37, 88` (M2) ArcRun streaming | Future feature work. |
| `arcgateway/runner.py:398` (M1) audit event           | Add in next milestone. |
| `arcagent/modules/session/search.py:7` (M2) re-rank   | Move to roadmap doc; do not delete. |
| `arcprompt/`, `arcmodel/` "Coming soon" stubs         | Product decision. |
| `arcllm/adapters/base.py:108` `# type: ignore`        | Phase 6 will document; if undocumented after, escalate. |
| Adding CSRF middleware to `arcui`                     | Pre-production hardening; not refactor. |

---

## 12. Verification Protocol (between *every* phase)

```bash
# 1. Per-package tests (uses uv workspace)
for pkg in <touched-packages>; do
  uv run --package "$pkg" pytest -q
done

# 2. Linting (must be 0 errors)
for pkg in <touched-packages>; do
  uv run --package "$pkg" ruff check "packages/$pkg"
done

# 3. Type-check (must be 0 errors)
for pkg in <touched-packages>; do
  uv run --package "$pkg" mypy --strict "packages/$pkg/src"
done

# 4. Diff baseline (catch silent regressions)
diff <(uv run pytest -q --collect-only 2>&1) baseline-collect.log
```

**Hard rule (per CLAUDE.md):** if `ruff` or `mypy` flags an error during a phase — even one we didn't introduce — fix it now, don't defer.

**Three-strikes rule:** if any phase requires more than 3 attempts to land green, stop and re-evaluate the architecture of the change.

---

## 13. Estimated Effort

| Phase | Sessions | Net LOC change |
| ----- | -------- | -------------- |
| 0 — Baseline                          | 0.25 | 0 |
| 1 — Comment cleanup                   | 0.25 | −150 |
| 2 — Feature-flag removal              | 0.5  | −400 |
| 3 — Backward-compat code paths        | 0.75 | −300 |
| 4 — Legacy module-shim deletion       | 1.5  | −3,500 to −5,000 (incl. tests) |
| 5 — Decomposition                     | 2.5  | ≈0 (moves, no deletes) |
| 6 — Refactor-only typing & doc        | 0.75 | +200 (typed schemas + helpers) / −0 |
| **Total**                             | **~6.5 sessions** | **~−4,500 LOC, ~50 fewer files** |

`arcagent/core/` LOC after Phase 5: targeted **≤ 3,200**, restoring the ADR-004 budget.

---

## 14. Decisions Required Before Execution Begins

These choices change the plan; resolve them before Phase 1.

| # | Decision | Resolution | Status |
| - | -------- | ---------- | ------ |
| 1 | Phase 4: delete legacy `arcagent.modules.telegram.bot` (732 LOC)? | **Delete** ✓ | resolved |
| 2 | Phase 4: replace `[modules.scheduler]` with `[modules.proactive]` in agent scaffolds at `arccli/commands/agent.py:138, 141, 803, 806`? | **Yes** ✓ | resolved |
| 3 | Phase 5.1: hard LOC cap on `arcagent/core/agent.py` after split | **500** ✓ | resolved |
| 4 | ~~Phase 5.7 frontend split~~ — **mooted.** No build step, no ES modules; split into sibling IIFE `<script>` files matching the existing pattern (see §8.7). | n/a | resolved |
| 5 | ~~Phase 6.3 HMAC key derivation~~ — **mooted.** HMAC integrity is new functionality, not refactor; removed from this plan (see §10). | n/a | resolved |
| 6 | ~~Phase 7 provider kill-list~~ — **mooted.** Verified: all 18 providers have lazy exports + config TOMLs + CLI references. None are dead. Phase 7 removed (see §10). | n/a | resolved |

**All decisions resolved. Plan is locked and ready to execute.**

---

## 15. Done Definition

This refactor is complete when:

- [ ] `arcagent/core/` total LOC ≤ 3,500.
- [ ] No file in `packages/*/src/` exceeds 500 LOC (test files exempt, but flagged for follow-up).
- [ ] Zero `legacy`/`backward[-_ ]compat`/`deprecated`/`vestigial`/`main_legacy`/`kept for` markers in source (test fixtures may keep the *words* but not the *patterns*).
- [ ] Zero `ARCUI_LEGACY_POLLING` references in code or env-var docs.
- [ ] Every `# type: ignore` and broad `except` carries a same-line explanation.
- [ ] All 13 packages: `pytest` green, `ruff check` clean, `mypy --strict` clean.
- [ ] `arcagent/modules/` contains only modules listed in the canonical capability registry.
- [ ] No public import path changed (in-tree callers don't move).
- [ ] No new behavior introduced (no new exceptions caught/uncaught, no new validation, no new audit events, no new failure modes). Wire output of every refactored route is byte-identical to baseline for fixed inputs.
