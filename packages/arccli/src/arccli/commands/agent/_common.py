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
import os
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from arccli.commands._shared import print_kv as _print_kv
from arccli.commands._shared import print_table as _print_table

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_GLOBAL_CAP_DIR = Path.home() / ".arc" / "capabilities"

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

[security]
tier = "personal"

[security.validators]
auto_run_agent_code = false

[execution]
# Code-execution isolation floor follows [security] tier:
#   federal -> VM (Firecracker/KVM), enterprise -> container, personal -> container.
# A PERSONAL-tier operator MAY relax down to a bare host subprocess
# ("sandbox off" — full host filesystem access, no container) by uncommenting
# exactly one of the lines below. Rejected at enterprise/federal (cannot go
# below the tier floor). Unset = the tier default (container for personal).
#   relax_isolation = "off"        # sandbox OFF: run on the host, no isolation
#   relax_isolation = "container"  # explicit container (same as the default)

[capabilities]
# Relax the AST import gate for agent-authored tools under
# workspace/capabilities/ WITHOUT moving them out of the protected root.
# Tier-gated: personal = all imports allowed (this block is ignored);
# enterprise = deny by default, set allow_all_imports = true or list
# allow_imports; federal = deny by default, ONLY allow_imports is honored
# (allow_all_imports is ignored). eval/exec/frame-traversal stay blocked always.
allow_all_imports = false
allow_imports = []

[modules.memory]
enabled = true

[modules.memory.config]
# Dual-speed analogical memory (arcmemory). Fresh agents get memory ON:
# zero-LLM capture writes daily-log bullets (workspace/memory/daily-log/
# YYYY-MM-DD.md), the episodic index (workspace/memory/index.db), and the
# entity graph on every turn. Set brain = "none" for a memory-less agent.
# Consolidation (entity cards + facts + insights) is opt-in — set
# distill_provider/distill_model to an LLM to enable it.
brain = "arcmemory"

[modules.policy]
enabled = true

[modules.policy.config]
eval_interval_turns = 5

[modules.scheduler]
enabled = true

[modules.scheduler.config]
check_interval_seconds = 30

[modules.messaging]
enabled = true

[modules.messaging.config]
# Join the shared team bus. ensure_live_backend degrades to an in-memory
# backend (with a warning) if no server is reachable, so a solo agent still runs.
nats_url = "nats://127.0.0.1:4222"

[modules.skills]
enabled = true

[modules.skills.config]
# arcskill is the workspace-declared default skills adapter (see root
# pyproject.toml) — without this block SkillsConfig defaults to
# adapter = "none" and the agent's scaffolded skills/improver never run.
adapter = "arcskill"
tier = "personal"

[arcstore]
enabled = true
store_raw_bodies = false
"""

_CALCULATOR_TOOL = '''\
"""Capability: calculate — safe arithmetic via AST parsing."""

from __future__ import annotations

import ast
import operator

from arcagent.tools import tool

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


@tool(
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

_ENV_PATHS = [
    Path.cwd() / ".env",
    Path.home() / ".arc" / ".env",
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
    paths = list(_ENV_PATHS)
    # Honor an isolated ARC_CONFIG_DIR so a self-contained deployment folder's
    # own .env loads (the ~/.arc default in _ENV_PATHS misses it) — "start up,
    # config, and go" without exporting keys by hand.
    cfg = os.environ.get("ARC_CONFIG_DIR")
    if cfg:
        paths.insert(0, Path(cfg).expanduser() / ".env")
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
    from arcagent.core.config import load_config

    from arccli.commands._capability_registry import build_capability_registry

    config_path = agent_dir / "arcagent.toml"
    if not config_path.is_file():
        return []
    try:
        config = load_config(config_path)
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
        context_path.write_text(_DEFAULT_CONTEXT)

    # Per-agent capabilities live at the AGENT root (trusted scan root).
    # Agent-authored capabilities go under workspace/capabilities (untrusted).
    (agent_dir / "capabilities").mkdir(exist_ok=True)
    (workspace / "capabilities").mkdir(exist_ok=True)

    # Only scaffold directories the runtime actually reads. Session transcripts
    # land in workspace/sessions/. Memory (workspace/memory/index.db + entities)
    # is created lazily by arcmemory when a Brain is selected, so it is not
    # pre-made here.
    (workspace / "sessions").mkdir(exist_ok=True)


def _print_scaffold_summary(display_name: str, agent_dir: Path) -> None:
    """Print directory structure and next-steps after scaffold."""
    sys.stdout.write("\n")
    sys.stdout.write("Structure:\n")
    sys.stdout.write(f"  {display_name}/\n")
    sys.stdout.write("    arcagent.toml\n")
    sys.stdout.write("    capabilities/             # per-agent capabilities (trusted)\n")
    sys.stdout.write("      calculator.py\n")
    sys.stdout.write("    workspace/\n")
    sys.stdout.write("      identity.md, policy.md, context.md\n")
    sys.stdout.write("      capabilities/          # agent-authored (UNTRUSTED, AST-validated)\n")
    sys.stdout.write("      sessions/              # chat transcripts (JSONL)\n")
    sys.stdout.write("      memory/                # lazily created when a Brain is enabled\n")
    sys.stdout.write("\n")
    sys.stdout.write("Next steps:\n")
    sys.stdout.write(f"  arc agent build {agent_dir}\n")
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
        ("global", _GLOBAL_CAP_DIR),
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
    "_CALCULATOR_TOOL",
    "_DEFAULT_CONFIG",
    "_DEFAULT_CONTEXT",
    "_DEFAULT_IDENTITY",
    "_ENV_PATHS",
    "_GLOBAL_CAP_DIR",
    "_capability_scan_roots",
    "_default_policy",
    "_discover_runtime_tools",
    "_discover_tools",
    "_iter_capability_files",
    "_iter_skill_folders",
    "_load_agent_config",
    "_load_arcagent",
    "_load_env",
    "_print_kv",
    "_print_result_json",
    "_print_scaffold_summary",
    "_print_table",
    "_resolve_agent_dir",
    "_scaffold_workspace",
    "asyncio",
]
