# Arc Build Standards

> Build like a top 1% developer. No shortcuts. Root causes, not workarounds.

---

## Non-Negotiables

### 1. Don't mix concerns

| Concern | Package |
|---------|---------|
| LLM calls | `arcllm` |
| Loop execution | `arcrun` |
| Agent (tools, skills, extensions, memory, …) | `arcagent` |

- `arcagent` must not own LLM-call or loop logic.
- `arcrun` must not own agent or LLM-provider logic.
- Concern purity is what keeps standalone packages, turnkey composition, and federal hardening possible at once. Mixing layers collapses all three.

```text
arcllm          standalone model adapter/router; knows nothing above it
   ^
arcrun          knows arcllm, owns the loop, does not know arcagent
   ^
arcagent        uses the arcrun facade only — never `import arcllm`
   ^        ^
arcgateway   arcui
```

- `arcgateway` funnels messaging into `arcagent`. `arcui` observes and operates it.
- `arcagent` knows about neither, and must run headless without them.
- Modules and extensions plug into `arcagent` through explicit typed contracts, never by reversing these arrows.

The durable execution record for this boundary is [`ARCAGENT_REFACTOR_PLAN.md`](ARCAGENT_REFACTOR_PLAN.md).

### 2. Components

For all packages
Module - An Arc plugin that adds a feature or capability that comes with arc and can be installed directly.
Extension - Arc feature or capability that is created by external parties and can interact with Arc. It may include scripts to install, add files, etc

All of these are
- completely removeable from the directory with no loss of function to the package (except that specific capability). So fully optional.
- the importing package has no need or knowledge off it other than the import.
- optional installs at the cli level
- optional imports, imported separately and specifically


### 3. One resolver per Arc-home path

`~/.arc` is the **install** and `~/arc` is the **operator's** — the fleet, plus
the source tarball beside it. Nothing is ever executed from `~/arc`.

`~/.arc` is split by lifecycle: `runtime/` is **replaced wholesale** on update,
`config/` is **preserved**, and `state/` (operator key, identity, trust store,
arcstore, NATS, bundles) is **never touched**. Runtimes install side by side
under `runtime/<version>/` behind a real `current` symlink, so an update is an
atomic flip and a rollback is flipping it back. The fleet (`arc_team()`) is
outside all of it, so dropping a fresh `~/.arc` in — or deleting it — costs a
reinstall and nothing else.

Every path under it comes from a named accessor in `arctrust.paths` —
`arc_config()`, `arc_state()`, `trust_dir()`, `config_file("arcagent.toml")`,
`module_root()`, … Each takes an optional explicit base for `--arc-dir`.

- **Never** compose your own (`arc_home() / "operator"`) and never re-read
  `ARC_CONFIG_DIR`. A split resolver is how one surface came to read a different
  directory than another, and how a test wrote into a developer's real `~/.arc`.
- Resolve **per call**, never at import — `ARC_CONFIG_DIR` is routinely exported
  after a module loads.
- Enforced by `tests/architecture/test_arc_home_single_resolver.py`.
- Nothing durable lives under `~/.arc`'s runtime, and nothing executable lives
  under `~/arc`. `team/` is gitignored because the fleet sits beside the source.

### 4. Agent state stays in the workspace — never via LLM file tools (ADR-029)

An agent's own state — memory, sessions, `context.md`, identity, the audit chain — is written with **direct filesystem I/O to the agent's workspace** (its home). It must **never** be saved by calling the LLM-facing tools (`write` / `bash` / `edit`).

**Why:** tools can open any project directory (their cwd becomes your coding cwd), but the agent's *brain must stay home*. Direct workspace I/O means moving tools into a project never drags memory into that repo or scatters one agent's brain across every folder opened.

| Writing… | How |
|----------|-----|
| **Project** files | Via the tools — correct; that's the point |
| **Agent** state | Workspace path + direct I/O only |

Breaking this re-couples "where the agent works" to "where the agent lives" and defeats the coding-agent model.

### 5. No legacy / backward-compat shims

This codebase is local-only and not deployed. **Never** add migration helpers, deprecation shims, vestigial methods, or "kept for compatibility" code.

- Change a behavior → **delete the old code in the same edit.** No commented-out blocks, no `_DELETE_ME_LATER`, no vestigial stubs.
- **One line beats five.** Don't replace a one-line fix with a multi-method "resolver + helper + warner." Smallest correct change wins.
- No comments explaining what changed or why this is "the new way." Code is current reality; commit messages hold history.

### 6. Leave it correct — no skipping pre-existing errors

