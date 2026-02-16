"""Agent CLI subcommands — create, build, chat with full ArcAgent orchestration."""

from __future__ import annotations

import asyncio
import importlib
import json
import os
import sys
import textwrap
import tomllib
import uuid
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import click
from dotenv import load_dotenv

from arccli.formatting import click_echo, print_kv, print_table

# Default .env search paths (tried in order)
_ENV_PATHS = [
    Path.home() / "AI" / "arcrun" / ".env",
    Path.home() / ".env",
    Path.cwd() / ".env",
]

_GLOBAL_SKILL_DIR = Path.home() / ".arcagent" / "skills"
_GLOBAL_EXT_DIR = Path.home() / ".arcagent" / "extensions"

# Default agent templates
_DEFAULT_IDENTITY = """\
You are a helpful assistant with access to tools.

When given a task:
1. Think about what tools you need
2. Use them to gather information
3. Provide a clear, concise answer

Be direct. No filler. Show your work when using tools.
"""

_DEFAULT_CONFIG = """\
[agent]
name = "{name}"
org = "local"
type = "executor"
workspace = "./workspace"

[llm]
model = "anthropic/claude-haiku-4-5-20251001"
max_tokens = 4096
temperature = 0.7

[identity]
did = ""
key_dir = "~/.arcagent/keys"

[vault]
backend = ""

[tools.policy]
allow = []
deny = []
timeout_seconds = 30

[telemetry]
enabled = true
service_name = "{name}"
log_level = "INFO"
export_traces = false

[context]
max_tokens = 128000

[eval]
provider = ""
model = ""
max_tokens = 1024
temperature = 0.2
fallback_behavior = "skip"

[memory]
context_budget_tokens = 2000
entity_extraction_enabled = false

[session]
retention_count = 50
retention_days = 30

[extensions]
global_dir = "~/.arcagent/extensions"
"""

_DEFAULT_POLICY = """\
# Policy

## Rules
- Be helpful and direct
- Use tools when appropriate
- Report errors clearly
"""

_DEFAULT_CONTEXT = """\
# Context

Working memory for the agent. Updated during conversations.
"""


_CALCULATOR_EXTENSION = '''\
"""Extension: calculator

Registers a safe math calculator tool with ArcAgent.
"""

from __future__ import annotations

import ast
import operator


def extension(api):
    """Factory function called by ExtensionLoader."""
    from arcrun import Tool, ToolContext

    _OPS = {
        ast.Add: operator.add,
        ast.Sub: operator.sub,
        ast.Mult: operator.mul,
        ast.Div: operator.truediv,
        ast.Mod: operator.mod,
        ast.Pow: operator.pow,
        ast.USub: operator.neg,
        ast.UAdd: operator.pos,
    }

    def _safe_eval(node: ast.AST) -> float:
        """Recursively evaluate an AST math expression."""
        if isinstance(node, ast.Expression):
            return _safe_eval(node.body)
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
            return node.value
        if isinstance(node, ast.BinOp):
            op_fn = _OPS.get(type(node.op))
            if op_fn is None:
                raise ValueError(f"Unsupported operator: {type(node.op).__name__}")
            return op_fn(_safe_eval(node.left), _safe_eval(node.right))
        if isinstance(node, ast.UnaryOp):
            op_fn = _OPS.get(type(node.op))
            if op_fn is None:
                raise ValueError(f"Unsupported operator: {type(node.op).__name__}")
            return op_fn(_safe_eval(node.operand))
        raise ValueError(f"Unsupported expression: {ast.dump(node)}")

    async def calculate(params: dict, ctx: ToolContext) -> str:
        """Evaluate a math expression safely using AST parsing."""
        expr = params["expression"]
        try:
            tree = ast.parse(expr, mode="eval")
            result = _safe_eval(tree)
            return str(result)
        except Exception as e:
            return f"Error: {e}"

    api.register_tool(
        Tool(
            name="calculate",
            description="Evaluate a math expression. Supports +, -, *, /, %, **.",
            input_schema={
                "type": "object",
                "properties": {
                    "expression": {
                        "type": "string",
                        "description": "Math expression to evaluate",
                    },
                },
                "required": ["expression"],
            },
            execute=calculate,
        )
    )
'''


def _load_env() -> None:
    """Load API keys from .env files."""
    for env_path in _ENV_PATHS:
        if env_path.exists():
            load_dotenv(env_path)


def _resolve_agent_dir(path: str) -> Path:
    """Resolve agent directory from path argument."""
    agent_dir = Path(path).expanduser().resolve()
    if not agent_dir.exists():
        raise click.ClickException(f"Agent directory not found: {agent_dir}")
    return agent_dir


def _load_agent_config(agent_dir: Path) -> dict[str, Any]:
    """Load and parse arcagent.toml from agent directory."""
    config_path = agent_dir / "arcagent.toml"
    if not config_path.exists():
        raise click.ClickException(f"No arcagent.toml in {agent_dir}")
    with open(config_path, "rb") as f:
        return tomllib.load(f)


def _load_arcagent(agent_dir: Path) -> tuple[Any, Any, Path]:
    """Load ArcAgent from agent directory.

    Returns (ArcAgent instance, ArcAgentConfig, config_path).
    """
    from arcagent.core.agent import ArcAgent
    from arcagent.core.config import load_config

    config_path = agent_dir / "arcagent.toml"
    if not config_path.exists():
        raise click.ClickException(f"No arcagent.toml in {agent_dir}")

    config = load_config(config_path)
    arc_agent = ArcAgent(config, config_path=config_path)
    return arc_agent, config, config_path


def _parse_model_id(model_id: str) -> tuple[str, str | None]:
    """Split 'provider/model' into (provider, model_name)."""
    if "/" in model_id:
        provider, _, model_name = model_id.partition("/")
        return provider, model_name
    return model_id, None


def _discover_tools(agent_dir: Path) -> list[Any]:
    """Import all tools from agent's tools/ directory."""
    tools_dir = agent_dir / "tools"
    if not tools_dir.is_dir():
        return []

    sys.path.insert(0, str(agent_dir))
    all_tools: list[Any] = []

    for tf in sorted(tools_dir.glob("*.py")):
        if tf.name == "__init__.py":
            continue
        module_name = f"tools.{tf.stem}"
        try:
            mod = importlib.import_module(module_name)
            if hasattr(mod, "get_tools"):
                all_tools.extend(mod.get_tools())
        except Exception as e:
            click_echo(f"  Warning: could not load tools/{tf.name}: {e}")

    sys.path.pop(0)
    return all_tools


def _make_event_logger(verbose: bool, show_events: bool = False):
    """Create an on_event callback for arcrun."""
    collected: list[Any] = []

    def log_event(event: Any) -> None:
        if show_events:
            collected.append(event)
        if not verbose:
            return
        if event.type == "tool.start":
            click_echo(f"  [tool]   {event.data['name']}({event.data['arguments']})")
        elif event.type == "tool.end":
            click_echo(
                f"  [tool]   {event.data['name']} -> "
                f"{event.data['result_length']} chars "
                f"({event.data['duration_ms']:.0f}ms)"
            )
        elif event.type == "tool.denied":
            click_echo(f"  [denied] {event.data['name']}: {event.data['reason']}")
        elif event.type == "tool.error":
            click_echo(f"  [error]  {event.data['name']}: {event.data['error']}")
        elif event.type == "llm.call":
            click_echo(
                f"  [llm]    stop={event.data['stop_reason']}, "
                f"latency={event.data['latency_ms']:.0f}ms"
            )
        elif event.type == "turn.start":
            click_echo(f"  [turn]   --- turn {event.data['turn_number']} ---")
        elif event.type == "strategy.selected":
            click_echo(f"  [strat]  {event.data['strategy']}")

    log_event._collected = collected  # type: ignore[attr-defined]
    return log_event


