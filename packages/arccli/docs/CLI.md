# Arc CLI Reference

Complete reference for all `arc` commands, flags, and options.

```
arc
├── llm          # LLM provider management and direct calls
│   ├── version
│   ├── config
│   ├── providers
│   ├── provider
│   ├── models
│   ├── call
│   └── validate
├── agent        # Agent lifecycle — create, configure, run, inspect
│   ├── create
│   ├── build
│   ├── chat
│   ├── tools
│   ├── config
│   ├── strategies
│   └── events
└── run          # Direct arcrun execution (no agent directory)
    ├── task
    ├── exec
    └── version
```

---

## `arc llm`

LLM provider management, model discovery, and direct calls via ArcLLM.

### `arc llm version`

Show version information.

```bash
arc llm version
arc llm version --json
```

| Flag | Description |
|------|-------------|
| `--json` | Output as JSON |

**Example output:**

```
arccli : 0.1.0
arcllm : 0.1.0
python : 3.13.9
```

### `arc llm config`

Show global ArcLLM configuration (defaults, modules, vault).

```bash
arc llm config
arc llm config --module telemetry
arc llm config --json
```

| Flag | Description |
|------|-------------|
| `--module NAME` | Show only a specific module's config |
| `--json` | Output as JSON |

**Example output:**

```
[defaults]
  provider = anthropic
  temperature = 0.7
  max_tokens = 4096

[modules.retry]
  enabled = False
  max_retries = 3
  backoff_base_seconds = 1.0
...
```

### `arc llm providers`

List all available LLM providers.

```bash
arc llm providers
arc llm providers --json
```

| Flag | Description |
|------|-------------|
| `--json` | Output as JSON |

**Example output:**

```
Name             API Format          Default Model
---------------  ------------------  ----------------------------------
anthropic        anthropic-messages  claude-sonnet-4-20250514
openai           openai-chat         gpt-4o
ollama           openai-chat         llama3.2
```

### `arc llm provider NAME`

Show provider details: connection settings and all models with pricing.

```bash
arc llm provider anthropic
arc llm provider openai --json
```

| Flag | Description |
|------|-------------|
| `--json` | Output as JSON |

**Example output:**

```
Provider: anthropic

  api_format          : anthropic-messages
  base_url            : https://api.anthropic.com
  api_key_env         : ANTHROPIC_API_KEY
  default_model       : claude-sonnet-4-20250514
  default_temperature : 0.7

Models:
Model                      Context  Max Output  Tools  Vision  Input $/1M  Output $/1M
-------------------------  -------  ----------  -----  ------  ----------  -----------
claude-sonnet-4-20250514   200000   8192        yes    yes     $3.00       $15.00
claude-haiku-4-5-20251001  200000   8192        yes    yes     $0.80       $4.00
```

### `arc llm models`

List all models across all providers.

```bash
arc llm models
arc llm models --provider anthropic
arc llm models --tools
arc llm models --vision
arc llm models --tools --json
```

| Flag | Description |
|------|-------------|
| `--provider NAME` | Filter by provider |
| `--tools` | Only models that support tool use |
| `--vision` | Only models that support vision |
| `--json` | Output as JSON |

### `arc llm call PROVIDER PROMPT`

Make an LLM call.

```bash
# Basic call
arc llm call anthropic "What is 2+2?"

# Override model and parameters
arc llm call anthropic "Summarize this" \
  --model claude-haiku-4-5-20251001 \
  --temperature 0.3 \
  --max-tokens 100 \
  --system "Be concise"

# With telemetry and verbose output
arc llm call anthropic "Hello" --telemetry --verbose

# Full JSON response for scripting
arc llm call anthropic "Hello" --json

# Toggle specific modules
arc llm call anthropic "Hello" --retry --no-audit
```

#### Generation Flags

| Flag | Description |
|------|-------------|
| `--model MODEL` | Override default model |
| `--temperature FLOAT` | Sampling temperature (0.0-2.0) |
| `--max-tokens INT` | Maximum output tokens |
| `--system TEXT` | System message |

#### Module Flags

Toggle modules on/off per call. Default (`None`) uses config.toml settings.

| Flag | Description |
|------|-------------|
| `--retry` / `--no-retry` | Retry with exponential backoff |
| `--fallback` / `--no-fallback` | Fallback to alternate providers |
| `--rate-limit` / `--no-rate-limit` | Token bucket rate limiting |
| `--telemetry` / `--no-telemetry` | Cost and timing tracking |
| `--audit` / `--no-audit` | Audit metadata logging |
| `--security` / `--no-security` | PII redaction + request signing |
| `--otel` / `--no-otel` | OpenTelemetry distributed tracing |

#### Output Flags

| Flag | Description |
|------|-------------|
| `--verbose` | Show usage details (tokens, cost, timing) above response |
| `--json` | Output full LLMResponse as JSON |