- If `ruff check`, `mypy`, or any quality gate surfaces an error during your work, **fix it now** — regardless of who introduced it or when.
- Never report "pre-existing — not my problem." The repo is left clean every session, commit, and PR. Inherited debt is paid when seen.
- Applies to lint, types, dead code, broken tests, missing docstrings, ambiguous Unicode, mutable defaults — everything the tools flag.
- If a fix is genuinely out of scope and you cannot land it, raise it to the user and ask before deferring. **Default is fix.**

---

## Build Principles

### 1. Simplicity

The core must be easy to read, hard to break, robust, and unambiguous.

- Prefer flat, explicit code over clever abstractions.
- Core stays under **5,000 LOC** (ADR-004).
- Complexity lives in extensions, plugins, and modules — never in the nucleus.
- If you need a comment to explain control flow, refactor.
- Nesting deeper than 2 levels → extract a named method.
- One class, one responsibility. One method, one job.

### 2. Security

Federal-first. This runs on DOE machines, in labs, in SCIFs.

#### Four Pillars are universal — not federal-only (ADR-019)

Every deployment, at every tier (personal / enterprise / federal), enforces:

1. **Identity** — every entity has a DID. `ArcAgent.__init__` requires it. Every tool dispatch carries `caller_did`. Primitives live in `arctrust`.
2. **Sign** — every loaded artifact (skill, extension, backend, pairing) is verified before use. No `UnsafeNoOp`, no `skip_sandbox`, no `require_manifest = federal-only`. `arctrust.keypair` + Sigstore + Rekor.
3. **Authorize** — `arctrust.policy.PolicyPipeline` on every tool call. First-DENY-wins. Fail-closed on exceptions.
4. **Audit** — `arctrust.audit.emit(AuditEvent, sink)` on every operation. Single emission point; sinks fan out: `JsonlSink` (compliance), `SignedChainSink` (tamper-evident chain), `arcui.bridge.UIBridgeSink` (live observability).

**Tier is stringency metadata, not a gate.** Federal: FIPS-validated crypto, signed allowlists, hard `max_turns` cap, all 5 policy layers. Personal: self-signed bundles (audit warn), Global-only policy layer, dynamic tool creation. **Every tier still identifies, verifies, authorizes, and audits.**

#### Other invariants

- Secure by default, not by configuration.
- Zero-trust everything: identity, comms, data, modules.
- Full observability: OpenTelemetry traces, metrics, structured logs on every action.
- Tamper-evident logging with classification awareness.
- Credentials never touch the filesystem — vault-backed, short-lived tokens only.
- **Lethal Trifecta:** private data + external comms + untrusted input never coexist without human approval.
- mTLS on all internal communications.

### 3. Scalability

Built for thousands of agents running concurrently.

- Shared-nothing per agent; coordinate via message bus (NATS).
- Async-first (`asyncio` + `uvloop`).
- Fail gracefully: circuit breakers, exponential backoff, failover chains.
- Cold start < 500ms; baseline memory < 50MB per agent.
- Horizontal scale; no singleton bottlenecks.
- Connection pooling, resource limits, and timeouts on everything external.

### 4. Composability

> **Standalone primitives. Turnkey whole. Federal-securable by construction.**

No line of code may sacrifice one of these audiences for another:

1. **Developer who wants one layer** — `pip install arcllm` (or `arcrun`) alone, with its own contract, without pulling higher layers.
2. **Non-technical user who wants everything** — layers compose into a turnkey stack that works with **zero configuration**. Ease comes from unbreakable defaults, never from a required setup step.
3. **Federal operator who hardens later** — personal → federal is a *stringency dial*, not a rewrite. Pillars already wired (see §2).
4. **Maintainable** - we can isolate and completely rewrite a module and as long as input and output format is the same, it will still work (no crossed concerns to worry about)

These stay compatible only when seams are clean. Intertwined concerns make all four impossible.

**How we keep all four open**

