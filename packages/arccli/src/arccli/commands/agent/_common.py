"""Shared helpers for the `arc agent` subcommand subpackage.

Sibling helpers used across multiple subcommand modules. Constants
(scaffolding templates, env-search path, global capabilities dir,
the bundled calculator capability source) live here so any
subcommand can import them without crossing files.

Re-exported through ``arccli.commands.agent`` so existing internal
imports
(``from arccli.commands.agent import _resolve_agent_dir``) keep
working unchanged.
"""

from __future__ import annotations

import asyncio
import importlib
import importlib.util
import json
import sys
import tomllib
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import arcagent
import arctrust
from arctrust.paths import dotenv_file, env_file

from arccli.commands._serve import (
    AgentAuditForwarder,
    build_control_artifact_authority,
    build_skill_revision_anchor_factory,
)
from arccli.commands._shared import print_kv as _print_kv
from arccli.commands._shared import print_table as _print_table

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# The per-agent arcllm.toml `arc agent build` provides when one is missing —
# the same file `arc agent create` and the dashboard write.
_DEFAULT_ARCLLM_CONFIG = arcagent.scaffold.render_arcllm_config()


def _env_paths() -> list[Path]:
    """The ``.env`` files an agent command loads, in precedence order.

    ``arc.env`` comes from :func:`arctrust.paths.env_file` — the same resolver
    :func:`arcagent.keys.default_env_file` writes through — so a key set by any
    surface is a key this loader reads. Resolved per call, never frozen at
    import: the Arc home is routinely relocated after this module loads, and the
    cwd can change within one process.
    """
    return [
        Path.cwd() / ".env",
        env_file(),
        Path.home() / ".env",
    ]


# ---------------------------------------------------------------------------
# Env / agent-dir / config / tool helpers
# ---------------------------------------------------------------------------


def _load_env(agent_dir: Path | None = None) -> None:
    """Load .env files without importing click."""
    try:
        from dotenv import load_dotenv
    except ImportError:
        return  # dotenv optional for status/read-only commands
    # The deployment's own .env wins over the ambient ones, so a self-contained
    # folder is "start up, config, and go" without exporting keys by hand.
    paths = [dotenv_file(), *_env_paths()]
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


def _load_agent_llm_config(agent_dir: Path) -> dict[str, Any]:
    """Load the agent's arcllm.toml — the home of `[llm]`/`[eval]`/`[budget]`.

    Returns an empty mapping when the file is absent so callers report their
    own "not configured" verdict rather than dying on a missing file.
    """
    config_path = agent_dir / "arcllm.toml"
    if not config_path.exists():
        return {}
    with open(config_path, "rb") as f:
        return tomllib.load(f)


def _import_capability_file(path: Path) -> Any:
    """Import a capability `.py` by file path (no package required)."""
    module_name = f"arccli_cap_{path.stem}"
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"could not create import spec for {path}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _discover_tools(agent_dir: Path) -> list[Any]:
    """Discover @tool-decorated capabilities in the agent's capabilities/ dir."""
    caps_dir = agent_dir / "capabilities"
    if not caps_dir.is_dir():
        return []
    all_tools: list[Any] = []
    for cf in sorted(caps_dir.glob("*.py")):
        if cf.name.startswith("_"):
            continue
        try:
            mod = _import_capability_file(cf)
        except Exception as e:  # reason: fail-open — continue
            sys.stdout.write(f"  Warning: could not load capabilities/{cf.name}: {e}\n")
            continue
        for value in vars(mod).values():
            meta = getattr(value, "_arc_capability_meta", None)
            if meta is not None and getattr(meta, "kind", None) == "tool":
                all_tools.append(meta)
    return all_tools


@dataclass(frozen=True)
class _DiscoveredTool:
    """One tool as the agent's real runtime registry would report it.

    ``source`` is the scan root the tool was found under ("builtins",
    "global", "agent", "workspace", or "module:<name>") — free provenance
    from :class:`~arcagent.capabilities.capability_registry.ToolEntry`,
    which already tracks it.
    """

    name: str
    description: str
    input_schema: dict[str, Any]
    source: str
    timeout_seconds: int | None = None


