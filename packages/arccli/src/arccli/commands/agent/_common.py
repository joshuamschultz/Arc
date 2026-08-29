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
import datetime
import importlib
import importlib.util
import json
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import arcagent
from arcokf import OKFValidationError, render_collection_index, validate
from arctrust.paths import dotenv_file, env_file

from arccli.commands._arcllm_surface import commented_module_surface
from arccli.commands._shared import print_kv as _print_kv
from arccli.commands._shared import print_table as _print_table

# ---------------------------------------------------------------------------
# Constants
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

_SEED_POLICY_BULLETS = (
    "Be helpful and direct",
    "Use tools when appropriate",
    "Report errors clearly",
)


def _default_policy() -> str:
    """Seed ``policy.md`` with structured ACE bullets.

    Each line carries the ``{score, uses, reviewed, created, source}`` trailer
    the policy engine expects, so the curator can score/update them and the UI
    can parse them. A bullet without that metadata is invisible to both.
    """
    today = datetime.date.today().isoformat()
    lines = ["# Policy", ""]
    for i, text in enumerate(_SEED_POLICY_BULLETS, start=1):
        lines.append(
            f"- [P{i:02d}] {text} "
            f"{{score:5, uses:0, reviewed:{today}, created:{today}, source:init}}"
        )
    return "\n".join(lines) + "\n"


_DEFAULT_CONTEXT = """\
# Context

Working memory for the agent. Updated during conversations.
"""

_DEFAULT_INDEX = render_collection_index(())

AGENT_TIERS = ("personal", "enterprise", "federal")
"""Canonical deployment tiers, least to most stringent."""

_ARCAGENT_HEADER = """\
# ArcAgent config — everything EXCEPT LLM-wire (arcllm.toml) and the agentic
# loop controls (arcrun.toml). Every operator-settable knob is present at its
# default with a doc comment, GENERATED from the Pydantic models themselves
# (arcagent.utils.config_render) rather than hand-copied, so a field added to
# a model appears here the next time an agent is scaffolded. Sibling files
# load from the SAME directory and compose into one effective config.
"""

# Deliberate scaffold choices that differ from a module's bare Pydantic
# default (e.g. "ship the tasks/messaging/workflows bus modules ON", "extract
# through the keyless browser backend, not the model's own http default").
# Each is a considered product decision, not a fact any model carries — so it
# lives here, once, instead of being encoded a second time per field.
_MODULE_CONFIG_OVERRIDES: dict[str, dict[str, Any]] = {
    "memory": {
        "brain": "arcmemory",
        "embed_backend": "local",
        "distill_provider": "anthropic",
        "distill_model": "claude-haiku-4-5-20251001",
    },
    "progress": {
        "heartbeat_after_seconds": 120,
        "heartbeat_every_seconds": 600,
    },
    "skills": {"adapter": "arcskill"},
    "proactive": {
        # These fields default to None (TOML-uncomment-able); the scaffold
        # ships them as live, empty, editable keys instead.
        "identity": "",
        "redis_url": "",
        "k8s_namespace": "",
        "k8s_lease_name": "",
    },
    "scheduler": {"enabled": True},
    "messaging": {"enabled": True, "nats_url": "nats://127.0.0.1:4222"},
    "tasks": {"enabled": True, "dispatch": True, "nats_url": "nats://127.0.0.1:4222"},
    "workflows": {"enabled": True, "nats_url": "nats://127.0.0.1:4222"},
    "runcontrol": {"enabled": True},
    "web": {"extract_provider": "browser"},
    "browser": {"security": {"allow_js_execution": True, "allow_downloads": True}},
}

# Modules whose config carries a `tier` field that must track [security].tier
# — a config federal in [security] but personal in [modules.web] is a hole,
# not a preference (see render_agent_config's docstring).
_MODULE_TIER_FIELDS = frozenset({"memory", "policy", "skills", "web", "voice", "browser"})


def _module_overrides_for_tier(tier: str) -> dict[str, dict[str, Any]]:
    overrides = {name: dict(fields) for name, fields in _MODULE_CONFIG_OVERRIDES.items()}
    for name in _MODULE_TIER_FIELDS:
        overrides.setdefault(name, {})["tier"] = tier
    return overrides


