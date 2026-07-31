# Brainstorm: Phase 2 — CodeExec

**Date:** 2026-02-14
**Status:** Deepened (research-enriched)
**Next:** `/build` or `/specify`
**Research:** 4 parallel agents — subprocess sandboxing, strategy selection, CodeExec patterns, strategy protocol

---

## Scope

Phase 2 delivers three tightly coupled components:

1. **ExecuteTool** — Sandboxed Python subprocess (builtin tool)
2. **CodeExec strategy** — Augmented system prompt encouraging code-writing
3. **Strategy selection** — Model picks from allowed strategies at run start

---

## Key Decisions Made

### ExecuteTool Sandbox Approach

**Decision:** Bare subprocess, sandbox via SandboxConfig

ExecuteTool runs model-generated Python in `subprocess.run()`. All security restrictions come from the existing `SandboxConfig.check` callback — the caller decides what's safe. No restricted imports, no AST analysis, no RestrictedPython. Container isolation comes in Phase 4.

**Rationale:** Keeps ExecuteTool simple. The sandbox system already exists and works. Callers who want restricted imports can implement that in their check callback. arcrun stays unopinionated about what "safe code" means.

**Security implication:** Without caller-provided sandbox, ExecuteTool runs arbitrary Python. This is by design — same as any other tool. The sandbox is the security gate, not the tool.

#### Research Insights: Subprocess Sandboxing

**Critical finding:** Subprocess sandboxing alone is insufficient for truly untrusted code. Production systems (OpenAI Code Interpreter, E2B) use Firecracker microVMs or container isolation. But subprocess with proper config is a reasonable Phase 2 baseline before Phase 4 adds containers.

**Practical recommendations for ExecuteTool:**
- Always use list args (not `shell=True`) to prevent command injection
- Use temp file (not `python -c`) — no size limits, better for multiline, can preserve for debugging. Use `tempfile.NamedTemporaryFile(suffix='.py', delete=False)` + unlink in finally block
- Hardcoded minimal env: `{'PATH': '/usr/bin:/bin', 'HOME': '/tmp', 'LANG': 'en_US.UTF-8'}` — never inherit `os.environ`
- Isolated temp dir via `tempfile.TemporaryDirectory()` as working directory
- Two-phase timeout: SIGTERM first, 5s grace period, then SIGKILL
- Use `start_new_session=True` for process group cleanup (kills child processes too)
- `capture_output=True` with truncation — `communicate()` buffers in memory

**Common escape vectors to document (for callers writing sandbox checks):**
- `__subclasses__()` introspection bypasses import restrictions entirely
- `ctypes` = complete sandbox escape (arbitrary memory access)
- `/proc/self/mem` manipulation
- Encoding tricks (UTF-7, unicode_escape) smuggle code past string filters
- **Key insight:** Keyword-based import filtering is fundamentally flawed. OS-level isolation (Phase 4) is the real fix.

**Reference implementations:** OpenEdX CodeJail (AppArmor-based), E2B (Firecracker microVMs)

### Strategy Selection

**Decision:** Model picks from allowed list, once at start

When `run()` receives multiple `allowed_strategies`, arcrun makes one LLM call asking the model to pick the best strategy for the task. The selected strategy runs for the entire execution. No mid-run switching.

**Rationale:** More autonomous than caller-specifies. Simpler than per-turn adaptive. One extra LLM call is acceptable overhead for better strategy matching.

**Implementation:** Selection call uses the same `model.invoke()` — no new dependencies. Model sees strategy names + descriptions + the task.

#### Research Insights: Strategy Selection Patterns

**Most reliable pattern: Structured output with tool calling.** Use arcllm's existing tool calling mechanism with a "select_strategy" tool that has an enum parameter. This guarantees valid selection — no regex parsing, no garbage output.

