# Arc CLI Reference

All commands available via the `arc` console script (installed by `arcmas` or `arccli`).

## Quick start

```bash
# Initialize Arc for the first time (tier-based wizard)
arc init

# Create a new agent
arc agent create myagent --model anthropic/claude-sonnet-4-5-20250929

# Validate the agent setup
arc agent build myagent --check

# Run a one-shot task
arc agent run myagent "Summarize workspace/reports/"

# Interactive chat session
arc agent chat myagent

# Start the multi-agent dashboard
arc ui start --port 8420

# Tail live events from a running dashboard
arc ui tail --viewer-token <token> --layer llm
```

---

## Command groups

### `arc agent` — Agent lifecycle

Manage agent directories: scaffold, configure, run, and inspect agents.

| Command | Purpose | Example |
|---|---|---|
| `arc agent create <name>` | Scaffold a new agent directory with workspace, config, and a calculator extension | `arc agent create myagent --model anthropic/claude-sonnet-4-5-20250929` |
| `arc agent build [path]` | Interactive build wizard — prompts for model and provider | `arc agent build myagent` |
| `arc agent build [path] --check` | Validate config and workspace without interactive prompts | `arc agent build myagent --check` |
| `arc agent chat [path]` | Start an interactive REPL chat session | `arc agent chat myagent` |
| `arc agent chat [path] --task "<task>"` | One-shot task via chat handler | `arc agent chat myagent --task "List all files"` |
| `arc agent run <path> <task>` | Run a single task non-interactively | `arc agent run myagent "Analyze data.csv"` |
| `arc agent serve [path]` | Start a long-running agent daemon (scheduler active) | `arc agent serve myagent --verbose` |
| `arc agent serve [path] --ui` | Start daemon with UI reporter module enabled | `arc agent serve myagent --ui` |
| `arc agent status [path]` | Show agent summary: DID, model, tool/skill/extension/session counts | `arc agent status myagent` |
| `arc agent config [path]` | Show parsed `arcagent.toml` | `arc agent config myagent --json` |
| `arc agent tools [path]` | List tools available to the agent | `arc agent tools myagent --json` |
| `arc agent skills [path]` | List discovered skills | `arc agent skills myagent` |
| `arc agent extensions [path]` | List loaded extensions | `arc agent extensions myagent` |
| `arc agent sessions [path]` | List session transcripts with timestamps and sizes | `arc agent sessions myagent` |
| `arc agent reload [path]` | Hot-reload extensions and skills | `arc agent reload myagent` |
| `arc agent strategies` | List available execution strategies (react, code) | `arc agent strategies` |
| `arc agent events` | List all event types emitted by arcrun and arcagent | `arc agent events` |

**Notes:**
- `path` defaults to `.` (current directory) for all subcommands that accept it
- `arc agent create` accepts `--dir <parent>` to set the parent directory (default: `.`)
- `arc agent run` and `arc agent chat` accept `--model <provider/model>` to override the configured model
- `arc agent run` accepts `--verbose` / `-v` for turn/cost summary and `--json` for structured output
- `arc agent chat` accepts `--max-turns <n>` (default 10) and `--session-id <id>` to resume a session

**In-chat REPL commands** (available while inside `arc agent chat`):
`/quit`, `/tools`, `/model`, `/cost`, `/reload`, `/skills`, `/extensions`, `/session`, `/sessions`, `/switch <id>`, `/identity`, `/status`

---

### `arc llm` — LLM provider operations

Inspect arcllm configuration, providers, and models.

| Command | Purpose | Example |
|---|---|---|
| `arc llm version` | Show arcllm and arccli versions | `arc llm version --json` |
| `arc llm config` | Show global arcllm configuration (modules, vault) | `arc llm config` |
| `arc llm config --module <name>` | Show a specific module config | `arc llm config --module audit` |
| `arc llm providers` | List all configured providers | `arc llm providers --json` |
| `arc llm provider <name>` | Show provider details and available models | `arc llm provider anthropic` |
| `arc llm models` | List all models across all providers | `arc llm models` |
| `arc llm models --provider <name>` | Filter models by provider | `arc llm models --provider openai` |
| `arc llm models --tools` | Only models that support tool calling | `arc llm models --tools` |
| `arc llm models --vision` | Only models that support vision | `arc llm models --vision` |
| `arc llm validate` | Validate provider configs and check API key availability | `arc llm validate` |
| `arc llm validate --provider <name>` | Validate a specific provider | `arc llm validate --provider anthropic` |

