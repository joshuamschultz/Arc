# Technical Context — Arc

> Stable technical context that informs all feature specifications.
> Feature-specific details go in `.claude/specs/{feature}/`.

## Validation Checklist

- [x] Technology stack documented
- [x] Project commands listed
- [x] Quality thresholds defined
- [x] Error handling pattern documented
- [x] Testing approach defined
- [x] Security requirements listed
- [x] No `[NEEDS CLARIFICATION]` markers

---

## Tech Stack

### Core Technologies

| Layer | Technology | Version | Notes |
|-------|------------|---------|-------|
| Language | Python | 3.12 (`.python-version`); minimum `>=3.11` per `pyproject.toml` | mypy `python_version = "3.11"` |
| Package mgmt | uv (Astral) | workspace mode | `[tool.uv.workspace] members = ["packages/*"]` |
| Build backend | Hatchling | per-package | Each package's own `pyproject.toml` |
| Async runtime | asyncio + uvloop (target) | stdlib | Event loop pinned for performance |
| Validation | Pydantic | 2.x | All data boundaries (config, messages, events, audit) |
| HTTP | httpx | 0.27+ | Provider transport — **no vendor SDKs** |
| Crypto | PyNaCl (libsodium) | 1.5+ | Ed25519 keypairs, signing, verification |
| Observability | OpenTelemetry SDK/API + OTLP exporters | 1.20+ | Spans + metrics + audit |
| Schema | jsonschema | latest | Tool argument validation |
| Config | PyYAML, pydantic-settings | latest | TOML primary, YAML for skill metadata |
| Scheduling | croniter, dateparser, cronsim | latest | Unified proactive scheduling engine |
| Messaging | python-telegram-bot, slack-bolt, NATS.py | latest | Gateway adapters |

### Development Tools

| Tool | Purpose | Config |
|------|---------|--------|
| `ruff` | Lint + format | `[tool.ruff]` in root `pyproject.toml`; line length 99; rules `E,W,F,I,N,UP,B,A,S,T20,RUF` |
| `mypy` | Strict type checking | `[tool.mypy] strict = true` |
| `pytest` | Test runner | `[tool.pytest.ini_options] asyncio_mode = "auto"` |
| `pytest-asyncio` | Async test support | bundled |
| `pytest-cov` | Coverage | enforced via `scripts/coverage_report.py` |
| `pytest-mock` | Mocking | per test as needed |
| `pip-audit` | Vulnerability audit | CI gate |

---

## Conventions

> Coding conventions every feature must follow, per `CLAUDE.md` and root `pyproject.toml`. Naming rules (files, classes, tests) live in `.claude/steering/structure.md`.

| ID | Constraint | Type | Rationale |
|----|------------|------|-----------|
| CON-1 | mypy `--strict` must pass | Technical | Pydantic + strict types prevent boundary bugs |
| CON-2 | All data boundaries are Pydantic models | Technical | Validation, schema generation, audit |
| CON-3 | All LLM calls go through `arcllm` (no direct SDK use) | Architectural | Auditability, no vendor lock-in |
| CON-4 | All tool dispatch goes through `ToolRegistry.dispatch` | Architectural | Single chokepoint for policy + audit |
| CON-5 | All audit emission goes through `arctrust.audit.emit` | Architectural | Single emission point; sinks fan out |
| CON-6 | `ArcAgent.__init__` requires DID | Security | Identity is universal, not federal-only |
| CON-7 | No vendor LLM SDKs | Security | Direct httpx for transport auditability |
| CON-8 | mTLS on all inter-agent NATS traffic | Security | ASI07 mitigation |
| CON-9 | No mutable default arguments | Code | Standard Python gotcha |
| CON-10 | No bare `except:` blocks | Code | Errors must be observable |
| CON-11 | No `print` statements (structured logging only) | Code | Audit trail integrity |
| CON-12 | No `# type: ignore` without inline justification | Code | Strict typing means strict justification |
| CON-13 | No legacy/back-compat code | Code | Local-only repo per CLAUDE.md — delete the old code in the same edit |

### Review

- All PRs must pass `make lint`, `make typecheck`, `make test`, and `make architecture-tests` before merge.
- Pre-existing lint/type/test errors surfaced during unrelated work are fixed in the same session, not deferred (per `CLAUDE.md`).

---

## Library Choices

> Standard, notable dependencies each package layer builds on — reuse rather than reinvent.

