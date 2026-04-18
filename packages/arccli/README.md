```
╭──────────────────────────────────────────────────────╮
│                                                      │
│   ▄▀█ █▀█ █▀▀ █▀▀ █▀▄▀█ █▀▄                       │
│   █▀█ █▀▄ █▄▄ █▄▄ █ ▀ █ █▄▀                       │
│                                                      │
│   Unified Command-Line Interface                     │
│   for the Arc Agent Platform                         │
│                                                      │
├──────────────────────────────────────────────────────┤
│  LLM · Agent · Run · Team · Memory · Init Wizard    │
╰──────────────────────────────────────────────────────╯
```

**The single CLI for the entire Arc stack.** arccmd wraps [ArcLLM](../arcllm/) (provider-agnostic LLM calls), [ArcRun](../arcrun/) (agentic runtime loop), [ArcAgent](../arcagent/) (agent orchestration), and [ArcTeam](../arcteam/) (multi-agent collaboration) into one unified `arc` command.

---

## Installation

```bash
pip install arccmd
```

Development install (install dependencies first):

```bash
pip install -e ../arcllm
pip install -e ../arcrun
pip install -e ../arcagent
pip install -e ../arcteam
pip install -e .
```

**Requirements:** Python 3.11+

---

## Quick Start

```bash
# --- Initialize ---
arc init                                  # tier-based setup wizard (open/enterprise/federal)
arc llm init --tier enterprise            # LLM-specific config setup

# --- LLM ---
arc llm providers                         # list providers
arc llm call anthropic "Hello"            # make an LLM call
arc llm validate                          # check configs and API keys

# --- Agent ---
arc agent create my-agent                 # scaffold agent directory
arc agent build my-agent                  # interactive onboarding wizard
arc agent chat my-agent                   # interactive REPL
arc agent chat my-agent --task "2+2?"     # one-shot task
arc agent bio_memory status               # biological memory overview

# --- Run ---
arc run task "What is 2+2?" --with-calc   # one-shot with tools
arc run exec "print(2 + 2)"              # sandboxed Python execution

# --- Team ---
arc team init                             # initialize team data directory
arc team status                           # team overview
arc team send --to agent://analyst "Hello" # send message
arc team memory search "vendors"          # search team knowledge
```

---

## Command Groups

| Group | Purpose |
|-------|---------|
| `arc init` | Tier-based initialization wizard (open/enterprise/federal) |
| `arc llm` | LLM provider management, model discovery, direct calls, init |
| `arc agent` | Agent lifecycle — create, configure, run, inspect, bio memory |
| `arc run` | Direct ArcRun execution without an agent directory |
| `arc team` | Team messaging, memory management, status |
| `arc ext` | Extension management |
| `arc skill` | Skill listing |

---

## `arc init`

Unified initialization wizard with tier-based module presets:

| Tier | Modules Enabled |
|------|----------------|
| `open` | All modules disabled — minimal setup |
| `enterprise` | Telemetry, audit, retry, fallback, rate limiting |
| `federal` | Full security: routing, PII redaction, signing, OpenTelemetry, budget enforcement |

```bash
arc init                          # interactive tier selection
arc init --tier federal           # direct tier selection
```

---

## `arc llm`

| Command | Description |
|---------|-------------|
| `arc llm init` | ArcLLM-specific setup with tier presets |
| `arc llm version` | Show version info |
| `arc llm config` | Show global ArcLLM configuration |
| `arc llm providers` | List all available providers |
| `arc llm provider NAME` | Show provider details and models |
| `arc llm models` | List all models across providers |
| `arc llm call PROVIDER PROMPT` | Make an LLM call |
| `arc llm validate` | Validate configs and API keys |

---

## `arc agent`

| Command | Description |
|---------|-------------|
| `arc agent create NAME` | Scaffold a new agent directory |
| `arc agent build [PATH]` | Interactive onboarding wizard (or `--check` to validate) |
| `arc agent chat [PATH]` | Interactive REPL or one-shot (`--task`) |
| `arc agent tools [PATH]` | List all tools available to an agent |
| `arc agent config [PATH]` | Show agent configuration |
| `arc agent strategies` | List available execution strategies |
| `arc agent events` | List all event types emitted by ArcRun |
| `arc agent bio_memory status` | Biological memory overview |
| `arc agent bio_memory identity` | Agent identity and traits |
| `arc agent bio_memory episodes` | Long-term episodic memories |
| `arc agent bio_memory working` | Current working memory |

---

## `arc run`

| Command | Description |
|---------|-------------|
| `arc run task PROMPT` | Run a one-shot task with ArcRun directly |
| `arc run exec CODE` | Execute Python code in a sandboxed subprocess |
| `arc run version` | Show ArcRun/ArcLLM versions and capabilities |

---

## `arc team`

| Command | Description |
|---------|-------------|
| `arc team init` | Initialize team data directory (entities, channels, HMAC key) |
| `arc team status` | Team overview (entities, channels, messages, audit) |
| `arc team config` | Show team configuration |
| `arc team register ID` | Register an agent or user |
| `arc team send` | Send a message |
| `arc team inbox` | Check inbox |
| `arc team drain` | Drain inbox, mark all read |
| `arc team read` | Read channel/DM history |
| `arc team thread ID` | View message thread |
| `arc team actions` | View pending action items |
| `arc team memory status` | Team memory status |
| `arc team memory entities` | List entities (with type filter) |
| `arc team memory entity ID` | Show entity details |
| `arc team memory search QUERY` | BM25 search with wiki-link traversal |
| `arc team memory rebuild-index` | Force full index rebuild |
| `arc team memory config` | Show memory configuration |

---

## Agent Directory Structure

```
my-agent/
  arcagent.toml          # Agent configuration (model, tools policy, telemetry)
  workspace/
    identity.md          # System prompt (required)
    policy.md            # Behavioral constraints (optional)
    context.md           # Additional context (optional)
  tools/
    __init__.py
    example.py           # Tool definitions (exports get_tools())
```

---

## Output Format

Every command supports `--json` for machine-readable output. Full reference: [docs/CLI.md](docs/CLI.md).

```bash
arc llm providers --json         # JSON array of providers
arc team status --json           # JSON team status object
arc team memory search "q" --json  # JSON search results
```

---

## License

This project is licensed under the [Apache License, Version 2.0](https://www.apache.org/licenses/LICENSE-2.0).

Copyright (c) 2025-2026 BlackArc Systems.
