"""SPEC arcui-reality-mirror COMP-007 — capability inventory seam.

A single read seam over :class:`CapabilityLoader` that enumerates every skill
and capability tool an agent would load across the four scan roots (package
builtins, the global ``~/.arc/capabilities`` root, the per-agent
``<agent>/capabilities`` root, and the agent-authored
``<agent>/workspace/capabilities`` root) and reports each item's loader/TOFU
verdict verbatim.

arcui consumes this instead of globbing skill or tool paths itself
(REQ-093/094/096). No discovery or verification logic lives here: the loader
owns both, and :attr:`CapabilityInventoryItem.status` is whatever the loader
recorded — never re-derived, so a new verdict added to the loader flows through
untouched.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from arctrust import TofuLayer, hash_source
from pydantic import BaseModel, ConfigDict

import arcagent.builtins.capabilities as _builtins_pkg
from arcagent.capabilities.capability_loader import CapabilityLoader, ScanRoot
from arcagent.capabilities.capability_registry import CapabilityRegistry
from arcagent.core.config import CapabilitiesConfig, SecurityConfig, load_config
from arcagent.tools._dynamic_loader import (
    DEFAULT_IMPORT_POLICY,
    ImportPolicy,
    resolve_workspace_import_policy,
)

_logger = logging.getLogger("arcagent.capabilities.inventory")

# Only skills and capability tools surface in the inventory; hooks, background
# tasks, and capability classes are out of scope for the arcui capability views.
_INVENTORY_KINDS: frozenset[str] = frozenset({"skill", "tool"})
_DEFAULT_GLOBAL_ROOT = Path("~/.arc/capabilities")
_KNOWN_TIERS: frozenset[str] = frozenset({"personal", "enterprise", "federal"})


class CapabilityInventoryItem(BaseModel):
    """One enumerated capability with its verbatim loader verdict.

    ``status`` mirrors :attr:`CapabilityOutcome.status` exactly — ``"loaded"``,
    a TOFU decision (``"deny"`` / ``"new_sighting"``), ``"unsigned"``,
    ``"invalid"``, ``"error"``, or any future verdict the loader introduces.
    """

    model_config = ConfigDict(frozen=True)

    kind: str
    name: str
    version: str
    description: str
    source_root: str
    source_path: str
    status: str
    status_detail: str


def _resolve_scan_roots(
    agent_dir: Path,
    workspace_dir: Path | None,
    global_root: Path | None,
    builtins_root: Path | None,
) -> list[ScanRoot]:
    """Build the four-root scan list, mirroring ``setup_capabilities``.

    Optional roots are included only when they exist on disk, matching the
    loader's own precedence order (builtins, global, agent, workspace).
    """
    builtins = builtins_root if builtins_root is not None else Path(_builtins_pkg.__file__).parent
    roots: list[ScanRoot] = [
        ("builtins", builtins),
        ("builtins-skills", builtins / "skills"),
    ]
    resolved_global = (
        global_root if global_root is not None else _DEFAULT_GLOBAL_ROOT
    ).expanduser()
    append_capability_scan_roots(roots, "global", resolved_global)
    append_capability_scan_roots(roots, "agent", agent_dir / "capabilities")
    workspace = workspace_dir if workspace_dir is not None else agent_dir / "workspace"
    append_capability_scan_roots(roots, "workspace", workspace / "capabilities")
    return roots


def append_capability_scan_roots(roots: list[ScanRoot], name: str, caps_dir: Path) -> None:
    """Append a capabilities root plus its ``skills/`` subdir, mirroring builtins.

    Tools live directly under ``<caps_dir>/`` and skills under
    ``<caps_dir>/skills/`` (where ``create_skill`` writes), so each agent-writable
    root contributes two loader roots — ``<name>`` and ``<name>-skills`` — exactly
    like ``builtins`` / ``builtins-skills``.
    """
    if caps_dir.is_dir():
        roots.append((name, caps_dir))
    skills_dir = caps_dir / "skills"
    if skills_dir.is_dir():
        roots.append((f"{name}-skills", skills_dir))


async def collect_capability_inventory(
    agent_dir: Path,
    *,
    workspace_dir: Path | None = None,
    global_root: Path | None = None,
    builtins_root: Path | None = None,
    tofu: TofuLayer | None = None,
    require_signature: bool = False,
    trusted_public_key: bytes | None = None,
    import_policy: ImportPolicy = DEFAULT_IMPORT_POLICY,
) -> list[CapabilityInventoryItem]:
    """Enumerate an agent's skills and capability tools with verbatim verdicts.

    Drives :class:`CapabilityLoader` over the four scan roots against a throwaway
    :class:`CapabilityRegistry` (this is a read seam; it never mutates the live
    agent registry). The trust arguments mirror the loader's construction so the
    caller can reproduce the agent's real tier posture — a personal-tier
    :class:`TofuLayer` with a pinned key surfaces signed workspace sources as
    ``loaded`` and unsigned ones as ``deny``.
    """
    scan_roots = _resolve_scan_roots(agent_dir, workspace_dir, global_root, builtins_root)
    loader = CapabilityLoader(
        scan_roots=scan_roots,
        registry=CapabilityRegistry(),
        tofu=tofu,
        require_signature=require_signature,
        trusted_public_key=trusted_public_key,
        import_policy=import_policy,
        # Task #39: this is a read-only scan over a throwaway registry — a
        # discovered @background_task must never actually start (its body
        # may depend on a live agent's module _runtime being configured).
        spawn_background_tasks=False,
    )
    delta = await loader.scan_and_register()
    return [
        CapabilityInventoryItem(
            kind=outcome.kind,
            name=outcome.name,
            version=outcome.version,
            description=outcome.description,
            source_root=outcome.scan_root,
            source_path=outcome.source_path,
            status=outcome.status,
            status_detail=outcome.status_detail,
        )
        for outcome in delta.outcomes
        if outcome.kind in _INVENTORY_KINDS
    ]


# ---------------------------------------------------------------------------
# Agent-aware companion — resolves the agent's real trust posture, then runs
# the seam. This is the single source of truth for "load posture", shared with
# ``agent_lifecycle.setup_capabilities`` so arcui mirrors reality without
# re-deriving security posture (REQ-094/096).
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TrustPosture:
    """The loader trust arguments an agent's config + identity resolve to.

    Both :func:`arcagent.core.agent_lifecycle.setup_capabilities` (live load)
    and :func:`collect_agent_capability_inventory` (read-only inventory) build
    this the same way, so a UI inventory and a real load agree on every verdict.
    """

    tofu: TofuLayer
    require_signature: bool
    trusted_public_key: bytes | None
    import_policy: ImportPolicy


def resolve_trust_posture(
    security: SecurityConfig,
    capabilities: CapabilitiesConfig,
    *,
    trusted_public_key: bytes | None,
) -> TrustPosture:
    """Resolve the load-time trust posture from an agent's config.

    ``require_signature`` is the enterprise/federal signature floor; the TOFU
    layer carries the per-tier source-approval policy; the import policy is the
    tier-resolved allowlist for agent-authored workspace tools. ``trusted_public_key``
    is the agent's pinned DID key (its own signatures verify against it) — the
    caller supplies it because it lives with the identity, not the config.
    """
    tier = security.tier
    import_policy = resolve_workspace_import_policy(
        tier,
        allow_all_imports=capabilities.allow_all_imports,
        allow_imports=capabilities.allow_imports,
    )
    tofu = TofuLayer(
        tier if tier in _KNOWN_TIERS else "personal",
        security.validators,
    )
    return TrustPosture(
        tofu=tofu,
        require_signature=tier in ("enterprise", "federal"),
        trusted_public_key=trusted_public_key,
        import_policy=import_policy,
    )


class RuntimeToolItem(BaseModel):
    """One tool registered in a LOADED agent's runtime ToolRegistry (REQ-095)."""

    model_config = ConfigDict(frozen=True)

    name: str
    description: str
    classification: str
    transport: str


