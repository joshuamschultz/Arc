"""Trust mutation seam for reviewed capability-import artifacts.

The import lifecycle lives in an agent module, while signing and revocation
remain capability trust concerns. Keeping this adapter in the capabilities
package prevents an agent module from reaching into the signing implementation
directly and keeps the operator-only seam explicit.
"""

from __future__ import annotations

from pathlib import Path

from arctrust import AuditSink

from arcagent.capabilities.capability_signing import revoke


def revoke_import_artifact(
    artifact: Path,
    *,
    config_path: Path,
    operator_did: str,
    audit_sink: AuditSink | None = None,
) -> None:
    """Withdraw trust from one artifact through the canonical signer seam."""
    revoke(
        artifact,
        config_path=config_path,
        operator_did=operator_did,
        audit_sink=audit_sink,
    )


__all__ = ["revoke_import_artifact"]