**Notes:**
- All subcommands accept `--json` for structured output
- `arc llm provider` shows context window, max output, pricing, tool/vision support per model

---

### `arc run` — arcrun loop operations

Run tasks directly with arcrun — no agent directory required.

| Command | Purpose | Example |
|---|---|---|
| `arc run version` | Show arcrun version, strategies, and public API | `arc run version --json` |
| `arc run exec <code>` | Execute Python code via arcrun's sandboxed executor | `arc run exec "print(2 ** 32)"` |
| `arc run exec <code> --timeout <s>` | Set execution timeout (default 30s) | `arc run exec "..." --timeout 60` |
| `arc run task <prompt>` | Run a single task with arcrun (registers `spawn_task` by default) | `arc run task "spawn agents to compute 4+5 and 6+7"` |
| `arc run task <prompt> --model <m>` | Specify provider/model | `arc run task "..." --model anthropic/claude-haiku-4-5-20251001` |
| `arc run task <prompt> --with-calc` | Add a safe math calculator tool | `arc run task "What is 1234 * 5678?" --with-calc` |
| `arc run task <prompt> --with-code-exec` | Add the sandboxed Python executor tool | `arc run task "Compute fibonacci(30)" --with-code-exec` |
| `arc run task <prompt> --no-spawn` | Disable the `spawn_task` tool (default: registered) | `arc run task "..." --no-spawn --with-calc` |
| `arc run task <prompt> --strategy <s>` | Force a specific strategy (react, code) | `arc run task "..." --strategy code` |
| `arc run task <prompt> --verbose` | Show per-turn tool and LLM events | `arc run task "..." --verbose` |
| `arc run task <prompt> --show-events` | Print full event log after completion | `arc run task "..." --show-events` |

**Notes:**
- `arc run task` registers `spawn_task` by default so the model can fan out work to parallel sub-agents. Pass `--no-spawn` to disable it.
- `arc run task` exits with an error only if no tools are available at all (no `--with-calc`, no `--with-code-exec`, AND `--no-spawn`).
- `arc run exec` does not require an LLM; it runs code directly in the sandbox.
- `arc run task` accepts `--max-turns <n>` (default 10) and `--tool-timeout <s>`.

**Orchestration patterns:**

The agent has two ways to fan out work across multiple `arcrun` loops:

- **LLM-driven decomposition** — the model decides at runtime to split a task into N sub-tasks, calling `spawn_task` multiple times in one response. The arcrun loop dispatches `parallel_safe` tools (which `spawn_task` is) concurrently via `asyncio.gather`. This is what `arc run task` exposes.
- **Code-level fan-out** — Python code (an embedding agent) calls `arcrun.run()` N times via `asyncio.gather` directly. The model never sees the decomposition; the agent collects results and synthesizes them. Use this when the shape of the work is known ahead of time and the LLM doesn't need to choose.

`arcrun` itself has zero spawn knowledge — `spawn_task` is owned by `arcagent.orchestration` and registered by the CLI on the user's behalf when `--no-spawn` is not set.

---

### `arc skill` — Skill management

Create, validate, and search agent skills (Markdown files with YAML frontmatter).

| Command | Purpose | Example |
|---|---|---|
| `arc skill list` | List all discovered skills (global + workspace) | `arc skill list` |
| `arc skill list --agent <path>` | Include skills from a specific agent workspace | `arc skill list --agent myagent` |
| `arc skill create <name>` | Scaffold a new SKILL.md with YAML frontmatter | `arc skill create data-analysis` |
| `arc skill create <name> --dir <dir>` | Write to a specific directory | `arc skill create audit-report --dir myagent/workspace/skills` |
| `arc skill create <name> --global` | Write to `~/.arcagent/skills/` | `arc skill create shared-tool --global` |
| `arc skill validate <path>` | Validate a skill file (checks required frontmatter fields) | `arc skill validate myskill.md` |
| `arc skill search <query>` | Search skills by name or description | `arc skill search "data analysis"` |
| `arc skill search <query> --agent <path>` | Include agent workspace in search | `arc skill search "report" --agent myagent` |