**Verbose example:**

```
Model: claude-sonnet-4-20250514
Usage: 10 input, 5 output, 15 total tokens
Cost: $0.000105
Stop: end_turn
---
Hello! How can I help you?
```

### `arc llm validate`

Validate all provider configs and check API key availability.

```bash
arc llm validate
arc llm validate --provider anthropic
arc llm validate --json
```

| Flag | Description |
|------|-------------|
| `--provider NAME` | Validate a specific provider only |
| `--json` | Output as JSON |

**Example output:**

```
Global config: OK

Provider         Config  API Key
---------------  ------  ---------------------------
anthropic        OK      OK
openai           OK      MISSING (OPENAI_API_KEY)
ollama           OK      OK
```

---

## `arc agent`

Agent lifecycle commands — create, configure (build), run (chat), and inspect.

### Agent Directory Structure

```
my-agent/
  arcagent.toml          # Agent configuration
  workspace/
    identity.md          # System prompt (required)
    policy.md            # Behavioral constraints (optional)
    context.md           # Additional context (optional)
  tools/
    __init__.py
    example.py           # Tool definitions
```

#### Tool Convention

Each `.py` file in `tools/` must export a `get_tools()` function returning `list[Tool]`:

```python
from arcrun import Tool, ToolContext

async def my_tool(params: dict, ctx: ToolContext) -> str:
    return "result"

def get_tools() -> list[Tool]:
    return [
        Tool(
            name="my_tool",
            description="What it does.",
            input_schema={
                "type": "object",
                "properties": {
                    "param": {"type": "string", "description": "..."},
                },
                "required": ["param"],
            },
            execute=my_tool,
        ),
    ]
```

#### System Prompt Assembly

The system prompt is built from workspace files in order:

1. `workspace/identity.md` — who the agent is
2. `workspace/policy.md` — behavioral constraints
3. `workspace/context.md` — additional context

Each section is labeled with `--- section_name ---` headers.

### `arc agent create NAME`

Scaffold a new agent directory with default config, system prompt, and example tools.

```bash
arc agent create my-agent
arc agent create my-agent --dir ~/agents
arc agent create my-agent --model openai/gpt-4o
arc agent create my-agent --with-code-exec
```

| Flag | Default | Description |
|------|---------|-------------|
| `--dir PATH` | `.` (cwd) | Parent directory for the new agent |
| `--model MODEL` | `anthropic/claude-haiku-4-5-20251001` | LLM model (provider/model format) |
| `--with-code-exec` | off | Include `execute_python` tool in generated code |

**Creates:**

```
my-agent/
  arcagent.toml
  workspace/identity.md
  tools/__init__.py
  tools/example.py        # calculator tool (+ execute_python if --with-code-exec)
```

### `arc agent build [PATH]`

Interactive onboarding wizard that walks through full agent configuration.

```bash
arc agent build my-agent            # interactive wizard
arc agent build my-agent --check    # validation only (no prompts)
```

| Flag | Description |
|------|-------------|
| `--check` | Skip interactive setup, just validate the agent |

#### Wizard Steps

1. **Agent name** — confirm or change the agent's name
2. **Provider selection** — choose from anthropic, openai, groq, deepseek, ollama (shows API key status)
3. **Model selection** — pick from provider's model list or enter custom
4. **System prompt** — review and optionally edit `workspace/identity.md`
5. **Tool setup** — review existing tools or add example calculator / `execute_python`
6. **Advanced settings** — max output tokens, temperature, context window

Writes updated `arcagent.toml` and `workspace/identity.md` when complete.

#### Validation Mode (`--check`)

Validates without prompting:

```bash
$ arc agent build my-agent --check
  [+] arcagent.toml (my-agent)
  [+] workspace/identity.md (229 chars)
  [+] model: anthropic/claude-haiku-4-5-20251001
  [+] ANTHROPIC_API_KEY is set
  [+]   tool: calculate
  [+] tools: 1 total
  [+] strategies: react, code

Ready. Run:
  arc agent chat my-agent
```

Markers: `[+]` OK, `[~]` warning, `[x]` failure.

### `arc agent chat [PATH]`

Interactive REPL or one-shot task execution with full arcrun feature access.

```bash
# Interactive REPL
arc agent chat my-agent
arc agent chat my-agent --verbose

# One-shot task
arc agent chat my-agent --task "What is 2+2?"
arc agent chat my-agent --task "What is 2+2?" --json

# Force strategy
arc agent chat my-agent --task "Compute fibonacci" --strategy code --with-code-exec

# Sandbox (restrict which tools the model can call)
arc agent chat my-agent --sandbox "calculate"

# Full observability
arc agent chat my-agent --task "..." --verbose --show-events
```

#### Flags