def _scaffold_workspace(agent_dir: Path, name: str) -> None:
    """Create the full Phase 1b workspace directory structure."""
    workspace = agent_dir / "workspace"
    workspace.mkdir(exist_ok=True)

    # Core workspace files
    identity_path = workspace / "identity.md"
    if not identity_path.exists():
        identity_path.write_text(_DEFAULT_IDENTITY)

    policy_path = workspace / "policy.md"
    if not policy_path.exists():
        policy_path.write_text(_DEFAULT_POLICY)

    context_path = workspace / "context.md"
    if not context_path.exists():
        context_path.write_text(_DEFAULT_CONTEXT)

    # Workspace subdirectories
    for subdir in [
        "notes",
        "entities",
        "skills",
        "skills/_agent-created",
        "extensions",
        "sessions",
        "archive",
        "library",
        "library/scripts",
        "library/templates",
        "library/prompts",
        "library/data",
        "library/snippets",
    ]:
        (workspace / subdir).mkdir(parents=True, exist_ok=True)

    # Tools directory at agent root
    tools_dir = agent_dir / "tools"
    tools_dir.mkdir(exist_ok=True)
    init_file = tools_dir / "__init__.py"
    if not init_file.exists():
        init_file.write_text("")


# ---------------------------------------------------------------------------
# Top-level group
# ---------------------------------------------------------------------------


@click.group()
def agent() -> None:
    """Agent commands — create, build, and chat with agents."""


# ---------------------------------------------------------------------------
# init — bootstrap full workspace
# ---------------------------------------------------------------------------


@agent.command()
@click.argument("path", default=".")
@click.option("--name", default=None, help="Agent name (default: directory name).")
@click.option("--model", default="anthropic/claude-haiku-4-5-20251001", help="LLM model.")
@click.option("--interactive", "-i", is_flag=True, help="Interactive setup prompts.")
def init(path: str, name: str | None, model: str, interactive: bool) -> None:
    """Bootstrap a full agent workspace.

    Creates the complete Phase 1b directory structure with arcagent.toml,
    workspace files, and all subdirectories.

    \b
    Examples:
      arc agent init my-agent
      arc agent init my-agent --name "Research Bot" --model openai/gpt-4o
      arc agent init . -i
    """
    agent_dir = Path(path).expanduser().resolve()

    if agent_dir == Path.cwd() and not (agent_dir / "arcagent.toml").exists():
        # Initializing in current directory
        pass
    elif not agent_dir.exists():
        agent_dir.mkdir(parents=True)

    agent_name = name or agent_dir.name

    if interactive:
        agent_name = click.prompt("Agent name", default=agent_name)
        model = click.prompt("Model", default=model)

    # Write config
    config_path = agent_dir / "arcagent.toml"
    if not config_path.exists():
        config_content = _DEFAULT_CONFIG.format(name=agent_name)
        if model != "anthropic/claude-haiku-4-5-20251001":
            config_content = config_content.replace(
                'model = "anthropic/claude-haiku-4-5-20251001"',
                f'model = "{model}"',
            )
        config_path.write_text(config_content)

    _scaffold_workspace(agent_dir, agent_name)

    # Write calculator extension if not present
    ext_path = agent_dir / "workspace" / "extensions" / "calculator.py"
    if not ext_path.exists():
        ext_path.write_text(_CALCULATOR_EXTENSION)

    click_echo(f"Initialized agent workspace: {agent_dir}")
    click_echo()
    click_echo("Structure:")
    click_echo(f"  {agent_dir.name}/")
    click_echo(f"    arcagent.toml")
    click_echo(f"    workspace/")
    click_echo(f"      identity.md, policy.md, context.md")
    click_echo(f"      notes/, entities/")
    click_echo(f"      skills/, skills/_agent-created/")
    click_echo(f"      extensions/")
    click_echo(f"        calculator.py")
    click_echo(f"      sessions/, archive/")
    click_echo(f"      library/scripts/, templates/, prompts/, data/, snippets/")
    click_echo(f"    tools/")
    click_echo()
    click_echo("Next steps:")
    click_echo(f"  arc agent build {agent_dir}")
    click_echo(f"  arc agent chat {agent_dir}")


# ---------------------------------------------------------------------------
# create — scaffold with example tool
# ---------------------------------------------------------------------------


@agent.command()
@click.argument("name")
@click.option("--dir", "parent_dir", default=".", help="Parent directory (default: cwd).")
@click.option("--model", default="anthropic/claude-haiku-4-5-20251001", help="LLM model.")
@click.option("--with-code-exec", is_flag=True, help="Include execute_python tool.")
def create(name: str, parent_dir: str, model: str, with_code_exec: bool) -> None:
    """Scaffold a new agent directory with example tools.

    Creates NAME/ with arcagent.toml, full workspace structure,
    an extension-based calculator tool, and tools/ directory.
    """
    parent = Path(parent_dir).expanduser().resolve()
    agent_dir = parent / name

    if agent_dir.exists():
        raise click.ClickException(f"Directory already exists: {agent_dir}")

    agent_dir.mkdir(parents=True)

    # Write config
    config_content = _DEFAULT_CONFIG.format(name=name)
    if model != "anthropic/claude-haiku-4-5-20251001":
        config_content = config_content.replace(
            'model = "anthropic/claude-haiku-4-5-20251001"',
            f'model = "{model}"',
        )
    (agent_dir / "arcagent.toml").write_text(config_content)

    # Scaffold full workspace
    _scaffold_workspace(agent_dir, name)

    # Write calculator extension (arcagent extension pattern)
    ext_path = agent_dir / "workspace" / "extensions" / "calculator.py"
    ext_path.write_text(_CALCULATOR_EXTENSION)

    click_echo(f"Created agent: {agent_dir}")
    click_echo()
    click_echo("Structure:")
    click_echo(f"  {name}/")
    click_echo(f"    arcagent.toml")
    click_echo(f"    workspace/")
    click_echo(f"      identity.md, policy.md, context.md")
    click_echo(f"      notes/, entities/")
    click_echo(f"      skills/, skills/_agent-created/")
    click_echo(f"      extensions/")
    click_echo(f"        calculator.py")
    click_echo(f"      sessions/, archive/")
    click_echo(f"      library/scripts/, templates/, prompts/, data/, snippets/")
    click_echo(f"    tools/")
    click_echo()
    click_echo("Next steps:")
    click_echo(f"  arc agent build {agent_dir}")
    click_echo(f"  arc agent chat {agent_dir}")


# ---------------------------------------------------------------------------
# build — interactive onboarding + validation
# ---------------------------------------------------------------------------

_PROVIDER_MODELS = {
    "anthropic": [
        "claude-haiku-4-5-20251001",
        "claude-sonnet-4-5-20250929",
        "claude-opus-4-6",
    ],
    "openai": [
        "gpt-4o-mini",
        "gpt-4o",
        "o1-mini",
    ],
    "groq": [
        "llama-3.3-70b-versatile",
        "mixtral-8x7b-32768",
    ],
    "deepseek": [
        "deepseek-chat",
        "deepseek-reasoner",
    ],
    "ollama": [
        "llama3.2",
        "mistral",
        "codellama",
    ],
}

_PROVIDER_ENV_VARS = {
    "anthropic": "ANTHROPIC_API_KEY",
    "openai": "OPENAI_API_KEY",
    "google": "GOOGLE_API_KEY",
    "groq": "GROQ_API_KEY",
    "deepseek": "DEEPSEEK_API_KEY",
    "mistral": "MISTRAL_API_KEY",
    "together": "TOGETHER_API_KEY",
    "fireworks": "FIREWORKS_API_KEY",
}


