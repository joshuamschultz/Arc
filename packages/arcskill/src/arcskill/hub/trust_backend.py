"""SPEC-033 #4/C1 — Sigstore hub backend behind the loader's TrustBackend seam.

The capability loader (arcagent) depends only on a structural ``TrustBackend``
Protocol — ``verify(artifact, content, *, trusted_public_key=None) -> bool`` —
so the verification mechanism is swappable per source. The Ed25519/DID backend
governs self-authored artifacts; this backend governs hub-sourced skills
installed into the global root, re-running Sigstore/Rekor verification at LOAD
via :func:`verify_artifact_at_load` (install-time and load-time are separate
trust boundaries — a signed bundle can be tampered on disk in between).

arcskill owns Sigstore, so the adapter lives here and satisfies arcagent's
Protocol structurally (no import cycle). A deployment wires an instance into
``CapabilityLoader(trust_backend=...)`` for the hub/global root.

TODO(SPEC-033): the loader currently holds a single ``trust_backend`` for all
untrusted roots; per-root backend selection (Ed25519 for agent/workspace,
this Sigstore backend for the hub-sourced global root) is the remaining wire.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

from arcskill.hub.config import HubConfig, SkillSource
from arcskill.hub.verify import verify_artifact_at_load

if TYPE_CHECKING:
    from arctrust import AuditSink

_logger = logging.getLogger("arcskill.hub.trust_backend")


class HubTrustBackend:
    """Load-time Sigstore verification for hub skills, behind the TrustBackend seam.

    ``source_resolver`` maps an on-disk artifact to the :class:`SkillSource` it
    was installed from (identity/issuer policy). ``config`` is the tier-aware
    :class:`HubConfig`. Fail-closed: any verification error (tampered bundle,
    Sigstore unavailable at federal, revoked) returns ``False``.
    """

    def __init__(
        self,
        config: HubConfig,
        source_resolver: Callable[[Path], SkillSource],
        *,
        audit_sink: AuditSink | None = None,
    ) -> None:
        self._config = config
        self._resolve_source = source_resolver
        self._audit_sink = audit_sink

    def verify(
        self, artifact: Path, content: bytes, *, trusted_public_key: bytes | None = None
    ) -> bool:
        """Re-verify ``artifact`` against its Sigstore bundle at load. Fail-closed.

        ``content`` and ``trusted_public_key`` are part of the shared Protocol
        contract but unused here: Sigstore recomputes the content hash from the
        bytes on disk and pins to Fulcio/OIDC identity, not an Ed25519 key.
        """
        try:
            source = self._resolve_source(artifact)
            result = verify_artifact_at_load(
                artifact, source, self._config, audit_sink=self._audit_sink
            )
        except Exception:  # reason: fail-closed — any verify error denies the load
            _logger.warning("hub load-time verification failed for %s; denying", artifact)
            return False
        # A policy-allowed skip (sigstore absent at personal/enterprise) loads;
        # federal never skips — it raises above and is denied. Revoked always denies.
        return (result.signature_valid or result.skipped) and not result.revoked


__all__ = ["HubTrustBackend"]
