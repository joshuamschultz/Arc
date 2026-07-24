"""Run-start prompt resolution + provenance wiring (editable-system-prompts COMP-006).

At agent setup the agent builds one :class:`~arcprompt.PromptResolver` — pinned to
the DEPLOYMENT OPERATOR's public key, because overlays are authored and signed by
the operator through arcui, not by the agent's own DID. At run start the agent
freezes a :class:`~arcprompt.PromptSnapshot` of the complete prompt set and emits
one provenance audit event enumerating every prompt with its source, digest, and
overlay signer (REQ-123, REQ-132). The snapshot then backs the overlay-aware
resolver passed into ``get_strategy_prompts`` and the agent's own assembled
sections, so an operator override takes effect on the next run and is attributable
to exact bytes — never silently, never per-turn-shifting.
"""

from __future__ import annotations

import tomllib
from collections.abc import Callable
from pathlib import Path
from typing import Any

from arcprompt import (
    PromptCatalog,
    PromptMissing,
    PromptResolver,
    PromptSnapshot,
    TrustPosture,
)
from arcprompt import snapshot as _build_snapshot
from arctrust.audit import AuditEvent

_KNOWN_POSTURES = {p.value for p in TrustPosture}

AuditEmit = Callable[[str, dict[str, Any]], None]


def _operator_public_key() -> bytes | None:
    """Return the deployment operator's Ed25519 verify key, or None if unavailable.

    Overlays are signed with the operator key (the arcui SigningAuthority seam),
    so pinning it here is what makes an operator override verify. Absent on a
    bare/test agent → None → the resolver refuses any overlay (fail-closed) while
    stock still resolves normally.
    """
    try:
        from arctrust import OperatorKey, default_operator_key_path

        return OperatorKey.load(default_operator_key_path(), generate_if_absent=False).public_key
    except (OSError, ValueError, RuntimeError):
        return None


def read_agent_tier(agent_root: Path) -> str:
    """Read ``[security].tier`` from an agent's ``arcagent.toml`` (default ``personal``).

    The single source of truth for "what tier is this agent" used by the prompt
    surfaces (arcui route, arccli) so they resolve the same posture for the same
    agent. An unreadable/absent/unknown tier degrades to the least-privileged
    label rather than raising — a parse miss must never weaken a configured gate.
    """
    try:
        data = tomllib.loads((agent_root / "arcagent.toml").read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return "personal"
    tier = str(data.get("security", {}).get("tier", "personal"))
    return tier if tier in _KNOWN_POSTURES else "personal"


def _resolver_for_root(agent_root: Path, tier: str) -> PromptResolver:
    """Build an overlay-aware resolver rooted at ``<agent_root>/context``.

    Overlay root is outside the workspace subtree agent file tools are confined to
    (COMP-007). ``tier`` maps to the trust posture; an unknown tier degrades to
    ``personal`` rather than raising.
    """
    posture = TrustPosture(tier) if tier in _KNOWN_POSTURES else TrustPosture.PERSONAL
    return PromptResolver(
        overlay_root=agent_root.resolve() / "context",
        trusted_public_key=_operator_public_key(),
        posture=posture,
    )


def build_prompt_resolver(config_path: Path, tier: str) -> PromptResolver:
    """Construct the agent's overlay-aware resolver once (SPEC-017 build-at-setup precedent)."""
    return _resolver_for_root(config_path.parent, tier)


def agent_prompt_resolve(agent_root: Path, tier: str) -> Callable[[str, str], str]:
    """Return a ``(package, name) -> effective body`` closure for a consumer to use.

    This is how a decoupled consumer (e.g. arcskill, which never imports arcprompt)
    is *handed* overlay-aware, signature-verified resolution: arcagent builds the
    resolver and passes this closure in. An operator override authored through
    arcui/CLI therefore takes effect in that consumer's prompts, while the consumer
    still runs on stock when handed no resolver at all.
    """
    resolver = _resolver_for_root(agent_root, tier)

    def _resolve(package: str, name: str) -> str:
        return resolver.resolve(package, name).body

    return _resolve


class _TelemetryAuditSink:
    """Adapt the agent's ``telemetry.audit_event`` to arctrust's ``AuditSink.write``."""

    def __init__(self, audit_event: AuditEmit) -> None:
        self._audit_event = audit_event

    def write(self, event: AuditEvent) -> None:
        self._audit_event(
            event.action,
            {"tier": event.tier, "prompts": event.extra.get("prompts", [])},
        )


def snapshot_run_prompts(
    resolver: PromptResolver,
    *,
    actor_did: str,
    audit_event: AuditEmit,
    request_id: str | None = None,
) -> PromptSnapshot:
    """Freeze the complete prompt set for a run and emit one provenance event."""
    refs = PromptCatalog().catalog()
    return _build_snapshot(
        resolver,
        refs,
        actor_did=actor_did,
        sink=_TelemetryAuditSink(audit_event),
        request_id=request_id,
    )


def snapshot_resolver(snapshot: PromptSnapshot) -> Callable[[str, str], str]:
    """A resolver closure reading bodies from a frozen snapshot (KeyError → PromptMissing).

    Passed to ``get_strategy_prompts`` and used for the agent's own assembled
    sections so every prompt in the assembled system prompt comes from the single
    run-frozen snapshot (REQ-123) — the same bytes the provenance event recorded.
    """

    def _resolve(package: str, name: str) -> str:
        try:
            return snapshot.get(package, name).body
        except KeyError as exc:
            raise PromptMissing(package, name) from exc

    return _resolve


__all__ = [
    "AuditEmit",
    "agent_prompt_resolve",
    "build_prompt_resolver",
    "read_agent_tier",
    "snapshot_resolver",
    "snapshot_run_prompts",
]
