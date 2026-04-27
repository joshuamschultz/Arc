"""Pure argparse handlers for the `arc agent` subcommand group.

All handlers are plain functions: argparse for CLI parsing, stdlib for I/O,
asyncio for the ArcAgent integration layer. No Click, no CliRunner.

Subcommand dispatch uses a simple argparse-based dispatcher so the top-level
`arc agent <sub> [args]` contract is preserved exactly.

Layer contract: this module may import from arcagent, arcrun, arcllm.
It MUST NOT import click or arccli.main_legacy.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib
import json
import os
import sys
import tomllib
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Internal helpers (duplicated lightly from arccli.agent to avoid importing
# Click decorators into this module's namespace)
# ---------------------------------------------------------------------------

_GLOBAL_SKILL_DIR = Path.home() / ".arcagent" / "skills"
_GLOBAL_EXT_DIR = Path.home() / ".arcagent" / "extensions"

# ---------------------------------------------------------------------------
# Agent scaffolding templates (kept here so this module is self-contained)
# ---------------------------------------------------------------------------

_DEFAULT_IDENTITY = """\
# Agent Identity

You are a helpful assistant with access to tools and a structured workspace.

## About Me

**My Name:** (Update when you learn your name)

**My Role:** (Update when you learn your purpose or how you should behave)

## About the User

**User's Name:** (Update when you learn the user's name)

## Behavior

**CRITICAL: You MUST use tools - never just say you did something.**

1. **ALWAYS use tools** when saving, reading, or searching
2. **Be direct and concise** - No filler, no hedging
3. **Show your work** - Report what tools you used and what they returned
"""

_DEFAULT_POLICY = """\
# Policy

- [P01] Be helpful and direct
- [P02] Use tools when appropriate
- [P03] Report errors clearly
"""

_DEFAULT_CONTEXT = """\
# Context

Working memory for the agent. Updated during conversations.
"""

_DEFAULT_CONFIG = """\
[agent]
name = "{name}"
org = "local"
type = "executor"
workspace = "./workspace"

[llm]
model = "anthropic/claude-sonnet-4-5-20250929"
max_tokens = 8192
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

[session]
retention_count = 50
retention_days = 30

[extensions]
global_dir = "~/.arcagent/extensions"

[modules.memory]
enabled = true

[modules.memory.config]
context_budget_tokens = 2000
entity_extraction_enabled = true

[modules.policy]
enabled = true

[modules.policy.config]
eval_interval_turns = 5

[modules.scheduler]
enabled = true

[modules.scheduler.config]
check_interval_seconds = 30
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

_ENV_PATHS = [
    Path.cwd() / ".env",
    Path.home() / ".arc" / ".env",
    Path.home() / ".env",
]


def _load_env(agent_dir: Path | None = None) -> None:
    """Load .env files without importing click."""
    try:
        from dotenv import load_dotenv
    except ImportError:
        return  # dotenv optional for status/read-only commands
    paths = list(_ENV_PATHS)
    if agent_dir is not None:
        paths.insert(0, agent_dir / ".env")
    for env_path in paths:
        if env_path.exists():
            load_dotenv(env_path)


def _resolve_agent_dir(path: str) -> Path:
    """Resolve and validate an agent directory path."""
    agent_dir = Path(path).expanduser().resolve()
    if not agent_dir.exists():
        sys.stderr.write(f"arc agent: directory not found: {agent_dir}\n")
        sys.exit(1)
    return agent_dir


def _load_agent_config(agent_dir: Path) -> dict[str, Any]:
    """Load arcagent.toml; exit 1 on failure."""
    config_path = agent_dir / "arcagent.toml"
    if not config_path.exists():
        sys.stderr.write(f"arc agent: no arcagent.toml in {agent_dir}\n")
        sys.exit(1)
    with open(config_path, "rb") as f:
        return tomllib.load(f)


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
            sys.stdout.write(f"  Warning: could not load tools/{tf.name}: {e}\n")
    sys.path.pop(0)
    return all_tools


def _scaffold_workspace(agent_dir: Path, name: str) -> None:
    """Create the full workspace directory structure."""
    workspace = agent_dir / "workspace"
    workspace.mkdir(exist_ok=True)

    identity_path = workspace / "identity.md"
    if not identity_path.exists():
        identity_path.write_text(_DEFAULT_IDENTITY)

    policy_path = workspace / "policy.md"
    if not policy_path.exists():
        policy_path.write_text(_DEFAULT_POLICY)

    context_path = workspace / "context.md"
    if not context_path.exists():
        context_path.write_text(_DEFAULT_CONTEXT)

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

    tools_dir = agent_dir / "tools"
    tools_dir.mkdir(exist_ok=True)
    init_file = tools_dir / "__init__.py"
    if not init_file.exists():
        init_file.write_text("")


