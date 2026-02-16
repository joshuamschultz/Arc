# Arc

A security-first autonomous agent framework built for environments where audit trails, cryptographic identity, and data sovereignty are non-negotiable.

Arc is a composable stack of four packages that gives you full control over every layer -- from raw LLM calls to multi-turn agent sessions -- without vendor SDKs, hidden state, or opaque abstractions.

## Why Arc Exists

Most agent frameworks optimize for developer convenience. Arc optimizes for **trust**.

Every LLM call can be signed, every tool invocation passes through a deny-by-default sandbox, every action emits a structured audit event, and PII never reaches a provider unless you explicitly allow it. Arc was designed for teams that need to demonstrate compliance -- not just claim it.

## Architecture

```
arccli          Operator CLI -- 32 commands, 22 REPL commands
  |
arcagent        Agent nucleus -- identity, sessions, extensions, memory
  |
arcrun          Execution engine -- ReAct loop, sandbox, event bus
  |
arcllm          LLM abstraction -- 11 providers, zero SDKs, direct HTTP
```

Each layer is independently usable. You can call `arcllm` directly for a single LLM request, use `arcrun` for a sandboxed tool loop without agent state, or run the full `arcagent` stack with cryptographic identity and persistent sessions.

## Packages

| Package | What It Does |
|---------|-------------|
| [arcllm](packages/arcllm/) | Unified interface to 11 LLM providers via direct HTTP. No provider SDKs. Opt-in modules for retry, fallback, rate limiting, PII redaction, request signing, audit logging, and OpenTelemetry. |
| [arcrun](packages/arcrun/) | Async ReAct execution loop with deny-by-default tool sandboxing, JSON Schema parameter validation, structured event bus, and pluggable execution strategies. |
| [arcagent](packages/arcagent/) | Full agent lifecycle: Ed25519 cryptographic identity (DIDs), TOML-driven configuration, module bus with priority-ordered event dispatch, context window management, session persistence, skill discovery, and extension sandboxing. |
| [arccli](packages/arccli/) | Operator CLI for the full stack. Agent scaffolding, interactive REPL, session management, provider introspection, and `--json` output on every command for CI/CD integration. |

---

## Security Architecture

### Zero Provider SDKs

Arc makes direct HTTP calls to every LLM provider using `httpx`. No provider SDK is ever imported. This eliminates transitive dependency risk, removes opaque SDK behavior from the trust boundary, and keeps the total runtime dependency count minimal.

**arcllm runtime dependencies:** `pydantic`, `httpx`, `opentelemetry-api`. That's it.

### PII Redaction

The security module scans messages in both directions -- outbound to the LLM and inbound from the response. Detected PII (SSN, credit card, email, phone, IP) is replaced with `[PII:TYPE]` placeholders before data leaves your environment. The detector is protocol-based and pluggable for custom patterns.

### Request Signing

Every LLM request can be cryptographically signed. Messages, tools, and model name are serialized to canonical JSON (`sort_keys=True`, compact separators) and signed with HMAC-SHA256. The signature and algorithm are attached to the response metadata for downstream verification. ECDSA P-256 support is in progress.

### Cryptographic Agent Identity

Each agent generates an Ed25519 keypair (via libsodium/PyNaCl) and derives a Decentralized Identifier:

```
did:arc:{org}:{agent_type}/{sha256_prefix}
```

Private keys are stored with `0600` permissions. Key loading rejects files with group or world-readable bits. Identity changes are tracked via a dual audit trail -- OpenTelemetry spans and an append-only JSONL file.

### Deny-by-Default Tool Sandbox

Tools are not callable unless explicitly allowed. The sandbox runs a three-stage check on every invocation:

1. **Allowlist gate** -- tool name must be in the configured list
2. **Custom checker** -- async callback for parameter-level policy (e.g., reject `rm -rf`)
3. **JSON Schema validation** -- parameters validated against the tool's schema before execution

Checker errors default to denial. Dynamically registered tools are denied until explicitly allowed.

### Sandboxed Code Execution

The built-in `execute_python` tool runs code in a stripped subprocess:

- **Minimal environment**: only `PATH=/usr/bin:/bin`, `HOME=/tmp`, `LANG=en_US.UTF-8`. Host environment is never inherited.
- **Process group isolation**: `start_new_session=True` with two-phase timeout (SIGTERM, 5s grace, SIGKILL).
- **Fresh workspace**: each execution gets a temporary directory, destroyed after use.
- **Output truncation**: stdout/stderr capped at 64KB.

