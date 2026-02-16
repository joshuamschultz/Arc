"""ArcLLM CLI subcommands — config, providers, models, and calls."""

import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any

import click

from arcllm.registry import load_model
from arccli.formatting import print_json, print_kv, print_table


@click.group()
def llm() -> None:
    """ArcLLM commands — config, providers, models, and calls."""


# ---------------------------------------------------------------------------
# version
# ---------------------------------------------------------------------------


@llm.command()
@click.option("--json", "as_json", is_flag=True, help="Output as JSON.")
def version(as_json: bool) -> None:
    """Show version information."""
    import arccli
    import arcllm

    data = {
        "arccli": arccli.__version__,
        "arcllm": getattr(arcllm, "__version__", "0.1.0"),
        "python": sys.version.split()[0],
    }
    if as_json:
        print_json(data)
    else:
        print_kv([(k, v) for k, v in data.items()])


# ---------------------------------------------------------------------------
# config
# ---------------------------------------------------------------------------


@llm.command()
@click.option("--module", "module_name", default=None, help="Show specific module config.")
@click.option("--json", "as_json", is_flag=True, help="Output as JSON.")
def config(module_name: str | None, as_json: bool) -> None:
    """Show global ArcLLM configuration."""
    from arcllm.config import load_global_config

    cfg = load_global_config()

    if module_name:
        mod = cfg.modules.get(module_name)
        if mod is None:
            raise click.ClickException(f"Module '{module_name}' not found in config.")
        if as_json:
            print_json({module_name: mod.model_dump()})
        else:
            click.echo(f"[modules.{module_name}]")
            for key, val in mod.model_dump().items():
                click.echo(f"  {key} = {val}")
        return

    data = {
        "defaults": cfg.defaults.model_dump(),
        "modules": {name: m.model_dump() for name, m in cfg.modules.items()},
        "vault": cfg.vault.model_dump(),
    }
    if as_json:
        print_json(data)
    else:
        click.echo("[defaults]")
        for key, val in cfg.defaults.model_dump().items():
            click.echo(f"  {key} = {val}")
        click.echo()
        for name, mod in cfg.modules.items():
            click.echo(f"[modules.{name}]")
            for key, val in mod.model_dump().items():
                click.echo(f"  {key} = {val}")
            click.echo()
        click.echo("[vault]")
        for key, val in cfg.vault.model_dump().items():
            click.echo(f"  {key} = {val}")


# ---------------------------------------------------------------------------
# helpers — provider discovery
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
    return sorted(
        p.stem for p in providers_dir.glob("*.toml") if p.stem != "__init__"
    )


# ---------------------------------------------------------------------------
# providers
# ---------------------------------------------------------------------------


@llm.command()
@click.option("--json", "as_json", is_flag=True, help="Output as JSON.")
def providers(as_json: bool) -> None:
    """List all available providers."""
    from arcllm.config import load_provider_config

    names = _list_provider_names()
    rows = []
    for name in names:
        try:
            cfg = load_provider_config(name)
            rows.append({
                "name": name,
                "api_format": cfg.provider.api_format,
                "default_model": cfg.provider.default_model,
            })
        except Exception:
            rows.append({
                "name": name,
                "api_format": "(error)",
                "default_model": "(error)",
            })

    if as_json:
        print_json(rows)
    else:
        print_table(
            ["Name", "API Format", "Default Model"],
            [[r["name"], r["api_format"], r["default_model"]] for r in rows],
        )


# ---------------------------------------------------------------------------
# provider <name>
# ---------------------------------------------------------------------------


