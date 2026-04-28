<div align="center">

# 🎁 arcmas

### **The Whole Arc Stack — One pip Install**
*The meta-package. `pip install arcmas` and you have everything.*

[![License: Apache 2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](https://opensource.org/licenses/Apache-2.0)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-3776AB.svg?logo=python&logoColor=white)](https://www.python.org/downloads/)
[![Providers](https://img.shields.io/badge/LLM_providers-16-orange.svg)](#)
[![Zero SDKs](https://img.shields.io/badge/vendor_SDKs-zero-DC2626.svg)](#)
[![SCIF Ready](https://img.shields.io/badge/SCIF-ready-7C3AED.svg)](#)

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

`pip install arcmas` installs the entire stack:

| Package | What It Does |
|---------|---------|
| 🪪 [**arctrust**](../arctrust/) | Cryptographic foundation — Ed25519 keypairs, DID identity, audit emission, policy pipeline |
| 🌐 [**arcllm**](../arcllm/) | 16 LLM providers via direct HTTP — no SDKs |
| ⚙️ [**arcrun**](../arcrun/) | Async think → act → observe loop — tool sandbox, streaming, parallel dispatch |
| 🤖 [**arcagent**](../arcagent/) | The agent — DID-required, skills, extensions, persistent sessions, module bus |
| 📡 [**arcgateway**](../arcgateway/) | Multi-platform daemon — Telegram, Slack, Discord with operator-approved pairing |
| 🔧 [**arcskill**](../arcskill/) | Verified skill install (Sigstore + Rekor), scan, lock, CRL lifecycle |
| 🤝 [**arcteam**](../arcteam/) | Multi-agent messaging — entity registry, channels, HMAC-signed audit |
| 📊 [**arcui**](../arcui/) | Real-time dashboard — live WebSocket telemetry, three-token auth |
| ⌨️ [**arccli**](../arccli/) | Unified `arc` command-line tool |

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

# 5. (Optional) watch in a browser
arc ui start --show-tokens          # terminal 1
arc agent serve my-agent --ui       # terminal 2
```

What `arc agent create` scaffolds:

```
my-agent/
├── arcagent.toml          # config
├── identity.md            # the agent's identity card
└── workspace/
    ├── extensions/        # Python tools
    ├── skills/            # markdown skills
    └── sessions/          # JSONL transcripts
```

A fresh Ed25519 keypair is generated. The DID is written into `arcagent.toml`. **Without that DID, the agent refuses to start.**

---

## 🎚️ Tier Presets

| Tier | Telemetry | Audit | Retry | Fallback | OpenTelemetry | PII redaction + signing |
|---|---|---|---|---|---|---|
| `open` | off | off | off | off | off | off |
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
