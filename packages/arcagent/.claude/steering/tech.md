# Technical Context

> This document provides stable technical context that informs all feature specifications.
> Feature-specific details go in `.claude/specs/{feature}/` documents.

## Validation Checklist

- [x] Technology stack documented
- [x] Project commands listed
- [x] Quality thresholds defined
- [x] Error handling patterns documented
- [x] Testing approach defined
- [x] Security requirements listed
- [x] No [NEEDS CLARIFICATION] markers

---

## Technology Stack

### Core Technologies

| Layer | Technology | Version | Notes |
|-------|------------|---------|-------|
| Language | Python | 3.12+ | Core agent, all modules |
| Language (secondary) | TypeScript | 5.x | UI/channels only (future) |
| LLM Layer | ArcLLM | Internal | Provider-agnostic calls, failover |
| Runtime | ArcRun | Internal | Agent loop (think-act-observe) |
| Config | TOML | - | Pydantic 2.x validation, env var overrides |
| Message Bus | NATS JetStream | Latest | Inter-agent comms, 18M msgs/sec |
| State (coordination) | PostgreSQL | 16+ | Fleet coordination state (arcTeam) |
| State (per-agent) | SQLite | 3.x | Agent-local state, portable |
| Vector Search | sqlite-vec | Latest | Per-agent vector index, no shared DB |
| Isolation | Firecracker microVMs | Latest | Hardware-level isolation, ~125ms cold start |
| Secrets | HashiCorp Vault | Latest | Short-lived tokens, dynamic secrets |
| Observability | OpenTelemetry | Latest | Traces, metrics, audit → any SIEM |
| Identity | Ed25519 + W3C DIDs | - | Challenge-response auth, PKI |
| Config Distribution | etcd / Consul | Latest | Distributed config with watch support |

### Key Libraries

| Library | Purpose | Version |
|---------|---------|---------|
| PyNaCl | Ed25519 key generation and signing | Latest |
| python-jose | JWT creation and validation | Latest |
| Pydantic | Data validation, schemas, config | 2.x |
| SQLAlchemy | ORM for PostgreSQL (arcTeam coordination) | 2.x |
| NATS.py | NATS client for Python | Latest |
| OpenTelemetry SDK | Tracing, metrics, audit export | Latest |
| httpx | Async HTTP client | Latest |
| uvloop | High-performance async event loop | Latest |

### Development Tools

| Tool | Purpose | Config File |
|------|---------|-------------|
| Ruff | Linting + formatting | `pyproject.toml` |
| mypy | Type checking | `pyproject.toml` |
| pytest | Testing framework | `pyproject.toml` |
| pytest-cov | Coverage reporting | `pyproject.toml` |
| pytest-asyncio | Async test support | `pyproject.toml` |

---

## Project Commands

### Development

```bash
# Initialize a new agent
arcagent init --name "Agent Name" --org blackarc --type executor

# Run an agent
arcagent run

# Check agent status
arcagent status

# Run in development mode (file-based keys, no Vault)
arcagent run --dev
```

### Testing

```bash
# Run all tests
pytest

# Run with coverage
pytest --cov=arcagent --cov-report=term-missing

# Run specific test file
pytest tests/unit/test_identity.py

# Run integration tests only
pytest tests/integration/

# Run with verbose output
pytest -v
```

### Quality Checks

```bash
# Lint and format check
ruff check .

# Auto-fix lint issues
ruff check --fix .

# Format code
ruff format .

# Type checking
mypy arcagent/

# All quality checks
ruff check . && mypy arcagent/ && pytest --cov=arcagent
```

### Security

```bash
# Audit dependencies
pip-audit

# Scan for secrets in code
detect-secrets scan

# Generate SBOM
syft . -o spdx-json > sbom.json
```

---

## Quality Thresholds

### Coverage Requirements

| Metric | Threshold | Enforcement |
|--------|-----------|-------------|
| Line Coverage | >= 80% | CI gate |
| Branch Coverage | >= 75% | CI gate |
| Function Coverage | >= 80% | CI gate |
| Core Components | >= 90% | CI gate (identity, auth, policy engine) |

### Code Quality

| Metric | Threshold | Enforcement |
|--------|-----------|-------------|
| Ruff Linting | 0 errors | CI gate |
| mypy Type Errors | 0 errors | CI gate |
| Complexity | <= 10 per function | Review |
| Duplication | <= 3% | Review |
| Core LOC | < 3,000 lines total | Architecture review |

### Security

| Metric | Threshold | Enforcement |
|--------|-----------|-------------|
| Critical Vulnerabilities | 0 | CI gate |
| High Vulnerabilities | 0 | CI gate |
| Dependency Audit | Pass | CI gate |
| SBOM Generated | Required | Release gate |
| Module Signature | Valid | Module loader |