@llm.command()
@click.argument("name")
@click.option("--json", "as_json", is_flag=True, help="Output as JSON.")
def provider(name: str, as_json: bool) -> None:
    """Show provider details and models."""
    from arcllm.config import load_provider_config
    from arcllm.exceptions import ArcLLMConfigError

    try:
        cfg = load_provider_config(name)
    except ArcLLMConfigError:
        raise click.ClickException(
            f"Provider '{name}' not found. Run `arc llm providers` to see available."
        )

    if as_json:
        print_json({
            "provider": cfg.provider.model_dump(),
            "models": {k: v.model_dump() for k, v in cfg.models.items()},
        })
        return

    click.echo(f"Provider: {name}")
    click.echo()
    print_kv([
        ("api_format", cfg.provider.api_format),
        ("base_url", cfg.provider.base_url),
        ("api_key_env", cfg.provider.api_key_env),
        ("default_model", cfg.provider.default_model),
        ("default_temperature", str(cfg.provider.default_temperature)),
    ])
    click.echo()
    click.echo("Models:")
    print_table(
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


# ---------------------------------------------------------------------------
# models
# ---------------------------------------------------------------------------


@llm.command()
@click.option("--provider", "provider_filter", default=None, help="Filter by provider.")
@click.option("--tools", is_flag=True, help="Only models supporting tools.")
@click.option("--vision", is_flag=True, help="Only models supporting vision.")
@click.option("--json", "as_json", is_flag=True, help="Output as JSON.")
def models(provider_filter: str | None, tools: bool, vision: bool, as_json: bool) -> None:
    """List all models across providers."""
    from arcllm.config import load_provider_config

    names = _list_provider_names()
    if provider_filter:
        names = [n for n in names if n == provider_filter]

    rows = []
    for name in names:
        try:
            cfg = load_provider_config(name)
        except Exception:
            continue
        for model_name, meta in cfg.models.items():
            if tools and not meta.supports_tools:
                continue
            if vision and not meta.supports_vision:
                continue
            rows.append({
                "provider": name,
                "model": model_name,
                "context_window": meta.context_window,
                "supports_tools": meta.supports_tools,
                "supports_vision": meta.supports_vision,
                "cost_input_per_1m": meta.cost_input_per_1m,
                "cost_output_per_1m": meta.cost_output_per_1m,
            })

    if as_json:
        print_json(rows)
    else:
        print_table(
            ["Provider", "Model", "Context", "Tools", "Vision", "Input $/1M", "Output $/1M"],
            [
                [
                    r["provider"],
                    r["model"],
                    str(r["context_window"]),
                    "yes" if r["supports_tools"] else "no",
                    "yes" if r["supports_vision"] else "no",
                    f"${r['cost_input_per_1m']:.2f}",
                    f"${r['cost_output_per_1m']:.2f}",
                ]
                for r in rows
            ],
        )


# ---------------------------------------------------------------------------
# call
# ---------------------------------------------------------------------------


@llm.command()
@click.argument("provider_name")
@click.argument("prompt")
@click.option("--model", default=None, help="Override default model.")
@click.option("--temperature", type=float, default=None, help="Sampling temperature.")
@click.option("--max-tokens", type=int, default=None, help="Max output tokens.")
@click.option("--system", "system_msg", default=None, help="System message.")
@click.option("--retry/--no-retry", default=None, help="Enable/disable retry module.")
@click.option("--fallback/--no-fallback", default=None, help="Enable/disable fallback module.")
@click.option("--rate-limit/--no-rate-limit", default=None, help="Enable/disable rate limiter.")
@click.option("--telemetry/--no-telemetry", default=None, help="Enable/disable telemetry.")
@click.option("--audit/--no-audit", default=None, help="Enable/disable audit logging.")
@click.option("--security/--no-security", default=None, help="Enable/disable security module.")
@click.option("--otel/--no-otel", default=None, help="Enable/disable OpenTelemetry.")
@click.option("--verbose", is_flag=True, help="Show usage details (tokens, cost, timing).")
@click.option("--json", "as_json", is_flag=True, help="Output full response as JSON.")
def call(
    provider_name: str,
    prompt: str,
    model: str | None,
    temperature: float | None,
    max_tokens: int | None,
    system_msg: str | None,
    retry: bool | None,
    fallback: bool | None,
    rate_limit: bool | None,
    telemetry: bool | None,
    audit: bool | None,
    security: bool | None,
    otel: bool | None,
    verbose: bool,
    as_json: bool,
) -> None:
    """Make an LLM call."""
    from arcllm.types import Message

    try:
        adapter = load_model(
            provider_name,
            model,
            retry=retry,
            fallback=fallback,
            rate_limit=rate_limit,
            telemetry=telemetry,
            audit=audit,
            security=security,
            otel=otel,
        )
    except Exception as e:
        raise click.ClickException(str(e))

    messages = []
    if system_msg:
        messages.append(Message(role="system", content=system_msg))
    messages.append(Message(role="user", content=prompt))

    invoke_kwargs: dict[str, Any] = {}
    if temperature is not None:
        invoke_kwargs["temperature"] = temperature
    if max_tokens is not None:
        invoke_kwargs["max_tokens"] = max_tokens

    async def _run() -> None:
        try:
            response = await adapter.invoke(messages, **invoke_kwargs)

            if as_json:
                print_json(response.model_dump(exclude={"raw"}))
            else:
                if verbose:
                    usage = response.usage
                    click.echo(f"Model: {response.model}")
                    click.echo(
                        f"Usage: {usage.input_tokens} input, "
                        f"{usage.output_tokens} output, "
                        f"{usage.total_tokens} total tokens"
                    )
                    if response.cost_usd is not None:
                        click.echo(f"Cost: ${response.cost_usd:.6f}")
                    click.echo(f"Stop: {response.stop_reason}")
                    click.echo("---")
                if response.content:
                    click.echo(response.content)
        finally:
            if hasattr(adapter, "close"):
                await adapter.close()

    try:
        asyncio.run(_run())
    except Exception as e:
        raise click.ClickException(str(e))


# ---------------------------------------------------------------------------
# validate
# ---------------------------------------------------------------------------


@llm.command()
@click.option("--provider", "provider_filter", default=None, help="Validate specific provider.")
@click.option("--json", "as_json", is_flag=True, help="Output as JSON.")
def validate(provider_filter: str | None, as_json: bool) -> None:
    """Validate configs and API key availability."""
    from arcllm.config import load_global_config, load_provider_config

    # Validate global config
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
        entry: dict[str, Any] = {"provider": name, "config_valid": False, "api_key_set": False, "error": ""}
        try:
            cfg = load_provider_config(name)
            entry["config_valid"] = True
            env_var = cfg.provider.api_key_env
            entry["api_key_env"] = env_var
            entry["api_key_set"] = bool(os.environ.get(env_var, ""))
            if not cfg.provider.api_key_required:
                entry["api_key_set"] = True  # not required = always ok
        except Exception as e:
            entry["error"] = str(e)
        results.append(entry)

    if as_json:
        print_json(results)
    else:
        if not global_ok:
            click.echo(f"Global config: INVALID ({global_error})")
        else:
            click.echo("Global config: OK")
        click.echo()
        print_table(
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