def _print_scaffold_summary(display_name: str, agent_dir: Path) -> None:
    """Print workspace structure and next-steps after scaffold."""
    sys.stdout.write("\n")
    sys.stdout.write("Structure:\n")
    sys.stdout.write(f"  {display_name}/\n")
    sys.stdout.write("    arcagent.toml\n")
    sys.stdout.write("    workspace/\n")
    sys.stdout.write("      identity.md, policy.md, context.md\n")
    sys.stdout.write("      notes/, entities/\n")
    sys.stdout.write("      skills/, skills/_agent-created/\n")
    sys.stdout.write("      extensions/\n")
    sys.stdout.write("        calculator.py\n")
    sys.stdout.write("      sessions/, archive/\n")
    sys.stdout.write("      library/scripts/, templates/, prompts/, data/, snippets/\n")
    sys.stdout.write("    tools/\n")
    sys.stdout.write("\n")
    sys.stdout.write("Next steps:\n")
    sys.stdout.write(f"  arc agent build {agent_dir}\n")
    sys.stdout.write(f"  arc agent chat {agent_dir}\n")


def _print_kv(pairs: list[tuple[str, str]]) -> None:
    """Print key-value pairs in aligned format."""
    try:
        from arccli.formatting import print_kv

        print_kv(pairs)
    except ImportError:
        width = max(len(k) for k, _ in pairs) if pairs else 0
        for k, v in pairs:
            sys.stdout.write(f"  {k:<{width}}  {v}\n")


def _print_table(headers: list[str], rows: list[list[str]]) -> None:
    """Print a table with headers."""
    try:
        from arccli.formatting import print_table

        print_table(headers, rows)
    except ImportError:
        sys.stdout.write("  " + "  ".join(headers) + "\n")
        for row in rows:
            sys.stdout.write("  " + "  ".join(row) + "\n")


# ---------------------------------------------------------------------------
# Subcommand implementations
# ---------------------------------------------------------------------------


def _status(args: argparse.Namespace) -> None:
    """Show agent status: config, workspace, tools, skills, extensions, sessions."""
    agent_dir = _resolve_agent_dir(args.path)
    config = _load_agent_config(agent_dir)
    workspace = agent_dir / "workspace"

    agent_name = config.get("agent", {}).get("name", "?")
    model_id = config.get("llm", {}).get("model", "?")
    did = config.get("identity", {}).get("did", "(not set)")

    tool_count = len(_discover_tools(agent_dir))

    skill_count = 0
    for skill_dir in [workspace / "skills", _GLOBAL_SKILL_DIR]:
        if skill_dir.is_dir():
            skill_count += len(list(skill_dir.glob("*.md")))

    ext_count = 0
    for ext_dir in [workspace / "extensions", _GLOBAL_EXT_DIR]:
        if ext_dir.is_dir():
            ext_count += len([f for f in ext_dir.glob("*.py") if not f.name.startswith("_")])

    sessions_dir = workspace / "sessions"
    session_count = 0
    latest_session = "none"
    if sessions_dir.is_dir():
        session_files = sorted(
            sessions_dir.glob("*.jsonl"),
            key=lambda f: f.stat().st_mtime,
            reverse=True,
        )
        session_count = len(session_files)
        if session_files:
            latest = session_files[0]
            mtime = datetime.fromtimestamp(latest.stat().st_mtime, tz=UTC)
            latest_session = f"{latest.stem} ({mtime.strftime('%Y-%m-%d %H:%M')})"

    _print_kv(
        [
            ("Name", agent_name),
            ("DID", did or "(not set)"),
            ("Model", model_id),
            ("Tools", str(tool_count)),
            ("Skills", str(skill_count)),
            ("Extensions", str(ext_count)),
            ("Sessions", str(session_count)),
            ("Latest session", latest_session),
            ("Path", str(agent_dir)),
        ]
    )


def _skills(args: argparse.Namespace) -> None:
    """List discovered skills for an agent."""
    agent_dir = _resolve_agent_dir(args.path)

    skills: list[Any] = []
    try:
        from arcagent.core.skill_registry import SkillRegistry

        registry = SkillRegistry()
        workspace = agent_dir / "workspace"
        skills = registry.discover(workspace, _GLOBAL_SKILL_DIR)
    except ImportError:
        try:
            from arccli.commands.skill import _discover_skills_fallback

            skills = _discover_skills_fallback(str(agent_dir))
        except (ImportError, AttributeError):
            skills = []

    if not skills:
        sys.stdout.write("No skills found.\n")
        return

    rows = []
    for s in skills:
        name = s.name if hasattr(s, "name") else s.get("name", "?")
        desc = s.description if hasattr(s, "description") else s.get("description", "")
        cat = s.category if hasattr(s, "category") else s.get("category", "")
        if len(desc) > 50:
            desc = desc[:47] + "..."
        rows.append([name, desc, cat])

    _print_table(["Name", "Description", "Category"], rows)


def _extensions(args: argparse.Namespace) -> None:
    """List loaded extensions for an agent."""
    agent_dir = _resolve_agent_dir(args.path)
    workspace = agent_dir / "workspace"

    rows: list[list[str]] = []
    ext_dirs = [
        ("workspace", workspace / "extensions"),
        ("global", _GLOBAL_EXT_DIR),
    ]
    for source, directory in ext_dirs:
        if not directory.is_dir():
            continue
        for py_file in sorted(directory.glob("*.py")):
            if py_file.name.startswith("_"):
                continue
            rows.append([py_file.stem, source, str(py_file)])

    if rows:
        _print_table(["Name", "Source", "Path"], rows)
    else:
        sys.stdout.write("No extensions found.\n")


