"""Account authority composition over authenticated, scoped Vault capabilities.

The trusted deployment supplies config/grant verification keys, a config head,
host credential provider, actor verifier, and strict audit sink. No identity or
local-key fallback is constructed here.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Protocol

from arctrust.audit import AuditSink, DurableAuditSink
from arctrust.authority_config import DeploymentAuthorityConfig, load_authority_config
from arctrust.callback_allowlist import CallbackAllowlistStore
from arctrust.identity import parse_did
from arctrust.monotonic import BootstrapAuthority, MonotonicAnchor
from arctrust.principals import PrincipalStore, Tier
from arctrust.transit_http import VaultTransitHTTP
from arctrust.users import UserStore
from arctrust.vault_anchor import VaultKVAnchor
from arctrust.vault_cipher import VaultCipher
from arctrust.vault_lease import (
    Capability,
    VaultCredentialProvider,
    VaultLease,
    open_vault_lease,
)


class AccountAuthorityError(RuntimeError):
    """Account authority cannot validate its caller or dependencies."""


class AccountActorVerifier(Protocol):
    """Deployment-supplied authentication and mutation authorization.

    A DID returned by this method is audit attribution. Its string value alone
    must never count as proof that the caller may mutate accounts.
    """

    def authenticate(self, proof: object, *, action: str) -> str: ...


class AccountAuthority:
    """Own fixed account adapters; bind every mutation store to a verified actor."""

    def __init__(
        self,
        config: DeploymentAuthorityConfig,
        leases: dict[Capability, VaultLease],
        *,
        audit_sink: AuditSink,
        strict_audit_sink: DurableAuditSink,
        actor_verifier: AccountActorVerifier,
        users_path: Path,
        bootstrap_authority: BootstrapAuthority | None = None,
    ) -> None:
        if set(leases) != {Capability.ISSUER, Capability.CIPHER, Capability.ANCHOR}:
            raise AccountAuthorityError("account capabilities are incomplete")
        for capability, lease in leases.items():
            grant = lease._grant
            if (
                grant.capability != capability
                or grant.deployment_id != config.deployment_id
                or grant.tenant_id != config.tenant_id
                or grant.config_digest != config.digest
            ):
                raise AccountAuthorityError("account capability scope mismatch")
        self._config = config
        self._leases = leases
        self._audit_sink = audit_sink
        self._strict_audit_sink = strict_audit_sink
        self._actor_verifier = actor_verifier
        self._users_path = users_path
        # Same private directory as the users cache: one authority, one file per record.
        self._principals_path = users_path.with_name("principals.json")
        self._callbacks_path = users_path.with_name("callbacks.json")
        self._issuer = VaultTransitHTTP(
            leases[Capability.ISSUER]._adapter_client(), mount=config.transit_mount
        )
        self._anchor, self._cipher = self._sealed_record("users", bootstrap_authority)
        self._principal_anchor, self._principal_cipher = self._sealed_record(
            "principals", bootstrap_authority
        )
        self._callback_anchor, self._callback_cipher = self._sealed_record(
            "callbacks", bootstrap_authority
        )

    def _sealed_record(
        self, record: str, bootstrap_authority: BootstrapAuthority | None
    ) -> tuple[VaultKVAnchor, VaultCipher]:
        """One authority record: its own anchor head and a cipher bound to that head's scope."""
        anchor = VaultKVAnchor(
            self._leases[Capability.ANCHOR]._adapter_client(),
            mount=self._config.anchor_mount,
            record=record,
            bootstrap_authority=bootstrap_authority,
        )
        cipher = VaultCipher(
            self._leases[Capability.CIPHER]._adapter_client(),
            mount=self._config.transit_mount,
            key="account-seal",
            scope=anchor.scope,
            record_id=record,
        )
        return anchor, cipher

    def _actor_did(self, proof: object, *, action: str) -> str:
        try:
            did = self._actor_verifier.authenticate(proof, action=action)
            parsed = parse_did(did)
            if parsed["org"] != self._config.tenant_id or not parsed["hash"]:
                raise ValueError("actor tenant mismatch")
            return did
        except (PermissionError, ValueError, TypeError, AttributeError) as exc:
            raise AccountAuthorityError("account actor is not authenticated") from exc

    def user_store(self, actor_proof: object) -> UserStore:
        """Bind a store to an independently authenticated actor and strict audit."""
        did = self._actor_did(actor_proof, action="users.mutate")
        return UserStore(
            self._users_path,
            issuer=self._issuer,
            anchor=self._anchor,
            cipher=self._cipher,
            audit_sink=self._audit_sink,
            strict_audit_sink=self._strict_audit_sink,
            actor_did=did,
        )

    def principal_store(self, actor_proof: object) -> PrincipalStore:
        """Bind the app-principal store to an authenticated actor and strict audit."""
        did = self._actor_did(actor_proof, action="principals.mutate")
        return PrincipalStore(
            self._principals_path,
            anchor=self._principal_anchor,
            cipher=self._principal_cipher,
            audit_sink=self._audit_sink,
            strict_audit_sink=self._strict_audit_sink,
            actor_did=did,
        )

    def callback_allowlist_store(
        self, actor_proof: object, *, tier: Tier
    ) -> CallbackAllowlistStore:
        """Bind the callback-origin allowlist to an authenticated actor and strict audit.

        ``tier`` is the deployment tier: it decides whether an add is active at
        once (personal) or pending a second step (enterprise, federal).
        """
        did = self._actor_did(actor_proof, action="callbacks.mutate")
        return CallbackAllowlistStore(
            self._callbacks_path,
            anchor=self._callback_anchor,
            cipher=self._callback_cipher,
            audit_sink=self._audit_sink,
            strict_audit_sink=self._strict_audit_sink,
            actor_did=did,
            tier=tier,
        )

    def close(self) -> None:
        for lease in self._leases.values():
            lease.close()