@agent.command()
@click.argument("path", default=".")
@click.option("--check", is_flag=True, help="Validation only (skip interactive setup).")
def build(path: str, check: bool) -> None:
    """Interactive onboarding wizard for an agent.

    Walks through LLM provider/model selection, API key verification,
    system prompt setup, and tool configuration. Writes everything
    to arcagent.toml and workspace/.

    Use --check to skip the wizard and just validate.

    \b
    Examples:
      arc agent build my-agent          # interactive onboarding
      arc agent build my-agent --check  # validation only
    """
    agent_dir = _resolve_agent_dir(path)
    _load_env()

    if check:
        _run_validation(agent_dir)
        return

    click_echo(f"\nSetting up agent: {agent_dir.name}")
    click_echo("=" * 50)

    config = {}
    config_path = agent_dir / "arcagent.toml"
    if config_path.exists():
        config = _load_agent_config(agent_dir)
        click_echo(f"Existing config found: {config.get('agent', {}).get('name', '?')}")
        click_echo()

    # --- Step 1: Agent name ---
    current_name = config.get("agent", {}).get("name", agent_dir.name)
    name = click.prompt("Agent name", default=current_name)

    # --- Step 2: Provider ---
    click_echo("\nAvailable LLM providers:")
    providers = list(_PROVIDER_MODELS.keys())
    for i, p in enumerate(providers, 1):
        env_var = _PROVIDER_ENV_VARS.get(p, "")
        has_key = "ollama" == p or bool(os.environ.get(env_var))
        status = "ready" if has_key else f"needs {env_var}"
        click_echo(f"  {i}. {p} ({status})")

    current_model = config.get("llm", {}).get("model", "anthropic/claude-haiku-4-5-20251001")
    current_provider = current_model.split("/")[0] if "/" in current_model else "anthropic"

    provider_idx = click.prompt(
        "\nSelect provider",
        type=click.IntRange(1, len(providers)),
        default=providers.index(current_provider) + 1 if current_provider in providers else 1,
    )
    provider = providers[provider_idx - 1]

    # Check API key
    env_var = _PROVIDER_ENV_VARS.get(provider, "")
    if provider != "ollama" and not os.environ.get(env_var):
        click_echo(f"\n  {env_var} is not set.")
        click_echo(f"  Add it to your .env file or export it in your shell.")
        if click.confirm("  Continue anyway?", default=True):
            pass
        else:
            return

    # --- Step 3: Model ---
    models = _PROVIDER_MODELS.get(provider, [])
    click_echo(f"\nModels for {provider}:")
    for i, m in enumerate(models, 1):
        click_echo(f"  {i}. {m}")
    click_echo(f"  {len(models) + 1}. (custom)")

    current_model_name = current_model.split("/")[1] if "/" in current_model else ""
    default_idx = 1
    if current_model_name in models:
        default_idx = models.index(current_model_name) + 1

    model_idx = click.prompt(
        "Select model",
        type=click.IntRange(1, len(models) + 1),
        default=default_idx,
    )
    if model_idx <= len(models):
        model_name = models[model_idx - 1]
    else:
        model_name = click.prompt("Enter model name")

    model_id = f"{provider}/{model_name}"

    # --- Step 4: System prompt ---
    click_echo("\nSystem prompt (workspace/identity.md):")
    workspace = agent_dir / "workspace"
    workspace.mkdir(exist_ok=True)
    identity_path = workspace / "identity.md"

    if identity_path.exists():
        current_identity = identity_path.read_text().strip()
        click_echo(f"  Current: {current_identity[:80]}...")
        if click.confirm("  Edit system prompt?", default=False):
            click_echo("  Enter new prompt (blank line to finish):")
            lines: list[str] = []
            while True:
                try:
                    line = input("  > ")
                    if line == "":
                        break
                    lines.append(line)
                except EOFError:
                    break
            if lines:
                identity_path.write_text("\n".join(lines) + "\n")
                click_echo(f"  Updated ({len(lines)} lines)")
    else:
        click_echo("  No identity.md found. Using default.")
        identity_path.write_text(_DEFAULT_IDENTITY)

    # --- Step 5: Tools ---
    click_echo("\nTool setup:")
    tools_dir = agent_dir / "tools"
    tools_dir.mkdir(exist_ok=True)
    (tools_dir / "__init__.py").touch()

    tool_files = [f for f in tools_dir.glob("*.py") if f.name != "__init__.py"]
    if tool_files:
        click_echo(f"  Found {len(tool_files)} tool file(s): {', '.join(f.name for f in tool_files)}")
    else:
        click_echo("  No tools found.")
        if click.confirm("  Add example calculator tool?", default=True):
            _write_example_tool(tools_dir, include_code_exec=False)
            click_echo("  Created tools/example.py with calculate tool")

    include_code = click.confirm("  Include execute_python (sandboxed code execution)?", default=False)
    if include_code and not tool_files:
        _write_example_tool(tools_dir, include_code_exec=True)
        click_echo("  Updated tools/example.py with execute_python")
    elif include_code:
        click_echo("  Use --with-code-exec flag when running: arc agent chat ... --with-code-exec")

    # --- Step 6: Advanced settings ---
    max_tokens = config.get("llm", {}).get("max_tokens", 4096)
    temperature = config.get("llm", {}).get("temperature", 0.7)
    context_max = config.get("context", {}).get("max_tokens", 128000)

    if click.confirm("\nConfigure advanced settings?", default=False):
        max_tokens = click.prompt("  Max output tokens", default=max_tokens, type=int)
        temperature = click.prompt("  Temperature (0.0-1.0)", default=temperature, type=float)
        context_max = click.prompt("  Context window (tokens)", default=context_max, type=int)

    # --- Write config ---
    config_toml = f"""\
[agent]
name = "{name}"
org = "local"
type = "executor"
workspace = "./workspace"

[llm]
model = "{model_id}"
max_tokens = {max_tokens}
temperature = {temperature}

[identity]
did = ""
key_dir = "~/.arcagent/keys"

[vault]
backend = ""

[tools.policy]
allow = []
deny = []
timeout_seconds = 30

[telemetry]
enabled = true
service_name = "{name}"
log_level = "INFO"
export_traces = false

[context]
max_tokens = {context_max}

[eval]
provider = ""
model = ""
max_tokens = 1024
temperature = 0.2
fallback_behavior = "skip"

[memory]
context_budget_tokens = 2000
entity_extraction_enabled = false

[session]
retention_count = 50
retention_days = 30

[extensions]
global_dir = "~/.arcagent/extensions"
"""
    config_path.write_text(config_toml)

    # --- Summary ---
    click_echo("\n" + "=" * 50)
    click_echo("Setup complete!")
    click_echo()
    print_kv([
        ("Name", name),
        ("Model", model_id),
        ("Max tokens", str(max_tokens)),
        ("Temperature", str(temperature)),
        ("Context window", str(context_max)),
        ("Identity", str(identity_path)),
        ("Tools dir", str(tools_dir)),
    ])

    click_echo()
    click_echo("Run validation:")
    click_echo(f"  arc agent build {agent_dir} --check")
    click_echo()
    click_echo("Start chatting:")
    click_echo(f"  arc agent chat {agent_dir}")
    click_echo(f"  arc agent chat {agent_dir} --verbose")
    click_echo(f"  arc agent chat {agent_dir} --task 'Hello!'")


