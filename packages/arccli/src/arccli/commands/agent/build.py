"""`arc agent build` — interactive build wizard or `--check` validation."""

from __future__ import annotations

import argparse
import importlib
import os
import sys
from pathlib import Path
from typing import Any

from arccli.commands.agent._common import (
    _load_agent_config,
    _load_env,
    _resolve_agent_dir,
    _scaffold_workspace,
)


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
                except Exception as e:  # reason: fail-open — continue
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
    except Exception:  # reason: fail-open — continue
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
        sys.stdout.write(f"Existing config: {config.get('agent', {}).get('name', '?')}\n")

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

    default_model = config.get("llm", {}).get("model", "anthropic/claude-sonnet-4-5-20250929")
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

[security]
tier = "personal"

[security.validators]
auto_run_agent_code = false

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