### Performance

| Metric | Threshold | Measurement |
|--------|-----------|-------------|
| Agent Cold Start | < 500ms (container), < 200ms (Firecracker) | Benchmark |
| Tool Call Latency | < 30s timeout default | Config |
| Context Compaction | < 5s for 100K token window | Benchmark |
| NATS Message Latency | < 10ms p95 | NATS monitoring |
| Memory per Agent | < 50MB baseline | Container metrics |

---

## Technical Constraints

| ID | Constraint | Type | Rationale |
|----|------------|------|-----------|
| CON-1 | Core < 3,000 LOC | Architecture | Inspectability. Security auditor reviews in 1 day. |
| CON-2 | Python strict typing (mypy strict) | Technical | Catch bugs early, document interfaces |
| CON-3 | No public network endpoints | Security | Mesh VPN only. Federal requirement. |
| CON-4 | mTLS on all internal comms | Security | Zero-trust. No plaintext inter-agent traffic. |
| CON-5 | All credentials vault-backed (prod) | Security | Never plaintext. Never in agent filesystem. |
| CON-6 | Every action is an audit event | Compliance | NIST 800-53 AU controls. 7-year retention. |
| CON-7 | Modules must be signed | Supply Chain | Code signing required. Provenance chain. |
| CON-8 | Classification-aware data flow | Compliance | Agent clearance >= data classification |
| CON-9 | Self-hostable, air-gap capable | Deployment | Must run in DOE labs, SCIFs |
| CON-10 | No Docker-in-Docker | Security | Container escape risk (CVE-2026-24763) |

---

## Error Handling Pattern

### Error Response Schema

```python
@dataclass
class AgentError:
    code: str              # Machine-readable error code
    message: str           # Human-readable message
    component: str         # Which component raised it (identity, tool_registry, etc.)
    trace_id: str          # OpenTelemetry trace ID
    details: dict | None = None  # Additional context
    classification: str = "UNCLASSIFIED"  # Error classification level
```

### Error Categories

| Category | Code Pattern | Example | Recovery |
|----------|-------------|---------|----------|
| Identity | IDENTITY_* | IDENTITY_AUTH_FAILED | Re-authenticate |
| Tool | TOOL_* | TOOL_TIMEOUT | Retry with backoff |
| Context | CONTEXT_* | CONTEXT_OVERFLOW | Trigger compaction |
| Module | MODULE_* | MODULE_SIGNATURE_INVALID | Reject module |
| Policy | POLICY_* | POLICY_BLOCKED | Log and deny |
| LLM | LLM_* | LLM_PROVIDER_DOWN | Failover via ArcLLM |
| Config | CONFIG_* | CONFIG_VALIDATION_FAILED | Reject config change |

### Error Handling Rules

1. **All errors are audit events.** Every error generates an OpenTelemetry span with error details.
2. **Errors carry classification.** An error about CUI data is itself CUI.
3. **No silent failures.** Modules that catch exceptions must re-emit as Module Bus events.
4. **Failover, not crash.** LLM errors trigger ArcLLM failover chain. Tool timeouts retry.
5. **Three strikes.** After 3 consecutive failures of the same type, escalate (question architecture, not just retry).

---

## Testing Approach

### Test Pyramid

```
         /\
        /  \  E2E (10%)
       /    \  Full agent loop with real LLM (expensive)
      /------\
     /        \  Integration (20%)
    /          \  Module Bus + modules, Identity + Vault, Context + compaction
   /--------------\
  /                \  Unit (70%)
 /                  \  Components, tools, config parsing, DID generation
```

### Test File Conventions

| Test Type | Location | Naming |
|-----------|----------|--------|
| Unit | `tests/unit/` mirroring `arcagent/` | `test_*.py` |
| Integration | `tests/integration/` | `test_*.py` |
| E2E | `tests/e2e/` | `test_*.py` |
| Security | `tests/security/` | `test_*.py` |
| Performance | `tests/performance/` | `bench_*.py` |

### Testing Libraries

| Purpose | Library | Usage |
|---------|---------|-------|
| Unit Tests | pytest | Component/function testing |
| Async Tests | pytest-asyncio | Async function testing |
| Coverage | pytest-cov | Coverage reporting |
| Mocking | unittest.mock / pytest-mock | External service simulation |
| Fixtures | pytest fixtures | Test data setup/teardown |
| Property Testing | Hypothesis | Edge case discovery |

### Test Data Strategy

| Approach | Use Case |
|----------|----------|
| Factories | Generating Agent, Module, Message objects |
| Fixtures | Predefined keypairs, DIDs, configs |
| Temp dirs | Isolated workspace per test |
| Mock Vault | VaultClient mock for unit tests |
| Embedded SQLite | Per-test database isolation |
| Mock NATS | In-process message bus for unit tests |

