"""SPEC-062 COMP-009 — ``ArtifactPinVerifier``, the approved build and nothing else.

An extension that runs third-party code declares exactly one build in its manifest:
one version, and one sha256 per platform it publishes
(:class:`~arcagent.extension.manifest.ArtifactPin`). This module is what makes that
declaration mean something at run time.

Three design points carry the requirement (REQ-290):

* **The digest is resolved for THIS host, or there is no digest.** A sha256 is
  published beside one platform's asset and describes no other's bytes, so a
  verifier holding a single digest is correct on one machine and comparing
  unrelated bytes everywhere else. A host the pin does not name is refused.

* **Before each execution, not at install.** Install-time verification proves what was
  approved, not what is about to run — and the gap between those two is the whole of
  the post-install swap. So :meth:`ArtifactPinVerifier.verify` caches nothing and is
  called from the launch path (:mod:`arcagent.extension.launcher`), which cannot spawn
  a pinned artifact without it.
* **This module cannot fetch.** It imports nothing that reaches the network or spawns a
  process, so the one tempting repair for a mismatch — download the expected bytes and
  carry on — is unavailable by construction. That, together with the manifest's refusal
  of a floating version, is what makes a bare ``npx -y <package>`` invocation
  unrepresentable: there is no code here that could go and get the package.

A refusal is recorded before it is raised. The exception reaches the operator; only the
audit event reaches the auditor, so the event carries both the expected and the actual
value (NIST AU-3) rather than a bare "verification failed".
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import NoReturn

from arctrust.audit import AuditEvent, AuditSink, emit

from arcagent.core.errors import ExtensionError
from arcagent.core.tier import Tier
from arcagent.extension.manifest import ArtifactPin
from arcagent.extension.platforms import host_platform

#: Read the artifact in bounded chunks — an extension bundle is operator-supplied and
#: may be large; a whole-file read would put its size on the heap for no benefit.
_READ_CHUNK_BYTES = 1024 * 1024

_ACTION = "extension.artifact.verify"


@dataclass(frozen=True)
class PinnedArtifact:
    """A manifest's declared pin paired with the build actually found on disk.

    Attributes:
        pin: The ``[artifact]`` table the manifest declared.
        path: The single file the pin covers, already resolved on this host. Nothing
            in this module creates it — a path that does not exist is a refusal, never
            a download.
        installed_version: The version the resolved build reports for itself, which is
            checked against the pin separately from the hash.
    """

    pin: ArtifactPin
    path: Path
    installed_version: str


class ArtifactPinVerifier:
    """Confirms the artifact about to run is the exact build that was approved.

    Args:
        audit_sink: Any :class:`~arctrust.audit.AuditSink`; every verdict reaches it
            through the single :func:`~arctrust.audit.emit` chokepoint (CON-5).
        tier: Deployment stringency, stamped on each record. It selects no behaviour
            here: the pin is checked identically everywhere, because a build nobody
            approved is not more acceptable on a laptop.
    """

    def __init__(self, *, audit_sink: AuditSink, tier: Tier) -> None:
        self._sink = audit_sink
        self._tier = tier

    def verify(self, artifact: PinnedArtifact, *, caller_did: str) -> None:
        """Check one artifact against its pin, recording the verdict either way.

        Args:
            artifact: The declared pin and the build resolved on this host.
            caller_did: The identity the run would happen under (Pillar 1).

        Raises:
            ExtensionError: The artifact is absent, is not a single file, reports a
                different version, or hashes differently than the pin declares. The
                refusal is emitted to the audit sink before it is raised.
        """
        pin = artifact.pin
        host = host_platform()
        covered = pin.for_host(host)
        if covered is None:
            self._refuse(
                artifact,
                caller_did=caller_did,
                reason="unpinned_platform",
                expected=f"a digest pinned for {host}",
                actual=f"pinned only for {', '.join(sorted(pin.platforms)) or 'nothing'}",
            )
        if not artifact.path.is_file():
            self._refuse(
                artifact,
                caller_did=caller_did,
                reason="artifact_missing",
                expected=str(artifact.path),
                actual="no readable file at that path",
            )
        if artifact.installed_version != pin.version:
            self._refuse(
                artifact,
                caller_did=caller_did,
                reason="version_mismatch",
                expected=pin.version,
                actual=artifact.installed_version,
            )
        digest = _sha256_of(artifact.path)
        if digest != covered.sha256:
            self._refuse(
                artifact,
                caller_did=caller_did,
                reason="hash_mismatch",
                expected=covered.sha256,
                actual=digest,
            )
        self._record(
            artifact,
            caller_did=caller_did,
            outcome="allow",
            extra={"sha256": digest, "version": artifact.installed_version},
        )

    def _refuse(
        self,
        artifact: PinnedArtifact,
        *,
        caller_did: str,
        reason: str,
        expected: str,
        actual: str,
    ) -> NoReturn:
        """Record the refusal, then raise it — in that order, so neither can be lost."""
        self._record(
            artifact,
            caller_did=caller_did,
            outcome="deny",
            extra={"reason": reason, "expected": expected, "actual": actual},
        )
        raise ExtensionError(
            code="ARTIFACT_PIN_MISMATCH",
            message=(
                f"artifact '{artifact.pin.package}' failed pin verification ({reason}): "
                f"expected {expected}, found {actual}"
            ),
            details={
                "package": artifact.pin.package,
                "reason": reason,
                "expected": expected,
                "actual": actual,
            },
        )

    def _record(
        self,
        artifact: PinnedArtifact,
        *,
        caller_did: str,
        outcome: str,
        extra: dict[str, str],
    ) -> None:
        """Hand one verdict to the single emission point."""
        pin = artifact.pin
        emit(
            AuditEvent(
                actor_did=caller_did,
                action=_ACTION,
                target=f"artifact:{pin.package}@{pin.version}",
                outcome=outcome,
                tier=self._tier.value,
                extra={"package": pin.package, "path": str(artifact.path), **extra},
            ),
            self._sink,
        )


def _sha256_of(path: Path) -> str:
    """The artifact's integrity hash, read in bounded chunks."""
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(_READ_CHUNK_BYTES):
            digest.update(chunk)
    return digest.hexdigest()


__all__ = ["ArtifactPinVerifier", "PinnedArtifact"]
