# phase-2-codeexec — build & deepen notes

The `/build` and `/deepen` output for this feature: research insights, architecture diagrams,
component lists, risk registers, open questions and handoff notes. Verbatim, in original order.

**Decisions from this build:** D-617–D-619 (3 total) — see [`.claude/decisions-log.md`](../../decisions-log.md).

---

## Build Decisions: Phase 2 — CodeExec

# Build Decisions: Phase 2 — CodeExec
**Date:** 2026-02-14
**Status:** Complete (all decisions made)
**Input:** `.claude/brainstorms/2026-02-14-phase-2-codeexec.md` (research-enriched)
**Next:** `/specify phase-2-codeexec`
## Decision Summary
| # | Decision | Choice | Source |
|---|----------|--------|--------|
| 032 | CodeExec loop structure | Wrapper around react_loop | /build |
| 033 | Strategy interface | ABC base class | /build |
| 034 | Strategy metadata | name + description | /build |
Plus 11 decisions from brainstorm + research:
| Topic | Choice | Source |
|-------|--------|--------|
| ExecuteTool sandbox | Bare subprocess, SandboxConfig owns policy | /brainstorm |
| Strategy selection | Model picks via tool calling with enum | /brainstorm + research |
| Output format | `{stdout, stderr, exit_code, duration_ms}` JSON | /brainstorm |
| Location | `src/arcrun/builtins/execute.py` | /brainstorm |
| System prompt | Hardcoded default, configurable, overridable | /brainstorm |
| Working directory | `tempfile.TemporaryDirectory()` | /deepen research |
| Code persistence | Temp file (not `python -c`) | /deepen research |
| Environment variables | Hardcoded minimal, never inherit | /deepen research |
| Timeout strategy | SIGTERM -> 5s grace -> SIGKILL | /deepen research |
| Process cleanup | `start_new_session=True` + `os.killpg()` | /deepen research |
| State persistence | Stateless (each exec is fresh subprocess) | /deepen research |
## Complete Architecture
```
src/arcrun/
├── __init__.py              # Add: make_execute_tool, Strategy exports
├── builtins/
│   ├── __init__.py          # exports make_execute_tool
│   └── execute.py           # ExecuteTool factory (~40 lines)
├── strategies/
│   ├── __init__.py          # Strategy ABC, registry, model-based selection (~50 lines)
│   ├── react.py             # ReactStrategy class wrapping react_loop (~140 lines)
│   └── code.py              # CodeExecStrategy: modify prompt, delegate to react (~30 lines)
└── (existing modules unchanged)
```
### Component Relationships
```
run() → select_strategy() → model.invoke() picks "code"
  │
  └─ CodeExecStrategy.__call__()
       │
       ├─ Prepend CodeExec system prompt to state.messages[0]
       │
       └─ react_loop(model, state, sandbox, max_turns)
            │
            ├─ Model writes code, calls execute_python tool
            │
            └─ ExecuteTool runs subprocess, returns structured result
```
### Strategy Selection Implementation
```python
async def select_strategy(allowed, model, state):
    if allowed is None:
        return "react"
    if len(allowed) == 1:
        return allowed[0]
    # Build selection tool with enum of allowed strategy names
    # model.invoke() with selection tool → guaranteed valid choice
    # Fallback: default to "react" if selection fails
```
### ExecuteTool Implementation Sketch
```python
async def _execute(params: dict, ctx: ToolContext) -> str:
    code = params["code"]
    with tempfile.TemporaryDirectory() as tmpdir:
        # Write code to temp file
        code_path = os.path.join(tmpdir, "script.py")
        with open(code_path, "w") as f:
            f.write(code)
        # Minimal environment
        env = {"PATH": "/usr/bin:/bin", "HOME": "/tmp", "LANG": "en_US.UTF-8"}
        env.update(extra_env)
        # Run with process group isolation
        proc = await asyncio.create_subprocess_exec(
            sys.executable, code_path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=tmpdir,
            env=env,
            start_new_session=True,
        )
        # Two-phase timeout: SIGTERM → grace → SIGKILL
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=timeout_seconds
            )
        except asyncio.TimeoutError:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            await asyncio.sleep(grace_period)
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            ...
    # Return structured result
    return json.dumps({
        "stdout": stdout[:max_output_bytes].decode(),
        "stderr": stderr[:max_output_bytes].decode(),
        "exit_code": proc.returncode,
        "duration_ms": duration_ms,
    })
```
## Event Coverage (Phase 2 Additions)
| Event | Emitted By | Data |
|-------|-----------|------|
| `strategy.selection.start` | strategies/__init__.py | allowed_strategies, task |
| `strategy.selection.complete` | strategies/__init__.py | selected, reasoning |
| `strategy.selection.fallback` | strategies/__init__.py | attempted, defaulted_to |
| `code.prompt.augmented` | strategies/code.py | original_length, augmented_length |
All existing events (tool.start, tool.end, tool.denied, etc.) apply unchanged to ExecuteTool calls.
## Notes for /specify
- react_loop function stays as-is internally. ReactStrategy class wraps it.
- CodeExecStrategy modifies state.messages[0] content (prepend to system prompt text), then calls react_loop.
- ExecuteTool uses `asyncio.create_subprocess_exec` (not subprocess.run) since we're async.
- Strategy ABC goes in strategies/__init__.py (not a separate file — under budget).
- The `select_strategy` function needs access to strategy descriptions, which means STRATEGIES dict values change from functions to Strategy instances.
- Public API additions: `make_execute_tool` and `Strategy` exported from `arcrun.__init__`.

---