def _write_example_tool(tools_dir: Path, include_code_exec: bool) -> None:
    """Write the example tool file."""
    source = textwrap.dedent("""\
        \"\"\"Example tools for the agent.\"\"\"

        from __future__ import annotations

        import json

        from arcrun import Tool, ToolContext


        async def calculate(params: dict, ctx: ToolContext) -> str:
            \"\"\"Evaluate a math expression safely.\"\"\"
            expr = params["expression"]
            allowed = set("0123456789+-*/().% ")
            if not all(c in allowed for c in expr):
                return "Error: expression contains disallowed characters"
            try:
                result = eval(expr)  # noqa: S307
                return str(result)
            except Exception as e:
                return f"Error: {e}"


        def get_tools() -> list[Tool]:
            \"\"\"Return all tools defined in this module.\"\"\"
            tools = [
                Tool(
                    name="calculate",
                    description="Evaluate a math expression. Supports +, -, *, /, (), %.",
                    input_schema={
                        "type": "object",
                        "properties": {
                            "expression": {
                                "type": "string",
                                "description": "Math expression to evaluate",
                            },
                        },
                        "required": ["expression"],
                    },
                    execute=calculate,
                ),
            ]
    """)
    if include_code_exec:
        source += "    from arcrun import make_execute_tool\n"
        source += "    tools.append(make_execute_tool(timeout_seconds=30))\n"
    source += "    return tools\n"
    (tools_dir / "example.py").write_text(source)


def _run_validation(agent_dir: Path) -> None:
    """Validate-only mode for build."""
    checks: list[tuple[str, str]] = []
    all_ok = True

    # 1. Config
    config_path = agent_dir / "arcagent.toml"
    if config_path.exists():
        try:
            config = _load_agent_config(agent_dir)
            checks.append(("OK", f"arcagent.toml ({config['agent']['name']})"))
        except Exception as e:
            checks.append(("FAIL", f"arcagent.toml: {e}"))
            all_ok = False
            config = {}
    else:
        checks.append(("FAIL", "arcagent.toml not found"))
        all_ok = False
        config = {}

    # 2. Workspace
    workspace = agent_dir / "workspace"
    if workspace.is_dir():
        for fname in ("identity.md", "policy.md", "context.md"):
            fpath = workspace / fname
            if fpath.exists():
                checks.append(("OK", f"workspace/{fname} ({len(fpath.read_text().strip())} chars)"))
            elif fname == "identity.md":
                checks.append(("WARN", "workspace/identity.md not found"))
    else:
        checks.append(("WARN", "workspace/ not found"))

    # 3. Model + API key
    model_id = config.get("llm", {}).get("model", "")
    if model_id:
        provider = model_id.split("/")[0] if "/" in model_id else model_id
        checks.append(("OK", f"model: {model_id}"))
        env_var = _PROVIDER_ENV_VARS.get(provider, f"{provider.upper()}_API_KEY")
        if provider == "ollama":
            checks.append(("OK", "ollama (no key needed)"))
        elif os.environ.get(env_var):
            checks.append(("OK", f"{env_var} is set"))
        else:
            checks.append(("FAIL", f"{env_var} not set"))
            all_ok = False
    else:
        checks.append(("FAIL", "No model configured"))
        all_ok = False

    # 4. Tools
    tools_dir = agent_dir / "tools"
    if tools_dir.is_dir():
        tool_files = [f for f in tools_dir.glob("*.py") if f.name != "__init__.py"]
        if tool_files:
            sys.path.insert(0, str(agent_dir))
            total_tools = 0
            for tf in tool_files:
                module_name = f"tools.{tf.stem}"
                try:
                    mod = importlib.import_module(module_name)
                    if hasattr(mod, "get_tools"):
                        discovered = mod.get_tools()
                        total_tools += len(discovered)
                        for t in discovered:
                            checks.append(("OK", f"  tool: {t.name}"))
                except Exception as e:
                    checks.append(("WARN", f"tools/{tf.name}: {e}"))
            sys.path.pop(0)
            checks.append(("OK", f"tools: {total_tools} total"))
        else:
            checks.append(("WARN", "tools/ has no .py files"))
    else:
        checks.append(("WARN", "tools/ not found"))

    # 5. Strategies
    try:
        from arcrun.strategies import STRATEGIES, _load_strategies
        if not STRATEGIES:
            _load_strategies()
        checks.append(("OK", f"strategies: {', '.join(STRATEGIES.keys())}"))
    except Exception:
        checks.append(("WARN", "could not load strategies"))

    for status, desc in checks:
        marker = {"OK": "+", "WARN": "~", "FAIL": "x"}[status]
        click_echo(f"  [{marker}] {desc}")

    click_echo()
    if all_ok:
        click_echo("Ready. Run:")
        click_echo(f"  arc agent chat {agent_dir}")
    else:
        click_echo("Fix the issues above, or run:")
        click_echo(f"  arc agent build {agent_dir}")


# ---------------------------------------------------------------------------
# run — one-shot task execution through ArcAgent
# ---------------------------------------------------------------------------


@agent.command("run")
@click.argument("path")
@click.argument("task")
@click.option("--model", default=None, help="Override model from config.")
@click.option("--verbose", is_flag=True, help="Show tool/LLM events.")
@click.option("--json", "as_json", is_flag=True, help="Output as JSON.")
def agent_run(path: str, task: str, model: str | None, verbose: bool, as_json: bool) -> None:
    """One-shot task execution through ArcAgent.

    \b
    Examples:
      arc agent run my-agent "What is 2+2?"
      arc agent run my-agent "Summarize this file" --verbose
    """
    _load_env()
    agent_dir = _resolve_agent_dir(path)
    asyncio.run(_agent_run_once(agent_dir, task, model, verbose, as_json))


async def _agent_run_once(
    agent_dir: Path, task: str, model_override: str | None, verbose: bool, as_json: bool,
) -> None:
    arc_agent, config, config_path = _load_arcagent(agent_dir)

    if model_override:
        config.llm.model = model_override

    await arc_agent.startup()
    try:
        result = await arc_agent.run(task)

        if as_json:
            _print_result_json(result)
        else:
            if result.content:
                click_echo(result.content)
            if verbose:
                click_echo(
                    f"\n[{result.turns} turns, {result.tool_calls_made} tool calls, "
                    f"${result.cost_usd:.4f}, strategy={result.strategy_used}]"
                )
    finally:
        await arc_agent.shutdown()


# ---------------------------------------------------------------------------
# status — show agent status
# ---------------------------------------------------------------------------


@agent.command("status")
@click.argument("path", default=".")
def agent_status(path: str) -> None:
    """Show agent status: config, workspace, tools, skills, extensions, sessions."""
    agent_dir = _resolve_agent_dir(path)
    config = _load_agent_config(agent_dir)
    workspace = agent_dir / "workspace"

    agent_name = config.get("agent", {}).get("name", "?")
    model_id = config.get("llm", {}).get("model", "?")
    did = config.get("identity", {}).get("did", "(not set)")

    # Count tools
    tool_count = len(_discover_tools(agent_dir))

    # Count skills
    skill_count = 0
    for skill_dir in [workspace / "skills", _GLOBAL_SKILL_DIR]:
        if skill_dir.is_dir():
            skill_count += len(list(skill_dir.glob("*.md")))

    # Count extensions
    ext_count = 0
    for ext_dir in [workspace / "extensions", _GLOBAL_EXT_DIR]:
        if ext_dir.is_dir():
            ext_count += len([f for f in ext_dir.glob("*.py") if not f.name.startswith("_")])

    # Count sessions
    sessions_dir = workspace / "sessions"
    session_count = 0
    latest_session = "none"
    if sessions_dir.is_dir():
        session_files = sorted(sessions_dir.glob("*.jsonl"), key=lambda f: f.stat().st_mtime, reverse=True)
        session_count = len(session_files)
        if session_files:
            latest = session_files[0]
            mtime = datetime.fromtimestamp(latest.stat().st_mtime, tz=timezone.utc)
            latest_session = f"{latest.stem} ({mtime.strftime('%Y-%m-%d %H:%M')})"

    print_kv([
        ("Name", agent_name),
        ("DID", did or "(not set)"),
        ("Model", model_id),
        ("Tools", str(tool_count)),
        ("Skills", str(skill_count)),
        ("Extensions", str(ext_count)),
        ("Sessions", str(session_count)),
        ("Latest session", latest_session),
        ("Path", str(agent_dir)),
    ])


