# ArcAgent Build Standards

> Build like a top 1% developer. No shortcuts. Root causes, not workarounds.

---

## Build Principles

### 1. Simplicity

The core must be simple: easy to read, hard to break, robust, no confusion.

- Favor flat, explicit code over clever abstractions
- Core stays under 3,000 LOC. Period.
- Complexity lives in extensions, plugins, and modules -- never in the nucleus
- If you need a comment to explain control flow, the code is too complex. Refactor.
- No nested logic deeper than 2 levels. Extract to named methods.
- One class, one responsibility. One method, one job.

### 2. Security

Federal-first. This runs on DOE machines, in labs, in SCIFs.

- Secure by default, not by configuration
- Zero-trust everything: identity, comms, data, modules
- Full observability: OpenTelemetry traces, metrics, structured logs on every action
- Audit trail on every operation. Every tool call, every LLM request, every state change
- Tamper-evident logging with classification awareness
- Credentials never touch the filesystem. Vault-backed, short-lived tokens only.
- Break the Lethal Trifecta: private data + external comms + untrusted input never coexist without human approval
- All modules signed and verified before loading
- mTLS on all internal communications

### 3. Scalability

Built for 1,000s of agents running concurrently.

- Shared-nothing per agent. Coordinate via message bus (NATS).
- Async-first. Use `asyncio` and `uvloop` everywhere.
- Fail gracefully: circuit breakers, exponential backoff, failover chains
- Cold start under 500ms. Memory per agent under 50MB baseline.
- Design for horizontal scale. No singleton bottlenecks.
- Connection pooling, resource limits, and timeouts on everything external

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
        config.py       # YAML config, validation
        telemetry.py    # OpenTelemetry, audit events
        loop.py         # Agent loop (from ArcRun)
        context.py      # Context management, compaction
        tools.py        # Tool registry
        bus.py          # Module Bus (event-driven extensions)
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
| Core LOC | < 3,000 |

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
