"""Create a new agent on disk — the one implementation every surface uses.

``arc agent create`` (CLI) and the dashboard's "New agent" (arcui) both build an
agent through :func:`create_agent`, so an agent made in the browser is the same
agent the terminal makes: the same three config files, the same workspace, a
minted DID, and operator-signed control-plane documents.

Signing goes through the operator's :class:`arctrust.Signer` *handle*: this module
never sees key material, so a vault-held operator key works exactly like a local
one. The agent's own key never signs anything here — an agent cannot approve
its own identity or capabilities.

What stays with the caller: resolving the operator signer (CLI custody vs the
dashboard's injected factory) and fleet registration (arcteam sits above
arcagent). The commented ``[llm.modules.*]`` surface comes through the arcrun
facade (``arcrun.model_module_surface``) — arcagent never imports arcllm.
"""

from __future__ import annotations

import datetime
import json
import re
import shutil
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import arcrun
from arcokf import OKFValidationError, render_folder_index, validate
from arcprompt import PromptHistory, record_if_unseen
from arctrust import AgentIdentity, Signer
from arctrust.artifact import sign_artifact_with_signer

from arcagent.capabilities.capability_signing import sign as sign_capability
from arcagent.core.config import load_config
from arcagent.core.prompt_context import signed_workspace_files
from arcagent.core.tier import Tier
from arcagent.tiers import SECURITY_CONFIG_KNOBS
from arcagent.utils import config_render

AGENT_TIERS: tuple[str, ...] = (Tier.PERSONAL.value, Tier.ENTERPRISE.value, Tier.FEDERAL.value)
"""Canonical deployment tiers, least to most stringent."""

DEFAULT_MODEL = "anthropic/claude-sonnet-4-5-20250929"

#: Lowercase, no dots or slashes: the name is a directory under the fleet root
#: and the agent's id in every URL, so anything that could climb out of the
#: fleet root or collide with a sibling by case is refused before any write.
_AGENT_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{1,39}$")

#: ``provider/model`` slugs only — the value is written into arcllm.toml.
_MODEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,199}$")

#: Persona documents an import may carry. Everything else in a source agent —
#: capability code, keys, config — is never imported: code from another box is
#: unreviewed, and keys never travel.
IMPORTABLE_DOCUMENTS: tuple[str, ...] = ("identity.md", "policy.md", "context.md", "pulse.md")

_MAX_DOCUMENT_BYTES = 256 * 1024

CONFIG_SNAPSHOT_FILENAME = ".arc-config-defaults.json"
"""Per-agent record of the value the scaffold last wrote at each dotted key."""

_SIGNATURE_SUFFIX = ".arcsig"


class AgentNameError(ValueError):
    """The requested agent name is not a safe directory name."""


class AgentExistsError(FileExistsError):
    """An agent with this name already exists in the fleet root."""


@dataclass(frozen=True)
class OperatorSigning:
    """The operator's signer handle and the DID its signatures are recorded under."""

    did: str
    signer: Signer


@dataclass(frozen=True)
class CreatedAgent:
    """What :func:`create_agent` produced."""

    name: str
    agent_dir: Path
    did: str
    public_key_hex: str
    signed: tuple[str, ...]
    """Files the operator signed (empty when no operator signer was supplied)."""


# ---------------------------------------------------------------------------
# Templates
# ---------------------------------------------------------------------------

