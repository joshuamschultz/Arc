"""Plain CommandDef handlers for the `arc llm` subcommand group.

T1.1.5 migration: replaces the legacy Click-based dispatch in registry.py.
Each function is a direct translation of the corresponding Click command body
in arccli.llm, with Click-specific calls replaced with stdlib equivalents.

Layer contract: this module may import from arcllm.
It MUST NOT import click or arccli.main_legacy.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _get_providers_dir() -> Path:
    """Return the providers/ directory inside the arcllm package."""
    import arcllm

    return Path(arcllm.__file__).parent / "providers"


def _list_provider_names() -> list[str]:
    """List available provider names by scanning TOML files."""
    providers_dir = _get_providers_dir()
    if not providers_dir.is_dir():
        return []
    return sorted(p.stem for p in providers_dir.glob("*.toml") if p.stem != "__init__")


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


def _print_json(data: Any) -> None:
    """Print data as indented JSON."""
    import json

    sys.stdout.write(json.dumps(data, indent=2, default=str) + "\n")


def _write(msg: str = "") -> None:
    """Write a line to stdout."""
    sys.stdout.write(msg + "\n")


# ---------------------------------------------------------------------------
# Subcommand implementations
# ---------------------------------------------------------------------------


def _version(args: argparse.Namespace) -> None:
    """Show version information."""
    import arcllm

    import arccli

    data = {
        "arccmd": arccli.__version__,
        "arcllm": getattr(arcllm, "__version__", "0.1.0"),
        "python": sys.version.split()[0],
    }
    if getattr(args, "as_json", False):
        _print_json(data)
    else:
        _print_kv(list(data.items()))


def _config(args: argparse.Namespace) -> None:
    """Show global ArcLLM configuration."""
    from arcllm.config import load_global_config

    cfg = load_global_config()
    module_name: str | None = getattr(args, "module", None)
    as_json: bool = getattr(args, "as_json", False)

    if module_name:
        mod = cfg.modules.get(module_name)
        if mod is None:
            sys.stderr.write(f"Error: Module '{module_name}' not found in config.\n")
            sys.exit(1)
        if as_json:
            _print_json({module_name: mod.model_dump()})
        else:
            _write(f"[modules.{module_name}]")
            for key, val in mod.model_dump().items():
                _write(f"  {key} = {val}")
        return

    data = {
        "defaults": cfg.defaults.model_dump(),
        "modules": {name: m.model_dump() for name, m in cfg.modules.items()},
        "vault": cfg.vault.model_dump(),
    }
    if as_json:
        _print_json(data)
    else:
        _write("[defaults]")
        for key, val in cfg.defaults.model_dump().items():
            _write(f"  {key} = {val}")
        _write()
        for name, mod in cfg.modules.items():
            _write(f"[modules.{name}]")
            for key, val in mod.model_dump().items():
                _write(f"  {key} = {val}")
            _write()
        _write("[vault]")
        for key, val in cfg.vault.model_dump().items():
            _write(f"  {key} = {val}")


def _providers(args: argparse.Namespace) -> None:
    """List all available providers."""
    from arcllm.config import load_provider_config

    as_json: bool = getattr(args, "as_json", False)
    names = _list_provider_names()
    rows = []
    for name in names:
        try:
            cfg = load_provider_config(name)
            rows.append(
                {
                    "name": name,
                    "api_format": cfg.provider.api_format,
                    "default_model": cfg.provider.default_model,
                }
            )
        except Exception:
            rows.append(
                {
                    "name": name,
                    "api_format": "(error)",
                    "default_model": "(error)",
                }
            )

    if as_json:
        _print_json(rows)
    else:
        _print_table(
            ["Name", "API Format", "Default Model"],
            [[r["name"], r["api_format"], r["default_model"]] for r in rows],
        )


def _provider(args: argparse.Namespace) -> None:
    """Show provider details and models."""
    from arcllm.config import load_provider_config
    from arcllm.exceptions import ArcLLMConfigError

    name: str = args.name
    as_json: bool = getattr(args, "as_json", False)

    try:
        cfg = load_provider_config(name)
    except ArcLLMConfigError:
        sys.stderr.write(
            f"Error: Provider '{name}' not found. Run `arc llm providers` to see available.\n"
        )
        sys.exit(1)

    if as_json:
        _print_json(
            {
                "provider": cfg.provider.model_dump(),
                "models": {k: v.model_dump() for k, v in cfg.models.items()},
            }
        )
        return

    _write(f"Provider: {name}")
    _write()
    _print_kv(
        [
            ("api_format", cfg.provider.api_format),
            ("base_url", cfg.provider.base_url),
            ("api_key_env", cfg.provider.api_key_env),
            ("default_model", cfg.provider.default_model),
            ("default_temperature", str(cfg.provider.default_temperature)),
        ]
    )
    _write()
    _write("Models:")
    _print_table(
        ["Model", "Context", "Max Output", "Tools", "Vision", "Input $/1M", "Output $/1M"],
        [
            [
                model_name,
                str(meta.context_window),
                str(meta.max_output_tokens),
                "yes" if meta.supports_tools else "no",
                "yes" if meta.supports_vision else "no",
                f"${meta.cost_input_per_1m:.2f}",
                f"${meta.cost_output_per_1m:.2f}",
            ]
            for model_name, meta in cfg.models.items()
        ],
    )


def _models(args: argparse.Namespace) -> None:
    """List all models across providers."""
    from arcllm.config import load_provider_config

    provider_filter: str | None = getattr(args, "provider_filter", None)
    tools_only: bool = getattr(args, "tools", False)
    vision_only: bool = getattr(args, "vision", False)
    as_json: bool = getattr(args, "as_json", False)

    names = _list_provider_names()
    if provider_filter:
        names = [n for n in names if n == provider_filter]

    rows = []
    for name in names:
        try:
            cfg = load_provider_config(name)
        except Exception:  # noqa: S112 — skip providers that fail to load (match legacy behavior)
            continue
        for model_name, meta in cfg.models.items():
            if tools_only and not meta.supports_tools:
                continue
            if vision_only and not meta.supports_vision:
                continue
            rows.append(
                {
                    "provider": name,
                    "model": model_name,
                    "context_window": meta.context_window,
                    "supports_tools": meta.supports_tools,
                    "supports_vision": meta.supports_vision,
                    "cost_input_per_1m": meta.cost_input_per_1m,
                    "cost_output_per_1m": meta.cost_output_per_1m,
                }
            )

    if as_json:
        _print_json(rows)
    else:
        _print_table(
            ["Provider", "Model", "Context", "Tools", "Vision", "Input $/1M", "Output $/1M"],
            [
                [
                    str(r["provider"]),
                    str(r["model"]),
                    str(r["context_window"]),
                    "yes" if r["supports_tools"] else "no",
                    "yes" if r["supports_vision"] else "no",
                    f"${r['cost_input_per_1m']:.2f}",
                    f"${r['cost_output_per_1m']:.2f}",
                ]
                for r in rows
            ],
        )


def _validate(args: argparse.Namespace) -> None:
    """Validate configs and API key availability."""
    import os

    from arcllm.config import load_global_config, load_provider_config

    provider_filter: str | None = getattr(args, "provider_filter", None)
    as_json: bool = getattr(args, "as_json", False)

    try:
        load_global_config()
        global_ok = True
        global_error = ""
    except Exception as e:
        global_ok = False
        global_error = str(e)

    names = _list_provider_names()
    if provider_filter:
        names = [n for n in names if n == provider_filter]

    results = []
    for name in names:
        entry: dict[str, Any] = {
            "provider": name,
            "config_valid": False,
            "api_key_set": False,
            "error": "",
        }
        try:
            cfg = load_provider_config(name)
            entry["config_valid"] = True
            env_var = cfg.provider.api_key_env
            entry["api_key_env"] = env_var
            entry["api_key_set"] = bool(os.environ.get(env_var, ""))
            if not cfg.provider.api_key_required:
                entry["api_key_set"] = True
        except Exception as e:
            entry["error"] = str(e)
        results.append(entry)

    if as_json:
        _print_json(results)
    else:
        if not global_ok:
            _write(f"Global config: INVALID ({global_error})")
        else:
            _write("Global config: OK")
        _write()
        _print_table(
            ["Provider", "Config", "API Key"],
            [
                [
                    r["provider"],
                    "OK" if r["config_valid"] else f"INVALID: {r['error']}",
                    "OK" if r["api_key_set"] else f"MISSING ({r.get('api_key_env', '?')})",
                ]
                for r in results
            ],
        )


def _prompt(args: argparse.Namespace) -> None:
    """Send a single-turn prompt to a provider and print the response."""
    import asyncio

    from arcllm import Message, TextBlock, load_model
    from arcllm.config import load_global_config

    text: str = args.text
    if text == "-":
        text = sys.stdin.read()
    if not text.strip():
        sys.stderr.write("arc llm prompt: empty prompt\n")
        sys.exit(2)

    provider: str = args.provider or load_global_config().defaults.provider
    model_id: str | None = args.model
    system: str | None = args.system
    as_json: bool = getattr(args, "as_json", False)

    invoke_kwargs: dict[str, Any] = {}
    if args.temperature is not None:
        invoke_kwargs["temperature"] = args.temperature
    if args.max_tokens is not None:
        invoke_kwargs["max_tokens"] = args.max_tokens

    messages: list[Message] = []
    if system:
        messages.append(Message(role="system", content=[TextBlock(text=system)]))
    messages.append(Message(role="user", content=[TextBlock(text=text)]))

    load_kwargs: dict[str, Any] = {}
    if args.security is not None:
        load_kwargs["security"] = args.security

    async def _run() -> Any:
        model = load_model(provider, model_id, **load_kwargs)
        try:
            return await model.invoke(messages, **invoke_kwargs)
        finally:
            await model.close()

    try:
        resp = asyncio.run(_run())
    except Exception as e:
        sys.stderr.write(f"arc llm prompt: {e}\n")
        sys.exit(1)

    if as_json:
        _print_json(resp.model_dump(exclude={"raw"}))
        return

    if resp.content:
        _write(resp.content)
    if resp.tool_calls:
        _write(f"[tool_calls: {len(resp.tool_calls)}]")


# ---------------------------------------------------------------------------
# Argparse-based dispatcher
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    """Build the argparse parser for `arc llm <sub> [args]`."""
    parser = argparse.ArgumentParser(
        prog="arc llm",
        description="ArcLLM commands — config, providers, models, calls.",
        add_help=True,
    )
    subs = parser.add_subparsers(dest="subcmd", metavar="<subcommand>")

    # version
    p = subs.add_parser("version", help="Show version information.")
    p.add_argument("--json", dest="as_json", action="store_true", help="Output as JSON.")

    # config
    p = subs.add_parser("config", help="Show global ArcLLM configuration.")
    p.add_argument("--module", dest="module", default=None, help="Show specific module config.")
    p.add_argument("--json", dest="as_json", action="store_true", help="Output as JSON.")

    # providers
    p = subs.add_parser("providers", help="List all available providers.")
    p.add_argument("--json", dest="as_json", action="store_true", help="Output as JSON.")

    # provider
    p = subs.add_parser("provider", help="Show provider details and models.")
    p.add_argument("name", help="Provider name.")
    p.add_argument("--json", dest="as_json", action="store_true", help="Output as JSON.")

    # models
    p = subs.add_parser("models", help="List all models across providers.")
    p.add_argument("--provider", dest="provider_filter", default=None, help="Filter by provider.")
    p.add_argument("--tools", action="store_true", help="Only models supporting tools.")
    p.add_argument("--vision", action="store_true", help="Only models supporting vision.")
    p.add_argument("--json", dest="as_json", action="store_true", help="Output as JSON.")

    # prompt
    p = subs.add_parser("prompt", help="Send a single-turn prompt and print the response.")
    p.add_argument("text", help="Prompt text. Use '-' to read from stdin.")
    p.add_argument("--provider", default=None, help="Provider name (defaults to config).")
    p.add_argument("--model", default=None, help="Model id (defaults to provider's default).")
    p.add_argument("--system", default=None, help="Optional system prompt.")
    p.add_argument(
        "--temperature", type=float, default=None, help="Sampling temperature override."
    )
    p.add_argument("--max-tokens", type=int, default=None, help="Max output tokens override.")
    sec = p.add_mutually_exclusive_group()
    sec.add_argument(
        "--security",
        dest="security",
        action="store_true",
        default=None,
        help="Force-enable SecurityModule (PII redaction + signing) for this call.",
    )
    sec.add_argument(
        "--no-security",
        dest="security",
        action="store_false",
        help="Force-disable SecurityModule for this call.",
    )
    p.add_argument(
        "--json", dest="as_json", action="store_true", help="Output full response JSON."
    )

    # validate
    p = subs.add_parser("validate", help="Validate configs and API key availability.")
    p.add_argument(
        "--provider", dest="provider_filter", default=None, help="Validate specific provider."
    )
    p.add_argument("--json", dest="as_json", action="store_true", help="Output as JSON.")

    return parser


_SUBCOMMAND_MAP = {
    "version": _version,
    "config": _config,
    "providers": _providers,
    "provider": _provider,
    "models": _models,
    "validate": _validate,
    "prompt": _prompt,
}


def llm_handler(args: list[str]) -> None:
    """Top-level handler for `arc llm <sub> [args]`.

    Called by arccli.commands.registry when the user runs `arc llm ...`.
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
        sys.stderr.write(f"arc llm: unknown subcommand '{parsed.subcmd}'\n")
        sys.exit(1)

    fn(parsed)