| Package | Notable Deps |
|---------|--------------|
| `arctrust` | PyNaCl, Pydantic — leaf, no Arc imports |
| `arcllm` | httpx, Pydantic, OpenTelemetry, `arctrust` |
| `arcrun` | jsonschema, `arcllm`, `arctrust` |
| `arcagent` | `arcrun`, `arcllm`, `arctrust`, PyYAML, croniter, dateparser |
| `arcskill` | `arctrust` (Sigstore + Rekor verification, AST scan) |
| `arcgateway` | `arcagent`, `arctrust`, telegram/slack adapters |
| `arcui` | `arctrust`, `arcagent`, WebSocket transport |
| `arccli` | `arcagent` — exposes the `arc` command |

---

## Build & CI

> Canonical commands. See `Makefile` and per-package `pyproject.toml`.

### Setup

```bash
# Install all packages in editable mode (canonical setup)
make install
```

### Testing

```bash
# Full test suite (excludes slow markers)
make test

# Per package
pytest packages/arcagent/tests/

# Single test
pytest packages/arcagent/tests/unit/test_agent.py::test_did_required

# Coverage report with thresholds (G1.7 gate)
make coverage

# Race / concurrency stress (100-run regression — G1.3 gate)
make race-stress
```

### Quality Checks

```bash
# Ruff lint across all packages
make lint

# mypy --strict
make typecheck

# Architecture regression tests (TX.1)
make architecture-tests

# LOC budget enforcement (G1.5, G1.6)
make loc-budgets

# All M1 acceptance gates at once
make m1-gates
```

### Agent Operations (CLI)

```bash
arc init                        # First-run setup wizard (tier selection)
arc agent create <name>         # Scaffold a new agent
arc agent build <name> --check  # Validate agent config (use --check; bare command is destructive)
arc agent chat <name>           # Interactive REPL
arc agent run <name> "task"     # One-shot
arc agent serve <name> --ui     # Daemon with arcui dashboard
arc team register               # Register agents for arcui visibility
```

Full CLI reference: `docs/cli.md`.

### CI Gates

#### Coverage

| Metric | Threshold | Enforcement |
|--------|-----------|-------------|
| Line coverage | ≥ 80% | `scripts/coverage_report.py` |
| Branch coverage | ≥ 75% | `scripts/coverage_report.py` |
| Core component coverage | ≥ 90% | `scripts/coverage_report.py` (per-package overrides) |

#### Code Quality

| Metric | Threshold | Enforcement |
|--------|-----------|-------------|
| Ruff errors | 0 | `make lint` |
| mypy errors | 0 | `make typecheck` (`--strict`) |
| Cyclomatic complexity | ≤ 10 per function | Code review (no automated rule yet) |
| Architecture regressions | 0 | `make architecture-tests` (TX.1) |
| Core LOC budget | < 3,500 (arcagent core) | `make loc-budgets` |

#### Security

| Metric | Threshold | Enforcement |
|--------|-----------|-------------|
| Critical vulnerabilities | 0 | `pip-audit` CI gate |
| High vulnerabilities | 0 | `pip-audit` CI gate |
| Hardcoded secrets | 0 | Code review + `.env` gitignored |
| Unsigned artifact loads | 0 (federal); warn (personal) | `arcskill` verification |

#### Performance Gates

| Metric | Threshold | Measurement |
|--------|-----------|-------------|
| Policy pipeline latency | p95 < 1ms | Benchmark (SPEC-017) |
| Agent cold start | < 500ms | Startup metric |
| Per-agent baseline memory | < 50MB | Process metric |
| Audit emission overhead | bounded; non-blocking | Async fan-out to sinks |

---

## Error Handling Pattern

> Arc is a library / framework, not an HTTP service. Errors surface as **typed exceptions** + **audit events**, not HTTP error envelopes.

### Exception Hierarchy

```python
ArcError                       # Base
├── ArcConfigError             # TOML / Pydantic validation failures
├── ArcIdentityError           # DID malformed, signature invalid
├── ArcPolicyDenied            # Policy pipeline rejection (with verdict + layer)
├── ArcSandboxViolation        # AST validator / restricted builtins / egress block
├── ArcAuditError              # Audit emission failure (rare; fail-loud)
├── ArcToolError               # Tool dispatch / argument validation
├── ArcLLMError                # Provider / transport / rate limit
└── ArcSpawnError              # Agent spawn / delegation failure
```

### Error Categories

| Category | Exception | Audit Event Type | User-Visible? |
|----------|-----------|------------------|----------------|
| Config | `ArcConfigError` | `config.invalid` | Yes — startup |
| Identity | `ArcIdentityError` | `identity.failure` | Yes — agent unusable |
| Policy denial | `ArcPolicyDenied` | `policy.denied` | Yes — tool call blocked |
| Sandbox | `ArcSandboxViolation` | `sandbox.violation` | Yes — agent code blocked |
| Tool | `ArcToolError` | `tool.error` | Yes — surfaced to agent |
| LLM | `ArcLLMError` | `llm.error` | Yes — surfaced to agent |