# ---------------------------------------------------------------------------
# reload — hot-reload extensions and skills
# ---------------------------------------------------------------------------


@agent.command("reload")
@click.argument("path", default=".")
def agent_reload(path: str) -> None:
    """Hot-reload extensions and skills for an agent."""
    agent_dir = _resolve_agent_dir(path)
    _load_env()
    asyncio.run(_agent_reload(agent_dir))


async def _agent_reload(agent_dir: Path) -> None:
    arc_agent, config, config_path = _load_arcagent(agent_dir)
    await arc_agent.startup()
    try:
        await arc_agent.reload()
        click_echo("Reload complete.")
        click_echo(f"  Skills:     {len(arc_agent.skills)}")
    finally:
        await arc_agent.shutdown()


# ---------------------------------------------------------------------------
# skills — list agent skills
# ---------------------------------------------------------------------------


@agent.command("skills")
@click.argument("path", default=".")
def agent_skills(path: str) -> None:
    """List discovered skills for an agent."""
    agent_dir = _resolve_agent_dir(path)

    try:
        from arcagent.core.skill_registry import SkillRegistry
        registry = SkillRegistry()
        workspace = agent_dir / "workspace"
        skills = registry.discover(workspace, _GLOBAL_SKILL_DIR)
    except ImportError:
        from arccli.skill import _discover_skills_fallback
        skills = _discover_skills_fallback(str(agent_dir))

    if not skills:
        click_echo("No skills found.")
        return

    rows = []
    for s in skills:
        name = s.name if hasattr(s, "name") else s.get("name", "?")
        desc = s.description if hasattr(s, "description") else s.get("description", "")
        cat = s.category if hasattr(s, "category") else s.get("category", "")
        if len(desc) > 50:
            desc = desc[:47] + "..."
        rows.append([name, desc, cat])

    print_table(["Name", "Description", "Category"], rows)


# ---------------------------------------------------------------------------
# extensions — list agent extensions
# ---------------------------------------------------------------------------


@agent.command("extensions")
@click.argument("path", default=".")
def agent_extensions(path: str) -> None:
    """List loaded extensions for an agent."""
    agent_dir = _resolve_agent_dir(path)
    workspace = agent_dir / "workspace"

    rows: list[list[str]] = []
    for source, directory in [("workspace", workspace / "extensions"), ("global", _GLOBAL_EXT_DIR)]:
        if not directory.is_dir():
            continue
        for py_file in sorted(directory.glob("*.py")):
            if py_file.name.startswith("_"):
                continue
            rows.append([py_file.stem, source, str(py_file)])

    if rows:
        print_table(["Name", "Source", "Path"], rows)
    else:
        click_echo("No extensions found.")


# ---------------------------------------------------------------------------
# sessions — list sessions
# ---------------------------------------------------------------------------


@agent.command("sessions")
@click.argument("path", default=".")
def agent_sessions(path: str) -> None:
    """List session transcripts for an agent."""
    agent_dir = _resolve_agent_dir(path)
    sessions_dir = agent_dir / "workspace" / "sessions"

    if not sessions_dir.is_dir():
        click_echo("No sessions directory found.")
        return

    session_files = sorted(sessions_dir.glob("*.jsonl"), key=lambda f: f.stat().st_mtime, reverse=True)
    if not session_files:
        click_echo("No sessions found.")
        return

    rows = []
    for sf in session_files:
        stat = sf.stat()
        mtime = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc)
        line_count = sum(1 for _ in open(sf))
        size_kb = stat.st_size / 1024
        rows.append([
            sf.stem,
            mtime.strftime("%Y-%m-%d %H:%M"),
            str(line_count),
            f"{size_kb:.1f} KB",
        ])

    print_table(["Session ID", "Last Modified", "Messages", "Size"], rows)


# ---------------------------------------------------------------------------
# session subgroup — resume, compact, fork
# ---------------------------------------------------------------------------


@agent.group("session")
def session_group() -> None:
    """Session management — resume, compact, fork."""


@session_group.command("resume")
@click.argument("path")
@click.argument("session_id")
@click.option("--verbose", is_flag=True, help="Show tool/LLM events.")
def session_resume(path: str, session_id: str, verbose: bool) -> None:
    """Resume a previous session.

    \b
    Examples:
      arc agent session resume my-agent abc123-def456
    """
    _load_env()
    agent_dir = _resolve_agent_dir(path)
    asyncio.run(_session_resume(agent_dir, session_id, verbose))


async def _session_resume(agent_dir: Path, session_id: str, verbose: bool) -> None:
    arc_agent, config, config_path = _load_arcagent(agent_dir)
    await arc_agent.startup()

    agent_name = config.agent.name
    model_id = config.llm.model

    click_echo(f"Resuming session: {session_id}")
    click_echo(f"Agent: {agent_name}  |  Model: {model_id}")
    click_echo("-" * 60)

    try:
        while True:
            try:
                user_input = input("\nyou> ").strip()
            except (EOFError, KeyboardInterrupt):
                click_echo()
                break

            if not user_input:
                continue
            if user_input == "/quit":
                break

            result = await arc_agent.chat(user_input, session_id=session_id)
            click_echo()
            if result.content:
                click_echo(result.content)
            if verbose:
                click_echo(
                    f"\n[{result.turns} turns, {result.tool_calls_made} tool calls, "
                    f"${result.cost_usd:.4f}]"
                )
    finally:
        await arc_agent.shutdown()


@session_group.command("compact")
@click.argument("path")
def session_compact(path: str) -> None:
    """Trigger compaction on the latest session."""
    _load_env()
    agent_dir = _resolve_agent_dir(path)
    asyncio.run(_session_compact(agent_dir))


async def _session_compact(agent_dir: Path) -> None:
    arc_agent, config, config_path = _load_arcagent(agent_dir)
    await arc_agent.startup()
    try:
        if arc_agent._session is not None:
            workspace = agent_dir / "workspace"
            await arc_agent._session.compact(arc_agent._model, workspace)
            click_echo("Compaction complete.")
        else:
            click_echo("No active session to compact.")
    finally:
        await arc_agent.shutdown()


@session_group.command("fork")
@click.argument("path")
def session_fork(path: str) -> None:
    """Fork the latest session to a new JSONL file."""
    agent_dir = _resolve_agent_dir(path)
    sessions_dir = agent_dir / "workspace" / "sessions"

    if not sessions_dir.is_dir():
        raise click.ClickException("No sessions directory found.")

    session_files = sorted(sessions_dir.glob("*.jsonl"), key=lambda f: f.stat().st_mtime, reverse=True)
    if not session_files:
        raise click.ClickException("No sessions to fork.")

    latest = session_files[0]
    new_id = str(uuid.uuid4())
    new_path = sessions_dir / f"{new_id}.jsonl"

    import shutil
    shutil.copy2(latest, new_path)
    click_echo(f"Forked: {latest.stem} -> {new_id}")


# ---------------------------------------------------------------------------
# settings — view/set runtime settings
# ---------------------------------------------------------------------------


@agent.command("settings")
@click.argument("path", default=".")
def agent_settings(path: str) -> None:
    """Show mutable runtime settings for an agent."""
    agent_dir = _resolve_agent_dir(path)
    _load_env()
    asyncio.run(_agent_settings(agent_dir))


