"""P18-2 — which cipher seals a deployment's connector credentials, and the custody it opens.

One custody code path at every tier (ADR-019); tier and custody choose only the
cipher:

* ``in_process`` custody (personal, and enterprise when chosen): the operator seed
  is in this process, so :class:`~arctrust.ConnectorSecretCipher` derives the key.
* ``vault_transit`` custody (enterprise default, federal mandatory): the key never
  leaves Vault. Until the Transit row cipher is wired (P18-2F) such a deployment
  REFUSES to store connector credentials, exactly as it refused before, rather
  than falling back to anything on the host.

The operator key is resolved through the one arctrust resolver
(:func:`arctrust.operator_key_for`); nothing here re-derives a key location.
"""

from __future__ import annotations

import os
import socket
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from arctrust import (
    ConnectorSecretCipher,
    OperatorKey,
    OperatorKeyIntegrityError,
    machine_security,
    operator_key_for,
)
from arctrust.audit import AuditSink
from arctrust.signer import IN_PROCESS

from arcagent.core.errors import ExtensionError
from arcagent.core.tier import Tier
from arcagent.extension.connection_health import HealthReporter
from arcagent.extension.credential_broker import AccessTokenBroker
from arcagent.extension.credentials import RENEWER_DID, RenewalPlanner
from arcagent.extension.custody import (
    CredentialCipher,
    CredentialRowStore,
    SealedCredentialBackend,
)
from arcagent.extension.grants import ConnectionRegistry
from arcagent.extension.oauth import post_form, refresh_access_token
from arcagent.extension.secrets import SecretStore
from arcagent.extension.state import ConnectionStateStore

VAULT_REQUIRED = "SECRET_STORE_VAULT_REQUIRED"


def connector_cipher(
    *, custody: str, operator_key: OperatorKey | None, require_fips: bool = False
) -> CredentialCipher:
    """The row cipher for this custody, or a refusal naming what to configure.

    Raises:
        ExtensionError: ``SECRET_STORE_VAULT_REQUIRED`` when the seed is not in
            this process (``vault_transit``) and no Transit row cipher exists yet.
    """
    if custody == IN_PROCESS and operator_key is not None:
        return ConnectorSecretCipher.for_operator_key(operator_key, require_fips=require_fips)
    raise ExtensionError(
        code=VAULT_REQUIRED,
        message=(
            "this deployment keeps its operator key in a vault (custody=vault_transit), and "
            "connector credentials need the Vault Transit row cipher, which is not configured"
        ),
        details={"custody": custody},
    )


def deployment_cipher(arc_dir: Path, *, tier: Tier) -> CredentialCipher:
    """The cipher an operator surface (CLI, arcui, TUI) seals with for this deployment.

    A personal deployment with no operator key yet mints one, the same rule the
    bundle signer follows; above personal a missing key is a refusal.
    """
    security = machine_security(arc_dir)
    try:
        key = operator_key_for(security, base=arc_dir, bootstrap=tier is Tier.PERSONAL)
    except (OSError, OperatorKeyIntegrityError) as exc:
        raise ExtensionError(
            code="SECRET_STORE_UNCONFIGURED",
            message=(
                "this deployment has no usable operator key to seal connector credentials "
                f"with ({type(exc).__name__})"
            ),
            details={"tier": tier.value},
        ) from exc
    return connector_cipher(custody=security.custody, operator_key=key)


def owner_id() -> str:
    """A lease owner unique to this process."""
    return f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"


@dataclass(frozen=True)
class Custody:
    """One process's view of connector custody: rows, the field store, the renewer."""

    rows: CredentialRowStore
    store: SecretStore
    planner: RenewalPlanner
    health: HealthReporter
    sink: AuditSink | None

    def broker(
        self,
        *,
        registry: Callable[[], ConnectionRegistry],
        bound_agent: str | None = None,
        bound_did: str | None = None,
    ) -> AccessTokenBroker:
        return AccessTokenBroker(
            self.rows,
            registry=registry,
            renewals=self.planner,
            health=self.health,
            sink=self.sink,
            bound_agent=bound_agent,
            bound_did=bound_did,
        )


def open_custody(
    backend: Any,
    cipher: CredentialCipher,
    *,
    health: HealthReporter,
    sink: AuditSink | None = None,
    actor_did: str = RENEWER_DID,
) -> Custody:
    """Compose custody over an open arcstore backend."""
    rows = CredentialRowStore(backend, cipher, sink=sink)
    planner = RenewalPlanner(
        rows=rows,
        refresh=lambda request: refresh_access_token(request, post=post_form),
        health=health,
        owner_id=owner_id(),
        state=ConnectionStateStore(backend, sink=sink),
        sink=sink,
        actor_did=actor_did,
    )
    store = SecretStore(SealedCredentialBackend(rows), sink=sink)
    return Custody(rows=rows, store=store, planner=planner, health=health, sink=sink)


__all__ = [
    "VAULT_REQUIRED",
    "Custody",
    "connector_cipher",
    "deployment_cipher",
    "open_custody",
    "owner_id",
]
