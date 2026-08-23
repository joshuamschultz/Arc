"""`arc agent build` — render the agent config surface, or `--check` validate."""

from __future__ import annotations

import argparse
import os
import sys
import tomllib
from pathlib import Path
from typing import Any

from arccli.commands.agent._common import (
    _DEFAULT_ARCLLM_CONFIG,
    _DEFAULT_ARCRUN_CONFIG,
    _discover_tools,
    _load_agent_config,
    _load_agent_llm_config,
    _load_env,
    _resolve_agent_dir,
    _scaffold_workspace,
    render_agent_config,
)


def _build(args: argparse.Namespace) -> None:
    """Render the agent's config surface at a tier (--check validates instead)."""
    agent_dir = _resolve_agent_dir(args.path)
    _load_env(agent_dir)

    if args.check:
        _run_validation(agent_dir)
        return

    _run_scaffold(
        agent_dir,
        tier=getattr(args, "tier", "personal"),
        force=getattr(args, "force", False),
    )


def _run_validation(agent_dir: Path) -> None:
    """Validation-only path for `arc agent build --check`."""
    import arcrun

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
        # Validate module configs against their declared models on the MERGED
        # view (user-wide overlay + this file) — the exact config the runtime
        # loads. A key a module's schema no longer accepts otherwise passes
        # every deploy gate and kills the agent at its first load.
        try:
            import arcagent

            merged = arcagent.load_config(config_path)
            module_errors = arcagent.validate_module_configs(dict(merged.modules))
        except Exception as exc:
            checks.append(("FAIL", f"merged config did not validate: {exc}"))
            all_ok = False
        else:
            if module_errors:
                for error in module_errors:
                    checks.append(("FAIL", error))
                all_ok = False
            else:
                checks.append(("OK", "module configs match their schemas"))
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

    model_id = _load_agent_llm_config(agent_dir).get("llm", {}).get("model", "")
    if model_id:
        provider = model_id.split("/")[0] if "/" in model_id else model_id
        checks.append(("OK", f"model: {model_id}"))
        # Ask the provider config which variable it needs. Deriving the name from
        # the provider ("deepseek" -> DEEPSEEK_API_KEY) fails any deployment that
        # points a provider at its own endpoint: a DGX box serves deepseek through
        # a local LiteLLM proxy and sets api_key_env = "LITELLM_API_KEY", so the
        # derived guess reported a missing key that was never needed and aborted a
        # live deploy. load_provider_config() deep-merges the user's arcllm.toml
        # over the packaged file, so that override is visible here.
        try:
            import arcllm

            settings = arcllm.load_provider_config(provider).provider
            env_var, required = settings.api_key_env, settings.api_key_required
        except Exception:
            # An unknown or unreadable provider is a real finding, but not this
            # check's to diagnose — say so plainly rather than inventing a name.
            checks.append(("WARN", f"provider {provider!r}: no config, key not checked"))
            env_var, required = "", False

        if not required or not env_var:
            checks.append(("OK", f"{provider} (no key needed)"))
        elif os.environ.get(env_var):
            checks.append(("OK", f"{env_var} is set"))
        else:
            checks.append(("FAIL", f"{env_var} not set"))
            all_ok = False
    else:
        checks.append(("FAIL", "No model configured"))
        all_ok = False

    caps_dir = agent_dir / "capabilities"
    if caps_dir.is_dir():
        discovered = _discover_tools(agent_dir)
        for t in discovered:
            checks.append(("OK", f"  tool: {t.name}"))
        checks.append(("OK", f"tools: {len(discovered)} total"))
    else:
        checks.append(("WARN", "capabilities/ not found"))

    try:
        checks.append(("OK", f"strategies: {', '.join(arcrun.available_strategies())}"))
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


def _run_scaffold(agent_dir: Path, *, tier: str, force: bool) -> None:
    """Write the full config surface for ``agent_dir`` at ``tier``.

    Renders arcagent.toml with every operator-settable knob at its default and
    a one-line note, so the file itself documents what is possible.

    Two things it deliberately does NOT do:

    * It never overwrites arcllm.toml or arcrun.toml. Those are the per-agent
      LLM-wire and loop overrides — they win over the user-wide files, and
      silently regenerating them would discard a deliberate override. Missing
      ones are provided so the override point always exists.
    * It never blanks ``[identity].did``. That DID is what the agent signs its
      capabilities with and is registered to arcteam under; regenerating over
      it orphans the agent.
    """
    agent_dir.mkdir(parents=True, exist_ok=True)
    config_path = agent_dir / "arcagent.toml"

    name = agent_dir.resolve().name
    did = ""
    if config_path.exists():
        if not force:
            sys.stderr.write(
                f"arc agent build: {config_path} already exists.\n"
                "  Pass --force to regenerate it (your DID and agent name are kept;\n"
                "  any other hand-edited values in this file are replaced).\n"
                "  arcllm.toml and arcrun.toml are never touched either way.\n"
            )
            sys.exit(1)
        existing = _read_toml(config_path)
        name = str(existing.get("agent", {}).get("name") or name)
        did = str(existing.get("identity", {}).get("did") or "")

    try:
        rendered = render_agent_config(name=name, tier=tier, did=did)
    except ValueError as exc:
        sys.stderr.write(f"arc agent build: {exc}\n")
        sys.exit(2)
    config_path.write_text(rendered, encoding="utf-8")

    written = [f"arcagent.toml  (full surface, tier={tier})"]
    written.append(_provide(agent_dir / "arcllm.toml", _DEFAULT_ARCLLM_CONFIG, "LLM-wire"))
    written.append(_provide(agent_dir / "arcrun.toml", _DEFAULT_ARCRUN_CONFIG, "loop controls"))

    _scaffold_workspace(agent_dir, name)

    sys.stdout.write(f"Built agent config: {agent_dir}\n\n")
    for line in written:
        sys.stdout.write(f"  {line}\n")
    if did:
        sys.stdout.write(f"\n  identity preserved: {did}\n")
    sys.stdout.write("\nValidate:\n")
    sys.stdout.write(f"  arc agent build {agent_dir} --check\n")


def _provide(path: Path, content: str, label: str) -> str:
    """Create ``path`` only when absent — an existing override is never touched."""
    if path.exists():
        return f"{path.name}      (left as-is — your {label} override)"
    path.write_text(content, encoding="utf-8")
    return f"{path.name}      (created — per-agent {label} override)"


def _read_toml(path: Path) -> dict[str, Any]:
    """Parse ``path``, treating an unreadable file as empty.

    A corrupt config must not block regenerating a good one — that is the
    situation `build` exists to get you out of.
    """
    try:
        with open(path, "rb") as fh:
            return tomllib.load(fh)
    except (OSError, tomllib.TOMLDecodeError):
        return {}