---

### `arc ext` — Extension management

Manage Python extensions that register tools and hooks with ArcAgent.

| Command | Purpose | Example |
|---|---|---|
| `arc ext list` | List all discovered extensions (global + workspace) | `arc ext list` |
| `arc ext list --agent <path>` | Include extensions from a specific agent workspace | `arc ext list --agent myagent` |
| `arc ext create <name>` | Scaffold a new extension with boilerplate `extension()` factory | `arc ext create web-search` |
| `arc ext create <name> --dir <dir>` | Write to a specific directory | `arc ext create scraper --dir myagent/workspace/extensions` |
| `arc ext create <name> --global` | Write to `~/.arcagent/extensions/` | `arc ext create shared-tool --global` |
| `arc ext install <source>` | Install a `.py` file or directory to `~/.arcagent/extensions/` | `arc ext install my_extension.py` |
| `arc ext validate <path>` | Validate an extension: imports cleanly and has `extension()` factory | `arc ext validate my_extension.py` |

---

### `arc team` — Team messaging

Manage arcteam entity registries, channels, and messaging.

| Command | Purpose | Example |
|---|---|---|
| `arc team status` | Show team overview: entity count, channels, messages, audit entries | `arc team status` |
| `arc team config` | Show team configuration | `arc team config --json` |
| `arc team init` | Initialize team data directory and generate HMAC key | `arc team init` |
| `arc team init --root <path>` | Initialize at a specific root path | `arc team init --root /var/arc/team` |
| `arc team register <id>` | Register an agent or user entity | `arc team register agent-1 --name "Analyst" --type agent` |
| `arc team register <id> --roles <r>` | Register with comma-separated roles | `arc team register agent-1 --name "Lead" --type agent --roles lead,reviewer` |
| `arc team entities` | List all registered entities | `arc team entities` |
| `arc team entities --role <r>` | Filter by role | `arc team entities --role lead` |
| `arc team channels` | List available channels | `arc team channels` |
| `arc team memory-status` | Show team memory index status | `arc team memory-status` |

**Global options (apply to all `arc team` subcommands):**
- `--root <path>` — override team data root directory
- `--json` — JSON output mode

---

### `arc ui` — Multi-agent dashboard

Start and observe the ArcUI real-time dashboard.

| Command | Purpose | Example |
|---|---|---|
| `arc ui start` | Launch the dashboard (default: `127.0.0.1:8420`) | `arc ui start` |
| `arc ui start --port <n>` | Bind to a specific port | `arc ui start --port 9000` |
| `arc ui start --host <h>` | Bind to a specific host | `arc ui start --host 0.0.0.0` |
| `arc ui start --show-tokens` | Print full tokens to stdout instead of masked | `arc ui start --show-tokens` |
| `arc ui start --viewer-token <t>` | Supply a viewer token (auto-generated if omitted) | `arc ui start --viewer-token mytoken` |
| `arc ui start --operator-token <t>` | Supply an operator token | `arc ui start --operator-token optoken` |
| `arc ui start --agent-token <t>` | Supply an agent token | `arc ui start --agent-token agtoken` |
| `arc ui start --max-agents <n>` | Maximum tracked agents (default: 100) | `arc ui start --max-agents 500` |
| `arc ui start --traces-dir <dir>` | Warm-start from a JSONL trace directory | `arc ui start --traces-dir /var/arc/traces` |
| `arc ui tail` | Stream live events to stdout as JSONL (requires `--viewer-token`) | `arc ui tail --viewer-token <t>` |
| `arc ui tail --host <h> --port <n>` | Connect to a non-default dashboard | `arc ui tail --host 10.0.0.1 --port 9000 --viewer-token <t>` |
| `arc ui tail --layer <l>` | Filter to a specific layer: `llm`, `run`, `agent`, or `team` | `arc ui tail --viewer-token <t> --layer llm` |
| `arc ui tail --agent <id>` | Filter to events from a specific agent (by ID or DID) | `arc ui tail --viewer-token <t> --agent did:arc:acme:analyst/abc` |
| `arc ui tail --group <name>` | Filter to events from agents in a specific team/group | `arc ui tail --viewer-token <t> --group research-team` |

