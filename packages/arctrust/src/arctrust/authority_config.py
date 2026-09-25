"""Signed, independently anchored deployment authority configuration.

The file is a control-plane artifact, not a trust root. The deployment and
tenant expectation, verification key, and monotonic anchor come from separate
trusted deployment inputs. A valid signature alone cannot authorize replay.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
from collections.abc import Callable
from pathlib import Path
from typing import Any, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from arctrust.canonical import canonical_json
from arctrust.monotonic import MonotonicAnchor
from arctrust.signer import ED25519, verify_signature

_DOMAIN = b"arc:deployment-authority-config:v1\0"
_MAX_FILE = 16_384


class AuthorityConfigError(RuntimeError):
    """Deployment authority config cannot be authenticated at current revision."""


class DeploymentAuthorityConfig(BaseModel):
    """Secret-free configuration; mount names are derived from authenticated IDs."""

    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    deployment_id: str = Field(pattern=r"^[a-z][a-z0-9-]{1,31}$")
    tenant_id: str = Field(pattern=r"^[a-z][a-z0-9-]{1,31}$")
    revision: int = Field(ge=1)
    vault_url: str
    vault_ca_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_url(self) -> Self:
        from urllib.parse import urlsplit

        parsed = urlsplit(self.vault_url)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("Vault URL must be credential-free HTTPS origin")
        return self

    @property
    def namespace(self) -> str:
        return f"arc-{self.tenant_id}-{self.deployment_id}"

    @property
    def transit_mount(self) -> str:
        return f"{self.namespace}-transit"

    @property
    def anchor_mount(self) -> str:
        return f"{self.namespace}-anchor"

    @property
    def config_anchor_scope(self) -> str:
        return f"{self.anchor_mount}/config"

    def canonical_bytes(self) -> bytes:
        return canonical_json(self.model_dump(mode="json"))

    @property
    def digest(self) -> str:
        return hashlib.sha256(self.canonical_bytes()).hexdigest()


def sign_authority_config(
    config: DeploymentAuthorityConfig, sign: Callable[[bytes], bytes]
) -> dict[str, Any]:
    """Build an envelope using an externally supplied signer, for enrollment tooling."""
    signature = sign(_DOMAIN + config.canonical_bytes())
    if len(signature) != 64:
        raise AuthorityConfigError("authority config signature is invalid")
    return {"config": config.model_dump(mode="json"), "signature": signature.hex()}


def load_authority_config(
    path: Path,
    *,
    trusted_public_key: bytes,
    expected_deployment: str,
    expected_tenant: str,
    anchor: MonotonicAnchor,
) -> DeploymentAuthorityConfig:
    """Authenticate the exact signed revision against independent trust inputs."""
    if len(trusted_public_key) != 32:
        raise AuthorityConfigError("authority config verification key is invalid")
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        try:
            meta = os.fstat(fd)
            if (
                not stat.S_ISREG(meta.st_mode)
                or meta.st_nlink != 1
                or meta.st_uid not in {0, os.getuid()}
                or stat.S_IMODE(meta.st_mode) & 0o022
                or meta.st_size > _MAX_FILE
            ):
                raise AuthorityConfigError("authority config file is unsafe")
            raw = os.read(fd, _MAX_FILE + 1)
        finally:
            os.close(fd)
        if len(raw) > _MAX_FILE:
            raise AuthorityConfigError("authority config file is too large")
        envelope = json.loads(raw)
        if not isinstance(envelope, dict) or set(envelope) != {"config", "signature"}:
            raise ValueError("invalid envelope")
        config = DeploymentAuthorityConfig.model_validate(envelope["config"])
        signature = bytes.fromhex(envelope["signature"])
        if not verify_signature(
            ED25519, _DOMAIN + config.canonical_bytes(), signature, trusted_public_key
        ):
            raise AuthorityConfigError("authority config signature refused")
        if config.deployment_id != expected_deployment or config.tenant_id != expected_tenant:
            raise AuthorityConfigError("authority config deployment or tenant mismatch")
        if anchor.scope != config.config_anchor_scope:
            raise AuthorityConfigError("authority config anchor namespace mismatch")
        head = anchor.latest()
        if (
            head is None
            or head.scope != anchor.scope
            or head.version != config.revision
            or head.digest != hashlib.sha256(config.canonical_bytes()).hexdigest()
        ):
            raise AuthorityConfigError("authority config revision is not current")
        return config
    except (OSError, ValueError, TypeError, KeyError) as exc:
        raise AuthorityConfigError("authority config unavailable or invalid") from exc