def render_agent_config(*, name: str, tier: str = "personal", did: str = "") -> str:
    """Render the full arcagent.toml surface for one agent at one tier.

    ``tier`` sets every subsystem's tier at once — [security], memory, policy,
    skills, web, voice, browser. They are one decision: a config that is federal
    in [security] but personal in [modules.web] is a hole, not a preference.

    ``did`` is substituted rather than blanked so a regeneration keeps the
    identity the agent signs its capabilities with and is registered to
    arcteam under.

    Field presence and defaults come from ``arcagent.utils.config_render``,
    which walks ``ArcAgentConfig`` (and, per module, its own ``<Name>Config``)
    directly — this function supplies only the per-agent identity plus the
    handful of deliberate overrides above; it never hand-copies a field list.
    """
    if tier not in AGENT_TIERS:
        raise ValueError(f"unknown tier {tier!r} — choose one of {', '.join(AGENT_TIERS)}")
    top_overrides: dict[str, dict[str, Any]] = {
        "agent": {"name": name, "org": "local"},
        "identity": {"did": did},
        "security": {"tier": tier, "policy_audit_log": "", **_crypto_posture(tier)},
        "telemetry": {"service_name": name},
    }
    body = arcagent.config_render.render_arcagent_toml(
        overrides=top_overrides,
        module_overrides=_module_overrides_for_tier(tier),
    )
    return _ARCAGENT_HEADER + "\n" + body


def _crypto_posture(tier: str) -> dict[str, str | bool]:
    """Tier-correct values for the crypto knobs the template states explicitly.

    ``SecurityConfig`` auto-resolves federal floors only for knobs the operator
    left unset; an explicitly *weaker* value is refused fail-closed. Because
    this template states every knob outright — that is the point of it — a
    federal render must state the federal floor, or the config it produces will
    not load at all.

    Federal values come from ``SECURITY_CONFIG_KNOBS`` rather than being
    duplicated here, so moving a floor moves the template with it.
    """
    if tier == "federal":
        floors = {k.name: k.federal_floor for k in arcagent.SECURITY_CONFIG_KNOBS}
        return {
            "signing_algorithm": str(floors["signing_algorithm"]),
            "custody": str(floors["custody"]),
            "require_fips": floors["require_fips"],
        }
    return {
        "signing_algorithm": "ed25519",
        # REQ-007: enterprise custody defaults to vault_transit; it may relax to
        # in_process, which is why this is a starting point and not a floor.
        "custody": "vault_transit" if tier == "enterprise" else "in_process",
        "require_fips": False,
    }


_ARCLLM_HEADER = """\
# ArcLLM config — everything LLM-wire for this agent. arcagent composes the
# [llm]/[eval]/[budget] tables below into the effective config. The full arcllm
# module surface is listed at the bottom under [llm.modules.*], commented at its
# packaged default — uncomment a line to override that module for THIS agent
# only. arcllm's OWN global module + provider defaults live in the user-wide
# ~/.arc/arcllm.toml ([defaults]/[modules]/[vault]), read by arcllm itself.
"""

# The default agent model + its deliberately larger-than-model-default output
# cap (ArcLLM's own LLMConfig.max_tokens is 4096; 8192 is the scaffold's
# choice for headroom on a general-purpose agent).
_DEFAULT_ARCLLM_OVERRIDES = {"model": "anthropic/claude-sonnet-4-5-20250929", "max_tokens": 8192}


def _build_default_arcllm_config() -> str:
    """Compose the per-agent arcllm.toml: [llm]/[eval]/[budget] (model-driven,
    via ``config_render``) + the full, commented [llm.modules.*] override
    surface (derived from arcllm's own packaged config.toml — a second,
    already-correct generator this one does not duplicate)."""
    parts = [
        _ARCLLM_HEADER,
        arcagent.config_render.render_arcllm_sections(
            llm_overrides=_DEFAULT_ARCLLM_OVERRIDES
        ).rstrip(),
        "",
        "# --- Per-agent arcllm module overrides. Every module below is shown",
        "# commented at its packaged default; uncomment a line to override that",
        "# module for THIS agent only (unknown module names are rejected). ---",
        commented_module_surface(prefix="llm."),
    ]
    return "\n".join(parts).rstrip() + "\n"


_DEFAULT_ARCLLM_CONFIG = _build_default_arcllm_config()

_ARCRUN_HEADER = """\
# ArcRun config — the agentic-loop controls arcagent hands to the run loop.
# (Per-run token/cost/request ceilings live in arcllm.toml [budget]; the
# tier-floored circuit breakers live in arcagent.toml [security].)
"""