async def _agent_settings(agent_dir: Path) -> None:
    arc_agent, config, config_path = _load_arcagent(agent_dir)
    await arc_agent.startup()
    try:
        if arc_agent.settings is None:
            click_echo("SettingsManager not available.")
            return

        from arcagent.core.settings_manager import SettingsManager
        pairs = []
        for key in SettingsManager.MUTABLE_KEYS:
            value = arc_agent.settings.get(key)
            pairs.append((key, str(value)))
        print_kv(pairs)
    finally:
        await arc_agent.shutdown()


@agent.command("set")
@click.argument("path")
@click.argument("key")
@click.argument("value")
def agent_settings_set(path: str, key: str, value: str) -> None:
    """Set a runtime setting for an agent.

    \b
    Examples:
      arc agent set my-agent model openai/gpt-4o
      arc agent set my-agent log_level DEBUG
    """
    _load_env()
    agent_dir = _resolve_agent_dir(path)
    asyncio.run(_agent_settings_set(agent_dir, key, value))


async def _agent_settings_set(agent_dir: Path, key: str, value: str) -> None:
    arc_agent, config, config_path = _load_arcagent(agent_dir)
    await arc_agent.startup()
    try:
        if arc_agent.settings is None:
            raise click.ClickException("SettingsManager not available.")

        # Type coerce based on key
        from arcagent.core.settings_manager import SettingsManager
        expected_type = SettingsManager.MUTABLE_KEYS.get(key)
        if expected_type is float:
            typed_value: Any = float(value)
        elif expected_type is int:
            typed_value = int(value)
        else:
            typed_value = value

        await arc_agent.settings.set(key, typed_value)
        click_echo(f"  {key} = {typed_value}")
    finally:
        await arc_agent.shutdown()


# ---------------------------------------------------------------------------
# chat — full-featured interactive + one-shot (rewired through ArcAgent)
# ---------------------------------------------------------------------------


@agent.command()
@click.argument("path", default=".")
@click.option("--task", default=None, help="Single task (non-interactive). Omit for REPL.")
@click.option("--model", default=None, help="Override model from config (provider/model).")
@click.option("--max-turns", default=10, type=int, help="Max loop iterations per task.")
@click.option("--tool-timeout", default=None, type=float, help="Global tool timeout (seconds).")
@click.option("--strategy", default=None, type=click.Choice(["react", "code"]), help="Force strategy.")
@click.option("--sandbox", "sandbox_tools", default=None, help="Comma-separated allowlist of tools.")
@click.option("--with-code-exec", is_flag=True, help="Add built-in execute_python tool.")
@click.option("--code-timeout", default=30, type=float, help="execute_python timeout (seconds).")
@click.option("--verbose", is_flag=True, help="Show tool/LLM events as they happen.")
@click.option("--show-events", is_flag=True, help="Print full event log after each run.")
@click.option("--json", "as_json", is_flag=True, help="Output result as JSON (one-shot only).")
@click.option("--session-id", default=None, help="Resume a specific session.")
def chat(
    path: str,
    task: str | None,
    model: str | None,
    max_turns: int,
    tool_timeout: float | None,
    strategy: str | None,
    sandbox_tools: str | None,
    with_code_exec: bool,
    code_timeout: float,
    verbose: bool,
    show_events: bool,
    as_json: bool,
    session_id: str | None,
) -> None:
    """Interactive terminal chat with an agent.

    Routes through ArcAgent orchestrator for full identity, telemetry,
    module bus, tool policy, context management, extensions, and skills.

    \b
    Examples:
      arc agent chat my-agent                          # interactive REPL
      arc agent chat my-agent --task "What is 2+2?"    # one-shot
      arc agent chat my-agent --strategy code --with-code-exec
      arc agent chat my-agent --sandbox "calculate"    # only allow calculate
      arc agent chat my-agent --verbose --show-events  # full observability
      arc agent chat my-agent --json --task "..."       # machine-readable output
    """
    agent_dir = _resolve_agent_dir(path)
    _load_env()

    if not os.environ.get("ANTHROPIC_API_KEY") and not os.environ.get("OPENAI_API_KEY"):
        raise click.ClickException(
            "No API key found. Set ANTHROPIC_API_KEY or OPENAI_API_KEY."
        )

    if task:
        if as_json:
            asyncio.run(_chat_oneshot_arcagent(
                agent_dir, task, model, verbose, as_json, max_turns,
            ))
        else:
            asyncio.run(_chat_oneshot_arcagent(
                agent_dir, task, model, verbose, as_json, max_turns,
            ))
    else:
        if as_json:
            raise click.ClickException("--json only works with --task (one-shot mode).")
        asyncio.run(_chat_interactive_arcagent(
            agent_dir=agent_dir,
            model_override=model,
            max_turns=max_turns,
            tool_timeout=tool_timeout,
            strategy=strategy,
            sandbox_tools=sandbox_tools,
            with_code_exec=with_code_exec,
            code_timeout=code_timeout,
            verbose=verbose,
            show_events=show_events,
            session_id=session_id,
        ))


async def _chat_oneshot_arcagent(
    agent_dir: Path,
    task: str,
    model_override: str | None,
    verbose: bool,
    as_json: bool,
    max_turns: int,
) -> None:
    """One-shot chat via ArcAgent."""
    arc_agent, config, config_path = _load_arcagent(agent_dir)

    if model_override:
        config.llm.model = model_override

    await arc_agent.startup()
    try:
        result = await arc_agent.run(task)

        if as_json:
            _print_result_json(result)
        else:
            if result.content:
                click_echo(result.content)
            if verbose:
                click_echo(
                    f"\n[{result.turns} turns, {result.tool_calls_made} tool calls, "
                    f"${result.cost_usd:.4f}, strategy={result.strategy_used}]"
                )
    finally:
        await arc_agent.shutdown()


