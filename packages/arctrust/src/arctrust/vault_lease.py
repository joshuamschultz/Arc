"""Short-lived Vault capability leases bound to signed deployment grants.

Credential acquisition is an injected trusted deployment contract. This module
does not invent a host identity mechanism or expose the resulting token.
"""

from __future__ import annotations

import hashlib
import ssl
import time
from collections.abc import Callable
from enum import StrEnum
from typing import Any, Protocol, Self

import httpx
from pydantic import BaseModel, ConfigDict, Field, model_validator

from arctrust.authority_config import DeploymentAuthorityConfig
from arctrust.canonical import canonical_json
from arctrust.signer import ED25519, verify_signature

_DOMAIN = b"arc:deployment-capability-grant:v1\0"


class VaultLeaseError(RuntimeError):
    """A scoped Vault lease cannot be authenticated or continued."""


class Capability(StrEnum):
    ISSUER = "issuer"
    CIPHER = "cipher"
    ANCHOR = "anchor"
    AUDIT_SIGNER = "audit-signer"


class CapabilityGrant(BaseModel):
    """Externally signed grant; caller cannot supply a Vault policy name."""

    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    deployment_id: str = Field(pattern=r"^[a-z][a-z0-9-]{1,31}$")
    tenant_id: str = Field(pattern=r"^[a-z][a-z0-9-]{1,31}$")
    config_revision: int = Field(ge=1)
    config_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    capability: Capability
    subject: str = Field(min_length=1, max_length=128)
    expires_at: int = Field(gt=0)

    @model_validator(mode="after")
    def validate_subject(self) -> Self:
        if any(ord(char) < 33 or ord(char) > 126 for char in self.subject):
            raise ValueError("invalid grant subject")
        return self

    @property
    def policy_name(self) -> str:
        return f"arc-{self.tenant_id}-{self.deployment_id}-{self.capability.value}"

    def canonical_bytes(self) -> bytes:
        return canonical_json(self.model_dump(mode="json"))


def sign_capability_grant(
    grant: CapabilityGrant, sign: Callable[[bytes], bytes]
) -> dict[str, Any]:
    signature = sign(_DOMAIN + grant.canonical_bytes())
    if len(signature) != 64:
        raise VaultLeaseError("capability grant signature is invalid")
    return {"grant": grant.model_dump(mode="json"), "signature": signature.hex()}


class VaultCredentialProvider(Protocol):
    """Trusted host identity adapter; installs a token into an owned TLS client.

    The adapter must obtain a short-lived token for the exact derived policy.
    It never returns the token to the authority API. Production enrollment of
    this adapter is deliberately outside this leaf implementation.
    """

    def authorize(
        self, client: httpx.Client, *, subject: str, policy_name: str,
        capability: Capability,
    ) -> None: ...


class VaultLease:
    """Internal lifetime owner of one authenticated, renewable Vault client."""

    def __init__(self, client: _LeaseClient, grant: CapabilityGrant):
        self._client = client
        self._grant = grant

    def _adapter_client(self) -> httpx.Client:
        """Only the arctrust authority factory passes this to Vault adapters."""
        return self._client

    def close(self) -> None:
        self._client.revoke_and_close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


class _LeaseClient(httpx.Client):
    def __init__(self, *, grant: CapabilityGrant, **kwargs: Any):
        super().__init__(**kwargs)
        self._grant = grant
        self._lease_deadline = 0.0
        self._revoked = False

    def establish(self) -> None:
        try:
            response = super().request("GET", "/v1/auth/token/lookup-self", timeout=5,
                                       follow_redirects=False)
            response.raise_for_status()
            data = response.json()["data"]
            if data["policies"] != [self._grant.policy_name] or data["renewable"] is not True:
                raise ValueError("unexpected Vault policy or nonrenewable token")
            ttl = data["ttl"]
            if type(ttl) is not int or ttl < 1 or ttl > 3600:
                raise ValueError("unsafe Vault token TTL")
            self._lease_deadline = time.monotonic() + ttl
        except (httpx.HTTPError, KeyError, TypeError, ValueError) as exc:
            raise VaultLeaseError("Vault lease scope or lifetime refused") from exc

    def request(self, method: str, url: httpx._types.URLTypes, **kwargs: Any) -> httpx.Response:
        if self._revoked or time.time() >= self._grant.expires_at:
            raise VaultLeaseError("Vault capability grant expired or revoked")
        if time.monotonic() + 5 >= self._lease_deadline:
            self._renew()
        return super().request(method, url, **kwargs)

    def _renew(self) -> None:
        try:
            response = super().request(
                "POST", "/v1/auth/token/renew-self", json={"increment": "30s"},
                timeout=5, follow_redirects=False,
            )
            response.raise_for_status()
            auth = response.json()["auth"]
            ttl = auth["lease_duration"]
            if (auth["renewable"] is not True or type(ttl) is not int
                    or ttl < 1 or ttl > 3600):
                raise ValueError("unsafe renewed Vault lease")
            self._lease_deadline = time.monotonic() + ttl
            self.establish()
        except (httpx.HTTPError, KeyError, TypeError, ValueError) as exc:
            raise VaultLeaseError("Vault lease renewal refused") from exc

    def revoke_and_close(self) -> None:
        if self._revoked:
            return
        self._revoked = True
        try:
            super().request("POST", "/v1/auth/token/revoke-self", timeout=5,
                            follow_redirects=False)
        except httpx.HTTPError:
            pass
        finally:
            self.close()


def open_vault_lease(
    config: DeploymentAuthorityConfig, signed_grant: dict[str, Any], *,
    trusted_grant_key: bytes, credential_provider: VaultCredentialProvider,
    ca_pem: bytes,
) -> VaultLease:
    """Authenticate a scoped grant, then verify actual Vault token policy/TTL."""
    try:
        if set(signed_grant) != {"grant", "signature"} or len(trusted_grant_key) != 32:
            raise ValueError("invalid grant envelope")
        grant = CapabilityGrant.model_validate_json(canonical_json(signed_grant["grant"]))
        signature = bytes.fromhex(signed_grant["signature"])
        if not verify_signature(ED25519, _DOMAIN + grant.canonical_bytes(),
                                signature, trusted_grant_key):
            raise ValueError("grant signature refused")
        if (grant.deployment_id != config.deployment_id or grant.tenant_id != config.tenant_id
                or grant.config_revision != config.revision or grant.config_digest != config.digest
                or time.time() >= grant.expires_at):
            raise ValueError("grant deployment, revision, or lifetime mismatch")
        if hashlib.sha256(ca_pem).hexdigest() != config.vault_ca_sha256:
            raise ValueError("Vault CA pin mismatch")
        context = ssl.create_default_context(cadata=ca_pem.decode("ascii"))
        client = _LeaseClient(grant=grant, base_url=config.vault_url, verify=context)
        try:
            credential_provider.authorize(client, subject=grant.subject,
                                          policy_name=grant.policy_name,
                                          capability=grant.capability)
            client.establish()
        except BaseException:
            client.close()
            raise
        return VaultLease(client, grant)
    except (ValueError, TypeError, KeyError, UnicodeError, ssl.SSLError, httpx.HTTPError,
            AttributeError) as exc:
        raise VaultLeaseError("Vault capability grant unavailable or invalid") from exc