DEFAULT_IDENTITY = """\
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


def default_policy() -> str:
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

_DEFAULT_INDEX = render_folder_index((), root=False)

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
        "security": {
            "tier": tier,
            "policy_audit_log": "",
            **_crypto_posture(tier),
            # Federal refuses the local journal fail-closed, and this template
            # states every knob outright — so federal must state its floor.
            "skill_revision_anchor": "vault" if tier == "federal" else "file",
        },
        "telemetry": {"service_name": name},
    }
    body = config_render.render_arcagent_toml(
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
        floors = {k.name: k.federal_floor for k in SECURITY_CONFIG_KNOBS}
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


def render_arcllm_config(*, model: str = DEFAULT_MODEL) -> str:
    """Compose the per-agent arcllm.toml.

    ``[llm]``/``[eval]``/``[budget]`` come from arcagent's own models; the output
    cap is NOT set here (arcllm owns that number). The commented
    ``[llm.modules.*]`` override surface is rendered by the model layer from its
    own packaged config, reached through arcrun.
    """
    parts = [
        _ARCLLM_HEADER,
        config_render.render_arcllm_sections(llm_overrides={"model": model}).rstrip(),
        "",
        "# --- Per-agent arcllm module overrides. Every module below is shown",
        "# commented at its packaged default; uncomment a line to override that",
        "# module for THIS agent only (unknown module names are rejected). ---",
        arcrun.model_module_surface(prefix="llm."),
    ]
    return "\n".join(parts).rstrip() + "\n"


_ARCRUN_HEADER = """\
# ArcRun config — the agentic-loop controls arcagent hands to the run loop.
# (Per-run token/cost/request ceilings live in arcllm.toml [budget]; the
# tier-floored circuit breakers live in arcagent.toml [security].)
"""

DEFAULT_ARCRUN_CONFIG = _ARCRUN_HEADER + "\n" + config_render.render_arcrun_toml()

CALCULATOR_TOOL = '''\
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


# ---------------------------------------------------------------------------
# Workspace + signing
# ---------------------------------------------------------------------------


def validate_agent_name(name: str) -> str:
    """Return ``name`` when it is a safe agent directory name, else raise."""
    if not isinstance(name, str) or not _AGENT_NAME_RE.fullmatch(name):
        raise AgentNameError(
            "Agent names are 2 to 40 characters: lowercase letters, digits, - or _, "
            "starting with a letter or digit."
        )
    return name


def _validate_documents(documents: Mapping[str, str]) -> dict[str, str]:
    """Check imported persona documents: known names, text, bounded size, valid OKF."""
    checked: dict[str, str] = {}
    for fname, text in documents.items():
        if fname not in IMPORTABLE_DOCUMENTS:
            allowed = ", ".join(IMPORTABLE_DOCUMENTS)
            raise ValueError(f"{fname!r} cannot be imported; only {allowed}")
        if not isinstance(text, str) or len(text.encode("utf-8")) > _MAX_DOCUMENT_BYTES:
            raise ValueError(f"{fname} must be text under 256 KB")
        if fname == "context.md":
            result = validate(text, path=fname)
            if not result.valid:
                raise OKFValidationError(result.diagnostics)
        checked[fname] = text
    if "identity.md" in checked and not checked["identity.md"].strip():
        raise ValueError("identity.md is empty")
    return checked


def scaffold_workspace(
    agent_dir: Path,
    name: str,
    *,
    operator: OperatorSigning | None,
    documents: Mapping[str, str] | None = None,
) -> tuple[str, ...]:
    """Create the agent workspace layout (SPEC-021), filling only missing files.

    ``documents`` overrides the default text of a persona file that does not
    exist yet (an import). A newly written ``identity.md`` is operator-signed when
    ``operator`` is given — an unsigned one is refused at every run start (J2 F2).
    Returns the files signed.
    """
    texts = _validate_documents(documents or {})
    workspace = agent_dir / "workspace"
    workspace.mkdir(exist_ok=True)

    identity_path = workspace / "identity.md"
    new_identity = not identity_path.exists()
    if new_identity:
        identity_path.write_text(texts.get("identity.md", DEFAULT_IDENTITY), encoding="utf-8")

    policy_path = workspace / "policy.md"
    if not policy_path.exists():
        policy_path.write_text(texts.get("policy.md", default_policy()), encoding="utf-8")

    context_path = workspace / "context.md"
    if not context_path.exists():
        context = texts.get("context.md", _DEFAULT_CONTEXT)
        result = validate(context, path=context_path.name)
        if not result.valid:
            raise OKFValidationError(result.diagnostics)
        context_path.write_text(context, encoding="utf-8")

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
        pulse_path.write_text(texts.get("pulse.md", _DEFAULT_PULSE), encoding="utf-8")

    # Per-agent capabilities live at the AGENT root (trusted scan root).
    # Agent-authored capabilities go under workspace/capabilities (untrusted).
    (agent_dir / "capabilities").mkdir(exist_ok=True)
    (workspace / "capabilities").mkdir(exist_ok=True)

    # Only scaffold directories the runtime actually reads. Session transcripts
    # land in workspace/sessions/. Memory is created lazily by arcmemory.
    (workspace / "sessions").mkdir(exist_ok=True)

    if new_identity and operator is not None:
        return tuple(sign_workspace_documents(agent_dir, operator))
    return ()


def sign_workspace_documents(agent_dir: Path, operator: OperatorSigning) -> list[str]:
    """Operator-sign every signed-document file present in ``agent_dir/workspace``.

    ``identity.md`` and ``policy_pinned.md`` are verified against the deployment
    operator key at every run start (J2 F2). The detached signature lives under
    the agent's ``context/workspace/`` overlay root, where the agent's own tools
    cannot reach it. Absent documents are skipped. Returns the file names signed.
    """
    signed: list[str] = []
    for (package, name), path in signed_workspace_files(agent_dir / "workspace").items():
        if not path.is_file():
            continue
        content = path.read_bytes()
        signature = sign_artifact_with_signer(
            content, signer_did=operator.did, signer=operator.signer
        ).to_json()
        sidecar = agent_dir / "context" / package / f"{name}.md{_SIGNATURE_SUFFIX}"
        sidecar.parent.mkdir(parents=True, exist_ok=True)
        sidecar.write_text(signature, encoding="utf-8")
        # Same capture-if-unseen path as every other signed write, so the version
        # history lists this signing and a re-sign of unchanged bytes adds nothing.
        record_if_unseen(PromptHistory(agent_dir, package, name), content, signature)
        signed.append(path.name)
    return signed


def mint_agent_identity(agent_dir: Path) -> AgentIdentity:
    """Materialize the agent's real identity from its scaffolded config.

    ``from_config`` mints + persists the keypair and writes the DID into
    ``arcagent.toml``, so this is the SAME identity the agent signs with at
    startup and registers with the fleet under.
    """
    config_path = agent_dir / "arcagent.toml"
    config = load_config(config_path)
    return AgentIdentity.from_config(
        config.identity,
        org=config.agent.org,
        agent_type=config.agent.type,
        config_path=config_path,
    )


def _flatten(table: Mapping[str, Any], prefix: str = "") -> dict[str, Any]:
    """Dotted-key -> leaf value; a list (including a table array) is one leaf."""
    flat: dict[str, Any] = {}
    for key, value in table.items():
        path = f"{prefix}{key}"
        if isinstance(value, dict):
            flat.update(_flatten(value, f"{path}."))
        else:
            flat[path] = value
    return flat


def flatten_config(table: Mapping[str, Any]) -> dict[str, Any]:
    """Dotted-key view of a parsed TOML table (the config-snapshot shape)."""
    return _flatten(table)


def write_config_snapshot(agent_dir: Path) -> None:
    """Record this agent's CURRENT arcagent.toml values as the refresh baseline (H-039).

    Written right after the file is — at creation, and after every config
    refresh — so the NEXT refresh can tell "operator changed this since" from
    "still whatever we last wrote here".
    """
    config_text = (agent_dir / "arcagent.toml").read_text(encoding="utf-8")
    flat = flatten_config(tomllib.loads(config_text))
    (agent_dir / CONFIG_SNAPSHOT_FILENAME).write_text(
        json.dumps(flat, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def _agent_dir_under(parent: Path, name: str) -> Path:
    """``parent/name``, proven to sit directly inside ``parent`` (no symlink escape)."""
    root = parent.expanduser().resolve()
    agent_dir = root / validate_agent_name(name)
    if agent_dir.parent != root or agent_dir.is_symlink():
        raise AgentNameError("Agent names must name a folder directly inside the fleet.")
    return agent_dir


def create_agent(
    parent: Path,
    name: str,
    *,
    tier: str = "personal",
    model: str = DEFAULT_MODEL,
    operator: OperatorSigning | None,
    documents: Mapping[str, str] | None = None,
) -> CreatedAgent:
    """Scaffold a complete new agent under ``parent/name`` and mint its identity.

    The directory must not exist (``AgentExistsError``). Writes the three config
    files (+ refresh snapshot), the workspace, and the example calculator
    capability; mints the agent's DID; and, when ``operator`` is given, signs
    ``identity.md`` and the calculator with the operator handle. ``documents``
    supplies imported persona text (see :data:`IMPORTABLE_DOCUMENTS`).
    """
    if tier not in AGENT_TIERS:
        raise ValueError(f"unknown tier {tier!r} — choose one of {', '.join(AGENT_TIERS)}")
    if not _MODEL_RE.fullmatch(model):
        raise ValueError("The model must look like provider/model-name.")
    checked = _validate_documents(documents or {})
    agent_dir = _agent_dir_under(parent, name)
    agent_dir.parent.mkdir(parents=True, exist_ok=True)
    try:
        agent_dir.mkdir()
    except FileExistsError as exc:
        raise AgentExistsError(f"An agent named {name} already exists.") from exc
    try:
        return _populate(
            agent_dir,
            name,
            tier=tier,
            model=model,
            operator=operator,
            documents=checked,
        )
    except BaseException:
        # Never leave a half-built agent the roster would list: this call made
        # the directory, so it removes it.
        shutil.rmtree(agent_dir, ignore_errors=True)
        raise


def _populate(
    agent_dir: Path,
    name: str,
    *,
    tier: str,
    model: str,
    operator: OperatorSigning | None,
    documents: Mapping[str, str],
) -> CreatedAgent:
    # Three sibling config files compose into one effective config.
    (agent_dir / "arcagent.toml").write_text(
        render_agent_config(name=name, tier=tier), encoding="utf-8"
    )
    write_config_snapshot(agent_dir)
    (agent_dir / "arcllm.toml").write_text(render_arcllm_config(model=model), encoding="utf-8")
    (agent_dir / "arcrun.toml").write_text(DEFAULT_ARCRUN_CONFIG, encoding="utf-8")

    # The DID is minted before anything is signed so the capability pin lands in
    # the same arcagent.toml the agent starts from.
    identity = mint_agent_identity(agent_dir)
    signed = list(scaffold_workspace(agent_dir, name, operator=operator, documents=documents))

    calc_path = agent_dir / "capabilities" / "calculator.py"
    calc_path.write_text(CALCULATOR_TOOL, encoding="utf-8")
    if operator is not None:
        # The agent can write capabilities/, so the operator — never the agent
        # key — signs there (SPEC-033).
        sign_capability(
            calc_path,
            signer_did=operator.did,
            signer=operator.signer,
            config_path=agent_dir / "arcagent.toml",
        )
        signed.append(calc_path.name)

    return CreatedAgent(
        name=name,
        agent_dir=agent_dir,
        did=identity.did,
        public_key_hex=identity.public_key.hex(),
        signed=tuple(signed),
    )


__all__ = [
    "AGENT_TIERS",
    "CALCULATOR_TOOL",
    "CONFIG_SNAPSHOT_FILENAME",
    "DEFAULT_ARCRUN_CONFIG",
    "DEFAULT_IDENTITY",
    "DEFAULT_MODEL",
    "IMPORTABLE_DOCUMENTS",
    "AgentExistsError",
    "AgentNameError",
    "CreatedAgent",
    "OperatorSigning",
    "create_agent",
    "default_policy",
    "flatten_config",
    "mint_agent_identity",
    "render_agent_config",
    "render_arcllm_config",
    "scaffold_workspace",
    "sign_workspace_documents",
    "validate_agent_name",
    "write_config_snapshot",
]