async def _chat_interactive_arcagent(
    *,
    agent_dir: Path,
    model_override: str | None,
    max_turns: int,
    tool_timeout: float | None,
    strategy: str | None,
    sandbox_tools: str | None,
    with_code_exec: bool,
    code_timeout: float,
    verbose: bool,
    show_events: bool,
    session_id: str | None,
) -> None:
    """Interactive REPL chat via ArcAgent."""
    arc_agent, config, config_path = _load_arcagent(agent_dir)

    if model_override:
        config.llm.model = model_override

    await arc_agent.startup()

    agent_name = config.agent.name
    model_id = config.llm.model

    click_echo(f"Agent: {agent_name}  |  Model: {model_id}")
    click_echo(f"Skills: {len(arc_agent.skills)}")
    click_echo()
    click_echo("Commands:")
    click_echo("  /quit              Exit")
    click_echo("  /tools             List tools")
    click_echo("  /model             Show model")
    click_echo("  /cost              Session cost")
    click_echo("  /events            Show last run's events")
    click_echo("  /sandbox [tools]   Set sandbox (comma-separated, empty=clear)")
    click_echo("  /strategy [name]   Set strategy (react|code|auto)")
    click_echo("  /max-turns [n]     Set max turns")
    click_echo("  /add-code-exec     Add execute_python tool")
    click_echo("  /verbose           Toggle verbose mode")
    click_echo("  /reload            Hot-reload extensions and skills")
    click_echo("  /skills            List available skills")
    click_echo("  /extensions        List loaded extensions")
    click_echo("  /compact           Trigger session compaction")
    click_echo("  /session           Show current session info")
    click_echo("  /sessions          List all sessions")
    click_echo("  /switch <id>       Switch to a different session")
    click_echo("  /fork              Fork current session")
    click_echo("  /settings          Show runtime settings")
    click_echo("  /set <key> <val>   Modify a runtime setting")
    click_echo("  /identity          Show agent DID and identity")
    click_echo("  /status            Show agent status summary")
    click_echo("-" * 60)

    total_cost = 0.0
    total_turns = 0
    total_tool_calls = 0
    last_events: list[Any] = []
    current_session_id = session_id

    try:
        while True:
            try:
                user_input = input("\nyou> ").strip()
            except (EOFError, KeyboardInterrupt):
                click_echo()
                break

            if not user_input:
                continue

            # --- REPL commands ---
            if user_input == "/quit":
                break

            if user_input == "/tools":
                _repl_tools(arc_agent)
                continue

            if user_input == "/model":
                click_echo(f"  {config.llm.model}")
                continue

            if user_input == "/cost":
                click_echo(
                    f"  Session: ${total_cost:.4f} "
                    f"({total_turns} turns, {total_tool_calls} tool calls)"
                )
                continue

            if user_input == "/events":
                if last_events:
                    _print_events(last_events)
                else:
                    click_echo("  No events yet.")
                continue

            if user_input.startswith("/sandbox"):
                arg = user_input[len("/sandbox"):].strip()
                if arg:
                    click_echo(f"  Sandbox set: {arg}")
                else:
                    click_echo("  Sandbox cleared (all tools allowed)")
                continue

            if user_input.startswith("/strategy"):
                arg = user_input[len("/strategy"):].strip()
                if arg and arg != "auto":
                    click_echo(f"  Strategy: {arg}")
                else:
                    click_echo("  Strategy: auto (model selects)")
                continue

            if user_input.startswith("/max-turns"):
                arg = user_input[len("/max-turns"):].strip()
                try:
                    max_turns = int(arg)
                    click_echo(f"  Max turns: {max_turns}")
                except ValueError:
                    click_echo(f"  Current max turns: {max_turns}")
                continue

            if user_input == "/add-code-exec":
                click_echo("  execute_python available through ArcAgent tool registry")
                continue

            if user_input == "/verbose":
                verbose = not verbose
                click_echo(f"  Verbose: {'on' if verbose else 'off'}")
                continue

            # --- New REPL commands ---
            if user_input == "/reload":
                await arc_agent.reload()
                click_echo(f"  Reloaded. Skills: {len(arc_agent.skills)}")
                continue

            if user_input == "/skills":
                _repl_skills(arc_agent)
                continue

            if user_input == "/extensions":
                _repl_extensions(agent_dir)
                continue

            if user_input == "/compact":
                if arc_agent._session is not None:
                    workspace = agent_dir / "workspace"
                    await arc_agent._session.compact(arc_agent._model, workspace)
                    click_echo("  Compaction complete.")
                else:
                    click_echo("  No active session.")
                continue

            if user_input == "/session":
                if arc_agent._session is not None:
                    click_echo(f"  Session ID: {arc_agent._session.session_id}")
                    click_echo(f"  Messages:   {arc_agent._session.message_count}")
                else:
                    click_echo("  No active session.")
                continue

            if user_input == "/sessions":
                _repl_sessions(agent_dir)
                continue

            if user_input.startswith("/switch"):
                arg = user_input[len("/switch"):].strip()
                if arg:
                    current_session_id = arg
                    click_echo(f"  Switched to session: {arg}")
                else:
                    click_echo("  Usage: /switch <session-id>")
                continue

            if user_input == "/fork":
                sessions_dir = agent_dir / "workspace" / "sessions"
                if sessions_dir.is_dir():
                    session_files = sorted(
                        sessions_dir.glob("*.jsonl"),
                        key=lambda f: f.stat().st_mtime,
                        reverse=True,
                    )
                    if session_files:
                        import shutil
                        new_id = str(uuid.uuid4())
                        new_path = sessions_dir / f"{new_id}.jsonl"
                        shutil.copy2(session_files[0], new_path)
                        click_echo(f"  Forked to: {new_id}")
                    else:
                        click_echo("  No sessions to fork.")
                else:
                    click_echo("  No sessions directory.")
                continue

            if user_input == "/settings":
                await _repl_settings(arc_agent)
                continue

            if user_input.startswith("/set "):
                parts = user_input[len("/set "):].strip().split(None, 1)
                if len(parts) == 2:
                    key, value = parts
                    try:
                        if arc_agent.settings is not None:
                            from arcagent.core.settings_manager import SettingsManager
                            expected_type = SettingsManager.MUTABLE_KEYS.get(key)
                            if expected_type is float:
                                typed_val: Any = float(value)
                            elif expected_type is int:
                                typed_val = int(value)
                            else:
                                typed_val = value
                            await arc_agent.settings.set(key, typed_val)
                            click_echo(f"  {key} = {typed_val}")
                        else:
                            click_echo("  SettingsManager not available.")
                    except Exception as e:
                        click_echo(f"  Error: {e}")
                else:
                    click_echo("  Usage: /set <key> <value>")
                continue

            if user_input == "/identity":
                if arc_agent._identity is not None:
                    click_echo(f"  DID: {arc_agent._identity.did}")
                    click_echo(f"  Can sign: {arc_agent._identity.can_sign}")
                else:
                    click_echo("  Identity not initialized.")
                continue

            if user_input == "/status":
                _repl_status(arc_agent, agent_dir, total_cost, total_turns, total_tool_calls)
                continue

            if user_input.startswith("/"):
                click_echo(f"  Unknown command: {user_input}")
                continue

            # --- Execute task via ArcAgent ---
            try:
                result = await arc_agent.chat(user_input, session_id=current_session_id)

                total_cost += result.cost_usd
                total_turns += result.turns
                total_tool_calls += result.tool_calls_made

                click_echo()
                if result.content:
                    click_echo(result.content)

                if verbose:
                    click_echo(
                        f"\n[{result.turns} turns, {result.tool_calls_made} tool calls, "
                        f"${result.cost_usd:.4f}, strategy={result.strategy_used}]"
                    )
            except Exception as e:
                click_echo(f"\nError: {e}")

    finally:
        await arc_agent.shutdown()

    click_echo(
        f"\nSession: ${total_cost:.4f} total "
        f"({total_turns} turns, {total_tool_calls} tool calls)"
    )


# ---------------------------------------------------------------------------
# tools — list tools for an agent
# ---------------------------------------------------------------------------


@agent.command("tools")
@click.argument("path", default=".")
@click.option("--json", "as_json", is_flag=True, help="Output as JSON.")
@click.option("--with-code-exec", is_flag=True, help="Include built-in execute_python.")
def list_tools(path: str, as_json: bool, with_code_exec: bool) -> None:
    """List all tools available to an agent."""
    agent_dir = _resolve_agent_dir(path)
    tools = _discover_tools(agent_dir)

    if with_code_exec:
        from arcrun import make_execute_tool
        tools.append(make_execute_tool())

    if as_json:
        from arccli.formatting import print_json
        print_json([
            {
                "name": t.name,
                "description": t.description,
                "input_schema": t.input_schema,
                "timeout_seconds": t.timeout_seconds,
            }
            for t in tools
        ])
    else:
        if not tools:
            click_echo("No tools found.")
            return
        for t in tools:
            click_echo(f"  {t.name}")
            click_echo(f"    {t.description}")
            params = t.input_schema.get("properties", {})
            required = t.input_schema.get("required", [])
            if params:
                for pname, pdef in params.items():
                    req = " (required)" if pname in required else ""
                    click_echo(f"    - {pname}: {pdef.get('type', '?')}{req} — {pdef.get('description', '')}")
            if t.timeout_seconds:
                click_echo(f"    timeout: {t.timeout_seconds}s")
            click_echo()


# ---------------------------------------------------------------------------
# strategies — list available strategies
# ---------------------------------------------------------------------------