| Rule | Meaning |
|------|---------|
| **Dependencies point one way, never up** | `arcrun` → `arcllm`; `arcagent` → `arcrun` **only**; `arcgateway`/`arcui` → `arcagent`; `arctrust` is a leaf. A lower layer never imports a higher one, and no layer reaches *past* its neighbour: `arcagent` consumes ArcLLM through the ArcRun facade, never `import arcllm`. Violate once and standalone dies. |
| **One root import, qualified names** | A cross-package consumer writes exactly `import arcllm`, `import arcrun`, or `import arcagent`, then reaches public names off that root (`arcrun.run_stream`, `arcagent.KeyStore`). Deep imports are reserved for genuinely separate extras/extensions with an intentionally public submodule API. A package's own implementation may still use its internal modules. Enforced by `packages/arcagent/tests/architecture/test_dependency_boundaries.py` and the arcui seam guard in `packages/arcgateway/tests/architecture/test_imports.py`. |
| **One contract per seam; default unbreakable** | Typed seam; base impl correct with zero config; native overrides opt-in. *Example (SPEC-059):* `StreamEvent` lives in `arcllm`; base `invoke_stream` yields a single-event fallback so any provider works; `arcrun` consumes that contract and knows nothing of provider wires. |
| **Security seams from day one** | Identity (`caller_did`), Sign, Authorize, Audit at every seam at every tier — personal runs them at low stringency. **Never ship a path that would need pillars retrofitted for federal.** |
| **Concern purity enables all four** | See Non-Negotiable §1. LLM-in-loop or loop-in-agent collapses the audience story into one tangled product. |

---

## Code Standards

### Readability

- Clean, readable code is non-negotiable.
- No complex inner loops with buried logic — break them out.
- Methods short enough to read without scrolling.
- Names communicate intent: `validate_module_signature`, not `check`.
- Comment the **why**, not the **what**. Comment at module, class, and non-obvious method level.

### Abstractions

- DRY: extract shared patterns into base classes and utilities.
- Don't abstract prematurely — **three instances** of a pattern before extracting.
- Abstractions must reduce cognitive load, not add it.
- Every abstraction needs a clear interface (`Protocol` or ABC).

### Maintainability

- Modular: a change in one component should not ripple across the codebase.
- Strong typing everywhere — `mypy --strict` must pass.
- Pydantic models for all data boundaries (config, messages, events).
- Depend on protocols, not concrete classes.
- Feature toggles via config, not code branches.

### Project structure (`arcagent`)

```
arcagent/
  core/                         # Nucleus (<3,500 LOC)
    identity.py                 # DID, keypairs, auth
    config.py                   # TOML config, Pydantic validation
    telemetry.py                # OpenTelemetry, audit events
    agent.py                    # Orchestrator (wires components, invokes ArcRun)
    session_internal/context.py # ContextManager
    session_internal/manager.py # Session + compaction (SessionManager)
    tool_registry.py            # Tool registry, 4 transports
    module_bus.py               # Module Bus (event-driven extensions)
  modules/                      # Official modules (independent)
  adapters/                     # External system adapters
  utils/                        # Shared utilities
tests/
  unit/                         # 60%
  integration/                  # 20%
  e2e/                          # 10%
  security/                     # 5%
  performance/                  # 5%
```

---

## Development Rules

### Process

1. **Test first** — failing test before implementation.
2. **Read before writing** — understand existing code before modifying.
3. **Verify before claiming** — fresh test output, not assumptions.
4. **Root cause, not band-aids** — if a fix feels like a workaround, it is.
5. **two strikes** — after 2 failed fix attempts, question the architecture.

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

## Stack & Quality Gates

### Foundation packages

| Package | Purpose |
|---------|---------|
| `arcllm` (`packages/arcllm`) | Provider-agnostic LLM calls |
| `arcrun` (`packages/arcrun`) | Runtime agentic loop |
| `arctrust` | Identity, sign, authorize, audit (leaf) |

### Key libraries

| Library | Purpose |
|---------|---------|
| Pydantic 2.x | Data validation, config schemas |
| PyNaCl | Ed25519 cryptography |
| OpenTelemetry SDK | Traces, metrics, audit |
| NATS.py | Message bus |
| httpx | Async HTTP |
| uvloop | High-performance event loop |

### Commands

```bash
ruff check .                    # Lint
ruff format .                   # Format
mypy arcagent/ --strict         # Type check
pytest --cov=arcagent           # Test + coverage
pip-audit                       # Dependency audit
```

### Gates

| Gate | Threshold |
|------|-----------|
| Line coverage | ≥ 80% |
| Branch coverage | ≥ 75% |
| Core component coverage | ≥ 90% |
| Cyclomatic complexity | ≤ 10 per function |
| Ruff errors | 0 |
| mypy errors | 0 |
| Critical/high vulnerabilities | 0 |
| Core LOC | < 5,000 |

---

## Threat Surface

These are the attack vectors adversaries use against agents in federal environments — design every component against them.

### OWASP Top 10 for LLM Applications (2025)

