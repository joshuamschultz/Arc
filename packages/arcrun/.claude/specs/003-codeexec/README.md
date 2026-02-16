# Spec 003: CodeExec — Strategy Selection + Code Execution

## Metadata

| Field | Value |
|-------|-------|
| ID | 003 |
| Name | codeexec |
| Phase | 2 |
| Status | PENDING |
| Type | Core Library |
| Created | 2026-02-14 |
| Decisions | 032-034 (see `.claude/decision-log.md`) |
| Brainstorm | `.claude/brainstorms/2026-02-14-phase-2-codeexec.md` |
| Build | `.claude/builds/phase-2-codeexec/decisions.md` |

## Summary

Phase 2 of arcrun: ExecuteTool (sandboxed Python subprocess), CodeExec strategy (augmented system prompt wrapping ReAct), and model-based strategy selection. Three tightly coupled components that enable agents to write and execute code as a problem-solving approach.

## Key Decisions

| # | Decision | Choice |
|---|----------|--------|
| 032 | CodeExec loop structure | Wrapper around react_loop |
| 033 | Strategy interface | ABC base class |
| 034 | Strategy metadata | name + description |

Plus from brainstorm/research:

| Topic | Choice |
|-------|--------|
| ExecuteTool sandbox | Bare subprocess, SandboxConfig owns policy |
| Strategy selection | Model picks via tool calling with enum |
| Output format | `{stdout, stderr, exit_code, duration_ms}` JSON |
| Location | `src/arcrun/builtins/execute.py` |
| System prompt | Hardcoded default, configurable, overridable |
| Working directory | `tempfile.TemporaryDirectory()` |
| Code persistence | Temp file (not `python -c`) |
| Environment | Hardcoded minimal, never inherit parent |
| Timeout | SIGTERM -> 5s grace -> SIGKILL |
| Process cleanup | `start_new_session=True` + `os.killpg()` |
| State persistence | Stateless (each exec is fresh) |

## Learnings

(Updated during implementation)

## Open Questions

- Streaming support for code execution output (deferred — depends on arcllm streaming)
- Stateful execution across turns (explicitly deferred — additive future change)