class AgentCapabilityInventory(BaseModel):
    """Full capability picture for one agent.

    ``items`` are the four-scan-root skills + capability tools with verbatim
    verdicts. ``runtime`` is True only when computed against a live agent, in
    which case ``runtime_tools`` lists every tool actually registered at runtime
    (builtins + capability-file + module + self-authored) — the truth a
    not-loaded agent can only approximate from its ``kind == "tool"`` items.
    """

    model_config = ConfigDict(frozen=True)

    items: list[CapabilityInventoryItem]
    runtime: bool
    runtime_tools: list[RuntimeToolItem]


async def collect_agent_capability_inventory(
    agent_config_path: Path,
    *,
    live_agent: Any = None,
    global_root: Path | None = None,
) -> AgentCapabilityInventory:
    """Enumerate one agent's capabilities at its real trust posture.

    Reads ``agent_config_path`` (an ``arcagent.toml``), resolves the agent's
    tier/validators/pinned-key/import-policy via :func:`resolve_trust_posture`,
    and runs the frozen inventory seam so verdicts match a real load. Pass the
    live in-process ``ArcAgent`` (from the embedded cache) to also surface its
    runtime-registered tool list; omit it for a not-loaded agent (``runtime``
    False, tools approximated by the ``kind == "tool"`` inventory items).
    """
    config = load_config(agent_config_path)
    agent_root = agent_config_path.parent
    # config.agent.workspace defaults to the relative "./workspace"; resolve it
    # against the agent root (an absolute value passes through unchanged).
    workspace = agent_root / (config.agent.workspace or "workspace")
    trusted_public_key = _resolve_pinned_key(config, agent_config_path, live_agent)
    posture = resolve_trust_posture(
        config.security, config.capabilities, trusted_public_key=trusted_public_key
    )
    items = await collect_capability_inventory(
        agent_root,
        workspace_dir=workspace,
        global_root=global_root,
        tofu=posture.tofu,
        require_signature=posture.require_signature,
        trusted_public_key=posture.trusted_public_key,
        import_policy=posture.import_policy,
    )
    if live_agent is None:
        return AgentCapabilityInventory(items=items, runtime=False, runtime_tools=[])
    runtime_tools = [
        RuntimeToolItem(
            name=tool.name,
            description=tool.description,
            classification=str(tool.classification),
            transport=str(getattr(tool, "transport", "")),
        )
        for tool in live_agent.registered_tools
    ]
    return AgentCapabilityInventory(items=items, runtime=True, runtime_tools=runtime_tools)