**Notes:**
- `arc ui start` auto-generates tokens if not supplied; the agent token is written to `~/.arcagent/ui-token`
- `arc ui tail` requires `--viewer-token` explicitly; it does not auto-read the token file (which contains the agent token, not the viewer token)
- `arc agent serve --ui` enables the UI reporter module so agent events flow to a running dashboard

---

### `arc ext` — Extension management

See the `arc ext` section above.

---

### `arc init` — First-time setup wizard

Interactive tier-based configuration wizard.

| Command | Purpose | Example |
|---|---|---|
| `arc init` | Interactive wizard: select tier (open/enterprise/federal), provider, API key | `arc init` |
| `arc init --tier <t>` | Non-interactive: write config for a specific tier | `arc init --tier enterprise` |
| `arc init --provider <p>` | Non-interactive: set provider | `arc init --tier open --provider anthropic` |
| `arc init --dir <path>` | Write config to a specific directory | `arc init --tier federal --dir /etc/arc` |

**Tier presets:**

| Tier | Telemetry | Audit | Retry | Fallback | OTel | Security (PII + signing) |
|---|---|---|---|---|---|---|
| `open` | off | off | off | off | off | off |
| `enterprise` | on | on | on (3x) | on | off | off |
| `federal` | on | on | on (3x) | on | on (OTLP gRPC) | on |

---

### `arc gateway` — Gateway control plane

Operator commands for managing DM pairing codes (gateway_only — these commands require arcgateway running).

| Command | Purpose | Example |
|---|---|---|
| `arc gateway pair approve <code>` | Approve an 8-char DM pairing code; adds user hash to session allowlist; code is consumed | `arc gateway pair approve ABCD1234` |
| `arc gateway pair list` | List all pending (unexpired, unconsumed) pairing codes | `arc gateway pair list` |
| `arc gateway pair revoke <code>` | Revoke a pending pairing code | `arc gateway pair revoke ABCD1234` |

**Notes:**
- Pairing codes are exactly 8 characters, uppercase
- Codes expire automatically; `pair list` shows remaining TTL in minutes
- These commands only work when arcgateway is configured with `gateway.pairing.enabled = true`

---

### `arc help` and `arc version`

| Command | Purpose |
|---|---|
| `arc help` (or `arc ?`) | Show all available commands grouped by category |
| `arc version` (or `arc ver`) | Show arccli version |
| `arc quit` (or `arc exit`, `arc q`, `arc bye`) | Exit the Arc REPL |

---

## Global flags

There are no global flags that apply to all commands. Per-command flags are documented above. Most data-returning commands accept `--json` for structured output suitable for CI/CD pipelines.

---

## Configuration

Agent configuration lives in `arcagent.toml` in the agent directory. Key sections:

```toml
[agent]
name = "myagent"
org = "local"
type = "executor"
workspace = "./workspace"

[llm]
model = "anthropic/claude-sonnet-4-5-20250929"
max_tokens = 8192
temperature = 0.7

[identity]
did = ""                        # populated by arc agent build
key_dir = "~/.arcagent/keys"

[vault]
backend = ""                    # vault URL, or empty for env-var fallback

[tools.policy]
allow = []                      # explicit tool allowlist
deny = []
timeout_seconds = 30

[telemetry]
enabled = true
service_name = "myagent"
log_level = "INFO"
export_traces = false

[context]
max_tokens = 128000

[session]
retention_count = 50
retention_days = 30

[extensions]
global_dir = "~/.arcagent/extensions"

[modules.memory]
enabled = true

[modules.policy]
enabled = true
```

For the full TOML schema, see `arcagent.core.config.ArcAgentConfig`.

---

## See also

- [ADR-019](architecture/decisions/ADR-019-four-pillars-universal.md) — Four Pillars Universal (Identity, Sign, Authorize, Audit)
- [ADR-018](architecture/decisions/ADR-018-no-mcp-no-migration-no-acp.md) — No MCP, No Migration, No ACP
- SPEC-007 — DID Identity Unification
- SPEC-016 — Multi-Agent UI
- SPEC-017 — Arc Core Hardening (argparse migration, policy pipeline)
- SPEC-018 — Hermes Parity (gateway session guard, skill verification)