```python
# Strategy selection as a tool call
select_tool = Tool(
    name="select_strategy",
    description="Select the best execution strategy for this task",
    input_schema={
        "type": "object",
        "properties": {
            "strategy": {
                "type": "string",
                "enum": ["react", "code"],
                "description": "react: iterative tool-calling loop. code: write and execute Python to solve."
            },
            "reasoning": {"type": "string"}
        },
        "required": ["strategy"]
    }
)
```

**Should model see the tools list?** Yes. Tool visibility improves selection accuracy — model can reason "I have execute_python available, so code strategy makes sense."

**Strategy metadata for selection:** Name + description + "when to use" guidance. Keep it short — this is a cheap selection call, not a planning phase.

**Fallback strategy:**
1. Validate output against known strategy names
2. If invalid → default to "react" (safest, most general)
3. Log the failure for monitoring
4. Never retry the selection call — just fall back

**Future optimization (not Phase 2):** Semantic routing (vector similarity, no LLM call) is 50x faster and ~100x cheaper. Could replace LLM-based selection for well-defined cases once patterns stabilize.

### ExecuteTool Output Format

**Decision:** Structured result — `{stdout, stderr, exit_code, duration_ms}` as JSON string

Model gets full signal about what happened. Can distinguish success from failure, see both output streams, and understand timing.

### ExecuteTool Location

**Decision:** `src/arcrun/builtins/execute.py`

New `builtins/` directory for arcrun-provided tools. Sets the pattern for SpawnTool in Phase 3. Caller must explicitly include ExecuteTool in their tool list — it's not auto-injected.

### CodeExec System Prompt

**Decision:** Hardcoded default, configurable at strategy level, overridable per-call

CodeExec ships a default system prompt prefix that teaches the model to write and execute Python. Callers can:
1. Use the default (most common)
2. Configure a custom default at strategy registration
3. Override per-call via a parameter

#### Research Insights: CodeExec Prompt Patterns

**Key finding from CodeAct (ICML 2024):** Code-as-action yields **20% higher success rates** than JSON/text-based tool calling across 17 LLMs. This validates the strategy's existence.

**Effective system prompt elements (from OpenAI, Open Interpreter, CodeAct):**
1. **Explicitly state code will be executed** — not described, not planned, *executed*
2. **List available libraries** — model won't import what it doesn't know exists
3. **Emphasize iteration** — "write code, get results, debug, refine"
4. **Small scripts preferred** — 20-50 lines focused on one sub-problem, not monolithic programs
5. **Persistence instruction** — "keep going until complete, don't stop prematurely"

**Draft default prompt prefix:**
```
You have access to a Python execution tool. Write executable Python code to solve tasks.

GUIDELINES:
- Write focused scripts (20-50 lines) solving one sub-problem at a time
- You will receive {stdout, stderr, exit_code, duration_ms} after each execution
- Each execution is stateless — variables do NOT persist between calls
- If code fails, examine the error and fix your approach
- After 3 failures on the same approach, try a fundamentally different method
- Use code for: computation, data processing, logic, file operations
- Use other tools for: external APIs, user confirmation, security-sensitive ops
```

**Hybrid approach is optimal:** CodeExec should also allow regular tool calls alongside code execution. Code becomes a meta-tool. System prompt guides when to use each.

**State persistence decision:** Research shows Jupyter-style stateful kernels are powerful but complex. For Phase 2, keep it stateless (each subprocess is fresh). State persistence can be added later if needed — it's an additive change.

---

## Architecture

```
src/arcrun/
├── builtins/
│   ├── __init__.py          # exports make_execute_tool
│   └── execute.py           # ExecuteTool factory
├── strategies/
│   ├── __init__.py          # strategy registry + model-based selection
│   ├── react.py             # existing ReAct loop
│   └── code.py              # CodeExec strategy
└── (existing modules unchanged)
```

### Data Flow