_DEFAULT_ARCRUN_CONFIG = _ARCRUN_HEADER + "\n" + arcagent.config_render.render_arcrun_toml()

# Back-compat symbol: some callers import `_DEFAULT_CONFIG` for its mere
# presence (see `arccli.commands.agent.__init__`'s re-export). It is no longer
# a `.format()` template — `render_agent_config` is the real entry point —
# but one rendered example keeps the name meaningful for any lingering import.
_DEFAULT_CONFIG = render_agent_config(name="agent")

_CALCULATOR_TOOL = '''\
"""Capability: calculate — safe arithmetic via AST parsing."""

from __future__ import annotations

import ast
import operator

import arcagent

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


@arcagent.tool(
    description="Evaluate a math expression. Supports +, -, *, /, %, **.",
    classification="read_only",
    capability_tags=["computation"],
    when_to_use="When you need to evaluate an arithmetic expression deterministically.",
    version="1.0.0",
)
async def calculate(expression: str) -> str:
    """Evaluate ``expression`` safely via AST parsing."""
    try:
        tree = ast.parse(expression, mode="eval")
        return str(_safe_eval(tree))
    except Exception as exc:  # reason: fail-open — continue
        return f"Error: {exc}"
'''


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


_DEFAULT_PULSE = """# Pulse — scheduled self-checks

The agent reads this file each time its pulse fires. The wake interval and the
on/off switch live in `arcagent.toml` under `[modules.pulse]`. Each `##` section
below is one scheduled check the agent runs when it is due; with none, the pulse
wakes and does nothing.

To add a check, follow this shape (the pulse tick is the floor — a check's own
interval decides how often it actually runs):

    ## morning_summary
    - **Interval:** 1440 minutes
    - **Action:** What the agent should do when this check runs.

No checks are defined yet.
"""


def _scaffold_workspace(agent_dir: Path, name: str) -> None:
    """Create the agent + workspace directory structure (SPEC-021 layout)."""
    workspace = agent_dir / "workspace"
    workspace.mkdir(exist_ok=True)

    identity_path = workspace / "identity.md"
    if not identity_path.exists():
        identity_path.write_text(_DEFAULT_IDENTITY)

    policy_path = workspace / "policy.md"
    if not policy_path.exists():
        policy_path.write_text(_default_policy())

    context_path = workspace / "context.md"
    if not context_path.exists():
        result = validate(_DEFAULT_CONTEXT, path=context_path.name)
        if not result.valid:
            raise OKFValidationError(result.diagnostics)
        context_path.write_text(_DEFAULT_CONTEXT)

    # This root index belongs to the workspace scaffold.  ArcMemory owns a
    # separate workspace/memory/index.md for curated-memory retrieval; the root
    # artifact is never treated as that memory index.
    index_path = workspace / "index.md"
    if not index_path.exists():
        index_path.write_text(_DEFAULT_INDEX, encoding="utf-8")

    # Every agent gets a pulse.md so the scheduled-check file exists and is ready
    # to edit; empty means the pulse is a no-op until checks are added.
    pulse_path = workspace / "pulse.md"
    if not pulse_path.exists():
        pulse_path.write_text(_DEFAULT_PULSE, encoding="utf-8")

    # Per-agent capabilities live at the AGENT root (trusted scan root).
    # Agent-authored capabilities go under workspace/capabilities (untrusted).
    (agent_dir / "capabilities").mkdir(exist_ok=True)
    (workspace / "capabilities").mkdir(exist_ok=True)

    # Only scaffold directories the runtime actually reads. Session transcripts
    # land in workspace/sessions/. Memory (workspace/memory/index.db + entities)
    # is created lazily by arcmemory when a Brain is selected, so it is not
    # pre-made here.
    (workspace / "sessions").mkdir(exist_ok=True)


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


def _load_arcagent(agent_dir: Path) -> tuple[Any, Any, Path]:
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

    arc_agent = arcagent.ArcAgent(config, config_path=config_path, fleet=ArcTeamFleet())
    return arc_agent, config, config_path


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
    "AGENT_TIERS",
    "_CALCULATOR_TOOL",
    "_DEFAULT_ARCLLM_CONFIG",
    "_DEFAULT_ARCRUN_CONFIG",
    "_DEFAULT_CONFIG",
    "_DEFAULT_CONTEXT",
    "_DEFAULT_IDENTITY",
    "_capability_scan_roots",
    "_default_policy",
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
    "render_agent_config",
]
