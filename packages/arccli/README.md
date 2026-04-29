<div align="center">

# ⌨️ arccli

### **The `arc` Command-Line Tool**
*Argparse-based. JSON output on every data command. The single front door to the entire Arc stack.*

[![License: Apache 2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](https://opensource.org/licenses/Apache-2.0)
[![Tests](https://img.shields.io/badge/tests-283-success.svg)](#status)
[![Strict mypy](https://img.shields.io/badge/mypy-strict-2563EB.svg)](#status)
[![Pure stdlib argparse](https://img.shields.io/badge/argparse-pure_stdlib-2563EB.svg)](#)

</div>

---

## ✨ What is arccli?

`arccli` is the unified `arc` command-line tool. Every Arc operation — creating an agent, running it, listing tools, inspecting LLM providers, starting the dashboard, approving a chat-platform pairing — is one `arc` subcommand.

It's pure stdlib `argparse`. **No Click, no Typer, no third-party CLI framework.** That's a deliberate choice: fewer moving parts in the trust path.

> ⚡ **Single binary. Argparse plain handlers. `--json` on every data command. CI-friendly by default.**

---

## 🏗️ Where It Fits

```mermaid
flowchart TB
    classDef cli fill:#FCD34D,stroke:#92400E,color:#451A03
    classDef other fill:#E5E7EB,stroke:#6B7280,color:#111827

    User[👤 User]:::other --> arccli
    arccli[arccli<br/>'arc' console script]:::cli --> arcagent[arcagent]:::other
    arccli --> arcrun[arcrun]:::other
    arccli --> arcllm[arcllm]:::other
    arccli --> arcteam[arcteam]:::other
    arccli --> arcui[arcui]:::other
    arccli --> arcskill[arcskill]:::other
    arccli --> arcgateway[arcgateway]:::other
```

`arccli` is a **terminal layer** — nothing in Arc depends on it. It installs the `arc` console script.

---

## 🚀 Install

```bash
pip install arccli              # standalone
# or
pip install arcmas              # full Arc stack (includes arccli)
```

After install, the `arc` command is on your PATH:

```bash
arc help
arc version
```

---

## 🎬 Five-Minute Tour

```bash
# First-time setup (interactive: tier, provider, API key)
arc init

# Create an agent
arc agent create my-agent --model anthropic/claude-sonnet-4-5-20250929

# Validate
arc agent build my-agent --check

# Run
arc agent chat my-agent
arc agent run my-agent "Summarize workspace/data/"
```

---

## 🧱 Command Groups

| Group | Purpose |
|---|---|
| **`arc agent`** | Agent lifecycle — create, build, chat, run, serve, status, tools, skills, extensions, sessions, config, reload |
| **`arc llm`** | LLM provider operations — version, config, providers, provider, models, validate |
| **`arc run`** | arcrun loop without an agent directory — version, exec, task |
| **`arc skill`** | Skill management — list, create, validate, search |
| **`arc ext`** | Extension management — list, create, install, validate |
| **`arc team`** | Team messaging — status, config, init, register, entities, channels, memory-status |
| **`arc ui`** | Multi-agent dashboard — start, tail |
| **`arc gateway pair`** | Gateway pairing operator commands — list, approve, revoke |
| **`arc init`** | Interactive first-time setup wizard with tier presets |
| **`arc help`, `arc version`** | Info and REPL utilities |

`--json` is supported on every data-returning subcommand for CI/CD integration.

---

## 📟 The Cheat Sheet

```bash
# === Setup ===
arc init                                                  # tier wizard
arc init --tier enterprise --provider anthropic           # non-interactive

# === Agents ===
arc agent create my-agent --model anthropic/claude-sonnet-4-5-20250929
arc agent build my-agent --check                          # ALWAYS pass --check
arc agent chat my-agent
arc agent run my-agent "task description"
arc agent serve my-agent                                  # daemon
arc agent serve my-agent --ui                             # daemon + dashboard
arc agent status my-agent
arc agent config my-agent --json
arc agent tools my-agent
arc agent skills my-agent
arc agent extensions my-agent
arc agent sessions my-agent
arc agent reload my-agent                                 # hot-reload
arc agent strategies                                      # available strategies
arc agent events                                          # event types

# === LLM introspection ===
arc llm version
arc llm config
arc llm config --module audit
arc llm providers
arc llm provider anthropic
arc llm models
arc llm models --provider openai
arc llm models --tools
arc llm models --vision
arc llm validate
arc llm validate --provider anthropic

# === Direct runs (no agent dir) ===
arc run version
arc run task "Calculate 2^32" --with-calc --model anthropic/claude-haiku-4-5-20251001
arc run exec --tool calculator --params '{"expression": "2 ** 32"}'

# === Skills (SPEC-021 folder format) ===
arc skill list
arc skill list --agent my-agent
arc skill create data-analysis                                  # creates ./data-analysis/SKILL.md
arc skill create data-analysis --dir my-agent/capabilities      # per-agent (trusted)
arc skill create shared --global                                # ~/.arc/capabilities/shared/
arc skill validate ./data-analysis                              # folder OR ./data-analysis/SKILL.md
arc skill search "data"
arc skill search "report" --agent my-agent

# === Capability files (Python @tool / @hook / @background_task / @capability) ===
arc ext list
arc ext list --agent my-agent
arc ext create web-search                                       # ./web-search.py with @tool template
arc ext create scraper --dir my-agent/capabilities              # per-agent (trusted)
arc ext create scraper --dir my-agent/workspace/.capabilities   # agent-authored (UNTRUSTED, AST-validated)
arc ext install ./my_capability.py                              # copies to ~/.arc/capabilities/
arc ext validate ./my_capability.py

# === Team messaging ===
arc team init
arc team init --root /var/arc/team
arc team register agent-1 --name "Analyst" --type agent
arc team register lead-1 --name "Lead" --type agent --roles lead,reviewer
arc team status
arc team entities
arc team entities --role lead
arc team channels
arc team memory-status

# === Multi-agent dashboard ===
arc ui start
arc ui start --port 9000 --show-tokens
arc ui start --host 0.0.0.0
arc ui start --max-agents 500
arc ui start --traces-dir /var/arc/traces
arc ui tail --viewer-token <t>
arc ui tail --viewer-token <t> --layer llm
arc ui tail --viewer-token <t> --agent did:arc:acme:.../
arc ui tail --viewer-token <t> --group research-team

# === Gateway pairing ===
arc gateway pair list
arc gateway pair approve ABCD1234
arc gateway pair revoke ABCD1234

# === Help ===
arc help
arc version
```

---

## 💬 In-Chat REPL Commands

While inside `arc agent chat`:

| Command | Effect |
|---|---|
| `/quit`, `/exit` | Exit chat |
| `/help` | Show all REPL commands |
| `/tools` | List tools the agent can call |
| `/model` | Show current model |
| `/cost` | Running USD spend |
| `/reload` | Hot-reload skills + extensions |
| `/skills` | List discovered skills |
| `/extensions` | List loaded extensions |
| `/session` | Current session ID |
| `/sessions` | List past sessions |
| `/switch <id>` | Resume a previous session |
| `/identity` | Show DID, org, type |
| `/status` | Full agent summary |

---

## 🎚️ The Tier Wizard

`arc init` is interactive by default. It writes a sensible config based on your tier choice.

| Tier | Telemetry | Audit | Retry | Fallback | OpenTelemetry | PII redaction + signing |
|---|---|---|---|---|---|---|
| `open` | off | off | off | off | off | off |
| `enterprise` | ✅ | ✅ | ✅ (3x) | ✅ | off | off |
| `federal` | ✅ | ✅ | ✅ (3x) | ✅ | ✅ (OTLP) | ✅ |

Non-interactive variant:

```bash
arc init --tier federal --provider anthropic --dir /etc/arc
```

---

## 🛡️ Why Argparse, Not Click?

Three reasons:

1. **Fewer dependencies in the trust path.** `argparse` ships with Python. Click is one more thing to audit.
2. **Predictable CI behavior.** No "did Click upgrade and change `--option` parsing?"
3. **Easier to read and modify.** Arc's CLI is a few hundred lines of plain handlers — anyone can grep for `def cmd_` and find the entry points.

The whole CLI is **pure stdlib argparse with plain handler functions.** No magic. No metaclasses. No decorator trees. Just `if args.subcommand == "...": call_function(args)`.

---

## 📋 Compliance Notes

`arccli` itself doesn't implement compliance controls — it's the operator interface to the packages that do. Useful properties:

- **No reflection on user input.** All commands flow through argparse subparsers — no `eval`, no `getattr` on user strings.
- **`--json` everywhere.** Structured output is the default for any data-returning command, so CI/CD pipelines can parse without scraping.
- **Audited side-effects.** `arc gateway pair approve`, `arc team register`, `arc skill validate` — every operator action emits an arctrust audit event before completing.
- **No interactive defaults.** Every interactive command has a non-interactive equivalent (e.g. `arc init --tier`, `arc agent build --check`) so it can be scripted without `expect`.

---

## 🧪 Status

```bash
uv run --no-sync pytest packages/arccli/tests
```

- **Tests:** 283
- **Type check:** `mypy --strict` clean
- **Lint:** `ruff check` clean

---

## 📚 Full Reference

The complete `arc <command>` reference with every flag, default, and example: [docs/cli.md](../../docs/cli.md).

---

## 📄 License

Apache 2.0 · Copyright © 2025-2026 BlackArc Systems.
