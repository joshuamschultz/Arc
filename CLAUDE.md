# ArcAgent Build Standards

> Build like a top 1% developer. No shortcuts. Root causes, not workarounds.

> Don't Mix concerns
- all llm calls are arcllm
- loop execution is arcrun
- agent with tools, skills, extentions, memory, etc is arcagent
Don't have agent do things related to llm calls, or loop.
Dont have arcrun do things that belong to agent or arcllm.

> **Agent state persists to the workspace — never through the LLM's file tools**
- An agent's own state — memory, sessions, `context.md`, identity, the audit chain — is written with **direct filesystem I/O to the agent's workspace** (its home). It must **never** be saved by calling the LLM-facing tools (`write`/`bash`/`edit`).
- **Why (plain terms):** an agent can be opened to work in *any* project directory — its file/exec tools' working dir becomes your cwd (the coding model) — but its *brain must stay home*. Because state is written straight to the workspace and not via the tools, moving the tools into your project **never drags the agent's memory into your repo**, and never scatters one agent's brain across every folder you opened. (ADR-029)
- Consequence for new code: a skill/capability that writes **project** files via the tools is correct (that's the point); one that saves **agent** state must use a workspace path via direct I/O. Breaking this invariant re-couples "where the agent works" to "where the agent lives" and defeats the whole coding-agent model.

> **Clean code, lean code — no legacy/backward-compat**
- This codebase is local-only and not deployed anywhere. **Never write migration helpers, deprecation shims, vestigial methods, or "kept for compatibility" code.**
- When changing a behavior, **delete the old code in the same edit.** No commented-out blocks, no `_DELETE_ME_LATER`, no "vestigial" stubs.
- **One line beats five.** Don't replace a one-line fix with a multi-method "resolver + helper + warner" abstraction. Smallest correct change wins.
- No comments explaining what was changed or why this is "the new way." Code reflects current reality; commit messages hold the history.

> **Leave it correct — no skipping pre-existing errors**
- If `ruff check`, `mypy`, or any quality gate surfaces an error during your work, **fix it now**. It does not matter who introduced it or when.
- Never report "pre-existing — not my problem." The repository is left clean every session, every commit, every PR. Inherited debt is paid off the moment it's seen.
- This applies to lint errors, type errors, dead code, broken tests, missing docstrings, ambiguous Unicode chars, mutable defaults — everything the linter flags.
- If a fix is genuinely out of scope for the current task and you cannot land it, raise it explicitly to the user and ask before deferring. Default is fix.

---

## Build Principles

### 1. Simplicity

The core must be simple: easy to read, hard to break, robust, no confusion.

- Favor flat, explicit code over clever abstractions
- Core stays under 3,500 LOC (see ADR-004 for budget increase rationale).
- Complexity lives in extensions, plugins, and modules -- never in the nucleus
- If you need a comment to explain control flow, the code is too complex. Refactor.
- No nested logic deeper than 2 levels. Extract to named methods.
- One class, one responsibility. One method, one job.

### 2. Security

Federal-first. This runs on DOE machines, in labs, in SCIFs.

#### The Four Pillars are universal — not federal-mode features (ADR-019)

Every Arc deployment, at every tier (personal / enterprise / federal), enforces:

1. **Identity** — every entity has a DID. `ArcAgent.__init__` requires it. Every tool dispatch carries `caller_did`. Identity primitives live in `arctrust`.
2. **Sign** — every loaded artifact (skill, extension, backend, pairing) is verified before use. No `UnsafeNoOp`, no `skip_sandbox`, no `require_manifest = federal-only`. `arctrust.keypair` + Sigstore + Rekor.
3. **Authorize** — `arctrust.policy.PolicyPipeline` evaluates every tool call. First-DENY-wins. Fail-closed on exceptions.
4. **Audit** — `arctrust.audit.emit(AuditEvent, sink)` on every operation. `JsonlSink` for compliance, `SignedChainSink` for tamper-evident chain, `arcui.bridge.UIBridgeSink` for live observability. Single emission point, sinks fan out.

Tier is **stringency metadata, not a gate**. Federal requires FIPS-validated crypto, signed allowlists, hard `max_turns` cap, all 5 policy layers. Personal allows self-signed bundles (with audit warn), Global-only policy layer, dynamic tool creation. **Every tier still verifies, authorizes, audits, and identifies.**

#### Other invariants

- Secure by default, not by configuration
- Zero-trust everything: identity, comms, data, modules
- Full observability: OpenTelemetry traces, metrics, structured logs on every action
- Tamper-evident logging with classification awareness
- Credentials never touch the filesystem. Vault-backed, short-lived tokens only.
- Break the Lethal Trifecta: private data + external comms + untrusted input never coexist without human approval
- mTLS on all internal communications

### 3. Scalability

Built for 1,000s of agents running concurrently.

- Shared-nothing per agent. Coordinate via message bus (NATS).
- Async-first. Use `asyncio` and `uvloop` everywhere.
- Fail gracefully: circuit breakers, exponential backoff, failover chains
- Cold start under 500ms. Memory per agent under 50MB baseline.
- Design for horizontal scale. No singleton bottlenecks.
- Connection pooling, resource limits, and timeouts on everything external

### 4. Composability

> **Standalone primitives. Turnkey whole. Federal-securable by construction.**

Arc must serve three audiences at once, and no line of code may sacrifice one for another:

1. **The developer who wants one layer.** Someone should be able to `pip install arcllm` and use it alone — a clean provider-agnostic LLM library — without pulling in arcrun or arcagent. Same for `arcrun` (the loop) without `arcagent`. Each package is an independently valuable, independently installable product with its own contract.
2. **The non-technical user who wants everything.** The layers compose into one turnkey stack that "just works" with zero configuration. Ease for this user comes from **unbreakable defaults**, never from a required setup step.
3. **The federal operator who needs it hardened later.** A deployment that starts personal-tier must be tightenable to federal **without re-architecture** — federal is a *stringency dial*, not a rewrite. See "The Four Pillars are universal" above.

These three are not in tension **if** the seams are designed right. They become impossible the moment code intertwines concerns.

**How we keep all three open:**

- **Dependencies point one way, never up.** `arcrun` → `arcllm`; `arcagent` → both; `arctrust` is a leaf that imports no sibling. A lower layer never imports a higher one — that is what makes it usable alone. Violate this once and the standalone story dies.
- **One contract per seam, and the default is unbreakable.** A layer exposes a single typed contract; the base implementation is correct with zero config; power users override it natively. *(Worked example — SPEC-059 streaming: `StreamEvent` lives in `arcllm` and is fully usable by an arcllm-only consumer; the base `invoke_stream` yields a single-event fallback so any provider works untouched; native overrides are opt-in. `arcrun` consumes that one contract and knows nothing of provider wires; `arcllm` knows nothing of the loop.)*
- **The security seams are present from day one, dormant until tightened.** Identity (`caller_did`), Sign, Authorize, Audit are wired at every seam at every tier — personal just runs them at low stringency. This is the load-bearing federal-preservation rule: because the audit/identity hook already exists on (e.g.) a transient injection or a tool dispatch, hardening to federal is config, not surgery. **Never ship a path that would need the pillars *retrofitted* to go federal** — that retrofit is the rewrite we are avoiding.
- **Concern purity is the enabler, not bureaucracy.** "Don't mix concerns" (top of this file) is what makes 1, 2, and 3 simultaneously true. LLM logic in the loop, or loop logic in the agent, collapses all three audiences into one tangled product.

**The tradeoffs, stated honestly:**

- A per-seam contract adds one layer of indirection over calling a provider SDK raw. **Accepted** — it is the price of standalone-usability + turnkey composition + federal-readiness, and it is small. It is *not* license to over-abstract: the three-instances rule (§Abstractions) still governs; add the seam when the boundary is real, not speculatively.
- An "unbreakable default" means writing a fallback even for providers that will always override it. **Accepted** — the default is what keeps the simple case simple and the turnkey user unconfigured.
- Keeping the pillars wired at personal tier costs a little code that a "personal-only" fork wouldn't need. **Accepted** — that cost *is* the federal option value; dropping it to save a few lines forecloses the future the whole project exists for.

---

## Code Standards

### Readability

- Clean, readable code is non-negotiable
- No complex inner loops with buried logic. Break it out.
- Methods should be short enough to read without scrolling
- Use descriptive names that communicate intent: `validate_module_signature`, not `check`
- Comment the WHY, not the WHAT. Code explains what; comments explain why.
- Comment completely at module, class, and non-obvious method level

### Abstractions

- DRY: Extract shared patterns into base classes and utilities
- But don't abstract prematurely. Three instances of a pattern before extracting.
- Abstractions should reduce cognitive load, not add it
- Every abstraction must have a clear interface (Protocol or ABC)

### Maintainability

- Modular architecture: changes to one component should not ripple across the codebase
- Strong typing everywhere. `mypy --strict` must pass.
- Pydantic models for all data boundaries (config, messages, events)
- Interfaces over implementations. Depend on protocols, not concrete classes.
- Feature toggles via config, not code branches

### Project Structure

```
arcagent/
    core/           # The nucleus (<3K LOC total)
        identity.py     # DID, keypairs, auth
        config.py       # TOML config, Pydantic validation
        telemetry.py    # OpenTelemetry, audit events
        agent.py        # Orchestrator (wires components, invokes ArcRun)
        session_internal/context.py   # Context management (ContextManager)
        session_internal/manager.py    # Session + compaction (SessionManager)
        tool_registry.py    # Tool registry, 4 transports
        module_bus.py       # Module Bus (event-driven extensions)
    modules/        # Official modules (each is independent)
    adapters/       # External system adapters
    utils/          # Shared utilities
tests/
    unit/           # 70% of tests
    integration/    # 20% of tests
    e2e/            # 10% of tests
    security/       # Security-specific tests
    performance/    # Benchmarks
```

---

## Development Rules

### Process

1. **Test first.** Write the failing test before the implementation.
2. **Read before writing.** Understand existing code before modifying.
3. **Verify before claiming.** Fresh test output, not assumptions.
4. **Root cause, not band-aids.** If a fix feels like a workaround, it is. Find the real problem.
5. **Three strikes rule.** After 3 failed fix attempts, question the architecture.

### What "Done" Means

- Tests pass (unit + integration)
- Types check (`mypy --strict`)
- Linter clean (`ruff check`)
- Audit trail emitted for all new operations
- No hardcoded secrets, no plaintext credentials
- Docstrings on public API

### What We Don't Do

- No monkey-patching
- No `# type: ignore` without a comment explaining why
- No bare `except:` blocks
- No mutable default arguments
- No global state outside of config
- No print statements (use structured logging)
- No shortcuts that trade security for convenience

---

## Dependencies

### Foundations (sibling projects)

| Project | Purpose | Location |
|---------|---------|----------|
| ArcLLM | Provider-agnostic LLM calls | `../arcllm/` |
| ArcRun | Runtime agentic loop | `../arcrun/` |

### Key Libraries

| Library | Purpose |
|---------|---------|
| Pydantic 2.x | Data validation, config schemas |
| PyNaCl | Ed25519 cryptography |
| OpenTelemetry SDK | Traces, metrics, audit |
| NATS.py | Message bus |
| httpx | Async HTTP |
| uvloop | High-performance event loop |

### Quality Tools

```bash
ruff check .                    # Lint
ruff format .                   # Format
mypy arcagent/ --strict         # Type check
pytest --cov=arcagent           # Test + coverage
pip-audit                       # Dependency audit
```

---

## Quality Gates

| Gate | Threshold |
|------|-----------|
| Line coverage | >= 80% |
| Branch coverage | >= 75% |
| Core component coverage | >= 90% |
| Cyclomatic complexity | <= 10 per function |
| Ruff errors | 0 |
| mypy errors | 0 |
| Critical/high vulnerabilities | 0 |
| Core LOC | < 3,500 |

---

## Threat Surface Awareness

Every component must be designed to protect against and mitigate these threat surfaces. These are not abstract risks -- they are the attack vectors adversaries will use against deployed agents in federal environments.

### OWASP Top 10 for LLM Applications (2025)

| Code | Threat | Our Mitigation |
|------|--------|----------------|
| LLM01 | **Prompt Injection** | Input validation, system prompt isolation, instruction hierarchy enforcement. Never trust user-adjacent content as instructions. |
| LLM02 | **Sensitive Information Disclosure** | Output filtering, classification-aware responses, PII/CUI detection before any data leaves the agent. |
| LLM03 | **Supply Chain** | Signed modules, SBOM generation, dependency auditing (`pip-audit`), provenance verification on all external components. |
| LLM04 | **Data Poisoning** | Validate training/fine-tuning data integrity. Checksums on all ingested datasets. Isolation between data sources. |
| LLM05 | **Improper Output Handling** | Sanitize and validate all LLM outputs before passing to tools, APIs, databases, or downstream systems. Never execute raw LLM output. |
| LLM06 | **Excessive Agency** | Least-privilege tool access. Explicit allowlists per agent. Human-in-the-loop gates for destructive or irreversible actions. |
| LLM07 | **System Prompt Leakage** | No secrets in system prompts. Treat prompts as potentially exfiltrable. Separate config from instructions. |
| LLM08 | **Vector and Embedding Weaknesses** | Validate embedding sources, access-control vector stores, prevent cross-tenant data leakage in shared indices. |
| LLM09 | **Misinformation** | Ground responses in verified data. Flag confidence levels. Never present LLM output as authoritative without verification. |
| LLM10 | **Unbounded Consumption** | Token budgets, request rate limits, cost ceilings, timeout enforcement on all LLM calls. Circuit breakers on runaway loops. |

### OWASP Top 10 for Agentic Applications (2026)

| Code | Threat | Our Mitigation |
|------|--------|----------------|
| ASI01 | **Agent Goal Hijack** | Immutable goal definitions in identity.md (read-only to agent). Policy engine enforces behavioral boundaries. Kill switches. |
| ASI02 | **Tool Misuse & Exploitation** | Tool-level allowlists/denylists. Parameter validation on every tool call. Audit logging of all tool invocations. |
| ASI03 | **Identity & Privilege Abuse** | Per-agent DID identity. Scoped namespace permissions (`domain:path:permission`). No shared credentials. No privilege inheritance without explicit grant. |
| ASI04 | **Agentic Supply Chain** | Runtime-loaded tools and modules must be signed. Pre-load vulnerability scanning. Sandboxed execution for third-party extensions. |
| ASI05 | **Unexpected Code Execution (RCE)** | Never execute agent-generated code without sandboxing. Firecracker microVM isolation. No `eval()`, no dynamic imports from untrusted sources. |
| ASI06 | **Memory & Context Poisoning** | Validate memory writes. Integrity checks on context.md and workspace files. Detect anomalous memory mutations. |
| ASI07 | **Insecure Inter-Agent Communication** | mTLS on all NATS channels. Message signing with Ed25519. Replay protection via nonce + timestamp. No plaintext inter-agent traffic. |
| ASI08 | **Cascading Failures** | Circuit breakers between agents. Blast radius containment via isolation boundaries. Shared-nothing architecture prevents cascade propagation. |
| ASI09 | **Human-Agent Trust Exploitation** | Agents never impersonate humans. Clear labeling of AI-generated content. Approval gates on consequential actions. |
| ASI10 | **Rogue Agents** | Behavioral monitoring via telemetry. Policy violations trigger alerts. Agent revocation via identity service. Anomaly detection on agent actions. |

### How This Applies to Development

When writing code, ask:

1. **Can this be injected?** -- Validate all inputs. Sanitize all outputs.
2. **Can this be abused?** -- Least privilege. Explicit allowlists. No implicit trust.
3. **Can this leak?** -- No secrets in prompts, logs, or error messages. Classification-aware data flow.
4. **Can this cascade?** -- Isolate failure domains. Circuit breakers. Timeouts.
5. **Can this be audited?** -- Every action is an event. Every event is logged. Every log is searchable.

---

## Compliance Context

This codebase must support authorization under:

- **FedRAMP** -- Federal Risk and Authorization Management
- **NIST 800-53** -- Security and Privacy Controls (IA, AU, AC families)
- **CMMC** -- Cybersecurity Maturity Model Certification

Every architectural decision should be evaluated through these compliance frameworks and the OWASP threat surfaces above.

<!-- code-review-graph MCP tools -->
## MCP Tools: code-review-graph

**IMPORTANT: This project has a knowledge graph. ALWAYS use the
code-review-graph MCP tools BEFORE using Grep/Glob/Read to explore
the codebase.** The graph is faster, cheaper (fewer tokens), and gives
you structural context (callers, dependents, test coverage) that file
scanning cannot.

### When to use graph tools FIRST

- **Exploring code**: `semantic_search_nodes` or `query_graph` instead of Grep
- **Understanding impact**: `get_impact_radius` instead of manually tracing imports
- **Code review**: `detect_changes` + `get_review_context` instead of reading entire files
- **Finding relationships**: `query_graph` with callers_of/callees_of/imports_of/tests_for
- **Architecture questions**: `get_architecture_overview` + `list_communities`

Fall back to Grep/Glob/Read **only** when the graph doesn't cover what you need.

### Key Tools

| Tool | Use when |
|------|----------|
| `detect_changes` | Reviewing code changes — gives risk-scored analysis |
| `get_review_context` | Need source snippets for review — token-efficient |
| `get_impact_radius` | Understanding blast radius of a change |
| `get_affected_flows` | Finding which execution paths are impacted |
| `query_graph` | Tracing callers, callees, imports, tests, dependencies |
| `semantic_search_nodes` | Finding functions/classes by name or keyword |
| `get_architecture_overview` | Understanding high-level codebase structure |
| `refactor_tool` | Planning renames, finding dead code |

### Workflow

1. The graph auto-updates on file changes (via hooks).
2. Use `detect_changes` for code review.
3. Use `get_affected_flows` to understand impact.
4. Use `query_graph` pattern="tests_for" to check coverage.