| Flag | Default | Description |
|------|---------|-------------|
| `--task TEXT` | (none) | Run a single task instead of interactive REPL |
| `--model MODEL` | from config | Override model (provider/model format) |
| `--max-turns INT` | `10` | Max loop iterations per task |
| `--tool-timeout FLOAT` | (none) | Global tool timeout in seconds |
| `--strategy react\|code` | auto | Force execution strategy |
| `--sandbox TOOLS` | (none) | Comma-separated allowlist of tool names |
| `--with-code-exec` | off | Add built-in `execute_python` tool |
| `--code-timeout FLOAT` | `30` | Timeout for `execute_python` in seconds |
| `--verbose` | off | Show tool/LLM events as they happen |
| `--show-events` | off | Print full event timeline after each run |
| `--json` | off | Output result as JSON (one-shot only) |

#### Interactive REPL Commands

When running without `--task`, the REPL supports these commands:

| Command | Description |
|---------|-------------|
| `/quit` | Exit the REPL |
| `/tools` | List all loaded tools |
| `/model` | Show current model |
| `/cost` | Show session cost and stats |
| `/events` | Show last run's event timeline |
| `/sandbox [tools]` | Set sandbox allowlist (comma-separated, empty to clear) |
| `/strategy [name]` | Set strategy (`react`, `code`, or `auto`) |
| `/max-turns [n]` | Set max turns per task |
| `/add-code-exec` | Add `execute_python` tool to current session |
| `/verbose` | Toggle verbose mode on/off |

#### JSON Output

With `--task` and `--json`, output is a complete run result:

```json
{
  "content": "2 + 2 = 4",
  "turns": 2,
  "tool_calls_made": 1,
  "tokens_used": {"input": 1361, "output": 67, "total": 1428},
  "strategy_used": "react",
  "cost_usd": 0.001356,
  "event_count": 11,
  "events": [
    {"type": "strategy.selected", "timestamp": 1771130149.35, "data": {"strategy": "react"}},
    {"type": "tool.start", "timestamp": 1771130149.50, "data": {"name": "calculate", "arguments": {"expression": "2+2"}}},
    ...
  ]
}
```

#### Verbose Output

With `--verbose`, tool calls and LLM responses stream inline:

```
Agent: my-agent  |  Model: anthropic/claude-haiku-4-5-20251001
Tools: calculate
--------------------------------------------------
  [strat]  react
  [turn]   --- turn 1 ---
  [llm]    stop=tool_use, latency=812ms
  [tool]   calculate({'expression': '2+2'})
  [tool]   calculate -> 1 chars (0ms)
  [turn]   --- turn 2 ---
  [llm]    stop=end_turn, latency=553ms
--------------------------------------------------
2 + 2 = 4

[2 turns, 1 tool calls, $0.0013, strategy=react]
```

#### Strategies

| Strategy | Description |
|----------|-------------|
| `react` | Iterative tool-calling loop. Reason, call tools, observe, repeat. Best for multi-step problems. |
| `code` | Write and execute Python code. Best for computation and data processing. |
| auto | Model selects the best strategy (default when `--strategy` is omitted). |

#### Sandbox

The `--sandbox` flag restricts which tools the model can invoke. Any tool call not in the allowlist is denied with an audit event.

```bash
# Only allow the calculate tool (block everything else)
arc agent chat my-agent --sandbox "calculate"

# Allow multiple tools
arc agent chat my-agent --sandbox "calculate,execute_python"
```

### `arc agent tools [PATH]`

List all tools discovered in the agent's `tools/` directory.

```bash
arc agent tools my-agent
arc agent tools my-agent --json
arc agent tools my-agent --with-code-exec
```

| Flag | Description |
|------|-------------|
| `--json` | Output tool definitions as JSON |
| `--with-code-exec` | Include built-in `execute_python` in the listing |

**Example output:**

```
  calculate
    Evaluate a math expression. Supports +, -, *, /, (), %.
    - expression: string (required) — Math expression to evaluate

  execute_python
    Execute Python code in a sandboxed subprocess. Returns stdout, stderr, exit_code, and duration.
    - code: string (required) — Python code to execute
```

### `arc agent config [PATH]`

Show the agent's `arcagent.toml` configuration.

```bash
arc agent config my-agent
arc agent config my-agent --json
```

| Flag | Description |
|------|-------------|
| `--json` | Output as JSON |

**Example output:**

```
[agent]
  name = my-agent
  org = local
  type = executor
  workspace = ./workspace

[llm]
  model = anthropic/claude-haiku-4-5-20251001
  max_tokens = 4096
  temperature = 0.7

[tools]
  policy = {'allow': [], 'deny': [], 'timeout_seconds': 30}

[telemetry]
  enabled = True
  service_name = my-agent
  log_level = INFO
  export_traces = False

[context]
  max_tokens = 128000
```

### `arc agent strategies`