---

## Security Requirements

### Identity & Authentication

- [x] Every agent has a W3C DID (`did:arc:{org}:{type}/{id}`)
- [x] Ed25519 keypair generated at registration
- [x] Challenge-response authentication (no passwords, no shared secrets)
- [x] JWT sessions with configurable expiry (default 1 hour)
- [x] Private keys stored in Vault (production) or file (dev mode)
- [x] Message signing for inter-agent comms

### Authorization

- [x] Namespace-based access grants: `domain:path:permission`
- [x] Wildcard support (`*`, `**`) for path matching
- [x] Direct grants + team-inherited grants
- [x] Tool-level allowlist/denylist in config
- [x] Classification-based gating (agent clearance >= data classification)

### Data Protection

- [x] All credentials vault-backed, never plaintext
- [x] mTLS on all internal communications
- [x] PII/CUI detection in agent outputs (output classification module)
- [x] Encryption at rest for agent workspace (configurable)
- [x] Pre-compact flush ensures no data loss during compaction

### Lethal Trifecta Prevention

The agent must NEVER simultaneously have all three without a human-in-the-loop gate:
1. Access to private data
2. Ability to communicate externally
3. Ability to ingest untrusted content

Enforced by the `lethal-trifecta-breaker` policy module with approval gates.

### Supply Chain Security

- [x] All modules must be signed (SHA256 + author signature)
- [x] Provenance chain verification before loading
- [x] Pre-install vulnerability scanning
- [x] SBOM required for all deployments
- [x] OpenClaw adapted skills run in sandboxed adapter with lower trust level

---

## API Conventions

### Agent Communication Protocol (NATS)

```json
{
  "from": "did:arc:blackarc:executor/a1b2c3d4",
  "to": "did:arc:blackarc:planner/e5f6g7h8",
  "type": "request | response | event | handoff",
  "subject": "procurement.analysis.complete",
  "payload": {},
  "signature": "base64-ed25519-signature",
  "trace_id": "otel-trace-id",
  "timestamp": "2026-02-14T10:30:00Z",
  "classification": "CUI",
  "ttl": 300
}
```

### Module Manifest Format

```yaml
apiVersion: arcagent/v1
kind: Module
metadata:
  name: module-name
  version: 1.0.0
  type: memory | skill | tool | channel | hook | policy
  author: author-name
  signature: sha256:abc123...
  classification: UNCLASSIFIED | CUI | SECRET | TS
spec:
  description: What this module does
  requires:
    arcagent: ">=1.0.0"
    tools: [required-tools]
    permissions: [required-permissions]
  events:
    subscribes: [lifecycle-events]
    emits: [custom-events]
```

### Config Format

Single TOML file (`arcagent.toml`) with Pydantic 2.x validation. Environment variable overrides supported with `ARCAGENT_` prefix.

---

## Activity Hints Reference

> Used in PLAN.md for agent selection during implementation.

| Activity | Description | Typical Agent |
|----------|-------------|---------------|
| `core-development` | ArcAgent core component implementation | python-pro |
| `identity-development` | DID, keypair, auth implementation | security-engineer |
| `module-development` | Module Bus, lifecycle events, module loading | python-pro |
| `security-hardening` | Vault integration, mTLS, policy engine | security-engineer |
| `unit-testing` | Write unit tests | test-implementation-agent |
| `integration-testing` | Write integration tests | test-implementation-agent |
| `performance-testing` | Benchmarks, load testing | performance-engineer |
| `config-design` | TOML parsing, Pydantic validation | python-pro |
| `protocol-design` | NATS messages, DID Documents, module manifests | api-designer |
| `infrastructure` | Docker, Firecracker, NATS, Vault setup | devops-engineer |
| `documentation` | Architecture docs, API docs | technical-writer |

---

## Open Questions (Technical)

- [ ] Should we use gRPC instead of REST for arcTeam API? (NATS handles inter-agent, but arcTeam management may need a traditional API)
- [ ] Firecracker vs gVisor for container isolation? (Firecracker is lower overhead but more complex to set up)
- [ ] Should policy.md use a structured format (JSONL) instead of markdown? (Markdown is human-readable but harder to parse programmatically)

---

## References

- Architecture plan: `arcagent-plan.md`
- Design document: `arcagent-design-v3.md`
- Enterprise mesh reference: `../enterprise-mesh/apex-core/`
- Identity service reference: `../enterprise-mesh/apex-core/apex_core/services/identity.py`
- Access service reference: `../enterprise-mesh/apex-core/apex_core/services/access.py`
