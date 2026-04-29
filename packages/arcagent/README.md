<div align="center">

# 🤖 arcagent

### **The Agent Itself**
*DID-required at construction. Unified capability loader (tools · skills · hooks · background tasks), sessions, module bus. Wraps arcrun + arcllm with everything an autonomous agent needs to be accountable.*

[![License: Apache 2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](https://opensource.org/licenses/Apache-2.0)
[![Tests](https://img.shields.io/badge/tests-3%2C136%2B-success.svg)](#status)
[![Coverage](https://img.shields.io/badge/core_coverage-≥90%25-brightgreen.svg)](#status)
[![Strict mypy](https://img.shields.io/badge/mypy-strict-2563EB.svg)](#status)
[![DID Required](https://img.shields.io/badge/DID-required-DC2626.svg)](#-the-four-pillars-built-in)

</div>

---

## ✨ What is arcagent?

`arcagent` is the agent. It's the thing that has an identity, a memory, a workspace, and a personality.

It wraps the lower layers (`arcrun` for the loop, `arcllm` for the model, `arctrust` for identity/audit/policy) with everything an autonomous agent needs to be more than a chatbot:

- 🪪 **Cryptographic identity** — refuses to start without a valid DID
- 🧩 **Unified capability system (SPEC-021)** — one loader discovers `@tool` / `@hook` / `@background_task` / `@capability`-class Python files and `SKILL.md` folders across four scan roots with explicit precedence
- 💾 **Persistent sessions** — JSONL transcripts of every conversation
- 🚌 **Module bus** — priority-ordered event handlers with veto power
- 🪟 **Progressive context management** — observation masking + emergency truncation
- ⚙️ **TOML configuration** — one file, full surface area, validated by Pydantic

> 🛡️ **Identity required. Tools deny-by-default. Every action audited. Sessions on disk you can read.**

---

## 🏗️ Where It Fits

```mermaid
flowchart TB
    classDef ag fill:#A78BFA,stroke:#5B21B6,color:#2E1065
    classDef rn fill:#34D399,stroke:#065F46,color:#022C22
    classDef llm fill:#22D3EE,stroke:#0E7490,color:#083344
    classDef sk fill:#60A5FA,stroke:#1E40AF,color:#0C1A47
    classDef tr fill:#94A3B8,stroke:#1E293B,color:#0F172A
    classDef other fill:#E5E7EB,stroke:#6B7280,color:#111827

    arccli[arccli]:::other --> arcagent
    arcgateway[arcgateway]:::other --> arcagent
    arcagent[arcagent<br/>identity · skills · extensions · sessions]:::ag --> arcskill[arcskill]:::sk
    arcagent --> arcrun[arcrun]:::rn
    arcagent --> arcllm[arcllm]:::llm
    arcagent --> arctrust[arctrust]:::tr
```

Depends on `arcrun`, `arcllm`, `arcskill`, `arctrust`.

---

## 🚀 Install

```bash
pip install arc-agent          # full agent stack
# or
pip install arcmas             # everything (includes CLI, dashboard, etc.)
```

---

## 🎬 Five-Minute Quickstart

```bash
# 1. Set up Arc once (interactive: tier, provider, API key)
arc init

# 2. Create an agent
arc agent create my-agent --model anthropic/claude-sonnet-4-5-20250929

# 3. Validate
arc agent build my-agent --check

# 4. Talk to it
arc agent chat my-agent
```

Or one-shot:

```bash
arc agent run my-agent "Summarize the CSVs in workspace/data/"
```

What `arc agent create` scaffolds:

```
my-agent/
├── arcagent.toml            # config (TOML, Pydantic-validated)
├── identity.md              # the agent's identity card (immutable to the agent)
├── capabilities/            # per-agent capabilities (.py + SKILL.md folders) — trusted
└── workspace/
    ├── .capabilities/       # agent-authored capabilities — UNTRUSTED, AST-validated
    └── sessions/            # JSONL transcripts of past conversations
```

> ℹ️ The runtime no longer reads `workspace/extensions/` or `workspace/skills/`. Capabilities now live under `<agent>/capabilities/` (operator-curated) or `<workspace>/.capabilities/` (agent-authored). See [Capabilities & Loading](#-capabilities--loading) below.

A fresh Ed25519 keypair is generated. The public DID is written into `arcagent.toml`. **Without that DID, the agent will refuse to start.**

---

## 🧪 Quick Example (Python API)

```python
from arcagent.core.agent import ArcAgent
from arcagent.core.config import load_config

config = load_config("my-agent/arcagent.toml")
agent = ArcAgent(config, config_path="my-agent/arcagent.toml")

await agent.startup()

result = await agent.run("Summarize the files in workspace/reports/")
print(result.content)
print(f"{result.turns} turns · ${result.cost_usd:.4f} · {result.tokens_used} tokens")

# Multi-turn chat
reply = await agent.chat("Now extract the action items.")
print(reply.content)

await agent.shutdown()
```

---

## 🏛️ The Four Pillars (Built In)

`arcagent` enforces all four guarantees automatically:

| Pillar | How It Shows Up |
|---|---|
| 🪪 **Identity** | `ArcAgent.__init__` refuses construction without a resolvable DID. Identity loaded from `[identity].did` in TOML; keypair from `key_dir`. Hard error on missing or wrong-permission keyfile |
| ✍️ **Sign** | Skills, extensions, and pairings are verified before loading. No "skip for testing" backdoors |
| ✅ **Authorize** | Every tool call goes through the 5-layer policy pipeline (`arctrust.policy`). First DENY wins. Fail-closed |
| 📜 **Audit** | Every operation emits an `arctrust.AuditEvent`. Sinks fan out: JSONL for compliance, hash-chained for tamper-evidence, WebSocket for live dashboards |

---

## ⚙️ Configuration: `arcagent.toml`

```toml
[agent]
name = "my-agent"
org = "acme"
type = "executor"
workspace = "./workspace"

[llm]
model = "anthropic/claude-sonnet-4-5-20250929"
max_tokens = 8192
temperature = 0.7

[identity]
did = "did:arc:acme:executor/abc123..."   # filled by `arc agent create`
key_dir = "~/.arcagent/keys"

[vault]
backend = ""                              # vault URL, or empty for env var fallback

[tools.policy]
allow = ["read_file", "write_file", "execute_python"]
deny = []
timeout_seconds = 30

[telemetry]
enabled = true
service_name = "my-agent"
log_level = "INFO"
export_traces = false                     # OpenTelemetry export

[context]
max_tokens = 128000

[session]
retention_count = 50
retention_days = 30

[security]
tier = "personal"                         # "personal" | "enterprise" | "federal"

[security.validators]
auto_run_agent_code = false               # personal-only escape hatch; federal/enterprise require TOFU

# [[security.validators.approved]]        # appended by `arc trust approve` — never hand-edited
# name = "my-validator"
# hash = "sha256:..."
# approver = "ops@acme"
# timestamp = "2026-04-29T12:00:00Z"

[modules.memory]
enabled = true

[modules.policy]
enabled = true

[modules.ui_reporter]
enabled = false                           # set true with `arc agent serve --ui`
```

**Three rules to know:**
- 🛑 The agent **refuses to start** without a valid DID under `[identity]`.
- 🛑 The tool allowlist is **deny-by-default**. Anything not in `allow` cannot be called.
- 🛑 These config paths **cannot be overridden by environment variables**: `vault.backend`, `tools.native`, `tools.process`, `identity.key_dir`. They must be set in this file. Prevents runtime injection.

---

## 🧩 Capabilities & Loading

A **capability** is anything the loader picks up from one of four scan roots — a `.py` file with a decorated callable or class, or a folder containing a `SKILL.md`. There are four decorators (`@tool`, `@hook`, `@background_task`, and the `@capability` class form for resources that need `setup()` / `teardown()`).

### Scan roots & precedence (last-wins)

`CapabilityLoader` walks these in order. Later roots **override earlier ones by name** for tools, skills, and `@capability` classes; hooks fan out (all keep firing in priority order); background tasks drain-then-replace.

| # | Root | Trust | Who writes it | What goes here |
|---|------|-------|---------------|----------------|
| 1 | `arcagent/builtins/capabilities/` | trusted | ships with the package | `bash`, `read`, `write`, `edit`, `find`, `grep`, `ls`, `reload`, plus the self-mod tools (`create_tool`, `create_skill`, `update_tool`, `update_skill`) and the 4 self-mod skill folders |
| 2 | `~/.arc/capabilities/` | trusted | the human operator | global, opt-in capabilities shared across every agent |
| 3 | `<agent_root>/capabilities/` | trusted | the human operator | per-agent capabilities and skill folders |
| 4 | `<agent_root>/workspace/.capabilities/` | **UNTRUSTED** | the agent itself, at runtime | passes through the AST validator + (future) TOFU + OS sandbox before being imported |

> Override by collision: define `web_search` (a `@tool`) at root 2, then again at root 3, and root 3 wins. The reload diff names it explicitly: `~1 replaced (web_search 1.0.0→1.1.0)`.

Plus: any module in `arcagent/modules/<mod>/` that has `[modules.<mod>].enabled = true` and a `capabilities.py` is loaded as an extra scan root (`module:<mod>`). Disabled modules are silently skipped.

### Skill folders

Skills are folders with a `SKILL.md` plus optional `references/`, `scripts/`, and `templates/`. The frontmatter's `name`, `description`, and `triggers` show up in the system prompt; the body is read lazily via the built-in `read` tool when the model decides to use it.

```
my-agent/capabilities/data-analysis/
├── SKILL.md                 # frontmatter + when-to-use + steps
├── references/
│   └── outlier-detection.md
└── scripts/
    └── summarize.py
```

```markdown
---
name: data-analysis
version: 1.0.0
description: Analyze CSV files for trends, anomalies, and summaries.
triggers: [csv, analysis, anomaly]
required_tools: [read, bash]
---

# When to use
…
```

### Decorator examples

```python
# my-agent/capabilities/web_search.py
from arcagent.tools._decorator import tool, hook, background_task, capability

@tool(
    name="web_search",
    description="Search the web via the configured engine.",
    classification="read_only",
    when_to_use="When the user needs current information from the public web.",
    version="1.0.0",
)
async def web_search(query: str) -> str:
    ...

@hook(name="log_tool_calls", event="tool:invoked", priority=200)
async def log_tool_calls(ctx) -> None:
    ...

@background_task(name="poll_inbox", interval=60.0)
async def poll_inbox() -> None:
    ...

@capability(name="browser", depends_on=())
class BrowserCapability:
    async def setup(self, ctx) -> None: ...
    async def teardown(self) -> None: ...
```

### Reload diff

`/reload` (REPL) and `arc agent reload` re-walk all four roots and emit a single-line diff:

```
reload: +2 added (web_search, log_tool_calls), ~1 replaced (data-analysis 1.0.0→1.1.0), -1 removed (legacy_scraper), 0 errors
```

Errors append one indented line per failure (`<path>: <short reason>`), and each emits `capability:registration_failed` on the bus + an audit event.

### Trust tiers in this loader

Only the workspace root is treated as untrusted code. Everything in builtins / global / per-agent is assumed operator-curated and skips the AST validator. The workspace root goes through:

1. **Source encoding check** — non-UTF-8 coding declarations rejected before the parser runs
2. **AST validator** — rejects 9 bypass categories (privileged imports, frame traversal, `eval`/`exec`/`compile`/`__import__`, `sys.modules` subscription, `__builtins__` mutation, `__init_subclass__`, starred `__builtins__` unpacking)
3. **Restricted builtins** — execute with a scrubbed `__builtins__` (36 safe names; no `open`/`eval`/`exec`/`compile`/`__import__`)
4. **Egress proxy** — network only via `ToolContext.http` with per-tool origin allowlist

Tier policy still composes on top: federal refuses agent-authored capabilities outright; enterprise requires TOFU approval recorded under `[security.validators.approved]`; personal allows them with `auto_run_agent_code = true`.

### Verified install (optional)

For supply-chain-secure third-party skill installs (Sigstore + Rekor + static scan + sandboxed dry-run), enable the skill hub:

```toml
[skills.hub]
enabled = true
```

Verified skills land under `~/.arc/capabilities/` (root 2) just like operator-installed ones.

---

## 💾 Sessions

Every conversation persists as a JSONL transcript:

```
my-agent/workspace/sessions/
├── 2026-04-28T14-30-00-abc123.jsonl
├── 2026-04-28T15-12-44-def456.jsonl
└── ...
```

Each line is one event: a user message, a model response, a tool call, a tool result, a turn boundary. Open the file in any text editor — there's no opaque format.

```bash
arc agent sessions my-agent                              # list with timestamps + sizes
arc agent chat my-agent --session-id abc123              # resume a session
```

In-chat: `/sessions`, `/switch <id>`.

Retention is configurable:

```toml
[session]
retention_count = 50
retention_days = 30
```

---

## 🚌 The Module Bus

Inside `arcagent`, every event flows through a priority-ordered module bus. Modules can observe events and **veto** actions (e.g., deny a tool call).

| Priority | Default Modules | Purpose |
|---|---|---|
| **10** | policy | Hardest gate — first to see, last to fail. Veto here = call denied |
| **50** | security | PII detection, classification checks |
| **100** | memory, default handlers | Standard processing |
| **200** | ui_reporter, logging | Observation only |

**Same-priority handlers run concurrently. Cross-priority groups run sequentially.**

Crucially: even if priority 10 vetoes the call, **all other modules still execute** so the audit record is complete. You see *what would have happened* + *who blocked it* + *why*.

Toggle modules in `arcagent.toml`:

```toml
[modules.memory]
enabled = true

[modules.policy]
enabled = true

[modules.ui_reporter]
enabled = true                            # streams events to the arcui dashboard
```

---

## 🪟 Progressive Context Management

Context window usage is managed in three tiers automatically:

| Threshold | Action |
|---|---|
| **< 70%** | No action |
| **70–95%** | **Observation masking** — old tool outputs replaced with `[output pruned]` placeholders |
| **> 95%** | **Emergency truncation** |

Recent messages are always protected within a 40% window so the model never loses immediate context.

---

## 📟 CLI Commands

```bash
# Lifecycle
arc agent create my-agent --model anthropic/claude-sonnet-4-5-20250929
arc agent build my-agent --check          # validate (always pass --check)
arc agent reload my-agent                 # re-walk all four capability scan roots

# Run
arc agent chat my-agent                   # interactive REPL
arc agent run my-agent "task"             # one-shot
arc agent serve my-agent                  # long-running daemon
arc agent serve my-agent --ui             # daemon + push events to dashboard

# Inspect
arc agent status my-agent                 # DID, model, counts
arc agent config my-agent --json          # full parsed config
arc agent tools my-agent                  # what tools it can call
arc agent skills my-agent                 # discovered skills
arc agent extensions my-agent             # loaded extensions
arc agent sessions my-agent               # past transcripts
arc agent strategies                      # available strategies (react, code)
arc agent events                          # all event types it emits
```

In-chat REPL: `/help`, `/quit`, `/tools`, `/model`, `/cost`, `/reload`, `/skills`, `/extensions`, `/session`, `/sessions`, `/switch <id>`, `/identity`, `/status`.

---

## 🛡️ Security Architecture

### Self-Improving Policy Engine

Every N turns, a reflector model critiques the agent's behavior. Good behaviors score up, harmful patterns score down. Bullets that drop below score 2 are automatically removed. The policy file is atomically written via tmp+rename, capped at 200 rules, and sorted by effectiveness. Every change is audited.

This is the ACE framework (arXiv:2510.04618).

### Dynamic Tool Safety (Four Defense Layers)

The four defenses gate the **untrusted scan root only** — `<workspace>/.capabilities/`, where the agent itself can write Python. Builtins, global, and per-agent roots are operator-curated and skip the AST gate.

1. **Source encoding check** — reject non-UTF-8 coding declarations (codec attacks lose **before** the AST parser).
2. **AST validator** — rejects 9 bypass categories: privileged imports (`os`, `ctypes`, `subprocess`, `pickle`, `sys`...), frame traversal (`gi_frame`, `f_back`, `__subclasses__`), dynamic execution (`eval`, `exec`, `compile`, `__import__`), `sys.modules` subscription, `__builtins__` assignment, `__init_subclass__` definitions, starred `__builtins__` unpacking. References real CVEs.
3. **Restricted builtins** — execute with a scrubbed `__builtins__` dict (36 safe names). `__import__`, `eval`, `exec`, `compile`, `open` are deliberately **not** present.
4. **Egress proxy** — network only via `ToolContext.http`, with per-tool origin allowlist (scheme + host + port). Deny-by-default. Every request audit-logged.

Tier gates: Federal refuses agent-authored capabilities entirely. Enterprise allows them only after a human records approval via `arc trust approve` (persisted under `[security.validators.approved]`). Personal allows them with `[security.validators] auto_run_agent_code = true`.

---

## 🧱 Public API

```python
from arcagent import (
    # Errors
    ArcAgentError, ConfigError, IdentityError, IdentityRequired,
    ToolError, ToolVetoedError, ContextError, ModuleBusError,
)

from arcagent.core import (
    ArcAgent,                 # the main agent class
    ArcAgentConfig,           # Pydantic config model
)

from arcagent.core.config import load_config
```

Every operation emits arctrust audit events.

---

## 📋 Compliance Mapping

| NIST 800-53 | What `arcagent` Provides |
|---|---|
| AC-3 | Tool allowlist deny-by-default |
| AC-6 | Per-agent allowlist + extension sandbox modes |
| AU-2, AU-3, AU-12 | Module bus emits structured audit events on every operation |
| CM-5 | Federal tier refuses dynamic tool/extension creation |
| CM-7 | Tools opt-in; sandbox modes; minimal context window |
| IA-3 | DID required at construction; refuses to start without one |
| SC-28 | Sessions persist with normal file permissions; keys with 0600 |

| OWASP Agentic | Mitigation |
|---|---|
| ASI01 (Goal Hijack) | `identity.md` is read-only to the agent; policy engine enforces boundaries |
| ASI02 (Tool Misuse) | 5-layer policy pipeline; parameter validation; full audit |
| ASI03 (Identity Abuse) | Per-agent DID required; HKDF child identities for spawned subagents |
| ASI05 (RCE) | 4-layer dynamic tool defense; sandboxed extensions; restricted builtins |
| ASI06 (Memory/Context Poisoning) | Workspace boundary; observation masking; protected recent-message window |

---

## 🧪 Status

```bash
uv run --no-sync pytest packages/arcagent/tests
```

- **Tests:** 3,136+
- **Coverage:** core components ≥ 90%
- **Type check:** `mypy --strict` (active cleanup in progress)
- **Lint:** `ruff check`

---

## 📄 License

Apache 2.0 · Copyright © 2025-2026 BlackArc Systems.
