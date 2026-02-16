# arcrun — Roadmap Context

## Current Phase: Phase 1 — Core Loop + ReAct

Building the foundation: `await run(model, tools, prompt, task)` works end-to-end with events and sandbox.

## Phase Overview

| Phase | Name | Goal | Line Budget | Status |
|-------|------|------|-------------|--------|
| 1 | Core Loop + ReAct | `run()` works end-to-end | ~500 lines | **Active** |
| 2 | CodeExec | Model writes + executes Python | ~650 lines | Planned |
| 3 | Recursive | Task decomposition via spawn | ~800 lines | Planned |
| 4 | Hardening | Container sandbox, event integrity, NIST docs | ~900 lines | Planned |
| 5 | RLM Integration | Recursive LM for long context | ~1,000 lines | Research |

## Phase 1: Core Loop + ReAct

### Steps

| Step | Component | Decision Points | Status |
|------|-----------|-----------------|--------|
| 1.1 | Package skeleton | None — structure is locked | Pending |
| 1.2 | Types (Tool, LoopResult, SandboxConfig) | Tool.execute: async only vs both sync/async | Pending |
| 1.3 | Event Bus | Event data: dict vs typed dataclass per event | Pending |
| 1.4 | Sandbox | Bash command analysis depth | Pending |
| 1.5 | ReAct Strategy | Tool result message format (confirmed by arcllm types) | Pending |
| 1.6 | run() — Wire It Together | None — pure orchestration | Pending |

### Phase 1 Gate

- [ ] `await run()` accepts arcllm model + tools + prompt + task
- [ ] ReAct loop calls `await model.invoke(messages, tools=tools)` correctly
- [ ] Tool results flow back into messages for next turn
- [ ] Every action emits an event
- [ ] Sandbox denials work and emit events
- [ ] LoopResult has content, turns, tool_calls_made, tokens_used, events
- [ ] Under 500 lines

## Phase 2: CodeExec

- ExecuteTool: sandboxed Python subprocess
- CodeExec strategy: augmented system prompt encouraging code-writing
- Strategy selection: model picks from allowed list

### Phase 2 Gate

- [ ] ExecuteTool runs Python in subprocess
- [ ] CodeExec strategy works
- [ ] Strategy selection when multiple allowed
- [ ] Under 650 lines

## Phase 3: Recursive

- SpawnTool: new `run()` call with isolated context
- Spawn budgets: depth + total limits, cost ceiling
- Recursive strategy: augmented prompt encouraging decomposition

### Phase 3 Gate

- [ ] SpawnTool creates isolated sub-loop
- [ ] Parent gets compact result only
- [ ] Depth + total limits enforced
- [ ] Sandbox inherited, can't expand
- [ ] Under 800 lines

## Phase 4: Hardening

- Container sandbox option (Docker/podman)
- Event integrity (checksums/signatures)
- Adversarial sandbox testing
- Concurrent spawn performance
- NIST 800-53 event mapping docs

### Phase 4 Gate

- [ ] Container sandbox available as option
- [ ] Event checksums prevent tampering
- [ ] Adversarial tests pass (prompt injection, path traversal)
- [ ] Concurrent spawns don't deadlock
- [ ] NIST control mapping documented
- [ ] Under 900 lines

## Phase 5: RLM Integration (Research Phase)

Recursive Language Models enable near-infinite context by storing data as variables in a REPL environment instead of embedding in prompts.

### Key Concepts from Research

| Concept | Description |
|---------|-------------|
| Context as variables | Large documents stored as Python variables, not in prompts |
| REPL execution | Model writes code in a sandboxed REPL to explore context |
| Recursive self-calls | Model can call itself on context subsets |
| Dual-model strategy | Expensive model for root, cheaper for recursive calls |
| `FINAL(answer)` | Explicit termination signal from the model |

### Integration Approach

RLM fits as a fourth execution strategy alongside ReAct, CodeExec, and Recursive:

```
strategies/
├── react.py       # Tool-calling loop
├── code.py        # Code execution loop
├── recursive.py   # Task decomposition via spawn
└── rlm.py         # REPL-based recursive context processing
```

The RLM strategy would:
1. Store the task context as REPL variables (not in the prompt)
2. Give the model a restricted Python REPL environment
3. Allow the model to programmatically call itself on context subsets
4. Use `FINAL(answer)` as the termination signal
5. Support dual-model: root model + recursive model (both via arcllm)

### References

- [alexzhang13/rlm](https://github.com/alexzhang13/rlm) — Original RLM implementation
- [ysz/recursive-llm](https://github.com/ysz/recursive-llm) — Async recursive LLM with parallel recursion

### Phase 5 Gate

- [ ] RLM strategy processes 100k+ token contexts
- [ ] Recursive self-calls work with spawn budget
- [ ] Dual-model support (root + recursive via arcllm)
- [ ] REPL sandbox secure (RestrictedPython or equivalent)
- [ ] Under 1,000 lines total

## Execution Framework

### Build Approach

- One phase at a time. Phase gate must pass before starting next phase.
- Every decision pushed to user, logged in `.claude/decision-log.md`
- TDD: test before implementation
- Line count tracked per phase

### Decision Governance

All architectural decisions follow this flow:

```
Identify Decision → Present Options + Tradeoffs → User Decides → Log Decision → Implement
```

Decisions logged in `.claude/decision-log.md` with:
- Context (why needed)
- Options considered
- Choice made
- Reasoning
- Available to all future sessions and builders

### Quality Gates (Every Phase)

1. All tests pass
2. Line count within budget
3. Every action emits events
4. Sandbox checks enforced
5. No arcllm boundary violations (arcrun never calls load_model())