def _discover_runtime_tools(agent_dir: Path) -> list[_DiscoveredTool]:
    """Answer "what tools would this agent actually have at startup?" (task #29).

    Unlike :func:`_discover_tools` (which only looks at the agent's OWN
    ``capabilities/`` directory — the right question for ``arc agent build``/
    ``arc agent status``), this builds the same standalone CapabilityRegistry
    ``arc ext inspect`` uses: builtins + global + agent + workspace + every
    ENABLED module's ``capabilities.py``. That registry is what closes the
    live bug — an agent with one scaffolded capability reported exactly one
    tool via ``arc agent tools`` while ``arc ext inspect`` correctly showed
    ~15 (the builtins were never scanned).
    """
    from arccli.commands._capability_registry import build_capability_registry

    config_path = agent_dir / "arcagent.toml"
    if not config_path.is_file():
        return []
    try:
        config = arcagent.load_config(config_path)
    except Exception:  # reason: fail-open — a listing command must degrade, not crash
        return []

    registry = build_capability_registry(config, agent_dir)
    if registry is None:
        return []

    # Snapshot read of the registry's private tool dict — the same read-only
    # pattern arcagent.extension.inspect._iter_registry already uses for this
    # exact purpose (inspection never mutates, so a private-dict read is the
    # accepted convention rather than growing CapabilityRegistry's public API
    # for a CLI-only need).
    entries = getattr(registry, "_tools", {}).values()
    tools = [
        _DiscoveredTool(
            name=entry.meta.name,
            description=entry.meta.description,
            input_schema=entry.meta.input_schema,
            source=entry.scan_root,
            timeout_seconds=getattr(entry.meta, "timeout_seconds", None),
        )
        for entry in entries
    ]
    return sorted(tools, key=lambda t: t.name)


# ---------------------------------------------------------------------------
# Workspace scaffold
# ---------------------------------------------------------------------------


def cli_operator_signing(*, bootstrap: bool = False) -> arcagent.scaffold.OperatorSigning | None:
    """The CLI's operator signer handle, or None when it cannot be resolved.

    ``bootstrap=True`` is the zero-config path ``arc agent create`` takes on a
    fresh box: the deployment's operator key is created where every verifier
    looks for it. Otherwise a missing key is None — re-signing an existing agent
    with a key minted on the spot would hide that the deployment lost its key.
    """
    from arctrust.operator_resolver import machine_security, operator_key_file
    from arctrust.signer import VAULT_TRANSIT

    from arccli.commands.operator import operator_signer_and_did

    try:
        security = machine_security()
        key_missing = not operator_key_file(security).is_file()
        if not bootstrap and security.custody != VAULT_TRANSIT and key_missing:
            return None
        did, signer = operator_signer_and_did()
    except (OSError, ValueError, RuntimeError, arctrust.SignerError):
        return None
    return arcagent.scaffold.OperatorSigning(did=did, signer=signer)


def _scaffold_workspace(agent_dir: Path, name: str) -> None:
    """Create any missing workspace files, operator-signing a new identity.md."""
    arcagent.scaffold.scaffold_workspace(agent_dir, name, operator=cli_operator_signing())


def _print_scaffold_summary(display_name: str, agent_dir: Path, tier: str = "personal") -> None:
    """Print directory structure and next-steps after scaffold."""
    sys.stdout.write("\n")
    sys.stdout.write(f"Tier: {tier}\n")
    sys.stdout.write("\n")
    sys.stdout.write("Structure:\n")
    sys.stdout.write(f"  {display_name}/\n")
    sys.stdout.write("    arcagent.toml             # agent/tools/security/modules/store\n")
    sys.stdout.write("    arcllm.toml               # LLM-wire: [llm] / [eval] / [budget]\n")
    sys.stdout.write("    arcrun.toml               # agentic-loop controls\n")
    sys.stdout.write("    capabilities/             # per-agent capabilities (trusted)\n")
    sys.stdout.write("      calculator.py\n")
    sys.stdout.write("    workspace/\n")
    sys.stdout.write("      identity.md, policy.md, context.md, index.md, pulse.md\n")
    sys.stdout.write("      capabilities/          # agent-authored (UNTRUSTED, AST-validated)\n")
    sys.stdout.write("      sessions/              # chat transcripts (JSONL)\n")
    sys.stdout.write("      memory/                # lazily created when a Brain is enabled\n")
    sys.stdout.write("\n")
    sys.stdout.write("Next steps:\n")
    # --check, not a bare `build`: the config already exists, and `build`
    # refuses to regenerate over it without --force.
    sys.stdout.write(f"  arc agent build {agent_dir} --check\n")
    sys.stdout.write(f"  arc agent chat {agent_dir}\n")


# ---------------------------------------------------------------------------
# Capability scan roots (used by status/skills/extensions and chat)
# ---------------------------------------------------------------------------