| Code | Threat | Our Mitigation |
|------|--------|----------------|
| LLM01 | **Prompt Injection** | Input validation, system-prompt isolation, instruction hierarchy. Never trust user-adjacent content as instructions. |
| LLM02 | **Sensitive Information Disclosure** | Output filtering, classification-aware responses, PII/CUI detection before data leaves the agent. |
| LLM03 | **Supply Chain** | Signed modules, SBOM, `pip-audit`, provenance verification on external components. |
| LLM04 | **Data Poisoning** | Validate training/fine-tuning integrity. Checksums on ingested datasets. Isolate data sources. |
| LLM05 | **Improper Output Handling** | Sanitize/validate all LLM outputs before tools, APIs, DBs, or downstream systems. Never execute raw LLM output. |
| LLM06 | **Excessive Agency** | Least-privilege tools. Explicit allowlists. Human-in-the-loop for destructive/irreversible actions. |
| LLM07 | **System Prompt Leakage** | No secrets in system prompts. Treat prompts as exfiltrable. Separate config from instructions. |
| LLM08 | **Vector / Embedding Weaknesses** | Validate embedding sources; access-control vector stores; prevent cross-tenant leakage in shared indices. |
| LLM09 | **Misinformation** | Ground in verified data. Flag confidence. Never present LLM output as authoritative without verification. |
| LLM10 | **Unbounded Consumption** | Token budgets, rate limits, cost ceilings, timeouts, circuit breakers on runaway loops. |

### OWASP Top 10 for Agentic Applications (2026)

| Code | Threat | Our Mitigation |
|------|--------|----------------|
| ASI01 | **Agent Goal Hijack** | Immutable goals in `identity.md` (read-only to agent). Policy boundaries. Kill switches. |
| ASI02 | **Tool Misuse & Exploitation** | Tool allow/deny lists. Parameter validation on every call. Audit all invocations. |
| ASI03 | **Identity & Privilege Abuse** | Per-agent DID. Scoped namespace permissions (`domain:path:permission`). No shared credentials. No privilege inheritance without explicit grant. |
| ASI04 | **Agentic Supply Chain** | Signed runtime-loaded tools/modules. Pre-load vuln scanning. Sandboxed third-party extensions. |
| ASI05 | **Unexpected Code Execution (RCE)** | Never execute agent-generated code without sandboxing. Firecracker microVM. No `eval()`; no dynamic imports from untrusted sources. |
| ASI06 | **Memory & Context Poisoning** | Validate memory writes. Integrity checks on `context.md` and workspace files. Detect anomalous mutations. |
| ASI07 | **Insecure Inter-Agent Communication** | mTLS on NATS. Ed25519 message signing. Replay protection (nonce + timestamp). No plaintext inter-agent traffic. |
| ASI08 | **Cascading Failures** | Circuit breakers. Blast-radius containment. Shared-nothing prevents cascade propagation. |
| ASI09 | **Human-Agent Trust Exploitation** | Agents never impersonate humans. Label AI-generated content. Approval gates on consequential actions. |
| ASI10 | **Rogue Agents** | Telemetry monitoring. Policy-violation alerts. Identity-service revocation. Anomaly detection. |

### Checklist when writing code

1. **Can this be injected?** — Validate inputs. Sanitize outputs.
2. **Can this be abused?** — Least privilege. Explicit allowlists. No implicit trust.
3. **Can this leak?** — No secrets in prompts, logs, or errors. Classification-aware data flow.
4. **Can this cascade?** — Isolate failure domains. Circuit breakers. Timeouts.
5. **Can this be audited?** — Every action is an event. Every event is logged. Every log is searchable.

---

## Compliance

Must support authorization under:

- **FedRAMP** — Federal Risk and Authorization Management
- **NIST 800-53** — Security and Privacy Controls (IA, AU, AC families)
- **CMMC** — Cybersecurity Maturity Model Certification

Evaluate every architectural decision through these frameworks and the OWASP threat surfaces above.

---

## MCP Tools: code-review-graph

This project has a knowledge graph. **Always use code-review-graph MCP tools before Grep/Glob/Read** to explore the codebase. The graph is faster, cheaper, and gives structural context (callers, dependents, coverage) that file scanning cannot. Fall back to Grep/Glob/Read only when the graph doesn't cover what you need.

| Need | Prefer |
|------|--------|
| Exploring code | `semantic_search_nodes` or `query_graph` |
| Blast radius | `get_impact_radius` |
| Code review | `detect_changes` + `get_review_context` |
| Relationships | `query_graph` (`callers_of` / `callees_of` / `imports_of` / `tests_for`) |
| Architecture | `get_architecture_overview` + `list_communities` |
| Affected flows | `get_affected_flows` |
| Renames / dead code | `refactor_tool` |

**Workflow:** graph auto-updates on file changes → `detect_changes` for review → `get_affected_flows` for impact → `query_graph` `tests_for` for coverage.