### Pattern

```python
try:
    result = await tool_registry.dispatch(name, args, ctx)
except ArcPolicyDenied as exc:
    # Policy pipeline already emitted policy.denied. Surface clean message to agent.
    raise
except ArcAuditError:
    # Audit failure is fail-loud. Do NOT swallow.
    raise
```

### Invariants

- **No bare `except:`.**
- **Audit emission never silently fails** — `ArcAuditError` propagates.
- **Policy denials are the same in personal and federal tiers** — only the policy *contents* differ.
- **All exception messages are safe to log** — no PII / CUI / secrets in `__str__`.

---

## Testing Approach

### Test Pyramid

```
         /\
        /  \  Security (adversarial: injection, sandbox bypass, signature forgery)
       /    \
      /------\  E2E (10%) — full agent runs, demo scenarios
     /        \
    /  Integ.  \  (20%) — cross-package: arcagent ↔ arcllm, policy ↔ tool dispatch
   /------------\
  /              \  Unit (70%) — per-package, fast, isolated
 /                \
```

### Test File Conventions

| Test Type | Location | Pattern |
|-----------|----------|---------|
| Unit | `packages/<pkg>/tests/unit/` | `test_<module>.py` |
| Integration | `packages/<pkg>/tests/integration/` | `test_<flow>.py` |
| Security | `packages/<pkg>/tests/security/` | `test_<attack>.py` |
| Performance | `packages/<pkg>/tests/performance/` | benchmark scripts |
| Architecture | `tests/architecture/` (root) | TX.1 regression guards |
| E2E / demo | `tests/e2e/` (root) | Demo scenario replays |

### Testing Libraries

| Purpose | Library |
|---------|---------|
| Test runner | pytest 8+ |
| Async tests | pytest-asyncio (`asyncio_mode = "auto"`) |
| Coverage | pytest-cov + custom thresholds via `scripts/coverage_report.py` |
| Mocking | pytest-mock (use sparingly — prefer real components) |
| HTTP mocking | `httpx.MockTransport` (provider tests) |

### Test Data Strategy

| Approach | Use Case |
|----------|----------|
| Pydantic factories | Domain object construction in tests |
| Fixture files | Synthetic SCAP scans, signed-skill fixtures |
| Recorded transcripts | Replayable LLM responses (cassette-style) |
| In-memory sinks | `JsonlSink(io.BytesIO())` for audit assertions |

### TDD Discipline

Per `CLAUDE.md` and global rules:

1. Write the failing test first.
2. Verify it fails for the *right reason* (feature missing, not syntax).
3. Implement the minimum to pass.
4. Verify it passes.
5. Refactor with tests green.

---

## Observability

> How Arc emits logs, metrics, and traces. Arc is a library / framework, not an HTTP service (see Error Handling Pattern above) — observability doubles as the audit trail, since every state-mutating action must be both traced and audited.

| Signal | Tool | Convention |
|--------|------|------------|
| Logs | Structured logging (stdlib `logging`) | No `print` statements (CON-11); messages must be safe to log — no PII/CUI/secrets |
| Metrics | OpenTelemetry SDK/API + OTLP exporters (1.20+) | Emitted on every tool dispatch and LLM call |
| Traces | OpenTelemetry spans | Request-scoped, propagated across `arcrun` → `arcagent` → `arcllm` |
| Audit | `arctrust.audit.emit` (single emission point, CON-5) | Fans out to `JsonlSink` (compliance), `SignedChainSink` (tamper-evident hash chain), `arcui.bridge.UIBridgeSink` (live telemetry) |
| Performance | Benchmarked against the thresholds in Build & CI → CI Gates → Performance Gates | `pytest-benchmark` / SPEC-017 |

---

## Compliance

```
regime: fedramp/nist
```

> Arc targets FedRAMP authorization and the NIST 800-53 control families (IA, AU, AC) per `CLAUDE.md`; CMMC drives supply-chain and least-privilege requirements. The OWASP **LLM Applications 2025** (LLM01–LLM10) and **Agentic Applications 2026** (ASI01–ASI10) threat tables in `CLAUDE.md` are the canonical mitigation map — **every spec must map at least one threat ID to a mitigation in its SDD.**