### Workspace Path Validation

All file-based tools route through `resolve_workspace_path()`, which enforces:

- Null byte injection guard
- Symlink traversal prevention (walks each path component)
- Workspace boundary enforcement via `Path.relative_to()`

### Vault Integration

API keys and signing secrets can be resolved from an external vault backend with TTL-cached lookups. The vault interface is a protocol -- any backend implementing `get_secret(path) -> str` works. Environment variable fallback is automatic when the vault is unreachable.

### Extension Sandboxing

Third-party extensions run in one of three sandbox modes:

| Mode | Filesystem | Subprocess | Network |
|------|-----------|------------|---------|
| `workspace` | Unrestricted | Allowed | Allowed |
| `paths` | Workspace + allowed paths only | Allowed | Allowed |
| `strict` | Workspace only | Blocked | Blocked |

`strict` mode monkey-patches `subprocess.run`, `os.system`, and `urllib.request.urlopen` during extension loading, with all patches restored in the `finally` block.

### HTTPS Enforcement

Provider base URLs are validated at config load time. HTTP is rejected for all remote hosts. HTTP is only permitted for `localhost`, `127.0.0.1`, and `[::1]` (local model servers like Ollama and vLLM).

### Log Injection Prevention

All structured log output sanitizes control characters (`\n`, `\r`, `\t`). Error bodies in exceptions are truncated to 500 characters. Audit logs emit metadata only (provider, model, token counts) by default -- raw message content requires explicit opt-in at DEBUG level.

### Environment Variable Security

Security-sensitive config paths are blocked from environment variable override:

```
vault__backend, tools__native, tools__process, identity__key_dir
```

These can only be set in the TOML config file, preventing runtime injection.

---

## Key Architectural Decisions

### Stateless LLM Layer

Model objects hold configuration, not conversation state. There is no hidden message history accumulating inside the provider abstraction. Agents manage their own message lists, making state explicit, inspectable, and serializable at every point.

### Config-Driven, Not Code-Driven

Model metadata (context windows, capabilities, pricing, supported modalities) lives in TOML files, not Python code. Adding a provider that speaks the OpenAI API format requires a 5-line adapter file and a TOML config. No registry edits, no import changes.

### Decorator-Pattern Module Stack

arcllm modules (retry, fallback, rate limit, security, audit, telemetry, otel) wrap the adapter using the decorator pattern. The stacking order is deterministic:

```
Otel → Telemetry → Audit → Security → Retry → Fallback → RateLimit → Adapter
```

Otel creates the root span. Telemetry measures timing and cost. Audit logs metadata. Security redacts PII and signs. Retry handles transient failures. Fallback tries alternate providers. RateLimit throttles before the wire. Each module is independently togglable per call.

### Non-Optional Event Emission

arcrun emits structured events for every action -- tool calls, LLM invocations, turn boundaries, strategy selection. Events are emitted synchronously inline and cannot be disabled. Observer callback failures are silently caught, so a broken logger never crashes the engine. Every `LoopResult` includes the complete event list.

### Cooperative Cancellation and Mid-Execution Steering

Running tasks support three intervention points:

- **Steer**: inject a message mid-turn, skipping remaining tool calls
- **Follow-up**: inject a message at end-of-turn, preventing loop exit
- **Cancel**: cooperative cancellation via `asyncio.Event`

### Module Bus with Priority and Veto

arcagent's module bus dispatches events with priority ordering (10=policy, 50=security, 100=default, 200=logging). Same-priority handlers run concurrently. Cross-priority groups run sequentially. Any handler can veto an action (e.g., deny a tool call), but all handlers still execute for audit completeness.

### Self-Improving Policy Engine

Implements the ACE framework (arXiv:2510.04618). A reflector model critiques agent behavior every N turns. Good behaviors score up, harmful ones score down. Bullets below score 2 are auto-removed. The policy file is atomically written via tmp+rename, capped at 200 rules, and sorted by effectiveness.

### Progressive Context Management

Context window usage is managed in three tiers:

| Threshold | Action |
|-----------|--------|
| < 70% | No action |
| 70-95% | Observation masking -- old tool outputs replaced with `[output pruned]` placeholders |
| > 95% | Emergency truncation |