def _sessions(args: argparse.Namespace) -> None:
    """List session transcripts for an agent."""
    agent_dir = _resolve_agent_dir(args.path)
    sessions_dir = agent_dir / "workspace" / "sessions"

    if not sessions_dir.is_dir():
        sys.stdout.write("No sessions directory found.\n")
        return

    session_files = sorted(
        sessions_dir.glob("*.jsonl"),
        key=lambda f: f.stat().st_mtime,
        reverse=True,
    )
    if not session_files:
        sys.stdout.write("No sessions found.\n")
        return

    rows = []
    for sf in session_files:
        stat = sf.stat()
        mtime = datetime.fromtimestamp(stat.st_mtime, tz=UTC)
        line_count = sum(1 for _ in open(sf))
        size_kb = stat.st_size / 1024
        rows.append(
            [
                sf.stem,
                mtime.strftime("%Y-%m-%d %H:%M"),
                str(line_count),
                f"{size_kb:.1f} KB",
            ]
        )

    _print_table(["Session ID", "Last Modified", "Messages", "Size"], rows)


def _build(args: argparse.Namespace) -> None:
    """Validate agent config and workspace (--check) or run interactive build.

    Interactive build (without --check) requires terminal prompts. In
    non-interactive contexts (CI, subprocess) use --check instead.
    """
    agent_dir = _resolve_agent_dir(args.path)
    _load_env(agent_dir)

    if args.check:
        _run_validation(agent_dir)
        return

    # Interactive build: prompt the user for model/provider/settings.
    # This path requires a real TTY; it cannot run in a CliRunner sandbox.
    _run_interactive_build(agent_dir)


def _run_validation(agent_dir: Path) -> None:
    """Validation-only path for `arc agent build --check`."""
    from arcrun.strategies import STRATEGIES, _load_strategies

    provider_env_vars = {
        "anthropic": "ANTHROPIC_API_KEY",
        "openai": "OPENAI_API_KEY",
        "azure_openai": "AZURE_OPENAI_API_KEY",
        "ollama": "",
        "groq": "GROQ_API_KEY",
        "cohere": "COHERE_API_KEY",
        "mistral": "MISTRAL_API_KEY",
    }

    checks: list[tuple[str, str]] = []
    all_ok = True

    config_path = agent_dir / "arcagent.toml"
    if config_path.exists():
        try:
            config = _load_agent_config(agent_dir)
            checks.append(("OK", f"arcagent.toml ({config['agent']['name']})"))
        except SystemExit:
            checks.append(("FAIL", "arcagent.toml: parse error"))
            all_ok = False
            config = {}
    else:
        checks.append(("FAIL", "arcagent.toml not found"))
        all_ok = False
        config = {}

    workspace = agent_dir / "workspace"
    if workspace.is_dir():
        for fname in ("identity.md", "policy.md", "context.md"):
            fpath = workspace / fname
            if fpath.exists():
                char_count = len(fpath.read_text().strip())
                checks.append(("OK", f"workspace/{fname} ({char_count} chars)"))
            elif fname == "identity.md":
                checks.append(("WARN", "workspace/identity.md not found"))
    else:
        checks.append(("WARN", "workspace/ not found"))

    model_id = config.get("llm", {}).get("model", "")
    if model_id:
        provider = model_id.split("/")[0] if "/" in model_id else model_id
        checks.append(("OK", f"model: {model_id}"))
        env_var = provider_env_vars.get(provider, f"{provider.upper()}_API_KEY")
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

    try:
        if not STRATEGIES:
            _load_strategies()
        checks.append(("OK", f"strategies: {', '.join(STRATEGIES.keys())}"))
    except Exception:
        checks.append(("WARN", "could not load strategies"))

    for status, desc in checks:
        marker = {"OK": "+", "WARN": "~", "FAIL": "x"}[status]
        sys.stdout.write(f"  [{marker}] {desc}\n")

    sys.stdout.write("\n")
    if all_ok:
        sys.stdout.write("Ready. Run:\n")
        sys.stdout.write(f"  arc agent chat {agent_dir}\n")
    else:
        sys.stdout.write("Fix the issues above, or run:\n")
        sys.stdout.write(f"  arc agent build {agent_dir}\n")
        sys.exit(1)


