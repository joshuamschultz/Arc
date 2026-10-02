"""Bind a live agent identity to externally anchored skill revisions."""

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path
from threading import Lock
from typing import Protocol

from arctrust import (
    LOCAL_ANCHOR_CUSTODY,
    AuditEvent,
    AuditSink,
    FileJournalAnchor,
    MonotonicAnchor,
    Signer,
    emit,
    skill_revision_anchor_dir,
)
from arctrust.policy import OperatorApprovalAuthority

from arcagent.modules.capability_import.revisions import (
    AnchoredSkillRevisionResolver,
    OperatorSkillRevisionWriter,
    skill_revision_scope,
)

_logger = logging.getLogger("arcagent.skill_revisions")


class _AnchorPosture(Protocol):
    """The two ``[security]`` fields that choose the revision authority."""

    @property
    def tier(self) -> str: ...

    @property
    def skill_revision_anchor(self) -> str: ...


def build_skill_revision_anchor_factory(
    security: _AnchorPosture,
    operator_signer: Callable[[], Signer],
    *,
    audit_sink: AuditSink | None = None,
) -> Callable[[str, str], MonotonicAnchor] | None:
    """Return the deployment's skill revision authority, or None (fail closed).

    ``file`` (personal/enterprise default) is the zero-config operator-signed
    local journal under :func:`arctrust.skill_revision_anchor_dir`, signed
    through the operator signing capability — never raw key material. Its
    first use is audited ``warn``: the head is custodied locally. Federal
    floors to an external anchor; no external client is composed here, so it
    answers None and every anchored skill route stays closed.
    """
    if security.skill_revision_anchor != "file":
        _logger.warning(
            "skill revision anchor %r has no configured client on this deployment; "
            "anchored skill revisions fail closed",
            security.skill_revision_anchor,
        )
        return None
    try:
        signer = operator_signer()
    except Exception as exc:  # reason: fail closed — no operator signer, no authority
        _logger.warning("skill revision authority unavailable: %s", type(exc).__name__)
        return None
    directory = skill_revision_anchor_dir()
    operator_did = OperatorApprovalAuthority(signer).did
    warned = Lock()
    pending = [True]

    def factory(agent_did: str, skill_name: str) -> MonotonicAnchor:
        if audit_sink is not None:
            with warned:
                first, pending[0] = pending[0], False
            if first:
                emit(
                    AuditEvent(
                        actor_did=operator_did,
                        action="revision_anchor.local",
                        target=str(directory),
                        outcome="warn",
                        tier=security.tier,
                        extra={"custody": LOCAL_ANCHOR_CUSTODY},
                    ),
                    audit_sink,
                )
        return FileJournalAnchor(
            directory,
            scope=skill_revision_scope(agent_did, skill_name),
            signer=signer,
            audit_sink=audit_sink,
            actor_did=operator_did,
        )

    return factory


class LiveSkillRevisionResolver:
    """Resolve skill bytes with the DID established by the running agent."""

    def __init__(
        self,
        *,
        agent_did: Callable[[], str],
        config_path: Path,
        anchor_factory: Callable[[str, str], MonotonicAnchor],
    ) -> None:
        self._agent_did = agent_did
        self._config_path = config_path
        self._anchor_factory = anchor_factory
        self._lock = Lock()
        self._bound: AnchoredSkillRevisionResolver | None = None
        self._bound_did: str | None = None

    def _resolver(self) -> AnchoredSkillRevisionResolver:
        did = self._agent_did()
        if not did.startswith("did:"):
            raise RuntimeError("agent identity is unavailable for skill revisions")
        with self._lock:
            if self._bound_did is not None and self._bound_did != did:
                raise RuntimeError("agent identity changed after skill authority binding")
            if self._bound is None:
                self._bound = AnchoredSkillRevisionResolver(
                    agent_did=did,
                    config_path=self._config_path,
                    anchor_factory=self._anchor_factory,
                )
                self._bound_did = did
            return self._bound

    def resolve(self, folder: Path, scan_root: str) -> Path | None:
        """Return the anchored, verified skill body (None while unenrolled)."""
        return self._resolver().resolve(folder, scan_root)

    def read_current(self, folder: Path, path: Path) -> str | None:
        """Verify the current head again before exposing its body."""
        return self._resolver().read_current(folder, path)

    def active_folder(self, folder: Path) -> Path | None:
        """The verified active revision folder (None while unenrolled)."""
        return self._resolver().active_folder(folder)

    def read_verified_file(self, folder: Path, relpath: str) -> bytes:
        """One bundle file of the active revision, verified against its signed manifest."""
        return self._resolver().read_verified_file(folder, relpath)

    def authority(self) -> AnchoredSkillRevisionResolver:
        """The anchored revision authority bound to the running agent's DID."""
        return self._resolver()


def build_operator_skill_writer(
    resolver: object,
    operator_signer: Signer | None,
    folder_of: Callable[[str], Path | None],
) -> OperatorSkillRevisionWriter | None:
    """The agent's one skill write path, or None when it cannot be operator-anchored.

    Needs both an anchored revision authority (``resolver``: the agent's
    :class:`LiveSkillRevisionResolver` or an :class:`AnchoredSkillRevisionResolver`)
    and the operator signer. Either missing → None, and every automated skill
    write (the improver) refuses instead of falling back to another signer.
    """
    if operator_signer is None:
        return None
    if isinstance(resolver, LiveSkillRevisionResolver):
        authority: Callable[[], AnchoredSkillRevisionResolver] = resolver.authority
    elif isinstance(resolver, AnchoredSkillRevisionResolver):
        bound = resolver

        def authority() -> AnchoredSkillRevisionResolver:
            return bound
    else:
        return None
    return OperatorSkillRevisionWriter(
        authority=authority,
        signer=operator_signer,
        operator_did=OperatorApprovalAuthority(operator_signer).did,
        folder_of=folder_of,
    )


__all__ = [
    "LiveSkillRevisionResolver",
    "build_operator_skill_writer",
    "build_skill_revision_anchor_factory",
]
