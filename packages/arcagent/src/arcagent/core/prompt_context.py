"""Run-start prompt resolution + provenance wiring (editable-system-prompts COMP-006).

At agent setup the agent builds one :class:`~arcprompt.PromptResolver` — pinned to
the DEPLOYMENT OPERATOR's public key, because overlays are authored and signed by
the operator through arcui, not by the agent's own DID. At run start the agent
freezes a :class:`~arcprompt.PromptSnapshot` of the complete prompt set and emits
one provenance audit event enumerating every prompt with its source, digest, and
overlay signer (REQ-123, REQ-132). The snapshot then backs the run's
``ResolverPromptSource`` handed to arcrun, module prompt sections, and spawned
children, so an operator override takes effect on the next run and is attributable
to exact bytes — never silently, never per-turn-shifting.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from arcprompt import (
    PromptCatalog,
    PromptMissing,
    PromptResolver,
    PromptSnapshot,
    PromptSource,
    StockPromptSource,
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
        from arctrust import operator_public_key_for

        return operator_public_key_for()
    except (OSError, ValueError, RuntimeError):
        return None


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


class _TelemetryAuditSink:
    """Adapt the agent's ``telemetry.audit_event`` to arctrust's ``AuditSink.write``."""

    def __init__(self, audit_event: AuditEmit) -> None:
        self._audit_event = audit_event

    def write(self, event: AuditEvent) -> None:
        self._audit_event(
            event.action,
            {
                "tier": event.tier,
                "request_id": event.request_id,
                "prompts": event.extra.get("prompts", []),
            },
        )


#: Snapshot key for operator-signed workspace documents (no catalog package owns them).
WORKSPACE_PACKAGE = "workspace"
IDENTITY_DOC = "identity"
PINNED_POLICY_DOC = "policy_pinned"

#: The workspace documents that are control-plane text: signed by the operator,
#: verified at run start, never trusted from a raw file read (J2 F2). The learned
#: ``policy.md`` is deliberately absent — it is the agent's own curated state.
SIGNED_WORKSPACE_DOCS: dict[str, str] = {
    IDENTITY_DOC: "identity.md",
    PINNED_POLICY_DOC: "policy_pinned.md",
}


def signed_workspace_files(workspace: Path) -> dict[tuple[str, str], Path]:
    """The ``(package, name) -> path`` map of signed workspace documents for ``workspace``."""
    return {
        (WORKSPACE_PACKAGE, name): workspace / fname
        for name, fname in SIGNED_WORKSPACE_DOCS.items()
    }


def signed_workspace_text(prompt_source: PromptSource | None, workspace: Path, name: str) -> str:
    """The verified text of a signed workspace document, or ``""`` when it does not exist.

    In a run, ``prompt_source`` is the frozen snapshot, so the text was verified at
    run start and cannot shift under a mid-run on-disk edit. With no source — or the
    stock source, which a bare/test agent without a resolver gets — there is no
    operator key to verify against, so the file is read as-is; every deployed agent
    builds a resolver in capability setup and never takes that path.
    """
    if prompt_source is None or isinstance(prompt_source, StockPromptSource):
        path = workspace / SIGNED_WORKSPACE_DOCS[name]
        return path.read_text(encoding="utf-8").strip() if path.is_file() else ""
    try:
        return prompt_source.resolve(WORKSPACE_PACKAGE, name).strip()
    except PromptMissing:
        return ""


def snapshot_run_prompts(
    resolver: PromptResolver,
    *,
    actor_did: str,
    audit_event: AuditEmit,
    request_id: str | None = None,
    workspace: Path | None = None,
) -> PromptSnapshot:
    """Freeze the complete prompt set for a run and emit one provenance event.

    With ``workspace`` the operator-signed documents (identity, pinned policy) are
    verified and frozen too: a tampered or unsigned one raises here, before the run.
    """
    refs = PromptCatalog().catalog()
    return _build_snapshot(
        resolver,
        refs,
        actor_did=actor_did,
        sink=_TelemetryAuditSink(audit_event),
        request_id=request_id,
        signed_files=signed_workspace_files(workspace) if workspace is not None else None,
    )


__all__ = [
    "IDENTITY_DOC",
    "PINNED_POLICY_DOC",
    "SIGNED_WORKSPACE_DOCS",
    "WORKSPACE_PACKAGE",
    "AuditEmit",
    "build_prompt_resolver",
    "signed_workspace_files",
    "signed_workspace_text",
    "snapshot_run_prompts",
]