def _run_interactive_build(agent_dir: Path) -> None:
    """Interactive build wizard — prompts user for model/provider/settings.

    This function requires a real TTY (stdin must be a terminal). It uses
    Python's built-in input() for prompts so there is no Click dependency.
    For non-interactive/CI contexts, use arc agent build --check instead.
    """
    # `os` is already imported at module level.
    provider_models = {
        "anthropic": [
            "claude-sonnet-4-5-20250929",
            "claude-opus-4-6",
        ],
        "openai": ["gpt-4o-mini", "gpt-4o"],
        "groq": ["llama-3.3-70b-versatile", "mixtral-8x7b-32768"],
        "deepseek": ["deepseek-chat"],
        "ollama": ["llama3.2", "mistral"],
        "azure_openai": [],
    }
    provider_env_vars = {
        "anthropic": "ANTHROPIC_API_KEY",
        "openai": "OPENAI_API_KEY",
        "azure_openai": "AZURE_OPENAI_API_KEY",
        "groq": "GROQ_API_KEY",
        "deepseek": "DEEPSEEK_API_KEY",
        "mistral": "MISTRAL_API_KEY",
        "ollama": "",
    }

    config: dict[str, Any] = {}
    config_path = agent_dir / "arcagent.toml"
    if config_path.exists():
        config = _load_agent_config(agent_dir)
        sys.stdout.write(
            f"Existing config: {config.get('agent', {}).get('name', '?')}\n"
        )

    def _prompt(label: str, default: str) -> str:
        sys.stdout.write(f"{label} [{default}]: ")
        sys.stdout.flush()
        try:
            val = input().strip()
        except EOFError:
            val = ""
        return val if val else default

    current_name = config.get("agent", {}).get("name", agent_dir.name)
    name = _prompt("Agent name", current_name)

    providers = list(provider_models.keys())
    sys.stdout.write("\nAvailable providers:\n")
    for i, p in enumerate(providers, 1):
        env_var = provider_env_vars.get(p, "")
        has_key = p == "ollama" or bool(os.environ.get(env_var))
        status = "ready" if has_key else f"needs {env_var}"
        sys.stdout.write(f"  {i}. {p} ({status})\n")

    default_model = config.get("llm", {}).get(
        "model", "anthropic/claude-sonnet-4-5-20250929"
    )
    current_provider = default_model.split("/")[0] if "/" in default_model else "anthropic"
    default_idx = providers.index(current_provider) + 1 if current_provider in providers else 1

    provider_str = _prompt(f"\nSelect provider (1-{len(providers)})", str(default_idx))
    try:
        provider_idx = int(provider_str) - 1
        provider = providers[provider_idx]
    except (ValueError, IndexError):
        provider = "anthropic"

    models = provider_models.get(provider, [])
    if models:
        sys.stdout.write(f"\nModels for {provider}:\n")
        for i, m in enumerate(models, 1):
            sys.stdout.write(f"  {i}. {m}\n")
        sys.stdout.write(f"  {len(models) + 1}. (custom)\n")
        model_str = _prompt(f"Select model (1-{len(models) + 1})", "1")
        try:
            model_idx = int(model_str) - 1
            default_name = models[0] if models else ""
            if model_idx < len(models):
                model_name = models[model_idx]
            else:
                model_name = _prompt("Model name", default_name)
        except (ValueError, IndexError):
            model_name = models[0] if models else ""
    else:
        model_name = _prompt("Model name", "")

    model_id = f"{provider}/{model_name}" if model_name else default_model
    llm_cfg = config.get("llm", {})
    max_tokens = int(_prompt("Max output tokens", str(llm_cfg.get("max_tokens", 4096))))
    temperature = float(_prompt("Temperature (0.0-1.0)", str(llm_cfg.get("temperature", 0.7))))

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
max_tokens = 128000

[eval]
provider = ""
model = ""
max_tokens = 1024
temperature = 0.2
fallback_behavior = "skip"

[session]
retention_count = 50
retention_days = 30

[extensions]
global_dir = "~/.arcagent/extensions"

[modules.memory]
enabled = true

[modules.memory.config]
context_budget_tokens = 2000
entity_extraction_enabled = true

[modules.policy]
enabled = true

[modules.policy.config]
eval_interval_turns = 5

[modules.scheduler]
enabled = true

