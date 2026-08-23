## Core Rules

### Non-Negotiables

#### 1. Don't mix concerns

| Concern | Package |
|---------|---------|
| LLM calls | `arcllm` |
| Loop execution | `arcrun` |
| Agent (tools, skills, extensions, memory, …) | `arcagent` |

- `arcagent` must not own LLM-call or loop logic.
- `arcrun` must not own agent or LLM-provider logic.
- Concern purity is what keeps standalone packages, turnkey composition, and federal hardening possible at once.

```text
arcllm          standalone model adapter/router; knows nothing above it
   ^
arcrun          knows arcllm, owns the loop, does not know arcagent
   ^
arcagent        uses the arcrun facade only — never `import arcllm`
   ^        ^
arcgateway   arcui
```

#### Fleet composition is outer layer

The alpha fleet direction is **`arcteam → arcagent`** and **`arcteam → arcmemory`**.
ArcTeam composes independently runnable ArcAgent instances. Neither `arcagent` nor `arcmemory` may import `arcteam`.

#### 3. One resolver per Arc-home path

`~/.arc` is the **install** and `~/arc` is the **operator's** — the fleet, plus the source tarball beside it. Nothing is ever executed from `~/arc`.

- Resolve **per call**, never at import.
- Enforced by `tests/architecture/test_arc_home_single_resolver.py`.

#### 4. Agent state stays in workspace (ADR-029)

An agent's own state — memory, sessions, `context.md`, identity, the audit chain — is written with **direct filesystem I/O to the agent's workspace** (its home). It must **never** be saved by calling the LLM-facing tools (`write` / `bash` / `edit`).

**Why:** tools can open any project directory, but the agent's *brain must stay home*.

| Writing… | How |
|----------|-----|
| **Project** files | Via the tools — correct |
| **Agent** state | Workspace path + direct I/O only |

#### 5. No legacy shims

This codebase is local-only. **Never** add migration helpers, deprecation shims, or vestigial methods. Change a behavior → **delete the old code in the same edit.**

#### 6. Leave it correct

If `ruff check`, `mypy`, or any quality gate surfaces an error during your work, **fix it now** — regardless of who introduced it.

---

### Development Process

1. **Test first** — failing test before implementation.
2. **Read before writing** — understand existing code before modifying.
3. **Verify before claiming** — fresh test output, not assumptions.
4. **Root cause, not band-aids** — if a fix feels like a workaround, it is.
5. **Two strikes** — after 2 failed fix attempts, question the architecture.

### Done means

- Unit + integration tests pass
- `mypy --strict` clean
- `ruff check` clean
- Audit trail emitted for all new operations
- No hardcoded secrets / plaintext credentials
- Docstrings on public API

### We don't

- Monkey-patch
- `# type: ignore` without an inline why
- Bare `except:`
- Mutable default arguments
- Global state outside config
- `print` (use structured logging)
- Trade security for convenience

---

### Security Checklist

1. **Can this be injected?** — Validate inputs. Sanitize outputs.
2. **Can this be abused?** — Least privilege. Explicit allowlists.
3. **Can this leak?** — No secrets in prompts, logs, or errors.
4. **Can this cascade?** — Isolate failure domains. Circuit breakers.
5. **Can this be audited?** — Every action is an event. Every event is logged.