List all available execution strategies.

```bash
arc agent strategies
```

**Example output:**

```
  react: Iterative tool-calling loop. Reasons about the task, calls tools, observes results, and repeats until complete.
  code: Write and execute Python code to solve tasks. Best for computation, data processing, and problems where code is more effective.
```

### `arc agent events`

List all event types emitted by arcrun during execution.

```bash
arc agent events
```

**Example output:**

```
Event                  When                            Data Keys
---------------------  ------------------------------  ------------------------------------------------
loop.start             run() called                    task, tool_names, strategy
loop.complete          Execution finished              content, turns, tool_calls, tokens, cost
loop.max_turns         Hit turn limit                  turns_used, max_turns
strategy.selected      Strategy chosen                 strategy
turn.start             Loop iteration begins           turn_number
turn.end               Loop iteration ends             turn_number
llm.call               model.invoke() returned         model, stop_reason, tokens, latency_ms, cost_usd
tool.start             Tool execution begins           name, arguments
tool.end               Tool execution complete         name, result_length, duration_ms
tool.denied            Sandbox blocked tool            name, reason
tool.error             Tool threw exception/timeout    name, error
tool.registered        New tool added to registry      name
tool.replaced          Existing tool replaced          name
tool.removed           Tool removed from registry      name
code.prompt.augmented  Code strategy augmented prompt  original_length, augmented_length
```

---

## `arc run`

Run tasks directly with arcrun. No agent directory needed — useful for quick one-off tasks, testing tools, and exploring arcrun features.

### `arc run task PROMPT`

Execute a single task with arcrun's agentic loop.

```bash
# With calculator tool
arc run task "What is 2+2?" --with-calc

# With code execution
arc run task "Write hello world" --with-code-exec --strategy code

# Override model
arc run task "Summarize this" --model openai/gpt-4o --verbose

# Full observability
arc run task "Analyze data" --with-code-exec --show-events --json
```

| Flag | Default | Description |
|------|---------|-------------|
| `--model MODEL` | `anthropic/claude-haiku-4-5-20251001` | LLM model (provider/model format) |
| `--system TEXT` | `"You are a helpful assistant."` | System prompt |
| `--max-turns INT` | `10` | Max loop iterations |
| `--tool-timeout FLOAT` | (none) | Global tool timeout in seconds |
| `--strategy react\|code` | auto | Force execution strategy |
| `--with-code-exec` | off | Add `execute_python` tool |
| `--code-timeout FLOAT` | `30` | Timeout for `execute_python` |
| `--with-calc` | off | Add built-in calculator tool |
| `--verbose` | off | Show events inline |
| `--show-events` | off | Print full event log after run |
| `--json` | off | Output as JSON |

At least one tool flag (`--with-calc` and/or `--with-code-exec`) is required.

### `arc run exec CODE`

Execute Python code directly via arcrun's sandboxed subprocess executor.

```bash
arc run exec "print(2 + 2)"
arc run exec "import math; print(math.pi)" --timeout 10
arc run exec "print('hello')" --json
```

| Flag | Default | Description |
|------|---------|-------------|
| `--timeout FLOAT` | `30` | Execution timeout in seconds |
| `--max-output INT` | `65536` | Max output bytes |
| `--json` | off | Output raw JSON result |

**Plain output:**

```
4
(20ms)
```

**JSON output:**

```json
{
  "stdout": "4\n",
  "stderr": "",
  "exit_code": 0,
  "duration_ms": 18.6
}
```

### `arc run version`

Show arcrun and arcllm versions, available strategies, built-in tools, and public API surface.

```bash
arc run version
arc run version --json
```

| Flag | Description |
|------|-------------|
| `--json` | Output as JSON |

**Example output:**

```
  arcrun     : 0.1.0
  arcllm     : 0.1.0
  strategies : react, code
  builtins   : execute_python

Public API:
  run
  run_async
  RunHandle
  Tool
  ToolContext
  ToolRegistry
  LoopResult
  SandboxConfig
  Event
  EventBus
  Strategy
  make_execute_tool
```

---

## Global Patterns

### `--json` Flag

Every command supports `--json` for machine-readable output. Use it for scripting and CI/CD pipelines.

### `.env` File Loading

Both `arc agent` and `arc run` commands automatically load API keys from `.env` files, checked in order:

1. `~/AI/arcrun/.env`
2. `~/.env`
3. `./.env` (current directory)

### Model Format

Models are specified as `provider/model_name`:

```
anthropic/claude-haiku-4-5-20251001
openai/gpt-4o
groq/llama-3.3-70b-versatile
deepseek/deepseek-chat
ollama/llama3.2
```

### Help

Every command has built-in help:

```bash
arc --help
arc llm --help
arc llm call --help
arc agent --help
arc agent chat --help
arc run --help
arc run task --help
```