@agent.command("strategies")
def list_strategies() -> None:
    """List available execution strategies."""
    from arcrun.strategies import STRATEGIES, _load_strategies

    if not STRATEGIES:
        _load_strategies()

    for name, strat in STRATEGIES.items():
        click_echo(f"  {name}: {strat.description}")


# ---------------------------------------------------------------------------
# events — show event types and descriptions
# ---------------------------------------------------------------------------


@agent.command("events")
def list_events() -> None:
    """List all event types emitted by arcrun and arcagent."""
    events = [
        ("loop.start", "run() called", "task, tool_names, strategy"),
        ("loop.complete", "Execution finished", "content, turns, tool_calls, tokens, cost"),
        ("loop.max_turns", "Hit turn limit", "turns_used, max_turns"),
        ("strategy.selected", "Strategy chosen", "strategy"),
        ("turn.start", "Loop iteration begins", "turn_number"),
        ("turn.end", "Loop iteration ends", "turn_number"),
        ("llm.call", "model.invoke() returned", "model, stop_reason, tokens, latency_ms, cost_usd"),
        ("tool.start", "Tool execution begins", "name, arguments"),
        ("tool.end", "Tool execution complete", "name, result_length, duration_ms"),
        ("tool.denied", "Sandbox blocked tool", "name, reason"),
        ("tool.error", "Tool threw exception/timeout", "name, error"),
        ("tool.registered", "New tool added to registry", "name"),
        ("tool.replaced", "Existing tool replaced", "name"),
        ("tool.removed", "Tool removed from registry", "name"),
        ("agent:init", "ArcAgent startup complete", "agent_name, did"),
        ("agent:shutdown", "ArcAgent shutdown", ""),
        ("agent:pre_respond", "Before arcrun.run()", "task"),
        ("agent:post_respond", "After arcrun.run()", "content, turns"),
        ("agent:pre_tool", "Before tool execution", "name"),
        ("agent:post_tool", "After tool execution", "name, result_length"),
        ("agent:extensions_loaded", "Extensions discovered", "count"),
        ("agent:skills_loaded", "Skills discovered", "count"),
        ("agent:settings_changed", "Runtime setting changed", "key, value"),
    ]
    print_table(
        ["Event", "When", "Data Keys"],
        [[e, w, d] for e, w, d in events],
    )


# ---------------------------------------------------------------------------
# config — show agent config
# ---------------------------------------------------------------------------


@agent.command("config")
@click.argument("path", default=".")
@click.option("--json", "as_json", is_flag=True, help="Output as JSON.")
def show_config(path: str, as_json: bool) -> None:
    """Show agent configuration."""
    agent_dir = _resolve_agent_dir(path)
    config = _load_agent_config(agent_dir)

    if as_json:
        from arccli.formatting import print_json
        print_json(config)
    else:
        for section, values in config.items():
            click_echo(f"[{section}]")
            if isinstance(values, dict):
                for key, val in values.items():
                    click_echo(f"  {key} = {val}")
            else:
                click_echo(f"  {values}")
            click_echo()


# ---------------------------------------------------------------------------
# REPL helper functions
# ---------------------------------------------------------------------------


def _repl_tools(arc_agent: Any) -> None:
    """Print tools from ArcAgent's tool registry."""
    if arc_agent._tool_registry is not None:
        tools = arc_agent._tool_registry.to_arcrun_tools()
        for t in tools:
            click_echo(f"  {t.name}: {t.description}")
    else:
        click_echo("  Tool registry not initialized.")


def _repl_skills(arc_agent: Any) -> None:
    """Print skills from ArcAgent."""
    skills = arc_agent.skills
    if not skills:
        click_echo("  No skills loaded.")
        return
    for s in skills:
        click_echo(f"  {s.name}: {s.description}")


def _repl_extensions(agent_dir: Path) -> None:
    """Print extensions for agent."""
    workspace = agent_dir / "workspace"
    found = False
    for source, directory in [("workspace", workspace / "extensions"), ("global", _GLOBAL_EXT_DIR)]:
        if not directory.is_dir():
            continue
        for py_file in sorted(directory.glob("*.py")):
            if py_file.name.startswith("_"):
                continue
            click_echo(f"  {py_file.stem} ({source})")
            found = True
    if not found:
        click_echo("  No extensions found.")


def _repl_sessions(agent_dir: Path) -> None:
    """Print sessions list."""
    sessions_dir = agent_dir / "workspace" / "sessions"
    if not sessions_dir.is_dir():
        click_echo("  No sessions directory.")
        return
    session_files = sorted(
        sessions_dir.glob("*.jsonl"),
        key=lambda f: f.stat().st_mtime,
        reverse=True,
    )
    if not session_files:
        click_echo("  No sessions.")
        return
    for sf in session_files[:10]:
        mtime = datetime.fromtimestamp(sf.stat().st_mtime, tz=timezone.utc)
        line_count = sum(1 for _ in open(sf))
        click_echo(f"  {sf.stem}  ({mtime.strftime('%Y-%m-%d %H:%M')}, {line_count} msgs)")


async def _repl_settings(arc_agent: Any) -> None:
    """Print runtime settings."""
    if arc_agent.settings is None:
        click_echo("  SettingsManager not available.")
        return
    from arcagent.core.settings_manager import SettingsManager
    for key in SettingsManager.MUTABLE_KEYS:
        value = arc_agent.settings.get(key)
        click_echo(f"  {key} = {value}")


def _repl_status(arc_agent: Any, agent_dir: Path, cost: float, turns: int, tool_calls: int) -> None:
    """Print agent status summary."""
    click_echo(f"  Agent:      {arc_agent._config.agent.name}")
    click_echo(f"  Model:      {arc_agent._config.llm.model}")
    if arc_agent._identity:
        click_echo(f"  DID:        {arc_agent._identity.did}")
    click_echo(f"  Skills:     {len(arc_agent.skills)}")
    if arc_agent._session:
        click_echo(f"  Session:    {arc_agent._session.session_id} ({arc_agent._session.message_count} msgs)")
    click_echo(f"  Cost:       ${cost:.4f}")
    click_echo(f"  Turns:      {turns}")
    click_echo(f"  Tool calls: {tool_calls}")


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _assemble_system_prompt(workspace: Path) -> str:
    """Build system prompt from workspace files (identity.md, policy.md, context.md)."""
    prompt_files = ["identity.md", "policy.md", "context.md"]
    sections: list[str] = []
    for filename in prompt_files:
        filepath = workspace / filename
        if filepath.exists():
            content = filepath.read_text(encoding="utf-8").strip()
            if content:
                section_name = filename.removesuffix(".md")
                sections.append(f"--- {section_name} ---\n{content}")

    return "\n\n".join(sections) if sections else "You are a helpful assistant."


def _print_events(events: list[Any]) -> None:
    """Print collected events as a timeline."""
    click_echo("\nEvent Log:")
    for i, event in enumerate(events):
        data_str = str(event.data)
        if len(data_str) > 120:
            data_str = data_str[:120] + "..."
        click_echo(f"  {i + 1:3d}. [{event.type:25s}] {data_str}")

    click_echo()
    type_counts = Counter(e.type for e in events)
    click_echo("Event Summary:")
    for t, c in sorted(type_counts.items()):
        click_echo(f"  {t:25s}: {c}")


def _print_result_json(result: Any) -> None:
    """Print LoopResult as JSON."""
    from arccli.formatting import print_json
    print_json({
        "content": result.content,
        "turns": result.turns,
        "tool_calls_made": result.tool_calls_made,
        "tokens_used": result.tokens_used,
        "strategy_used": result.strategy_used,
        "cost_usd": result.cost_usd,
        "event_count": len(result.events),
        "events": [
            {
                "type": e.type,
                "timestamp": e.timestamp,
                "data": e.data,
            }
            for e in result.events
        ],
    })