```
run(model, tools, prompt, task, allowed_strategies=["react", "code"])
  │
  ├─ model.invoke() picks strategy → "code"
  │
  ├─ CodeExec prepends code-execution instructions to system prompt
  │
  └─ Loop runs (same as ReAct but model writes code)
       │
       ├─ Model returns tool_call: execute_python(code="...")
       │
       ├─ SandboxConfig.check("execute_python", {code: "..."}) → allow/deny
       │
       ├─ subprocess.run(["python", "-c", code], ...) → {stdout, stderr, exit_code, duration_ms}
       │
       └─ Result flows back into conversation
```

### ExecuteTool Factory

```python
def make_execute_tool(*, timeout_seconds=30, max_output_bytes=65536) -> Tool:
    """Factory for sandboxed Python execution tool."""
    ...
```

Caller creates and includes in their tool list:
```python
tools = [
    make_execute_tool(timeout_seconds=10),
    # ... other tools
]
```

### Strategy Selection Flow

```
allowed_strategies provided?
  │
  ├─ None → default to "react" (no LLM call)
  ├─ Single → use it directly (no LLM call)
  └─ Multiple → model.invoke() picks one (one LLM call)
```

---

## Open Questions for /build

Research resolved most open questions. Remaining:

1. ~~**Strategy descriptions**~~ **Resolved:** Name + description + "when to use" string. Shown to model during selection.
2. ~~**Selection prompt**~~ **Resolved:** Use tool calling with enum. Model sees strategy descriptions + tool list + task.
3. ~~**ExecuteTool working directory**~~ **Resolved:** Isolated `tempfile.TemporaryDirectory()`. Most secure, automatic cleanup.
4. ~~**Code persistence**~~ **Resolved:** Temp file (not `python -c`). No size limits, better for multiline, preserves for event traceability.
5. ~~**Environment variables**~~ **Resolved:** Hardcoded minimal env. Never inherit parent. Optional `extra_env` param on factory for caller additions.
6. **Strategy protocol** — Yes. `typing.Protocol` with `name: str`, `description: str`, `__call__` signature. Keeps react.py as plain function (wrap in class) or make both strategies classes. **Decision needed in /build.**
7. **NEW: _build_result duplication** — Both react.py and code.py will need `_build_result()`. Extract to shared module? Or keep in each strategy? **Decision needed in /build.**
8. **NEW: CodeExec loop structure** — Is code.py a copy of react.py with modified system prompt? Or does it call react_loop with augmented messages? **Decision needed in /build.**

---

## Approaches Considered

### Approach A: Full Sandbox in ExecuteTool (Rejected)

RestrictedPython or import blocking baked into ExecuteTool. Rejected because it couples security policy to the tool instead of the sandbox system. Violates the existing pattern where SandboxConfig owns all permission decisions.

### Approach B: Per-Turn Strategy Switching (Rejected)

Model switches between ReAct and CodeExec between turns. Rejected because state migration between strategies is complex, and the benefit is marginal. Model can always use ReAct with ExecuteTool in the tool list — it just won't get the CodeExec system prompt encouragement.

### Approach C: Caller-Only Strategy Selection (Not chosen)

`run(strategy="code")` — no model involvement. Not chosen because it reduces autonomy. The model knows the task better than the caller in most cases.

---

## Research Synthesis: Key Insights Across All Streams

### What Production Systems Actually Do

| System | Isolation | State | Strategy Selection |
|--------|-----------|-------|--------------------|
| OpenAI Code Interpreter | Firecracker microVM | Stateful (Jupyter kernel) | Implicit (always code) |
| E2B | Firecracker microVM | Stateful (Jupyter kernel) | Caller decides |
| Open Interpreter | Host process (unsafe) | Stateful (IPython) | Always code |
| AutoGen | Docker or local | Configurable (stateful/stateless) | Agent conversation |
| CodeAct | Docker container | Stateful (IPython) | Always code |
| OpenEdX CodeJail | AppArmor profiles | Stateless | N/A (grading) |

### arcrun's Position