| Control | Requirement | Source |
|---------|-------------|--------|
| Identity (NIST IA) | Every `ArcAgent` constructed with a DID; every tool dispatch carries `caller_did` | `CLAUDE.md` Four Pillars |
| Audit (NIST AU) | Every operation emits `AuditEvent` via the single `arctrust.audit.emit` chokepoint | `CLAUDE.md` Four Pillars |
| Access Control (NIST AC) | 5-layer policy pipeline (Global → Provider → Agent → Team → Sandbox), first-DENY-wins, fail-closed | `arctrust.policy.PolicyPipeline` |
| Supply chain (CMMC) | Signed skills/extensions (Sigstore + Rekor), `pip-audit` CI gate | `arcskill`, CI |

### Identity

- [x] Every `ArcAgent` is constructed with a DID (`did:arc:{org}:{type}/{hash}`).
- [x] Every tool dispatch carries `caller_did`.
- [x] Inter-agent NATS messages signed with Ed25519; replay-protected via nonce + timestamp.

### Authorization

- [x] 5-layer policy pipeline (Global → Provider → Agent → Team → Sandbox), first-DENY-wins.
- [x] Fail-closed on exceptions in policy evaluation.
- [x] Tier (personal/enterprise/federal) selects policy content; the *pipeline* runs identically.

### Data Protection

- [x] No secrets in system prompts (treat prompts as exfiltratable — LLM07).
- [x] No PII / CUI in logs, error messages, or audit events without explicit handling.
- [x] Bidirectional PII redaction (SSN, credit card, email, phone, IP) before crossing trust boundary.
- [x] mTLS on inter-agent comms; TLS on all external calls; vault-backed short-lived credentials only.

### Threat Surface (Canonical Source)

The OWASP **LLM Applications 2025** (LLM01–LLM10) and **Agentic Applications 2026** (ASI01–ASI10) threat tables in `CLAUDE.md` are the canonical mitigation map. **Every spec must map at least one threat ID to a mitigation in its SDD.**

Quick reference:

| Top concerns | Arc's primary mitigation |
|--------------|-------------------------|
| LLM01 Prompt injection | System-prompt isolation; instruction hierarchy; never trust user-adjacent content as instruction |
| LLM05 Improper output handling | Sanitize/validate all LLM output before tool/API/DB use |
| LLM06 Excessive agency | Allowlists, policy pipeline, human-in-the-loop on irreversible actions |
| LLM10 Unbounded consumption | Token budgets, timeouts, circuit breakers |
| ASI04 Agentic supply chain | `arcskill` Sigstore+Rekor signing, AST scan, sandboxed dry-run |
| ASI05 Unexpected code execution | Defense-in-depth sandbox (ADR-017C): AST → restricted builtins → egress proxy |
| ASI06 Memory/context poisoning | Validate memory writes; integrity checks on context/workspace |

---

## Activity Hints Reference

> Used in PLAN.md for agent selection during implementation.

| Activity | Description | Typical Agent |
|----------|-------------|---------------|
| `unit-testing` | Write unit tests | test-implementation-agent |
| `integration-testing` | Write integration tests | test-implementation-agent |
| `e2e-testing` | Write E2E tests | test-implementation-agent |
| `backend-development` | Backend / module / package implementation | python-pro / fullstack-developer |
| `policy-implementation` | Policy pipeline / authorization | security-engineer |
| `provider-implementation` | New `arcllm` provider | python-pro |
| `cli-development` | `arc` CLI surface | cli-developer |
| `frontend-development` | `arcui` dashboard | frontend-developer |
| `security-review` | Security assessment | security-auditor / security-engineer |
| `performance-optimization` | Latency / memory tuning | performance-engineer |
| `lint-code` | `make lint` | quality-checker |
| `type-check` | `make typecheck` | quality-checker |
| `run-tests` | `make test` | quality-checker |
| `architecture-test` | TX.1 regression guards | architect-reviewer |
| `spec-compliance` | Verify against PRD/SDD/PLAN | spec-validator |

---

## Open Questions (Technical)

- [ ] mypy `python_version` is "3.11" but `.python-version` pins 3.12 — intentional minimum-floor coverage, or should it move to 3.12?
- [ ] No automated cyclomatic-complexity gate yet (review-only). Worth adding as a `ruff` extension or separate tool?

---

## References

- `Makefile` — canonical commands and gates
- `pyproject.toml` (root) — workspace, ruff, mypy, pytest config
- `packages/*/pyproject.toml` — per-package deps and config
- `docs/cli.md` — full CLI reference
- `CLAUDE.md` — build standards, threat-surface tables (canonical source)
- `scripts/coverage_report.py` — coverage threshold enforcement
- `.claude/adrs/ADR-017A..D-*.md` — accepted architectural decisions
- `.claude/steering/structure.md` — directory layout, module boundaries, naming rules
