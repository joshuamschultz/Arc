# ARC Security Gap Analysis & Mitigation Plan

**Date:** 2026-02-21
**Sources:** OWASP Top 10 for LLM Applications 2025, OWASP Agentic AI Threats v1.0, OWASP GenAI IR Guide, OWASP GenAI Solutions Reference Guide
**Scope:** arcllm, arcrun, arcagent, arcteam
**Target:** Federal deployment (FedRAMP, NIST 800-53, CMMC)

---

## Table of Contents

1. [Executive Summary](#1-executive-summary)
2. [Coverage Matrix](#2-coverage-matrix)
3. [ArcLLM Gaps & Additions](#3-arcllm-gaps--additions)
4. [ArcRun Gaps & Additions](#4-arcrun-gaps--additions)
5. [ArcAgent Gaps & Additions](#5-arcagent-gaps--additions)
6. [ArcTeam Gaps & Additions](#6-arcteam-gaps--additions)
7. [Cross-Cutting Concerns](#7-cross-cutting-concerns)
8. [NIST 800-53 Control Mapping](#8-nist-800-53-control-mapping)
9. [Incident Response Integration](#9-incident-response-integration)
10. [Red Team Framework](#10-red-team-framework)

---

## 1. Executive Summary

### What Exists (Strong)

- **ArcLLM**: Full middleware security stack (PII redaction, request signing, rate limiting, retry with backoff, fallback chains, vault-backed credentials, mTLS on telemetry export, HTTPS enforcement, audit logging, OTel tracing). All modules individually toggleable via TOML config.
- **ArcRun**: Sandbox with deny-by-default allowlists + custom policy callbacks, JSON Schema parameter validation on every tool call, subprocess isolation for code execution, recursion depth limiting, immutable parent system prompts on spawn, concurrency semaphores, comprehensive event bus.
- **ArcAgent**: Ed25519 DID identity, module import path allowlists, workspace path boundary enforcement with symlink traversal prevention, extension sandboxing (3 modes), tool allow/deny policy with event veto system, context poisoning sanitization, prompt injection boundary markers, sensitive value redaction in audit logs, runtime settings mutation blocking.
- **ArcTeam**: Chained HMAC tamper-evident audit trail, sender registration verification, channel membership enforcement, path traversal prevention, atomic writes with file locking, forward-only cursor advancement, DLQ for failed messages, classification-aware audit records.

### What's Missing (Critical for Federal)

| Gap | Severity | Package | OWASP Reference |
|-----|----------|---------|-----------------|
| Token/cost budget enforcement in loop | High | arcrun | LLM10, ASI08 |
| Output content filtering (PII/CUI on responses to users) | High | arcagent | LLM02, NIST SI-10 |
| Human-in-the-loop approval gates | High | arcagent | LLM06, ASI09 |
| Message-level Ed25519 signing | High | arcteam | ASI07 |
| Module cryptographic signature verification | High | arcagent | ASI04, LLM03 |
| Process-level sandbox isolation (seccomp/landlock) | High | arcrun, arcagent | ASI05 |
| Network restriction on code execution | Medium | arcrun | ASI05 |
| Behavioral anomaly detection | Medium | arcagent | ASI10 |
| Agent identity revocation | Medium | arcagent, arcteam | ASI03, ASI10 |
| Data poisoning validation | Medium | arcagent | LLM04 |
| Vector store access control | Medium | arcagent | LLM08 |
| Misinformation grounding/confidence | Medium | arcagent | LLM09 |
| SBOM generation | Medium | all | LLM03, ASI04 |
| Tool call rate limiting (per-tool) | Medium | arcrun | ASI02, LLM10 |
| Event data PII redaction | Low | arcrun | LLM02 |
| Replay protection (nonce+timestamp) | Low | arcteam | ASI07 |
| Incident response event hooks | Low | all | GenAI IR Guide |

---

## 2. Coverage Matrix

### OWASP Top 10 for LLM Applications 2025

| Code | Threat | arcllm | arcrun | arcagent | arcteam | Status |
|------|--------|--------|--------|----------|---------|--------|
| LLM01 | Prompt Injection | -- | Role-constrained messages | Boundary markers, XML escaping, policy reflection prompt | -- | Partial |
| LLM02 | Sensitive Info Disclosure | PII redaction, error truncation, log sanitization | Error truncation, env restriction | Audit log redaction | -- | **Gap: output filtering** |
| LLM03 | Supply Chain | Vault allowlist, adapter namespace restriction | -- | Module prefix allowlist, native tool validation | -- | **Gap: module signing, SBOM** |
| LLM04 | Data/Model Poisoning | -- | -- | -- | -- | **Not addressed** |
| LLM05 | Improper Output Handling | Pydantic response validation | JSON Schema tool args, error truncation | Context sanitization (NFKC, zero-width removal) | -- | Adequate |
| LLM06 | Excessive Agency | -- | Sandbox allowlists, tool policy callbacks | Tool allow/deny, veto system, append-only notes, path boundaries | -- | **Gap: HITL gates** |
| LLM07 | System Prompt Leakage | Vault credentials, PII on system messages | System prompt rebuilt from source | Minimal memory guidance, config/instruction separation | -- | Adequate |
| LLM08 | Vector/Embedding Weaknesses | -- | -- | -- | -- | **Not addressed** |
| LLM09 | Misinformation | -- | -- | -- | -- | **Not addressed** |
| LLM10 | Unbounded Consumption | Rate limiting, retry caps, fallback chain cap, cost tracking | max_turns, max_depth, tool timeouts, spawn semaphore | Context window management, file size limits, ReDoS protection | Body size limit, poll limits | **Gap: budget enforcement** |

### OWASP Agentic AI Threats 2026

| Code | Threat | arcllm | arcrun | arcagent | arcteam | Status |
|------|--------|--------|--------|----------|---------|--------|
| ASI01 | Agent Goal Hijack | -- | Immutable parent system prompt on spawn | -- | -- | **Gap: policy engine enforcement** |
| ASI02 | Tool Misuse | -- | Sandbox check + JSON Schema validation + audit | Tool allow/deny + schema validation + veto | URI validation | **Gap: per-tool rate limiting** |
| ASI03 | Identity & Privilege Abuse | -- | -- | Ed25519 DID, key file permissions, config mutation blocking | Sender registration, duplicate rejection | **Gap: identity revocation** |
| ASI04 | Agentic Supply Chain | Vault allowlist | -- | Module prefix allowlist, no eval/exec | -- | **Gap: cryptographic module signing** |
| ASI05 | RCE | Config path traversal prevention | Subprocess isolation, restricted env, timeout | Extension sandbox (3 modes), bash workspace scoping | No eval/exec, JSON-only parsing | **Gap: process-level isolation** |
| ASI06 | Memory/Context Poisoning | -- | -- | NFKC sanitization, identity.md audit, append-only notes, bash deny on memory paths | Path traversal prevention, chained HMAC | Adequate |
| ASI07 | Insecure Inter-Agent Comms | HMAC request signing, mTLS on OTel, HTTPS enforcement | -- | -- | HMAC audit chain | **Gap: message-level signing, mTLS** |
| ASI08 | Cascading Failures | Retry backoff, fallback chain cap, rate limiting | Depth limiting, spawn semaphore, cancel events, gather isolation | Module bus handler isolation, tool timeouts | DLQ, atomic writes, poll limits | **Gap: circuit breakers** |
| ASI09 | Human-Agent Trust Exploitation | -- | -- | -- | EntityType enum (agent vs user) | **Gap: HITL gates, AI content labeling** |
| ASI10 | Rogue Agents | OTel tracing | Tool denial events, parent-child audit correlation | Telemetry, audit events | Full audit trail, tamper detection | **Gap: anomaly detection, revocation** |

### Agentic AI Threats (Extended from OWASP Document)

| Threat | Current Coverage | Gap |
|--------|-----------------|-----|
| T1 Memory Poisoning | Context sanitization, append-only notes, identity.md audit | Need: memory write validation rules, anomaly detection on memory mutations |
| T2 Tool Misuse | Sandbox + schema validation + veto + audit | Need: per-tool rate limits, parameter value sanitization beyond schema |
| T3 Privilege Compromise | DID identity, config mutation blocking | Need: runtime privilege escalation detection, identity revocation |
| T4 Resource Overload | Rate limiting, timeouts, depth limits | Need: budget enforcement, per-tool rate limits |
| T5 Cascading Hallucination | -- | Need: confidence scoring, grounding verification |
| T6 Intent Breaking | Immutable parent prompts, boundary markers | Need: goal drift detection, behavioral monitoring |
| T7 Misaligned Behaviors | Policy engine reflection | Need: continuous behavioral validation against identity.md |
| T8 Repudiation | Full audit trail, chained HMAC | Adequate for current phase |
| T9 Identity Spoofing | Ed25519 DID, sender verification | Need: mutual authentication on message exchange |
| T10 Overwhelming HITL | -- | Need: HITL rate management, decision fatigue detection |
| T11 RCE | Extension sandbox, subprocess isolation | Need: process-level isolation (seccomp/landlock) |
| T12 Agent Comm Poisoning | -- | Need: message-level signing + verification |
| T13 Rogue Agents | Audit trail, tamper detection | Need: behavioral anomaly detection, automated quarantine |
| T14 Human Attacks on MAS | -- | Need: admin action audit, multi-party authorization for critical ops |
| T15 Human Manipulation | -- | Need: social engineering detection, confirmation for high-impact requests |

---

## 3. ArcLLM Gaps & Additions

ArcLLM's separation of concerns: **all LLM provider calls**. Security controls here apply to the request/response boundary with LLM providers.

### 3.1 Output Content Classifier Module (NEW)

**OWASP:** LLM02 (Sensitive Info Disclosure), LLM09 (Misinformation)
**NIST:** SI-10 (Information Input Validation), SC-28 (Protection of Information at Rest)

**Purpose:** Classify and filter LLM response content before it reaches the caller. Unlike the existing PII redaction (which catches structured patterns like SSN/email), this module detects and flags classification-sensitive content (CUI, FOUO, PII in natural language).

**Config additions to `config.toml`:**

```toml
[modules.output_filter]
enabled = false

# Classification levels to detect and flag
classification_levels = ["CUI", "FOUO", "SECRET", "TOP_SECRET"]

# Action on detection: "flag" (add metadata), "redact" (replace), "block" (raise error)
action = "flag"

# Custom detection patterns (regex-based, extensible)
custom_patterns = []

# Confidence threshold for flagging (0.0-1.0)
confidence_threshold = 0.8

# Enable natural-language PII detection (beyond regex patterns)
nlp_pii_enabled = false

# Grounding verification: flag responses that lack source attribution
grounding_check_enabled = false
```

**Implementation:** New `OutputFilterModule` in the middleware stack, positioned between SecurityModule and AuditModule (so audit sees the filtered version). Uses the existing `BaseModule` pattern.

**Files to create:**
- `arcllm/modules/output_filter.py` -- Module implementation
- `arcllm/_classifier.py` -- Classification detection engine (Protocol + regex implementation, extensible to NLP)

**Files to modify:**
- `arcllm/registry.py` -- Add `output_filter` to module stack ordering
- `arcllm/config.py` -- Add `OutputFilterConfig` model

---

### 3.2 Token Budget Tracking Module (NEW)

**OWASP:** LLM10 (Unbounded Consumption)
**NIST:** SC-6 (Resource Availability)

**Purpose:** Track cumulative token usage and cost across invocations within a session, enforcing configurable budgets. Currently, `telemetry.py` tracks cost per-invocation but has no session-level enforcement.

**Config additions:**

```toml
[modules.budget]
enabled = false

# Maximum tokens per session (0 = unlimited)
session_token_limit = 0

# Maximum cost per session in USD (0.0 = unlimited)
session_cost_limit = 0.0

# Action when budget exceeded: "warn" (log + metadata), "soft_block" (raise retriable error), "hard_block" (raise non-retriable error)
action = "soft_block"

# Warning threshold as percentage of limit (0.0-1.0)
warning_threshold = 0.8
```

**Implementation:** New `BudgetModule` wrapping the inner provider. Maintains cumulative counters. Emits OTel events at warning threshold and on enforcement.

**Files to create:**
- `arcllm/modules/budget.py`

**Files to modify:**
- `arcllm/registry.py` -- Add to stack (outermost, before OTel)
- `arcllm/config.py` -- Add `BudgetConfig` model

---

### 3.3 ECDSA-P256 Signing Completion

**OWASP:** ASI07 (Insecure Inter-Agent Communication)
**NIST:** SC-13 (Cryptographic Protection)

**Current state:** `_signing.py:72` raises "not yet fully implemented" for `ecdsa-p256`.

**Files to modify:**
- `arcllm/_signing.py` -- Complete `EcdsaSigner` implementation using PyNaCl or `cryptography` library

---

### 3.4 SBOM Generation Hook

**OWASP:** LLM03 (Supply Chain), ASI04 (Agentic Supply Chain)
**NIST:** SA-12 (Supply Chain Protection)

**Purpose:** Generate Software Bill of Materials on build/release.

**Files to modify:**
- `pyproject.toml` -- Add `cyclonedx-bom` or `syft` to dev dependencies
- Add `scripts/generate-sbom.sh` for CI integration

---

## 4. ArcRun Gaps & Additions

ArcRun's separation of concerns: **agentic execution loop**. Security controls here govern how tools are invoked, how loops terminate, and how child runs are bounded.

### 4.1 Token/Cost Budget Enforcement in Loop (MODIFY)

**OWASP:** LLM10 (Unbounded Consumption), ASI08 (Cascading Failures)
**NIST:** SC-6 (Resource Availability)

**Current state:** `RunState` defines `token_budget` and `cost_budget` fields but they are never checked.

**Config additions to `run()` / `run_async()` parameters:**

```python
# Already exists in RunState, needs enforcement:
token_budget: int | None = None      # Max total tokens (input + output)
cost_budget: float | None = None     # Max USD cost

# New parameters:
budget_action: Literal["warn", "stop"] = "stop"  # What to do when budget exceeded
budget_warning_threshold: float = 0.8             # Emit event at this % of budget
```

**Files to modify:**
- `arcrun/strategies/react.py` -- Add budget check after `_accumulate_usage()` (around line 179). If `state.token_budget` is set and `state.tokens_used["total"] >= state.token_budget`, emit `loop.budget_exceeded` event and break loop. Same for `cost_budget`.
- `arcrun/state.py` -- Add `budget_action` and `budget_warning_threshold` fields
- `arcrun/loop.py` -- Pass new params through to `_build_state()`

---

### 4.2 Per-Tool Call Rate Limiting (NEW)

**OWASP:** ASI02 (Tool Misuse), LLM10 (Unbounded Consumption)
**NIST:** SC-6 (Resource Availability)

**Purpose:** Prevent the LLM from calling the same tool excessively within a single run (e.g., calling `execute_python` 100 times in a loop).

**Config additions:**

```python
@dataclass
class ToolRateLimit:
    max_calls_per_turn: int | None = None   # Max calls to this tool per turn
    max_calls_per_run: int | None = None    # Max calls to this tool per run
    cooldown_seconds: float | None = None   # Min time between calls
```

**Files to modify:**
- `arcrun/types.py` -- Add `ToolRateLimit` dataclass, add `rate_limit: ToolRateLimit | None` to `Tool`
- `arcrun/executor.py` -- Check rate limits before sandbox check in `execute_tool_call()`
- `arcrun/state.py` -- Add `tool_call_counts: dict[str, int]` for per-run tracking

---

### 4.3 Tool Argument Value Sanitization Hook (NEW)

**OWASP:** ASI02 (Tool Misuse), LLM05 (Improper Output Handling)
**NIST:** SI-10 (Information Input Validation)

**Purpose:** JSON Schema validates structure but not content. Allow tool authors to register value sanitizers (e.g., path traversal checks, SQL injection detection).

**Config additions:**

```python
@dataclass
class Tool:
    # ... existing fields ...
    sanitize: Callable[[dict[str, Any]], dict[str, Any]] | None = None  # Optional argument sanitizer
```

**Files to modify:**
- `arcrun/types.py` -- Add `sanitize` field to `Tool`
- `arcrun/executor.py` -- Call `tool_def.sanitize(tc.arguments)` after schema validation, before execution

---

### 4.4 Event Data Redaction (MODIFY)

**OWASP:** LLM02 (Sensitive Info Disclosure)
**NIST:** AU-3 (Content of Audit Records)

**Current state:** Events like `tool.start` include full `tc.arguments` which may contain PII or credentials.

**Config additions:**

```python
@dataclass
class EventBusConfig:
    redact_tool_arguments: bool = False          # Redact tool args in events
    argument_redaction_keys: list[str] | None = None  # Keys to redact (None = all)
```

**Files to modify:**
- `arcrun/events.py` -- Add optional redaction callback to `EventBus`
- `arcrun/executor.py` -- Apply redaction before emitting `tool.start` events

---

### 4.5 Process-Level Sandbox for Code Execution (MODIFY)

**OWASP:** ASI05 (RCE)
**NIST:** SC-39 (Process Isolation)

**Current state:** `execute.py` uses restricted env vars and temp directories but no process-level isolation (no chroot, no seccomp, no namespace isolation, no network restriction).

**Config additions:**

```python
def make_execute_tool(
    # ... existing params ...
    sandbox_level: Literal["basic", "namespace", "firecracker"] = "basic",
    network_enabled: bool = False,       # Allow network access in subprocess
    filesystem_readonly: bool = False,   # Mount filesystem read-only except tmpdir
    max_memory_mb: int = 256,           # Memory limit for subprocess
    max_processes: int = 10,            # Process limit (nproc)
) -> Tool:
```

**Implementation phases:**
1. **basic** (current): Env restriction + tmpdir + process group
2. **namespace** (Phase 2): Linux `unshare` for PID/network/mount namespaces. Use `subprocess` with `preexec_fn` to call `os.unshare()` where available. Falls back to basic on macOS.
3. **firecracker** (Phase 3): Full microVM isolation via Firecracker. For DOE/SCIF deployment.

**Files to modify:**
- `arcrun/builtins/execute.py` -- Add `sandbox_level` parameter, implement namespace isolation
- `arcrun/types.py` -- Document sandbox levels

---

### 4.6 Network Restriction on Spawned Processes (NEW)

**OWASP:** ASI05 (RCE), LLM02 (Sensitive Info Disclosure)
**NIST:** SC-7 (Boundary Protection)

**Purpose:** Prevent code execution tools from making outbound network requests (data exfiltration vector).

**Implementation:** When `network_enabled=False` (default), use platform-appropriate isolation:
- Linux: `unshare(CLONE_NEWNET)` creates a network namespace with no connectivity
- macOS: Use `sandbox-exec` profile or document as platform limitation

**Files to modify:**
- `arcrun/builtins/execute.py` -- Add network isolation to namespace sandbox level

---

## 5. ArcAgent Gaps & Additions

ArcAgent's separation of concerns: **agent orchestration with tools, skills, extensions, memory**. Security controls here govern agent behavior, identity, and the agent's interaction with its environment.

### 5.1 Human-in-the-Loop Approval Gates (NEW)

**OWASP:** LLM06 (Excessive Agency), ASI09 (Human-Agent Trust Exploitation), Playbook 5 (HITL Decision Fatigue)
**NIST:** AC-3 (Access Enforcement), AC-6 (Least Privilege)

**Purpose:** Require human approval for destructive, irreversible, or high-impact tool calls before execution. The veto system (`module_bus.py`) provides the hook; this module implements the approval logic.

**Config additions to `arcagent.toml`:**

```toml
[modules.approval]
enabled = false

# Tools that always require approval
require_approval = ["bash", "write"]

# Approval timeout in seconds (auto-deny if exceeded)
timeout_seconds = 300

# Approval channel: "cli" (stdin prompt), "team" (arcteam message), "webhook" (HTTP callback)
channel = "cli"

# Auto-approve in non-interactive mode (CI/CD). DANGEROUS - requires explicit opt-in.
auto_approve_non_interactive = false

# Batch approval: allow approving multiple pending requests at once
batch_approval = false

# Decision fatigue mitigation: max approvals per hour before forcing a cooldown
max_approvals_per_hour = 50
cooldown_minutes = 15
```

**Files to create:**
- `arcagent/modules/approval/` -- New module directory
  - `__init__.py`
  - `approval_engine.py` -- Core logic: subscribes to `agent:pre_tool` at priority 5 (before memory at 10), vetos if tool requires approval, waits for human response
  - `channels.py` -- Pluggable approval channel implementations (CLI, Team, Webhook)
  - `config.py` -- Pydantic config model

---

### 5.2 Output Content Filtering (NEW)

**OWASP:** LLM02 (Sensitive Info Disclosure)
**NIST:** SI-10 (Information Input Validation), AC-4 (Information Flow Enforcement)

**Purpose:** Filter agent responses before they reach the user. Catches PII/CUI that the LLM generates in natural language (not just structured patterns). This is distinct from ArcLLM's PII redaction (which operates on provider request/response); this operates on the agent's final output.

**Config additions:**

```toml
[modules.output_guard]
enabled = false

# Classification level of the operating environment
environment_classification = "UNCLASSIFIED"

# Block responses containing content above environment classification
block_above_classification = true

# PII detection mode: "regex" (fast, pattern-based), "llm" (uses eval LLM for NL detection)
pii_detection_mode = "regex"

# Custom PII patterns (name + regex)
custom_pii_patterns = []

# CUI category markers to detect
cui_categories = ["CTI", "PRVCY", "PROPIN", "ITAR"]

# Action: "redact" (replace detected content), "block" (reject entire response), "flag" (add metadata only)
action = "redact"
```

**Files to create:**
- `arcagent/modules/output_guard/` -- New module
  - `__init__.py`
  - `guard.py` -- Subscribes to `agent:post_respond`, inspects response content
  - `detectors.py` -- Pluggable detection backends (regex, LLM-based)
  - `config.py`

---

### 5.3 Module Cryptographic Signature Verification (MODIFY)

**OWASP:** ASI04 (Agentic Supply Chain), LLM03 (Supply Chain)
**NIST:** SI-7 (Software, Firmware, and Information Integrity)

**Current state:** `module_loader.py` validates import path prefixes but does not verify cryptographic signatures.

**Config additions:**

```toml
[modules._loader]
# Require Ed25519 signature on module files before loading
require_signatures = false

# Path to public key for signature verification
signature_public_key = ""

# Path to signature manifest file (maps module paths to signatures)
signature_manifest = ""

# Action on missing signature: "warn" (log + load), "block" (refuse to load)
missing_signature_action = "warn"
```

**Files to modify:**
- `arcagent/core/module_loader.py` -- Add signature verification step after path prefix check, before `importlib.import_module()`. Use Ed25519 verify from `identity.py`.

**Files to create:**
- `arcagent/core/module_signer.py` -- CLI utility to generate signature manifests: `python -m arcagent.core.module_signer sign --key /path/to/key --manifest modules.sig arcagent/modules/`

---

### 5.4 Identity Revocation (NEW)

**OWASP:** ASI03 (Identity & Privilege Abuse), ASI10 (Rogue Agents)
**NIST:** IA-5 (Authenticator Management)

**Purpose:** Revoke compromised or rogue agent identities. Currently, there is no mechanism to invalidate an agent's DID after creation.

**Config additions:**

```toml
[identity]
# ... existing fields ...

# Path to revocation list (JSON file of revoked DIDs)
revocation_list = ""

# Check revocation on startup
check_revocation_on_start = true

# Revocation check interval in seconds (0 = startup only)
revocation_check_interval = 0
```

**Files to modify:**
- `arcagent/core/identity.py` -- Add `is_revoked()` method that checks DID against revocation list
- `arcagent/core/agent.py` -- Check revocation on startup and optionally on interval

**Files to create:**
- `arcagent/core/revocation.py` -- Revocation list management (load, check, CLI for adding/removing DIDs)

---

### 5.5 Behavioral Anomaly Detection (NEW)

**OWASP:** ASI10 (Rogue Agents), ASI01 (Agent Goal Hijack), T6 (Intent Breaking), T7 (Misaligned Behaviors)
**NIST:** SI-4 (Information System Monitoring), AU-6 (Audit Review, Analysis, and Reporting)

**Purpose:** Monitor agent behavior patterns and flag anomalies that may indicate goal hijack, rogue behavior, or misaligned operation. Uses the existing telemetry/audit event stream.

**Config additions:**

```toml
[modules.behavioral_monitor]
enabled = false

# Baseline window: number of turns to establish behavioral baseline
baseline_window = 20

# Anomaly detection thresholds
max_tool_calls_per_turn = 10
max_consecutive_failures = 5
max_unique_tools_per_turn = 5

# Flag if agent attempts tools not in its identity.md skill set
flag_off_profile_tools = true

# Flag if agent's output diverges significantly from its system prompt persona
flag_persona_drift = false

# Action on anomaly: "log" (emit event), "alert" (emit + send arcteam alert), "pause" (suspend execution for human review)
action = "log"

# Cooldown between alerts (seconds)
alert_cooldown_seconds = 300
```

**Files to create:**
- `arcagent/modules/behavioral_monitor/` -- New module
  - `__init__.py`
  - `monitor.py` -- Subscribes to `agent:post_tool`, `agent:post_respond`, tracks patterns
  - `baselines.py` -- Statistical baseline computation
  - `config.py`

---

### 5.6 Data Poisoning Validation (NEW)

**OWASP:** LLM04 (Data/Model Poisoning), T1 (Memory Poisoning)
**NIST:** SI-10 (Information Input Validation)

**Purpose:** Validate data integrity before it enters the agent's memory or context. Applies to: memory writes, context.md updates, skill content, extension-provided data.

**Config additions:**

```toml
[modules.data_validation]
enabled = false

# Validate memory writes for injection attempts
validate_memory_writes = true

# Maximum entropy score for memory entries (high entropy = potential obfuscated payload)
max_entropy_threshold = 4.5

# Check for known prompt injection patterns in incoming data
injection_pattern_check = true

# Checksum verification for ingested datasets/files
checksum_verification = false
checksum_manifest = ""
```

**Files to create:**
- `arcagent/modules/data_validation/` -- New module
  - `__init__.py`
  - `validator.py` -- Subscribes to memory write events, validates content
  - `patterns.py` -- Known injection/poisoning patterns
  - `config.py`

---

### 5.7 Vector Store Access Control (NEW)

**OWASP:** LLM08 (Vector/Embedding Weaknesses)
**NIST:** AC-3 (Access Enforcement), AC-4 (Information Flow Enforcement)

**Purpose:** If/when the agent uses vector stores for RAG, enforce access control on embedding retrieval to prevent cross-tenant data leakage.

**Config additions:**

```toml
[modules.vector_security]
enabled = false

# Require namespace isolation in vector queries
require_namespace = true

# Default namespace (agent DID)
default_namespace = ""

# Maximum results per query (prevent bulk extraction)
max_results_per_query = 20

# Embedding source validation
validate_embedding_source = true
```

**Files to create:**
- `arcagent/modules/vector_security/` -- New module (placeholder for Phase 2+ when vector stores are integrated)

---

### 5.8 Confidence Scoring on Responses (NEW)

**OWASP:** LLM09 (Misinformation), T5 (Cascading Hallucination)
**NIST:** SI-12 (Information Handling and Retention)

**Purpose:** Flag LLM responses that lack grounding or have low confidence. For federal use, agents should never present unverified information as authoritative.

**Config additions:**

```toml
[modules.confidence]
enabled = false

# Require source attribution for factual claims
require_attribution = false

# Use eval LLM to score response confidence (expensive but thorough)
llm_confidence_check = false

# Minimum confidence threshold (0.0-1.0). Below this, flag response.
min_confidence = 0.7

# Action on low confidence: "flag" (metadata), "prepend_warning" (add disclaimer), "block"
action = "flag"
```

**Files to create:**
- `arcagent/modules/confidence/` -- New module
  - `__init__.py`
  - `scorer.py` -- Confidence estimation (heuristic and/or LLM-based)
  - `config.py`

---

### 5.9 Extension Sandbox Enhancement (MODIFY)

**OWASP:** ASI05 (RCE)
**NIST:** SC-39 (Process Isolation)

**Current state:** `extensions.py:496-497` explicitly notes this is "best-effort Phase 1."

**Config additions:**

```toml
[extensions]
# ... existing fields ...

# Phase 2 sandbox: run extensions in subprocess with restricted capabilities
process_isolation = false

# Linux-only: seccomp profile for extension subprocess
seccomp_profile = ""

# Linux-only: landlock filesystem restrictions
landlock_enabled = false

# Resource limits for extension execution
max_memory_mb = 128
max_cpu_seconds = 30
max_file_descriptors = 64
```

**Files to modify:**
- `arcagent/core/extensions.py` -- Add `process_isolation` mode that runs extension code in a subprocess with resource limits via `resource.setrlimit()`

---

## 6. ArcTeam Gaps & Additions

ArcTeam's separation of concerns: **multi-agent team coordination**. Security controls here govern inter-agent communication, identity verification, and team-level trust.

### 6.1 Message-Level Ed25519 Signing (NEW)

**OWASP:** ASI07 (Insecure Inter-Agent Communication), T12 (Agent Communication Poisoning), T9 (Identity Spoofing)
**NIST:** SC-13 (Cryptographic Protection), SC-8 (Transmission Confidentiality and Integrity)
**Mitigation Playbook 6:** Securing Multi-Agent Communication & Trust Mechanisms

**Purpose:** Sign every message with the sender's Ed25519 private key. Verify signature before delivery. Currently mentioned in CLAUDE.md but not implemented.

**Config additions:**

```toml
[team]
# ... existing fields ...

# Require Ed25519 signatures on all messages
require_message_signatures = false

# Action on invalid/missing signature: "dlq" (dead letter queue), "warn" (deliver with flag), "reject" (drop + log)
invalid_signature_action = "dlq"

# Public key registry path (maps entity DIDs to public keys)
public_key_registry = ""

# Auto-register public keys from arcagent identity on entity registration
auto_register_keys = true
```

**Files to modify:**
- `arcteam/types.py` -- Add `signature: str | None` and `signer_did: str | None` fields to `Message`
- `arcteam/messenger.py` -- Sign messages in `send()`, verify in delivery pipeline. DLQ on invalid signature.

**Files to create:**
- `arcteam/crypto.py` -- Ed25519 sign/verify using PyNaCl (can share code with arcagent's `identity.py`)
- `arcteam/key_registry.py` -- Public key storage and lookup

---

### 6.2 Replay Protection (NEW)

**OWASP:** ASI07 (Insecure Inter-Agent Communication)
**NIST:** SC-8 (Transmission Confidentiality and Integrity)
**Mitigation Playbook 6:** Securing Multi-Agent Communication

**Purpose:** Prevent message replay attacks. The current forward-only cursors prevent re-reading, but don't prevent re-injection of previously valid messages.

**Config additions:**

```toml
[team]
# ... existing fields ...

# Enable nonce-based replay protection
replay_protection = false

# Nonce window size (max age of valid messages in seconds)
nonce_window_seconds = 300

# Nonce storage backend: "memory" (in-process), "file" (persistent)
nonce_storage = "memory"
```

**Files to modify:**
- `arcteam/types.py` -- Add `nonce: str | None` and `timestamp: float | None` to `Message`
- `arcteam/messenger.py` -- Generate nonce on send, validate nonce freshness + uniqueness on delivery

**Files to create:**
- `arcteam/nonce_store.py` -- Nonce tracking with window-based expiry

---

### 6.3 Circuit Breakers (NEW)

**OWASP:** ASI08 (Cascading Failures)
**NIST:** SC-5 (Denial of Service Protection)

**Purpose:** Prevent cascading failures when a downstream agent or channel becomes unresponsive. Currently, messaging has no circuit breaker pattern.

**Config additions:**

```toml
[team]
# ... existing fields ...

# Enable circuit breakers on message delivery
circuit_breaker_enabled = false

# Failure threshold before opening circuit
failure_threshold = 5

# Window for counting failures (seconds)
failure_window_seconds = 60

# Time to wait before attempting half-open (seconds)
recovery_timeout_seconds = 30
```

**Files to create:**
- `arcteam/circuit_breaker.py` -- Circuit breaker state machine (closed -> open -> half-open -> closed)

**Files to modify:**
- `arcteam/messenger.py` -- Wrap delivery attempts with circuit breaker check

---

### 6.4 Multi-Party Authorization for Critical Operations (NEW)

**OWASP:** T14 (Human Attacks on MAS), ASI09 (Human-Agent Trust Exploitation)
**NIST:** AC-3 (Access Enforcement)
**Mitigation Playbook 4:** Strengthening Authentication, Identity & Privilege Controls

**Purpose:** Require approval from multiple entities for critical team-level operations (e.g., removing agents, changing channel membership, modifying team config).

**Config additions:**

```toml
[team]
# ... existing fields ...

# Operations requiring multi-party approval
multi_party_operations = ["entity.remove", "channel.delete", "config.modify"]

# Minimum approvers required
min_approvers = 2

# Approval timeout (seconds)
approval_timeout_seconds = 600
```

**Files to create:**
- `arcteam/multi_party.py` -- Approval tracking, quorum logic

---

### 6.5 Admin Action Audit Enhancement (MODIFY)

**OWASP:** T14 (Human Attacks on MAS)
**NIST:** AU-2 (Audit Events), AU-12 (Audit Generation)

**Current state:** Audit covers message send, entity registration, status changes. Missing: config changes, permission changes, audit log access.

**Files to modify:**
- `arcteam/cli.py` -- Emit audit events for all admin CLI commands (currently only some emit events)
- `arcteam/config.py` -- Emit audit event on config modification
- `arcteam/audit.py` -- Emit audit event when audit log is read/verified (AU-9 compliance: detect unauthorized access to audit records)

---

### 6.6 OpenTelemetry Integration (NEW)

**OWASP:** ASI10 (Rogue Agents)
**NIST:** AU-3 (Content of Audit Records), AU-6 (Audit Review)

**Purpose:** Export team-level events to OpenTelemetry for centralized observability alongside arcllm and arcagent traces.

**Config additions:**

```toml
[team.telemetry]
enabled = false
service_name = "arcteam"
exporter = "otlp"
endpoint = "http://localhost:4317"
protocol = "grpc"
sample_rate = 1.0
```

**Files to create:**
- `arcteam/otel.py` -- OTel trace/span integration for message send/receive, registration, audit events

---

## 7. Cross-Cutting Concerns

### 7.1 SBOM Generation (ALL PACKAGES)

**OWASP:** LLM03, ASI04
**NIST:** SA-12 (Supply Chain Protection)

Add to each package's build pipeline:

```bash
# CycloneDX SBOM generation
pip install cyclonedx-bom
cyclonedx-py environment -o sbom.json --format json

# Or via syft for container images
syft packages dir:. -o cyclonedx-json > sbom.json
```

**Files to create per package:**
- `scripts/generate-sbom.sh`
- Add `cyclonedx-bom` to dev dependencies in `pyproject.toml`

---

### 7.2 Dependency Vulnerability Scanning (ALL PACKAGES)

**OWASP:** LLM03, ASI04
**NIST:** RA-5 (Vulnerability Scanning)

**Current state:** `pip-audit` is listed in arcagent's quality tools but not integrated into CI.

**Files to modify per package:**
- `pyproject.toml` -- Ensure `pip-audit` in dev dependencies
- CI config -- Add `pip-audit --strict` to quality gate

---

### 7.3 Structured Error Codes (ALL PACKAGES)

**OWASP:** GenAI IR Guide (Severity Matrix, Incident Classification)
**NIST:** SI-11 (Error Handling)

**Purpose:** Standardize error codes across all ARC packages for incident response correlation.

**Proposed error code format:** `ARC-{PACKAGE}-{CATEGORY}-{NUMBER}`

```
ARC-LLM-SEC-001  -- PII detected in outbound request
ARC-LLM-SEC-002  -- Signing key not available
ARC-RUN-SBX-001  -- Tool denied by sandbox
ARC-RUN-SBX-002  -- Tool not found in registry
ARC-RUN-BUD-001  -- Token budget exceeded
ARC-AGT-IDN-001  -- Identity key file insecure permissions
ARC-AGT-POL-001  -- Tool denied by policy
ARC-AGT-EXT-001  -- Extension sandbox violation
ARC-TM-MSG-001   -- Sender unauthorized
ARC-TM-MSG-002   -- Invalid message signature
ARC-TM-AUD-001   -- Audit chain integrity failure
```

**Files to modify:**
- Each package's error/exception module -- Add structured error codes

---

### 7.4 Classification-Aware Data Flow (ALL PACKAGES)

**OWASP:** LLM02
**NIST:** AC-4 (Information Flow Enforcement), SC-16 (Transmission of Security Attributes)

**Purpose:** Tag data with classification levels and enforce flow rules (e.g., SECRET data cannot flow to UNCLASSIFIED channels).

**Config additions (shared pattern):**

```toml
[security]
# Operating environment classification
environment_classification = "UNCLASSIFIED"

# Enforce classification flow rules
enforce_classification_flow = false

# Log classification boundary crossings
log_classification_crossings = true
```

This would be a shared utility in a new `arccore` or `arcsecurity` package, consumed by all four packages.

---

## 8. NIST 800-53 Control Mapping

### Currently Implemented

| Control | Family | Description | Implementation |
|---------|--------|-------------|----------------|
| **AC-3** | Access Control | Access Enforcement | arcrun sandbox allowlists, arcagent tool policy, arcteam sender verification |
| **AC-6** | Access Control | Least Privilege | arcrun tool allowlists, arcagent tool deny lists, arcteam channel membership |
| **AU-2** | Audit | Audit Events | All packages emit structured audit events |
| **AU-3** | Audit | Content of Audit Records | Events include timestamp, actor, action, outcome, correlation IDs |
| **AU-9** | Audit | Protection of Audit Information | arcteam chained HMAC on audit records |
| **AU-12** | Audit | Audit Generation | arcagent telemetry always-on, arcteam audit on all state mutations |
| **IA-5** | Identification | Authenticator Management | arcllm vault-backed credentials, arcagent Ed25519 key management |
| **SC-8** | System Comms | Transmission Integrity | arcllm HTTPS enforcement, request signing |
| **SC-13** | System Comms | Cryptographic Protection | arcllm HMAC-SHA256/ECDSA signing, arcagent Ed25519, arcteam HMAC chain |
| **SI-10** | System Integrity | Information Input Validation | arcrun JSON Schema validation, arcagent context sanitization |
| **SI-11** | System Integrity | Error Handling | arcllm error truncation, arcrun error sanitization |

### Gaps Requiring Implementation

| Control | Family | Description | Required Addition |
|---------|--------|-------------|-------------------|
| **AC-2** | Access Control | Account Management | Agent identity lifecycle (creation, modification, revocation, review) |
| **AC-4** | Access Control | Information Flow Enforcement | Classification-aware data flow controls |
| **AC-17** | Access Control | Remote Access | mTLS on NATS when network transport is used |
| **AU-6** | Audit | Audit Review/Analysis | Behavioral anomaly detection on audit stream |
| **AU-10** | Audit | Non-repudiation | Message-level Ed25519 signing in arcteam |
| **CA-7** | Security Assessment | Continuous Monitoring | Behavioral monitoring module in arcagent |
| **IA-3** | Identification | Device Identification | Mutual authentication between agents |
| **IA-8** | Identification | Identification of Non-Org Users | Agent type distinction enforcement (agent vs user) |
| **IR-4** | Incident Response | Incident Handling | Incident response event hooks across all packages |
| **IR-5** | Incident Response | Incident Monitoring | SIEM/SOAR integration points |
| **RA-5** | Risk Assessment | Vulnerability Scanning | SBOM generation, automated dependency scanning in CI |
| **SA-12** | System Acquisition | Supply Chain Protection | Module cryptographic signing, SBOM |
| **SC-5** | System Comms | DoS Protection | Circuit breakers in arcteam, budget enforcement in arcrun |
| **SC-6** | System Comms | Resource Availability | Token/cost budgets in arcrun, body size limits |
| **SC-7** | System Comms | Boundary Protection | Network restriction on code execution |
| **SC-39** | System Comms | Process Isolation | Process-level sandbox (seccomp/landlock) |
| **SI-4** | System Integrity | System Monitoring | Behavioral anomaly detection |
| **SI-7** | System Integrity | Software Integrity | Module signature verification |

---

## 9. Incident Response Integration

Based on the OWASP GenAI IR Guide, the following integration points are needed:

### 9.1 Event Hook System for SIEM/SOAR

**Purpose:** Enable all ARC packages to emit events in a format consumable by SIEM/SOAR platforms (Splunk, Elastic, Sentinel).

**Config additions (per package):**

```toml
[incident_response]
enabled = false

# SIEM export format: "cef" (Common Event Format), "leef" (Log Event Extended Format), "json"
export_format = "json"

# Export destination: "file" (local JSONL), "syslog" (RFC 5424), "http" (webhook)
export_destination = "file"
export_path = "/var/log/arc/events.jsonl"

# Severity mapping overrides
[incident_response.severity_overrides]
"tool.denied" = "medium"
"audit.chain_invalid" = "critical"
"budget.exceeded" = "high"
```

### 9.2 Severity Classification

Based on the GenAI IR Guide's 5-dimension severity matrix:

| Event Type | Functionality | Data/IP | Operational | Reputation | Default Severity |
|------------|--------------|---------|-------------|------------|-----------------|
| `tool.denied` | Low | None | None | None | LOW |
| `audit.chain_invalid` | None | High | High | High | CRITICAL |
| `budget.exceeded` | Medium | None | Medium | None | MEDIUM |
| `sandbox.violation` | Low | Medium | Low | Medium | MEDIUM |
| `identity.revoked` | High | Medium | High | High | CRITICAL |
| `signature.invalid` | None | High | Medium | High | HIGH |
| `anomaly.detected` | Medium | Medium | Medium | Medium | HIGH |
| `pii.detected_outbound` | None | High | None | High | HIGH |
| `rce.attempted` | High | High | High | Critical | CRITICAL |

### 9.3 Incident Response Playbook Hooks

**Files to create:**
- `arcagent/modules/incident_response/` -- IR module that:
  - Subscribes to high-severity events
  - Triggers automated containment (e.g., suspend agent, close circuit breakers)
  - Emits IR-specific events for SIEM correlation
  - Provides CLI commands for manual IR actions

---

## 10. Red Team Framework

Based on the OWASP GenAI Solutions Guide's Red Teaming Framework:

### 10.1 Attack Surface Categories to Test

| Category | ARC Component | Test Approach |
|----------|--------------|---------------|
| **Prompt Injection** | arcllm, arcagent | Inject instructions in user messages, tool results, memory content |
| **Tool Exploitation** | arcrun, arcagent | Call tools with malicious parameters, attempt unauthorized tools |
| **Identity Spoofing** | arcagent, arcteam | Forge messages from other agents, attempt registration as existing entity |
| **Memory Poisoning** | arcagent | Write adversarial content to context.md, notes, memory |
| **Goal Hijacking** | arcrun | Inject steering messages, manipulate system prompt via tool results |
| **Resource Exhaustion** | arcllm, arcrun | Trigger infinite loops, unbounded token consumption, spawn storms |
| **Privilege Escalation** | arcagent, arcteam | Attempt to access tools/channels outside allowlist |
| **Data Exfiltration** | arcrun | Use code execution to exfiltrate environment variables, files, network |
| **Cascading Failure** | arcteam | Overwhelm messaging, trigger circuit breakers, corrupt audit chain |
| **Supply Chain** | arcagent | Attempt to load unsigned modules, inject malicious extensions |

### 10.2 Test Infrastructure Needed

**Files to create:**
- `tests/security/red_team/` -- Red team test suite
  - `test_prompt_injection.py` -- Injection attempts across all message roles
  - `test_tool_exploitation.py` -- Malicious tool parameters, schema bypass attempts
  - `test_identity_spoofing.py` -- Forged messages, duplicate registration
  - `test_memory_poisoning.py` -- Adversarial writes to memory/context
  - `test_resource_exhaustion.py` -- Budget enforcement, timeout enforcement
  - `test_privilege_escalation.py` -- Unauthorized tool/channel access
  - `test_data_exfiltration.py` -- Environment/file/network access from sandbox
  - `test_supply_chain.py` -- Unsigned module loading, malicious extensions

---

## Summary: Priority Order

### Phase 1 (Critical for Federal MVP)

1. **Token/cost budget enforcement** in arcrun (modify `react.py`, `state.py`)
2. **Human-in-the-loop approval gates** in arcagent (new module)
3. **Output content filtering** in arcagent (new module)
4. **Message-level Ed25519 signing** in arcteam (modify + new files)
5. **Module signature verification** in arcagent (modify `module_loader.py`)
6. **SBOM generation** in all packages (scripts + CI)
7. **Structured error codes** across all packages

### Phase 2 (Required for FedRAMP Authorization)

8. **Process-level sandbox** (namespace isolation) in arcrun
9. **Behavioral anomaly detection** in arcagent
10. **Identity revocation** in arcagent + arcteam
11. **Replay protection** in arcteam
12. **Circuit breakers** in arcteam
13. **Network restriction** on code execution in arcrun
14. **Per-tool rate limiting** in arcrun
15. **Classification-aware data flow** (cross-cutting)

### Phase 3 (Full NIST 800-53 Compliance)

16. **Incident response integration** (SIEM/SOAR hooks)
17. **Data poisoning validation** in arcagent
18. **Vector store access control** in arcagent
19. **Confidence scoring** in arcagent
20. **Multi-party authorization** in arcteam
21. **Extension process isolation** (seccomp/landlock) in arcagent
22. **Red team test suite**
23. **Event data PII redaction** in arcrun
24. **ECDSA-P256 signing completion** in arcllm
25. **OpenTelemetry integration** in arcteam

---

## Appendix: Config Reference (All New Options)

### arcllm/config.toml additions

```toml
[modules.output_filter]
enabled = false
classification_levels = ["CUI", "FOUO", "SECRET", "TOP_SECRET"]
action = "flag"
custom_patterns = []
confidence_threshold = 0.8
nlp_pii_enabled = false
grounding_check_enabled = false

[modules.budget]
enabled = false
session_token_limit = 0
session_cost_limit = 0.0
action = "soft_block"
warning_threshold = 0.8
```

### arcrun parameter additions

```python
# run() / run_async() new parameters
token_budget: int | None = None
cost_budget: float | None = None
budget_action: Literal["warn", "stop"] = "stop"
budget_warning_threshold: float = 0.8

# make_execute_tool() new parameters
sandbox_level: Literal["basic", "namespace", "firecracker"] = "basic"
network_enabled: bool = False
filesystem_readonly: bool = False
max_memory_mb: int = 256
max_processes: int = 10

# Tool dataclass additions
rate_limit: ToolRateLimit | None = None
sanitize: Callable[[dict[str, Any]], dict[str, Any]] | None = None
```

### arcagent/arcagent.toml additions

```toml
[modules.approval]
enabled = false
require_approval = ["bash", "write"]
timeout_seconds = 300
channel = "cli"
auto_approve_non_interactive = false
batch_approval = false
max_approvals_per_hour = 50
cooldown_minutes = 15

[modules.output_guard]
enabled = false
environment_classification = "UNCLASSIFIED"
block_above_classification = true
pii_detection_mode = "regex"
custom_pii_patterns = []
cui_categories = ["CTI", "PRVCY", "PROPIN", "ITAR"]
action = "redact"

[modules.behavioral_monitor]
enabled = false
baseline_window = 20
max_tool_calls_per_turn = 10
max_consecutive_failures = 5
max_unique_tools_per_turn = 5
flag_off_profile_tools = true
flag_persona_drift = false
action = "log"
alert_cooldown_seconds = 300

[modules.data_validation]
enabled = false
validate_memory_writes = true
max_entropy_threshold = 4.5
injection_pattern_check = true
checksum_verification = false
checksum_manifest = ""

[modules.confidence]
enabled = false
require_attribution = false
llm_confidence_check = false
min_confidence = 0.7
action = "flag"

[modules._loader]
require_signatures = false
signature_public_key = ""
signature_manifest = ""
missing_signature_action = "warn"

[identity]
revocation_list = ""
check_revocation_on_start = true
revocation_check_interval = 0

[extensions]
process_isolation = false
seccomp_profile = ""
landlock_enabled = false
max_memory_mb = 128
max_cpu_seconds = 30
max_file_descriptors = 64

[security]
environment_classification = "UNCLASSIFIED"
enforce_classification_flow = false
log_classification_crossings = true

[incident_response]
enabled = false
export_format = "json"
export_destination = "file"
export_path = "/var/log/arc/events.jsonl"
```

### arcteam config additions

```toml
[team]
require_message_signatures = false
invalid_signature_action = "dlq"
public_key_registry = ""
auto_register_keys = true
replay_protection = false
nonce_window_seconds = 300
nonce_storage = "memory"
circuit_breaker_enabled = false
failure_threshold = 5
failure_window_seconds = 60
recovery_timeout_seconds = 30
multi_party_operations = ["entity.remove", "channel.delete", "config.modify"]
min_approvers = 2
approval_timeout_seconds = 600

[team.telemetry]
enabled = false
service_name = "arcteam"
exporter = "otlp"
endpoint = "http://localhost:4317"
protocol = "grpc"
sample_rate = 1.0
```
