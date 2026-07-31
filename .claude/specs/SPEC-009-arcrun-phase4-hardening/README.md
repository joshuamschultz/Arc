# SPEC-009: ArcRun Phase 4 — Hardening

| Field | Value |
|-------|-------|
| **ID** | SPEC-009 |
| **Feature** | arcrun-phase4-hardening |
| **Type** | Security / Integration |
| **Status** | COMPLETE |
| **Created** | 2026-02-21 |
| **Completed** | 2026-02-21 |
| **Package** | `packages/arcrun/` |
| **LOC Budget** | 1,400 (revised from 900 per D13) |
| **Final LOC** | 1,460 (formatted) — 60 over estimate, well under 3,500 project limit |
| **Net New LOC** | 91 lines |

## Prior Work

| Stage | Output | Date |
|-------|--------|------|
| Brainstorm | `.claude/brainstorms/2026-02-16-recursive-agent-spawning.md` | 2026-02-16 |
| Build | `.claude/decisions-log.md` (Feature: ArcRun Phase 4 — Hardening) | 2026-02-21 |
| Deepen | 5 research agents synthesized into decisions-log.md | 2026-02-21 |

## Build Decisions (14)

| # | Decision | Choice |
|---|----------|--------|
| D1 | Container runtime | Docker SDK with configurable socket (Docker + Podman) |
| D2 | Container scope | CodeExec only |
| D3 | Integration pattern | New factory `make_contained_execute_tool()` |
| D4 | Container defaults | Maximum lockdown (no net, ro FS, mem/cpu/pid limits) |
| D5 | Event integrity | SHA-256 hash chain |
| D6 | Verification API | `verify_integrity()` on LoopResult |
| D7 | Adversarial test scope | Comprehensive (8 categories) |
| D8 | Test location | Dedicated `tests/security/` directory |
| D9 | Concurrent testing | Stress tests in `test_timing_attacks.py` |
| D10 | NIST docs format | Standalone `docs/security/` directory |
| D11 | NIST scope | Full audit (38 controls, 8 families) |
| D12 | Docker dependency | Optional via `pip install arcrun[container]` |
| D13 | LOC budget | Revised to 1,400 |
| D14 | Image management | Caller specifies, no auto-pull (air-gap safe) |

## Research Insights Summary

- 3 real bugs discovered (mutable Event data, no seccomp on execute, no PID limits)
- 38 NIST 800-53 controls mapped (21 fully implemented, 14 partial, 3 planned)
- Complete implementation patterns for container factory and hash chain
- Docker SDK + Podman compatibility verified
- Seccomp profile and tar injection patterns researched

## Risks

1. `Event.data: dict -> MappingProxyType` is a breaking change — must audit all callers
2. Container runtime availability varies — clear error messages required
3. Seccomp profile portability across kernel versions
4. EventBus thread safety requires `threading.Lock` addition
5. `LoopResult.events: list[Any]` needs typing upgrade to `list[Event]`

## Learnings

1. **MappingProxyType backward compatibility**: `Event.__post_init__` with `object.__setattr__` auto-converts dict to MappingProxyType — zero breakage across 183 existing tests
2. **Ruff format inflates LOC**: Compact code written at 1,398 lines expands to 1,460 after ruff formatting. LOC budgets should account for formatter expansion.
3. **Mock exec_run output**: Docker SDK's `exec_run` returns `(exit_code, (stdout, stderr))` with `demux=True`. Mocks must set `exec_result.output = (stdout, stderr)` explicitly — MagicMock defaults don't unpack.
4. **Observer outside lock**: EventBus observer callbacks must run outside `threading.Lock` or they'll deadlock if the observer calls `emit()` recursively.
5. **Security test isolation**: `tests/security/conftest.py` with its own MockModel/ToolCall/LLMResponse avoids importing test fixtures from `tests/conftest.py` — clean separation between unit and security tests.
6. **Tool name unicode spoofing**: Sandbox denies tool names with zero-width spaces because `"safe_tool\u200b" not in ["safe_tool"]` — exact string matching is the right default for security.

## Related Specs

- SPEC-001 through SPEC-008: Prior arcrun/arcllm specifications