[modules.scheduler.config]
check_interval_seconds = 30
"""
    config_path.write_text(config_toml)

    workspace = agent_dir / "workspace"
    workspace.mkdir(exist_ok=True)
    _scaffold_workspace(agent_dir, name)

    sys.stdout.write("\nSetup complete!\n")
    sys.stdout.write(f"  Name:    {name}\n")
    sys.stdout.write(f"  Model:   {model_id}\n")
    sys.stdout.write("\nRun validation:\n")
    sys.stdout.write(f"  arc agent build {agent_dir} --check\n")
    sys.stdout.write("\nStart chatting:\n")
    sys.stdout.write(f"  arc agent chat {agent_dir}\n")


def _tools(args: argparse.Namespace) -> None:
    """List all tools available to an agent."""
    agent_dir = _resolve_agent_dir(args.path)
    tools = _discover_tools(agent_dir)

    if getattr(args, "with_code_exec", False):
        from arcrun import make_execute_tool

        tools.append(make_execute_tool())

    if getattr(args, "json", False):
        data = [
            {
                "name": t.name,
                "description": t.description,
                "input_schema": t.input_schema,
                "timeout_seconds": t.timeout_seconds,
            }
            for t in tools
        ]
        sys.stdout.write(json.dumps(data, indent=2) + "\n")
        return

    if not tools:
        sys.stdout.write("No tools found.\n")
        return
    for t in tools:
        sys.stdout.write(f"  {t.name}\n")
        sys.stdout.write(f"    {t.description}\n")
        params = t.input_schema.get("properties", {})
        required = t.input_schema.get("required", [])
        if params:
            for pname, pdef in params.items():
                req = " (required)" if pname in required else ""
                ptype = pdef.get("type", "?")
                pdesc = pdef.get("description", "")
                sys.stdout.write(f"    - {pname}: {ptype}{req} — {pdesc}\n")
        if t.timeout_seconds:
            sys.stdout.write(f"    timeout: {t.timeout_seconds}s\n")
        sys.stdout.write("\n")


def _config(args: argparse.Namespace) -> None:
    """Show agent configuration."""
    agent_dir = _resolve_agent_dir(args.path)
    config = _load_agent_config(agent_dir)

    if getattr(args, "json", False):
        sys.stdout.write(json.dumps(config, indent=2) + "\n")
        return

    for section, values in config.items():
        sys.stdout.write(f"[{section}]\n")
        if isinstance(values, dict):
            for key, val in values.items():
                sys.stdout.write(f"  {key} = {val}\n")
        else:
            sys.stdout.write(f"  {values}\n")
        sys.stdout.write("\n")


def _reload(args: argparse.Namespace) -> None:
    """Hot-reload extensions and skills for an agent."""
    agent_dir = _resolve_agent_dir(args.path)
    _load_env(agent_dir)

    from arcagent.core.agent import ArcAgent
    from arcagent.core.config import load_config

    config_path = agent_dir / "arcagent.toml"
    config = load_config(config_path)
    arc_agent = ArcAgent(config, config_path=config_path)

    async def _do_reload() -> None:
        await arc_agent.startup()
        try:
            await arc_agent.reload()
            sys.stdout.write("Reload complete.\n")
            sys.stdout.write(f"  Skills:     {len(arc_agent.skills)}\n")
        finally:
            await arc_agent.shutdown()

    asyncio.run(_do_reload())


def _strategies(_args: argparse.Namespace) -> None:
    """List available execution strategies."""
    from arcrun.strategies import STRATEGIES, _load_strategies

    if not STRATEGIES:
        _load_strategies()
    for name, strat in STRATEGIES.items():
        sys.stdout.write(f"  {name}: {strat.description}\n")


def _events(_args: argparse.Namespace) -> None:
    """List all event types emitted by arcrun and arcagent."""
    events = [
        ("loop.start", "run() called", "task, tool_names, strategy"),
        ("loop.complete", "Execution finished", "content, turns, tool_calls, tokens, cost"),
        ("loop.max_turns", "Hit turn limit", "turns_used, max_turns"),
        ("strategy.selected", "Strategy chosen", "strategy"),
        ("turn.start", "Loop iteration begins", "turn_number"),
        ("turn.end", "Loop iteration ends", "turn_number"),
        (
            "llm.call",
            "model.invoke() returned",
            "model, stop_reason, tokens, latency_ms, cost_usd",
        ),
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
    _print_table(["Event", "When", "Data Keys"], [[e, w, d] for e, w, d in events])


def _create(args: argparse.Namespace) -> None:
    """Scaffold a new agent directory with example tools."""
    name: str = args.name
    parent_dir: str = getattr(args, "parent_dir", ".")
    model: str = getattr(args, "model", "anthropic/claude-sonnet-4-5-20250929")

    parent = Path(parent_dir).expanduser().resolve()
    agent_dir = parent / name

    if agent_dir.exists():
        sys.stderr.write(f"Error: Directory already exists: {agent_dir}\n")
        sys.exit(1)

    agent_dir.mkdir(parents=True)

    config_content = _DEFAULT_CONFIG.format(name=name)
    if model != "anthropic/claude-sonnet-4-5-20250929":
        config_content = config_content.replace(
            'model = "anthropic/claude-sonnet-4-5-20250929"',
            f'model = "{model}"',
        )
    (agent_dir / "arcagent.toml").write_text(config_content)

    _scaffold_workspace(agent_dir, name)

    ext_path = agent_dir / "workspace" / "extensions" / "calculator.py"
    ext_path.write_text(_CALCULATOR_EXTENSION)

    sys.stdout.write(f"Created agent: {agent_dir}\n")
    _print_scaffold_summary(name, agent_dir)


def _load_arcagent(agent_dir: Path) -> tuple[Any, Any, Path]:
    """Load ArcAgent from agent directory.

    Returns (ArcAgent instance, ArcAgentConfig, config_path).
    Exits 1 with a clear message if arcagent.toml is missing or
    ArcAgent / load_config cannot be imported.
    """
    from arcagent.core.agent import ArcAgent
    from arcagent.core.config import load_config

    config_path = agent_dir / "arcagent.toml"
    if not config_path.exists():
        sys.stderr.write(f"arc agent: no arcagent.toml in {agent_dir}\n")
        sys.exit(1)

    config = load_config(config_path)
    arc_agent = ArcAgent(config, config_path=config_path)
    return arc_agent, config, config_path


def _print_result_json(result: Any) -> None:
    """Serialize a LoopResult to JSON and write to stdout."""
    data = {
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
    }
    sys.stdout.write(json.dumps(data, indent=2) + "\n")


async def _agent_run_once(
    agent_dir: Path,
    task: str,
    model_override: str | None,
    verbose: bool,
    as_json: bool,
) -> None:
    """One-shot task execution coroutine."""
    arc_agent, config, _config_path = _load_arcagent(agent_dir)
    _scaffold_workspace(agent_dir, config.agent.name)

    if model_override:
        config.llm.model = model_override

    await arc_agent.startup()
    try:
        result = await arc_agent.run(task)

        if as_json:
            _print_result_json(result)
        else:
            if result.content:
                sys.stdout.write(result.content + "\n")
            if verbose:
                sys.stdout.write(
                    f"\n[{result.turns} turns, {result.tool_calls_made} tool calls, "
                    f"${result.cost_usd:.4f}, strategy={result.strategy_used}]\n"
                )
    finally:
        await arc_agent.shutdown()


def _run(args: argparse.Namespace) -> None:
    """Run a task against an agent (non-interactive one-shot)."""
    agent_dir = _resolve_agent_dir(args.path)
    _load_env(agent_dir)
    asyncio.run(
        _agent_run_once(
            agent_dir=agent_dir,
            task=args.task,
            model_override=getattr(args, "model", None),
            verbose=getattr(args, "verbose", False),
            as_json=getattr(args, "as_json", False),
        )
    )


async def _serve_daemon(
    agent_dir: Path,
    shutdown_event: asyncio.Event,
    verbose: bool,
    *,
    ui: bool = False,
) -> None:
    """Async serve coroutine — startup, wait for shutdown, cleanup."""
    import logging

    arc_agent, config, _config_path = _load_arcagent(agent_dir)
    _scaffold_workspace(agent_dir, config.agent.name)

    # Route logs to stderr so systemd/supervisord captures them.
    logging.basicConfig(
        level=logging.WARNING,
        format="%(name)s %(levelname)s: %(message)s",
    )
    logging.getLogger("arcagent").setLevel(logging.INFO)
    logging.getLogger("arcagent.audit").setLevel(logging.WARNING)
    logging.getLogger("arcagent.tool_registry").setLevel(logging.WARNING)
    if verbose:
        logging.getLogger("arcagent").setLevel(logging.DEBUG)
        logging.getLogger("arcagent.audit").setLevel(logging.INFO)
        logging.getLogger("arcagent.tool_registry").setLevel(logging.INFO)
        logging.getLogger("httpx").setLevel(logging.INFO)

    if ui:
        _enable_ui_reporter(config)

    await arc_agent.startup()
    agent_name = config.agent.name
    sys.stdout.write(f"Serving agent: {agent_name}\n")
    sys.stdout.write("Scheduler active. Press Ctrl+C to stop.\n")
    sys.stdout.write("-" * 40 + "\n")
    sys.stdout.flush()

    try:
        await shutdown_event.wait()
    finally:
        sys.stdout.write("\nShutting down...\n")
        sys.stdout.flush()
        await arc_agent.shutdown()
        sys.stdout.write("Done.\n")
        sys.stdout.flush()


def _enable_ui_reporter(config: Any) -> None:
    """Enable the UI reporter module in agent config."""
    from arcagent.core.config import ModuleEntry

    entry = ModuleEntry(enabled=True, config={"enabled": True})
    config.modules["ui_reporter"] = entry


def _serve(args: argparse.Namespace) -> None:
    """Start a long-running agent daemon."""
    import signal

    agent_dir = _resolve_agent_dir(args.path)
    _load_env(agent_dir)
    verbose = getattr(args, "verbose", False)
    ui = getattr(args, "ui", False)

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    shutdown_event = asyncio.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, shutdown_event.set)

    try:
        loop.run_until_complete(
            _serve_daemon(agent_dir, shutdown_event, verbose, ui=ui)
        )
    finally:
        loop.close()


async def _chat_interactive(
    agent_dir: Path,
    model_override: str | None,
    max_turns: int,
    verbose: bool,
    session_id: str | None,
) -> None:
    """Interactive REPL chat coroutine via ArcAgent."""
    arc_agent, config, _config_path = _load_arcagent(agent_dir)
    _scaffold_workspace(agent_dir, config.agent.name)

    if model_override:
        config.llm.model = model_override

    await arc_agent.startup()

    agent_name = config.agent.name
    model_id = config.llm.model

    sys.stdout.write(f"Agent: {agent_name}  |  Model: {model_id}\n")
    sys.stdout.write(f"Skills: {len(arc_agent.skills)}\n")
    sys.stdout.write("\n")
    sys.stdout.write("Commands:\n")
    sys.stdout.write("  /quit              Exit\n")
    sys.stdout.write("  /tools             List tools\n")
    sys.stdout.write("  /model             Show model\n")
    sys.stdout.write("  /cost              Session cost\n")
    sys.stdout.write("  /reload            Hot-reload extensions and skills\n")
    sys.stdout.write("  /skills            List available skills\n")
    sys.stdout.write("  /extensions        List loaded extensions\n")
    sys.stdout.write("  /session           Show current session info\n")
    sys.stdout.write("  /sessions          List all sessions\n")
    sys.stdout.write("  /identity          Show agent DID and identity\n")
    sys.stdout.write("  /status            Show agent status summary\n")
    sys.stdout.write("-" * 60 + "\n")
    sys.stdout.flush()

    total_cost = 0.0
    total_turns = 0
    total_tool_calls = 0
    current_session_id = session_id

    try:
        while True:
            try:
                sys.stdout.write("\nyou> ")
                sys.stdout.flush()
                user_input = input().strip()
            except (EOFError, KeyboardInterrupt):
                sys.stdout.write("\n")
                break

            if not user_input:
                continue

            if user_input == "/quit":
                break

            if user_input == "/tools":
                if arc_agent._tool_registry is not None:
                    tools = arc_agent._tool_registry.to_arcrun_tools()
                    for t in tools:
                        sys.stdout.write(f"  {t.name}: {t.description}\n")
                else:
                    sys.stdout.write("  Tool registry not initialized.\n")
                continue

            if user_input == "/model":
                sys.stdout.write(f"  {config.llm.model}\n")
                continue

            if user_input == "/cost":
                sys.stdout.write(
                    f"  Session: ${total_cost:.4f} "
                    f"({total_turns} turns, {total_tool_calls} tool calls)\n"
                )
                continue

            if user_input == "/reload":
                await arc_agent.reload()
                sys.stdout.write(f"  Reloaded. Skills: {len(arc_agent.skills)}\n")
                continue

            if user_input == "/skills":
                skills = arc_agent.skills
                if not skills:
                    sys.stdout.write("  No skills loaded.\n")
                else:
                    for s in skills:
                        sys.stdout.write(f"  {s.name}: {s.description}\n")
                continue

            if user_input == "/extensions":
                workspace = agent_dir / "workspace"
                found = False
                for source, directory in [
                    ("workspace", workspace / "extensions"),
                    ("global", _GLOBAL_EXT_DIR),
                ]:
                    if not directory.is_dir():
                        continue
                    for py_file in sorted(directory.glob("*.py")):
                        if py_file.name.startswith("_"):
                            continue
                        sys.stdout.write(f"  {py_file.stem} ({source})\n")
                        found = True
                if not found:
                    sys.stdout.write("  No extensions found.\n")
                continue

            if user_input == "/session":
                if arc_agent._session is not None:
                    sys.stdout.write(f"  Session ID: {arc_agent._session.session_id}\n")
                    sys.stdout.write(f"  Messages:   {arc_agent._session.message_count}\n")
                else:
                    sys.stdout.write("  No active session.\n")
                continue

            if user_input == "/sessions":
                sessions_dir = agent_dir / "workspace" / "sessions"
                if not sessions_dir.is_dir():
                    sys.stdout.write("  No sessions directory.\n")
                else:
                    session_files = sorted(
                        sessions_dir.glob("*.jsonl"),
                        key=lambda f: f.stat().st_mtime,
                        reverse=True,
                    )
                    if not session_files:
                        sys.stdout.write("  No sessions.\n")
                    else:
                        for sf in session_files[:10]:
                            mtime = datetime.fromtimestamp(sf.stat().st_mtime, tz=UTC)
                            line_count = sum(1 for _ in open(sf))
                            sys.stdout.write(
                                f"  {sf.stem}  "
                                f"({mtime.strftime('%Y-%m-%d %H:%M')}, "
                                f"{line_count} msgs)\n"
                            )
                continue

            if user_input.startswith("/switch"):
                arg = user_input[len("/switch"):].strip()
                if arg:
                    current_session_id = arg
                    sys.stdout.write(f"  Switched to session: {arg}\n")
                else:
                    sys.stdout.write("  Usage: /switch <session-id>\n")
                continue

            if user_input == "/identity":
                if arc_agent._identity is not None:
                    sys.stdout.write(f"  DID: {arc_agent._identity.did}\n")
                    sys.stdout.write(f"  Can sign: {arc_agent._identity.can_sign}\n")
                else:
                    sys.stdout.write("  Identity not initialized.\n")
                continue

            if user_input == "/status":
                sys.stdout.write(f"  Agent:      {arc_agent._config.agent.name}\n")
                sys.stdout.write(f"  Model:      {arc_agent._config.llm.model}\n")
                if arc_agent._identity:
                    sys.stdout.write(f"  DID:        {arc_agent._identity.did}\n")
                sys.stdout.write(f"  Skills:     {len(arc_agent.skills)}\n")
                sys.stdout.write(f"  Cost:       ${total_cost:.4f}\n")
                sys.stdout.write(f"  Turns:      {total_turns}\n")
                sys.stdout.write(f"  Tool calls: {total_tool_calls}\n")
                continue

            if user_input.startswith("/"):
                sys.stdout.write(f"  Unknown command: {user_input}\n")
                continue

            # Execute task via ArcAgent
            try:
                result = await arc_agent.chat(user_input, session_id=current_session_id)

                total_cost += result.cost_usd
                total_turns += result.turns
                total_tool_calls += result.tool_calls_made

                sys.stdout.write("\n")
                if result.content:
                    sys.stdout.write(result.content + "\n")

                if verbose:
                    sys.stdout.write(
                        f"\n[{result.turns} turns, {result.tool_calls_made} tool calls, "
                        f"${result.cost_usd:.4f}, strategy={result.strategy_used}]\n"
                    )
                sys.stdout.flush()
            except Exception as e:
                sys.stdout.write(f"\nError: {e}\n")

    finally:
        await arc_agent.shutdown()

    sys.stdout.write(
        f"\nSession: ${total_cost:.4f} total "
        f"({total_turns} turns, {total_tool_calls} tool calls)\n"
    )


def _chat(args: argparse.Namespace) -> None:
    """Start interactive chat session with an agent."""
    agent_dir = _resolve_agent_dir(args.path)
    _load_env(agent_dir)
    task = getattr(args, "task", None)
    verbose = getattr(args, "verbose", False)
    session_id = getattr(args, "session_id", None)

    if task:
        # One-shot mode: reuse _agent_run_once
        asyncio.run(
            _agent_run_once(
                agent_dir=agent_dir,
                task=task,
                model_override=getattr(args, "model", None),
                verbose=verbose,
                as_json=False,
            )
        )
    else:
        # Interactive REPL mode
        asyncio.run(
            _chat_interactive(
                agent_dir=agent_dir,
                model_override=getattr(args, "model", None),
                max_turns=getattr(args, "max_turns", 10),
                verbose=verbose,
                session_id=session_id,
            )
        )


# ---------------------------------------------------------------------------
# Argparse-based dispatcher
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    """Build the argparse parser for `arc agent <sub> [args]`."""
    parser = argparse.ArgumentParser(
        prog="arc agent",
        description="Agent management — create, build, chat with agents.",
        add_help=True,
    )
    subs = parser.add_subparsers(dest="subcmd", metavar="<subcommand>")

    # create
    p = subs.add_parser("create", help="Scaffold a new agent directory.")
    p.add_argument("name", help="Agent name.")
    p.add_argument("--dir", dest="parent_dir", default=".", help="Parent directory (default: .)")
    p.add_argument(
        "--model",
        default="anthropic/claude-sonnet-4-5-20250929",
        help="LLM model.",
    )
    p.add_argument("--with-code-exec", dest="with_code_exec", action="store_true")

    # status
    p = subs.add_parser("status", help="Show agent status summary.")
    p.add_argument("path", nargs="?", default=".", help="Agent directory (default: .)")

    # skills
    p = subs.add_parser("skills", help="List agent skills.")
    p.add_argument("path", nargs="?", default=".", help="Agent directory (default: .)")

    # extensions
    p = subs.add_parser("extensions", help="List agent extensions.")
    p.add_argument("path", nargs="?", default=".", help="Agent directory (default: .)")

    # sessions
    p = subs.add_parser("sessions", help="List agent sessions.")
    p.add_argument("path", nargs="?", default=".", help="Agent directory (default: .)")

    # build
    p = subs.add_parser("build", help="Interactive build / validate agent setup.")
    p.add_argument("path", nargs="?", default=".", help="Agent directory (default: .)")
    p.add_argument("--check", action="store_true", help="Validate only; skip interactive setup.")

    # tools
    p = subs.add_parser("tools", help="List agent tools.")
    p.add_argument("path", nargs="?", default=".", help="Agent directory (default: .)")
    p.add_argument("--json", dest="json", action="store_true", help="Output as JSON.")
    p.add_argument("--with-code-exec", dest="with_code_exec", action="store_true")

    # config
    p = subs.add_parser("config", help="Show agent configuration.")
    p.add_argument("path", nargs="?", default=".", help="Agent directory (default: .)")
    p.add_argument("--json", dest="json", action="store_true", help="Output as JSON.")

    # reload
    p = subs.add_parser("reload", help="Hot-reload extensions and skills.")
    p.add_argument("path", nargs="?", default=".", help="Agent directory (default: .)")

    # strategies
    subs.add_parser("strategies", help="List available execution strategies.")

    # events
    subs.add_parser("events", help="List all event types.")

    # run
    p = subs.add_parser("run", help="Run a task against an agent.")
    p.add_argument("path", help="Agent directory.")
    p.add_argument("task", help="Task to run.")
    p.add_argument("--verbose", "-v", action="store_true")
    p.add_argument("--max-turns", dest="max_turns", type=int, default=None)

    # serve
    p = subs.add_parser("serve", help="Start agent daemon.")
    p.add_argument("path", nargs="?", default=".", help="Agent directory (default: .)")
    p.add_argument("--verbose", "-v", action="store_true")
    p.add_argument(
        "--ui",
        action="store_true",
        help="Enable UI reporter module (streams events to ArcUI).",
    )

    # chat
    p = subs.add_parser("chat", help="Interactive chat session.")
    p.add_argument("path", nargs="?", default=".", help="Agent directory (default: .)")
    p.add_argument("--task", "-t", default=None, help="One-shot task instead of REPL.")
    p.add_argument("--verbose", "-v", action="store_true")
    p.add_argument("--model", default=None, help="Override model (provider/model).")
    p.add_argument("--max-turns", dest="max_turns", type=int, default=10)
    p.add_argument(
        "--session-id",
        dest="session_id",
        default=None,
        help="Resume a specific session.",
    )

    return parser


_SUBCOMMAND_MAP = {
    "create": _create,
    "status": _status,
    "skills": _skills,
    "extensions": _extensions,
    "sessions": _sessions,
    "build": _build,
    "tools": _tools,
    "config": _config,
    "reload": _reload,
    "strategies": _strategies,
    "events": _events,
    "run": _run,
    "serve": _serve,
    "chat": _chat,
}


def agent_handler(args: list[str]) -> None:
    """Top-level handler for `arc agent <sub> [args]`.

    Called by arccli.commands.registry when the user runs `arc agent ...`.
    """
    parser = _build_parser()

    if not args:
        parser.print_help()
        sys.exit(0)

    parsed = parser.parse_args(args)

    if parsed.subcmd is None:
        parser.print_help()
        sys.exit(0)

    fn = _SUBCOMMAND_MAP.get(parsed.subcmd)
    if fn is None:
        sys.stderr.write(f"arc agent: unknown subcommand '{parsed.subcmd}'\n")
        sys.exit(1)

    fn(parsed)
