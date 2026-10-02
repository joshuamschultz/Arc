"""The deployment's schedule and pulse authority, built once for every entry point.

``arc agent run``, ``arc ui start``, ``python -m arcagent`` and ``arcui.serve()``
all hand agents the same binding, so a schedule written by one surface verifies
under another. Each entry point supplies only how it resolves the operator
signer; the tier policy lives here and nowhere else.
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Callable
from typing import Any

import arctrust
import arctrust.policy
from arctrust import AuditSink, Signer

from arcagent.core.control_contract import ControlArtifactBinding

_logger = logging.getLogger("arcagent.control_binding")


def build_control_artifact_authority(
    security: Any,
    operator_signer: Callable[[], Signer],
    *,
    audit_sink: AuditSink | None = None,
) -> ControlArtifactBinding | None:
    """Return the deployment's schedule and pulse authority, or None (fail closed).

    Personal and enterprise get the zero-config operator-signed local journal
    (:class:`arctrust.LocalControlArtifactAuthority` under
    :func:`arctrust.control_artifact_journal_dir`) and a local run-trigger issuer,
    both signing through the operator capability, never raw key material.
    Federal floors to an externally custodied authority; none is composed here,
    so federal answers None and every schedule write and firing stays closed.

    The tenant is derived from the operator public key: stable across restarts,
    and bound to the same custody root that signs the journals.
    """
    try:
        if security.tier == "federal":
            _logger.warning("federal tier has no local schedule authority; schedules fail closed")
            return None
        signer = operator_signer()
    except Exception as exc:  # reason: fail closed, no operator signer means no authority
        _logger.warning("schedule authority unavailable: %s", type(exc).__name__)
        return None
    authority = arctrust.LocalControlArtifactAuthority(
        arctrust.control_artifact_journal_dir(),
        signer=signer,
        operator_did=arctrust.policy.OperatorApprovalAuthority(signer).did,
        audit_sink=audit_sink,
    )
    return ControlArtifactBinding(
        authority=authority,
        tenant_id="arc-" + hashlib.sha256(bytes(signer.public_key)).hexdigest()[:32],
        trigger_issuer=arctrust.LocalRunTriggerIssuer(signer),
        operator_proof=authority.operator_proof,
    )


__all__ = ["build_control_artifact_authority"]
