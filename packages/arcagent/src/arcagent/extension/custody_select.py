"""P18-2 — which cipher seals a deployment's connector credentials, and the custody it opens.

One custody code path at every tier (ADR-019); tier and custody choose only the
cipher:

* ``in_process`` custody (personal, and enterprise when chosen): the operator seed
  is in this process, so :class:`~arctrust.ConnectorSecretCipher` derives the key.
* ``vault_transit`` custody (enterprise default, federal mandatory): the custody
  key never leaves the transit. :class:`~arctrust.TransitConnectorCipher` seals
  every value by reference (AES-256-GCM, FIPS-approved) through the deployment's
  transit (P18-2F). A transit that cannot serve is a refusal
  (``SECRET_STORE_VAULT_REQUIRED``), never a fall back to anything on the host.

The operator key and the transit are resolved through the one arctrust resolver
(:func:`arctrust.operator_key_for`, :func:`arctrust.operator_transit_for`);
nothing here re-derives a key location.
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
    TransitConnectorCipher,
    machine_security,
    operator_key_for,
)
from arctrust.audit import AuditSink
from arctrust.operator_resolver import (
    operator_key_file,
    operator_transit_for,
    prior_in_process_operator_key,
)
from arctrust.signer import IN_PROCESS, VAULT_TRANSIT, SignerError
from arctrust.transit_cipher import TransitCipher

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
    *,
    custody: str,
    operator_key: OperatorKey | None,
    transit: TransitCipher | None = None,
    require_fips: bool = False,
) -> CredentialCipher:
    """The row cipher for this custody: the ONE place it is chosen.

    ``in_process`` with the operator seed here gives the in-process cipher;
    ``vault_transit`` with a transit gives the Transit cipher. Nothing else, and
    never the in-process cipher for a ``vault_transit`` deployment.

    Raises:
        ExtensionError: ``SECRET_STORE_VAULT_REQUIRED`` when the custody's cipher
            cannot be built (no seed in process, or no servable transit).
        ArcTrustFipsError: ``require_fips`` and the cipher or backend is not approved.
    """
    if custody == IN_PROCESS and operator_key is not None:
        return ConnectorSecretCipher.for_operator_key(operator_key, require_fips=require_fips)
    if custody == VAULT_TRANSIT and transit is not None:
        return TransitConnectorCipher(transit, require_fips=require_fips)
    needs = (
        "the operator key in this process"
        if custody == IN_PROCESS
        else "the deployment's Vault Transit, which cannot serve right now"
    )
    raise ExtensionError(
        code=VAULT_REQUIRED,
        message=f"connector credentials under custody={custody} need {needs}",
        details={"custody": custody},
    )


def deployment_cipher(arc_dir: Path, *, tier: Tier) -> CredentialCipher:
    """The cipher an operator surface (CLI, arcui, TUI) seals with for this deployment.

    A personal deployment with no operator key yet mints one, the same rule the
    bundle signer follows; above personal a missing key is a refusal.
    """
    security = machine_security(arc_dir)
    if security.custody == VAULT_TRANSIT:
        return connector_cipher(
            custody=VAULT_TRANSIT, operator_key=None, transit=_transit(security, arc_dir)
        )
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


def reseal_source_cipher(arc_dir: Path) -> CredentialCipher:
    """The in-process cipher that sealed rows BEFORE this deployment moved to transit.

    Only the explicit, operator-run ``arc connector migrate-secrets --reseal``
    asks for this: it reads the old on-disk operator key once, read-only, never
    minting one. A long-running process never holds it.

    Raises:
        ExtensionError: ``RESEAL_NOT_APPLICABLE`` when the deployment is not on
            ``vault_transit``; ``RESEAL_SOURCE_KEY_MISSING`` when the old key file
            is gone (those rows can only be reconnected).
    """
    security = machine_security(arc_dir)
    if security.custody != VAULT_TRANSIT:
        raise ExtensionError(
            code="RESEAL_NOT_APPLICABLE",
            message=(
                "re-sealing moves credentials into the vault; set [security] "
                'custody = "vault_transit" first'
            ),
            details={"custody": security.custody},
        )
    path = operator_key_file(security, base=arc_dir)
    try:
        key = prior_in_process_operator_key(security, base=arc_dir)
    except (OSError, OperatorKeyIntegrityError) as exc:
        raise ExtensionError(
            code="RESEAL_SOURCE_KEY_MISSING",
            message=(
                f"the old operator key at {path} is not readable ({type(exc).__name__}); rows "
                "sealed under it cannot be moved and must be connected again"
            ),
            details={"path": str(path)},
        ) from exc
    return ConnectorSecretCipher.for_operator_key(key)


def _transit(security: Any, arc_dir: Path) -> TransitCipher | None:
    """The deployment's transit, or ``None`` (a refusal) when it cannot serve."""
    try:
        return operator_transit_for(security, base=arc_dir)
    except SignerError:
        return None


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
    "reseal_source_cipher",
]