def _capability_scan_roots(agent_dir: Path) -> list[tuple[str, Path]]:
    """Return the four user-visible capability scan roots in precedence order.

    Mirrors `arcagent.core.agent_lifecycle.setup_capabilities` (SPEC-021 R-001)
    but skips the package-internal builtins root, which the user never edits.
    """
    workspace = agent_dir / "workspace"
    return [
        ("global", arcagent.global_capabilities_root()),
        ("agent", agent_dir / "capabilities"),
        ("workspace", workspace / "capabilities"),
    ]


def _iter_capability_files(agent_dir: Path) -> list[tuple[str, Path]]:
    """Yield (root_name, .py path) for every capability file across roots."""
    out: list[tuple[str, Path]] = []
    for root_name, root in _capability_scan_roots(agent_dir):
        if not root.is_dir():
            continue
        for py_file in sorted(root.glob("*.py")):
            if py_file.name.startswith("_"):
                continue
            out.append((root_name, py_file))
    return out


def _iter_skill_folders(agent_dir: Path) -> list[tuple[str, Path]]:
    """Yield (root_name, folder) for every <root>/<name>/SKILL.md skill folder."""
    out: list[tuple[str, Path]] = []
    for root_name, root in _capability_scan_roots(agent_dir):
        if not root.is_dir():
            continue
        for entry in sorted(root.iterdir()):
            if entry.is_dir() and (entry / "SKILL.md").exists():
                out.append((root_name, entry))
    return out


# ---------------------------------------------------------------------------
# Shared ArcAgent loader (used by run/serve/chat)
# ---------------------------------------------------------------------------


def _load_arcagent(
    agent_dir: Path,
    *,
    skill_revision_anchor_factory: Callable[[str, str], arctrust.MonotonicAnchor] | None = None,
    control: arcagent.ControlArtifactBinding | None = None,
) -> tuple[Any, Any, Path]:
    """Load ArcAgent from agent directory.

    Returns (ArcAgent instance, ArcAgentConfig, config_path).
    Exits 1 with a clear message if arcagent.toml is missing or
    ArcAgent / load_config cannot be imported.
    """
    config_path = agent_dir / "arcagent.toml"
    if not config_path.exists():
        sys.stderr.write(f"arc agent: no arcagent.toml in {agent_dir}\n")
        sys.exit(1)

    config = arcagent.load_config(config_path)
    # An agent addressed from the CLI is still part of whatever fleet it belongs
    # to: without this it starts with no directory and no inbox, and every
    # fleet-facing tool reports itself unavailable on that path alone.
    from arcteam.agent_fleet import ArcTeamFleet

    arc_agent: arcagent.ArcAgent
    resolver = (
        arcagent.LiveSkillRevisionResolver(
            agent_did=lambda: arc_agent.did,
            config_path=config_path,
            anchor_factory=skill_revision_anchor_factory,
        )
        if skill_revision_anchor_factory is not None
        else None
    )
    arc_agent = arcagent.ArcAgent(
        config,
        config_path=config_path,
        fleet=ArcTeamFleet(),
        skill_artifact_resolver=resolver,
        **(control.agent_kwargs() if control is not None else {}),
    )
    return arc_agent, config, config_path


def load_cli_agent(agent_dir: Path) -> tuple[Any, Any, Path]:
    """Load an ArcAgent for ``arc agent run/serve/chat`` (and ``arc mcp``).

    Builds the deployment's skill revision anchor WITH an audit sink bound to the
    agent it loads, so revision-anchor events reach that agent's audit chain.
    """
    audit = AgentAuditForwarder()
    loaded = _load_arcagent(
        agent_dir,
        skill_revision_anchor_factory=build_skill_revision_anchor_factory(audit),
        control=build_control_artifact_authority(audit),
    )
    audit.bind(loaded[0])
    return loaded


def _print_result_json(result: Any) -> None:
    """Serialize a ``RunResult`` (from ``collect``) to JSON and write to stdout."""
    data = {
        "content": result.content,
        "turns": result.turns,
        "tool_calls_made": result.tool_calls_made,
        "cost_usd": result.cost_usd,
    }
    sys.stdout.write(json.dumps(data, indent=2) + "\n")


# Re-export asyncio for convenience in subcommand modules that call asyncio.run.
__all__ = [
    "_DEFAULT_ARCLLM_CONFIG",
    "_capability_scan_roots",
    "_discover_runtime_tools",
    "_discover_tools",
    "_env_paths",
    "_iter_capability_files",
    "_iter_skill_folders",
    "_load_agent_config",
    "_load_agent_llm_config",
    "_load_arcagent",
    "_load_env",
    "_print_kv",
    "_print_result_json",
    "_print_scaffold_summary",
    "_print_table",
    "_resolve_agent_dir",
    "_scaffold_workspace",
    "asyncio",
    "cli_operator_signing",
    "load_cli_agent",
]