def _resolve_pinned_key(config: Any, config_path: Path, live_agent: Any) -> bytes | None:
    """Best-effort pinned DID public key for signature verification.

    Live agent → its already-loaded identity key (fully faithful). Otherwise
    load the persisted identity for the config's DID (read-only — a DID is
    always set for a deployed agent, so this never triggers key generation or a
    config rewrite). Returns None when unresolvable (e.g. a vault-only federal
    key with no resolver), which faithfully yields the "cannot verify" posture.
    """
    if live_agent is not None:
        identity = getattr(live_agent, "_identity", None)
        if identity is not None:
            key: bytes = identity.public_key
            return key
    if not config.identity.did:
        return None
    try:
        from arctrust.identity import AgentIdentity

        identity = AgentIdentity.from_config(
            config.identity,
            org=config.agent.org,
            agent_type=config.agent.type,
            config_path=config_path,
        )
        loaded_key: bytes = identity.public_key
        return loaded_key
    except Exception:  # reason: fail-open — unresolved key => cannot-verify posture
        _logger.debug("pinned key unresolved for %s", config_path, exc_info=True)
        return None


# ---------------------------------------------------------------------------
# Gated-capability listing — the arcui/CLI read seam for ``arc trust``.
# Discovery (which capabilities did NOT load, and their current source hash)
# lives here in arcagent because it drives the inventory; the APPROVAL of a
# gated capability is a trust-store mutation owned by arctrust (``arctrust.approve``
# / ``arctrust.disapprove``). A caller lists here, then approves via arctrust.
# ---------------------------------------------------------------------------


class GatedItem(BaseModel):
    """One capability with its load verdict, as the trust surfaces present it.

    Field order and names are the frozen ``/api/trust`` wire contract:
    ``model_dump(mode="json")`` is the GatedItem the arcui frontend consumes.
    ``path`` is the gated artifact (the ``.py`` for a tool, ``SKILL.md`` for a
    skill); ``hash`` is the sha256 of that artifact's current bytes — the hash
    an approval would pin.
    """

    model_config = ConfigDict(frozen=True)

    agent_id: str
    agent_label: str
    name: str
    kind: str
    status: str
    path: str
    hash: str
    detail: str


def read_capability_source(path: Path) -> str | None:
    """Decode an artifact's bytes exactly as the loader does before hashing.

    The loader hashes ``path.read_bytes().decode("utf-8")``; matching that byte
    path is what makes an approval pin line up with a later load. Returns None
    when the source cannot be read (a caller surfaces this rather than pinning
    an empty hash).
    """
    try:
        return path.read_bytes().decode("utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def pin_name_for(item: GatedItem) -> str:
    """The name TofuLayer keys ``item`` on — NOT always its display name.

    The loader gates a tool under its file stem and a skill under its FOLDER
    name (``SKILL.md``'s parent), while a skill's displayed ``name`` is its
    frontmatter name, which can differ. Deriving the pin name from the source
    path keeps an approval aligned with what the loader will look up. Callers
    pass this to ``arctrust.approve`` / ``arctrust.disapprove``.
    """
    source = Path(item.path)
    if item.kind == "skill":
        return source.parent.name
    return source.stem


async def list_gated(
    agent_root: Path,
    *,
    agent_id: str = "",
    agent_label: str = "",
    global_root: Path | None = None,
    include_loaded: bool = False,
) -> list[GatedItem]:
    """Enumerate ``agent_root``'s gated capabilities at its real trust posture.

    Runs the capability inventory seam (the same one a real load uses) and
    returns every item whose ``status`` is not ``"loaded"`` — i.e. refused,
    awaiting approval, or errored. Pass ``include_loaded=True`` to return the
    loaded ones too (a caller that shows the whole picture). Each item carries
    the current sha256 of its source so a UI can show what a pin would fix.
    """
    config_path = agent_root / "arcagent.toml"
    inventory = await collect_agent_capability_inventory(config_path, global_root=global_root)
    gated: list[GatedItem] = []
    for item in inventory.items:
        if item.status == "loaded" and not include_loaded:
            continue
        source_text = read_capability_source(Path(item.source_path))
        gated.append(
            GatedItem(
                agent_id=agent_id,
                agent_label=agent_label or agent_id,
                name=item.name,
                kind=item.kind,
                status=item.status,
                path=item.source_path,
                hash=hash_source(source_text) if source_text is not None else "",
                detail=item.status_detail,
            )
        )
    return gated


__all__ = [
    "AgentCapabilityInventory",
    "CapabilityInventoryItem",
    "GatedItem",
    "RuntimeToolItem",
    "TrustPosture",
    "append_capability_scan_roots",
    "collect_agent_capability_inventory",
    "collect_capability_inventory",
    "list_gated",
    "pin_name_for",
    "read_capability_source",
    "resolve_trust_posture",
]