Recent messages are always protected within a 40% window.

---

## Air-Gapped and On-Premises Deployment

Three providers work entirely on-premises with no internet access and no API key:

| Provider | Default URL | Use Case |
|----------|------------|----------|
| Ollama | `localhost:11434` | Local open-weight models |
| vLLM | `localhost:8000` | High-throughput GPU serving |
| HuggingFace TGI | `localhost:8080` | Text generation inference |

Combined with vault-backed secrets, HTTPS enforcement, and zero external SDK dependencies, Arc runs in fully air-gapped environments with no code changes.

---

## Compliance Mapping

### NIST SP 800-53

| Control | Arc Implementation |
|---------|-------------------|
| AC-3 (Access Enforcement) | Deny-by-default sandbox, tool allowlists |
| AC-4 (Information Flow) | PII redaction on input and output, context transform isolation |
| AC-6 (Least Privilege) | Explicit tool allowlists, extension sandbox modes |
| AU-2 (Event Logging) | Non-optional event emission on every action |
| AU-3 (Audit Content) | Events carry timestamp, run_id, tool name, arguments, duration |
| AU-8 (Timestamps) | `time.time()` on every event, UTC ISO in session records |
| AU-9 (Audit Protection) | Dual audit trail -- OTel spans + independent JSONL files |
| AU-12 (Audit Generation) | Events emitted inline, cannot be deferred or skipped |
| CM-7 (Least Functionality) | Tools are opt-in, minimal subprocess environment |
| IA-3 (Device Identification) | Ed25519 agent identity with DID |
| SC-28 (Protection at Rest) | Ephemeral run state, key file permissions enforced |
| SI-4 (System Monitoring) | Full event bus, token/cost tracking, OTel export |
| SI-10 (Input Validation) | JSON Schema on every tool call, path traversal guards |
| SI-11 (Error Handling) | Errors returned as structured results, never leaked to caller |

### OWASP LLM Top 10 (2025)

| Threat | Mitigation |
|--------|-----------|
| LLM01 (Prompt Injection) | PII redaction, content scanning pipeline |
| LLM02 (Sensitive Information) | Bidirectional PII detection, audit-safe logging |
| LLM04 (Data Poisoning) | Request signing with canonical JSON |
| LLM06 (Excessive Agency) | Deny-by-default sandbox, tool allowlists, parameter validation |
| LLM08 (Unbounded Consumption) | Rate limiting, token budget tracking, context window management |
| LLM10 (Output Handling) | Response PII scanning, output truncation |

---

## Observability

Arc provides three levels of observability:

**Level 1 -- Event Bus** (always on): Every tool call, LLM invocation, and turn boundary emits a structured event with timestamp, run_id, and relevant metrics. Available in every `LoopResult`.

**Level 2 -- Telemetry Module** (opt-in): Wall-clock timing, per-call USD cost calculation from provider pricing metadata, and structured logging.

**Level 3 -- OpenTelemetry** (opt-in): Full distributed tracing with GenAI semantic conventions. Supports OTLP gRPC and HTTP exporters, configurable sampling, batch span processing, and mTLS.

All three levels can run simultaneously. Level 1 has zero performance overhead. Levels 2 and 3 are toggled per call or globally via config.

---

## Quick Start

### Install

```bash
git clone https://github.com/joshuamschultz/Arc.git
cd Arc
uv sync --all-packages
```

### Create an Agent

```bash
arc agent create my-agent --name "My Agent" --model anthropic/claude-sonnet-4-20250514
```

### Interactive Chat

```bash
cd my-agent
arc agent chat
```

### One-Shot Task

```bash
arc agent run --task "Analyze the CSV in data/ and summarize the key trends"
```

### Direct LLM Call (No Agent)

```bash
arc llm call anthropic "What is the capital of France?" --security --audit
```

### Direct Run (No Agent State)

```bash
arc run task anthropic/claude-sonnet-4-20250514 "Calculate 2^32" --with-calc
```

---

## Individual Package Install

Each package is independently installable:

```bash
pip install arcllm       # LLM abstraction only
pip install arcrun       # Execution engine + arcllm
pip install arcagent     # Full agent + arcllm + arcrun
pip install arccli       # CLI + everything
```

---

## License

Copyright BlackArc Systems / CTG Federal. See individual package licenses for details.