def open_account_authority(
    config_path: Path,
    *,
    trusted_config_key: bytes,
    expected_deployment: str,
    expected_tenant: str,
    config_anchor: MonotonicAnchor,
    signed_grants: dict[Capability, dict[str, Any]],
    trusted_grant_key: bytes,
    credential_provider: VaultCredentialProvider,
    ca_pem: bytes,
    audit_sink: AuditSink,
    strict_audit_sink: DurableAuditSink,
    actor_verifier: AccountActorVerifier,
    users_path: Path,
    bootstrap_authority: BootstrapAuthority | None = None,
) -> AccountAuthority:
    """Open only the three account capabilities after validating config first."""
    config = load_authority_config(
        config_path,
        trusted_public_key=trusted_config_key,
        expected_deployment=expected_deployment,
        expected_tenant=expected_tenant,
        anchor=config_anchor,
    )
    required = {Capability.ISSUER, Capability.CIPHER, Capability.ANCHOR}
    if set(signed_grants) != required:
        raise AccountAuthorityError("account capability grants are incomplete")
    leases: dict[Capability, VaultLease] = {}
    try:
        for capability in sorted(required):
            lease = open_vault_lease(
                config,
                signed_grants[capability],
                trusted_grant_key=trusted_grant_key,
                credential_provider=credential_provider,
                ca_pem=ca_pem,
            )
            if lease._grant.capability != capability:
                lease.close()
                raise AccountAuthorityError("account capability grant mismatch")
            leases[capability] = lease
        return AccountAuthority(
            config,
            leases,
            audit_sink=audit_sink,
            strict_audit_sink=strict_audit_sink,
            actor_verifier=actor_verifier,
            users_path=users_path,
            bootstrap_authority=bootstrap_authority,
        )
    except BaseException:
        for lease in leases.values():
            lease.close()
        raise