arcrun is unique: it's a **strategy-agnostic runtime**, not a code execution framework. The CodeExec strategy is one option alongside ReAct (and future Recursive, RLM). This means:

1. **ExecuteTool stays simple** — it's just a tool, not a platform. No Jupyter kernels, no package management, no state persistence. Those are caller/agent concerns.
2. **CodeExec strategy stays thin** — it's ReAct with an augmented system prompt. Same loop, same executor, same events.
3. **Strategy selection is the novel contribution** — most frameworks don't have model-based strategy routing. arcrun can pick the right approach per-task.

### Security Posture Summary

Phase 2 adds the highest-risk component (arbitrary code execution) with the minimum viable security boundary (subprocess + SandboxConfig). This is explicitly a **stepping stone** to Phase 4's container isolation.

**What callers MUST understand:** ExecuteTool without a sandbox check runs arbitrary Python with the parent process's permissions. The security model is: arcrun provides the gate (`SandboxConfig.check`), caller provides the policy. Documentation must be loud about this.

### Resolved Design Questions from Research

| Question | Answer | Source |
|----------|--------|--------|
| Temp file vs `python -c`? | Temp file | Subprocess research: no size limits, secure creation, traceability |
| Inherit env? | No — hardcoded minimal | Subprocess research: LD_PRELOAD/PYTHONPATH attacks |
| Working directory? | `tempfile.TemporaryDirectory()` | Subprocess research: isolation + automatic cleanup |
| Timeout strategy? | SIGTERM → 5s grace → SIGKILL | Subprocess research: two-phase standard |
| Kill child processes? | `start_new_session=True` + `os.killpg()` | Subprocess research: process group cleanup |
| Selection mechanism? | Tool calling with enum | Strategy research: structured output, guaranteed valid |
| Model see tools? | Yes | Strategy research: improves selection accuracy |
| Fallback on bad selection? | Default to "react" | Strategy research: safe, general, no retry |
| Code script size? | Small (20-50 lines) | CodeExec research: iterative refinement > monolithic |
| State persistence? | Stateless (each exec fresh) | CodeExec research: simpler, add state later if needed |
| Code + tools hybrid? | Yes — both allowed | CodeExec research: code as meta-tool alongside regular tools |

---

## Sources

### Subprocess Sandboxing
- [Running Untrusted Python Code — Andrew Healey](https://healeycodes.com/running-untrusted-python-code)
- [OpenEdX CodeJail](https://github.com/openedx/codejail) — AppArmor-based sandboxing
- [E2B Code Interpreter](https://github.com/e2b-dev/code-interpreter) — Firecracker microVMs
- [Python Sandbox Escape Techniques](https://book.hacktricks.xyz/generic-methodologies-and-resources/python/bypass-python-sandboxes)

### Strategy Selection
- [Semantic Router — Aurelio AI](https://www.aurelio.ai/semantic-router) — 50x faster than LLM routing
- [Structured Outputs — OpenAI](https://platform.openai.com/docs/guides/structured-outputs)
- [Few-shot prompting for tool calling — LangChain](https://blog.langchain.com/few-shot-prompting-to-improve-tool-calling-performance/)
- [AI Agent Orchestration Patterns — Microsoft](https://learn.microsoft.com/en-us/azure/architecture/ai-ml/guide/ai-agent-design-patterns)

### CodeExec Strategy
- [CodeAct: Executable Code Actions (ICML 2024)](https://arxiv.org/abs/2402.01030) — 20% higher success rate
- [Building Agents That Use Code — Hugging Face](https://huggingface.co/learn/agents-course/unit2/smolagents/code_agents)
- [AutoGen Jupyter Code Executor](https://microsoft.github.io/autogen/0.2/docs/topics/code-execution/jupyter-code-executor/)
- [Docker Sandboxes for Coding Agents](https://www.docker.com/blog/docker-sandboxes-run-claude-code-and-other-coding-agents-unsupervised-but-safely/)
