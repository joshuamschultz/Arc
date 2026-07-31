# Build Decisions: Phase 2 — CodeExec

**Date:** 2026-02-14
**Status:** Complete (all decisions made)
**Input:** `.claude/brainstorms/2026-02-14-phase-2-codeexec.md` (research-enriched)
**Next:** `/specify phase-2-codeexec`

---

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

---

## DECISION-032: CodeExec Wraps react_loop

**Context:** CodeExec is "ReAct with an augmented system prompt." Three options for how code.py relates to react.py.

**Options:**
1. Wrapper around react_loop — code.py prepends prompt, delegates to react (~15 lines)
2. Copy + modify react.py — full duplication allowing future divergence (~135 lines)
3. Parameterized react_loop — add system_prompt_prefix param to react_loop itself

**Decision:** Wrapper around react_loop

**Reasoning:** Zero duplication. CodeExec is literally: modify the system message in state.messages, then call react_loop. If CodeExec needs to diverge later (different error retry logic, code-specific turn handling), it can be unwrapped into its own loop. YAGNI until then.

**Implication:** `_build_result()` stays in react.py — no extraction needed. CodeExecStrategy.__call__ modifies state.messages[0] (system prompt), then delegates.

---

## DECISION-033: ABC Base Class for Strategy

**Context:** With two strategies, the implicit function-signature convention needs formalization.

**Options:**
1. `typing.Protocol` — structural typing, strategies just need the right shape
2. Keep implicit — functions in a dict, metadata stored separately
3. `ABC` base class — explicit inheritance, clear contract

**Decision:** ABC base class

**Reasoning:** Clearer contract than Protocol. Forces every strategy to declare name + description + implement __call__. Good for documentation. Both strategies become classes:
- `ReactStrategy` wraps the existing `react_loop` function
- `CodeExecStrategy` inherits nothing from ReactStrategy — it's its own class that calls react_loop

**Shape:**
```python
from abc import ABC, abstractmethod

class Strategy(ABC):
    @property
    @abstractmethod
    def name(self) -> str: ...

    @property
    @abstractmethod
    def description(self) -> str: ...

    @abstractmethod
    async def __call__(self, model, state, sandbox, max_turns) -> LoopResult: ...
```

---

## DECISION-034: Strategy Metadata is name + description

**Context:** What metadata does each strategy expose for model-based selection?

**Options:**
1. name + description — two strings, minimal
2. name + description + when_to_use — three strings, more guidance
3. name + description + capabilities — structured tags

**Decision:** name + description

**Reasoning:** Sufficient for model routing. The description already explains when to use ("iterative tool-calling loop for multi-step problems" vs "write and execute Python code to solve tasks"). Adding when_to_use is redundant. Capability tags add boilerplate that only matters with many strategies — premature with two.

---

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

---

## Event Coverage (Phase 2 Additions)

| Event | Emitted By | Data |
|-------|-----------|------|
| `strategy.selection.start` | strategies/__init__.py | allowed_strategies, task |
| `strategy.selection.complete` | strategies/__init__.py | selected, reasoning |
| `strategy.selection.fallback` | strategies/__init__.py | attempted, defaulted_to |
| `code.prompt.augmented` | strategies/code.py | original_length, augmented_length |

All existing events (tool.start, tool.end, tool.denied, etc.) apply unchanged to ExecuteTool calls.

---

## Notes for /specify

- react_loop function stays as-is internally. ReactStrategy class wraps it.
- CodeExecStrategy modifies state.messages[0] content (prepend to system prompt text), then calls react_loop.
- ExecuteTool uses `asyncio.create_subprocess_exec` (not subprocess.run) since we're async.
- Strategy ABC goes in strategies/__init__.py (not a separate file — under budget).
- The `select_strategy` function needs access to strategy descriptions, which means STRATEGIES dict values change from functions to Strategy instances.
- Public API additions: `make_execute_tool` and `Strategy` exported from `arcrun.__init__`.
