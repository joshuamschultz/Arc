<div align="center">

# 🎁 arcmas

### **The Whole Arc Stack — One pip Install**
*The meta-package. `pip install arcmas` and you have everything.*

[![License: Apache 2.0](https://img.shields.io/badge/License-Apache_2.0-002550.svg)](https://opensource.org/licenses/Apache-2.0)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-0073FE.svg?logo=python&logoColor=white)](https://www.python.org/downloads/)
[![Providers](https://img.shields.io/badge/LLM_providers-16-F68D2E.svg)](#)
[![Zero SDKs](https://img.shields.io/badge/vendor_SDKs-zero-54585C.svg)](#)
[![SCIF Ready](https://img.shields.io/badge/SCIF-ready-002550.svg)](#)

</div>

---

## ✨ What is Arc?

Arc is a **security-first autonomous agent framework** built for environments where audit trails, cryptographic identity, and data sovereignty are non-negotiable.

Every LLM call is attributable. Every tool invocation is authorized. Every action emits a tamper-evident audit event. No vendor SDKs anywhere in the dependency tree.

> 🛡️ **One install. Full stack. CLI ready. Production-grade out of the box.**

```bash
pip install arcmas
```

---

## 🏛️ The Four Pillars

Every Arc agent operates under four guarantees at all tiers (Personal, Enterprise, Federal):

1. **🪪 Identity** — each agent carries a unique Ed25519 DID (`did:arc:{org}:{type}/{hash}`)
2. **✍️ Sign** — every pairing and delegation is cryptographically signed
3. **✅ Authorize** — all tool calls pass through a deny-by-default policy pipeline
4. **📜 Audit** — every operation emits a structured, hash-chained audit event

The deployment tier only changes how *strict* the verification is — not whether it happens.

---

## 📦 What's in the Box

`arcmas` declares two direct dependencies — `arccmd` (the `arc` CLI) and `arcmemory`. The rest of
the **core runtime stack** arrives transitively through the CLI's dependency graph. This is the full
set a single `pip install arcmas` resolves:

| Package | What It Does |
|---------|---------|
| 🪪 [**arctrust**](../arctrust/) | Cryptographic foundation — Ed25519 keypairs, DID identity, audit emission, policy pipeline |
| 🌐 [**arcllm**](../arcllm/) | LLM providers via direct HTTP — no vendor SDKs |
| 🗄️ [**arcstore**](../arcstore/) | The shared record every surface reads from |
| ✍️ [**arcprompt**](../arcprompt/) | Signed, overlay-able system prompts |
| ⚙️ [**arcrun**](../arcrun/) | Async think → act → observe loop — tool sandbox, streaming, parallel dispatch |
| 🤖 [**arcagent**](../arcagent/) *(dist `arc-agent`)* | The agent — DID-required, skills, extensions, persistent sessions, module bus |
| 🧠 [**arcmemory**](../arcmemory/) | Dual-speed analogical memory — daily-log journal, episodic index, entity graph; the scaffold-default Brain |
| 📦 [**arcbundle**](../arcbundle/) | Signed config-only distribution units (blueprints) |
| 🤝 [**arcteam**](../arcteam/) | Multi-agent messaging — entity registry, channels, operator-key-signed audit |
| ⌨️ [**arccli**](../arccli/) *(dist `arccmd`)* | Unified `arc` command-line tool |

### Surfaces you add separately

These ship as their own packages and are **not** pulled in by `arcmas`. Install the ones you need:

| Package | Install | What It Adds |
|---------|---------|--------------|
| 📡 [**arcgateway**](../arcgateway/) | `pip install 'arcgateway[telegram]'` | Multi-platform daemon — Telegram, Slack, Mattermost with operator-approved pairing |
| 📊 [**arcui**](../arcui/) | `pip install arcui` | Real-time dashboard — reads on demand from the shared arcstore record, two-token auth |
| 🔧 [**arcskill**](../arcskill/) | `pip install arcskill` | Verified skill install (Sigstore + Rekor), scan, lock, CRL lifecycle, plus optional self-improvement (`arcskill.improver`) |

The `arc gateway`, `arc ui`, and `arc skill` command groups are present in the CLI but need their
package installed before they can run.

---

## ⭐ Top Features

What makes Arc uniquely suited for production autonomous agent deployments:

### **Security-First Architecture**
- **DID-required identity** — Every agent must have a valid `did:arc:{org}:{type}/{hash}` identity; no anonymous operations allowed
- **Zero vendor SDKs** — All 17 LLM providers accessed via direct HTTP; minimal dependency tree for easier auditing
- **Deny-by-default policy** — All tool calls pass through `PolicyPipeline`; nothing executes without explicit permission
- **Hash-chained audit trails** — Every operation emits tamper-evident events; `verify_chain()` detects any modification

### **Production-Grade Reliability**
- **2300+ tests across core packages** — 90%+ coverage on nucleus packages; battle-tested before production use
- **Tier-aware deployment** — Personal, Enterprise, Federal tiers with increasing security strictness; same code, different enforcement
- **Cache-preserving context** — Append-only turns keep provider prompt cache warm; saves 50-80% on token costs for long sessions

### **Developer Experience**
- **One-command install** — `pip install arcmas` gives you the full stack; CLI ready immediately
- **Glass-box memory** — All memories stored as markdown files; inspectable, editable, git-friendly
- **Mid-execution steering** — Inject messages, cancel tasks, or follow-up at turn boundaries with full audit trail

### **The Four Pillars (Built-In)**
- **Identity** — Ed25519 keypairs and DIDs (`did:arc:{org}:{type}/{hash}`); cryptographic proof of agent identity
- **Sign** — Every pairing, delegation, and audit event cryptographically signed; tamper evidence
- **Authorize** — Deny-by-default policy pipeline; all tool calls must be explicitly allowed
- **Audit** — Structured, hash-chained events written to durable WORM store; operator-signed chains

### **Tier System**
- **Personal tier** — Local development with host-fallback sandboxing; audit warnings for relaxed security
- **Enterprise tier** — Docker isolation, operator approval for new capabilities, signed audit chains
- **Federal tier** — Firecracker micro-VMs, refuses unsigned code, FIPS-compliant crypto enforcement

### **Multi-Agent Coordination**
- **Relevance-triaged messaging** — Questions routed to the agent that actually holds the answer
- **ArcFlow workflows** — Signed, deterministic DAGs for complex multi-agent tasks
- **Operator-gated approvals** — HITL gates for tool calls, skill installs, and sensitive operations

---

## 🚀 Install

### Full Stack

```bash
pip install arcmas
# or
uv pip install arcmas
```

### Just the Layers You Need

```bash
pip install arctrust       # identity + audit + policy only
pip install arcllm         # LLM client only
pip install arcrun         # arcllm + the agent loop
pip install arc-agent      # full agent (arcrun + arcllm + arctrust)
```

### From Source

```bash
git clone https://github.com/joshuamschultz/Arc.git
cd Arc
uv sync --all-packages
```

---

## 🎬 Five-Minute Quickstart

```bash
# 1. Interactive setup (tier, provider, API key)
arc init

# 2. Create an agent
arc agent create my-agent --model anthropic/claude-sonnet-4-5-20250929

# 3. Validate
arc agent build my-agent --check

# 4. Talk to it
arc agent chat my-agent

# 5. (Optional) watch in a browser — needs `pip install arcui`
arc ui start --show-tokens          # reads agent activity from arcstore on demand
```

What `arc agent create` scaffolds:

```
my-agent/
├── arcagent.toml          # config
├── identity.md            # the agent's identity card
├── capabilities/          # per-agent capabilities (.py + SKILL.md folders) — trusted
└── workspace/
    ├── .capabilities/     # agent-authored capabilities — UNTRUSTED, AST-validated
    └── sessions/          # JSONL transcripts
```

A fresh Ed25519 keypair is generated. The DID is written into `arcagent.toml`. **Without that DID, the agent refuses to start.**

---

## 🎚️ Tier Presets

| Tier | Telemetry | Audit | Retry | Fallback | OpenTelemetry | PII redaction + signing |
|---|---|---|---|---|---|---|
| `personal` | off | off | off | off | off | off |
| `enterprise` | ✅ | ✅ | ✅ (3x) | ✅ | off | off |
| `federal` | ✅ | ✅ | ✅ (3x) | ✅ | ✅ (OTLP) | ✅ |

Non-interactive:

```bash
arc init --tier enterprise --provider anthropic
```

---

## ✨ Key Features

| Feature | What It Means |
|---|---|
| 🔌 **Air-gap ready** | Ollama, vLLM, HuggingFace TGI work with no internet, no API keys |
| 🔐 **Vault-backed secrets** | API keys never touch the filesystem; TTL-cached resolution |
| 🛑 **Deny-by-default tools** | Tools must be explicitly allowlisted; parameter-level policy on every call |
| 🚫 **Bidirectional PII redaction** | Sensitive data (SSN, credit card, email, phone, IP) redacted before leaving your environment |
| 🪟 **Progressive context management** | Observation masking at 70%, emergency truncation at 95% |
| 📊 **Multi-agent dashboard** | `arc ui start` launches a real-time dashboard with WebSocket streaming |
| ⚡ **Parallel tool dispatch** | Multiple tool calls in one turn run concurrently |
| 🔄 **Mid-execution steering** | Inject messages, follow up at end-of-turn, cancel cooperatively |
| 🎚️ **Federal-ready** | FedRAMP, NIST 800-53, CMMC compliance mapping built in |
| 🌐 **16 LLM providers** | Direct HTTP, zero SDK imports |

---

## 📋 Compliance

Arc maps directly to the control families federal programs require:

| Framework | Where It's Implemented |
|---|---|
| **NIST 800-53** | AC-3, AC-4, AC-6, AU-2/3/5/8/9/12, CM-5/7/8, IA-3/5, SC-8/12/13/28, SI-4/7/10/11 |
| **FedRAMP** | Continuous monitoring (SI-4), boundary enforcement, audit trail integrity |
| **CMMC** | Controlled access, incident response, system integrity monitoring |
| **OWASP LLM Top 10 (2025)** | Mitigations for all 10 categories |
| **OWASP Agentic Top 10 (2026)** | Mitigations for all 10 categories |

See the [main README compliance section](../../README.md#-compliance-mapping) for the full control-by-control breakdown.

---

## 📚 CLI Reference

Full reference: [docs/cli.md](../../docs/cli.md).

Top-level command groups:

```bash
arc agent     # agent lifecycle
arc llm       # LLM provider operations
arc run       # arcrun without an agent directory
arc skill     # skill management
arc ext       # extension management
arc team      # team messaging
arc ui        # multi-agent dashboard
arc gateway   # chat-platform pairing
arc init      # tier wizard
```

---

## 📄 License

Apache 2.0 · Copyright © 2025-2026 BlackArc Systems.
