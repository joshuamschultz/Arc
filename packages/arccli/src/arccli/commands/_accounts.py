"""Compose the Vault/OpenBao account authority for ``arc ui`` and ``arc user``.

arctrust owns every primitive (signed config, signed grants, scoped leases, the
Transit issuer, the sealed record, the KV anchor). This module only wires them to
the machine ``[security.accounts]`` block, the operator's public key and the
deployment audit chain. It holds no key material and no Vault token beyond the
in-memory lease clients arctrust creates.
"""

from __future__ import annotations

import json
import logging
import os
import ssl
import stat
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
from arctrust import AuditEvent, AuditSink, UserStore
from arctrust.authority import AccountAuthority, open_account_authority
from arctrust.authority_config import DeploymentAuthorityConfig
from arctrust.identity import did_from_public_key
from arctrust.transit_cipher import TransitUnavailableError
from arctrust.users import default_users_path
from arctrust.vault_anchor import VaultKVAnchor
from arctrust.vault_auth import TokenSession, read_secret_source
from arctrust.vault_lease import Capability, VaultCredentialProvider, VaultLeaseError
from pydantic import ValidationError

_logger = logging.getLogger(__name__)

_MAX_FILE = 65_536
_ACCOUNT_CAPABILITIES = (Capability.ISSUER, Capability.CIPHER, Capability.ANCHOR)
_BOOTSTRAP_MARKER = "users-bootstrap.once"


class AccountsConfigError(RuntimeError):
    """``[security.accounts]`` is present but cannot produce a working authority."""


# --------------------------------------------------------------------------- files


def read_private_file(path: Path, *, what: str) -> bytes:
    """Read a small regular file that no other user may have written.

    ``O_NOFOLLOW`` plus an ``fstat`` on the opened descriptor, so a swap between
    check and read cannot redirect it.
    """
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except OSError as exc:
        raise AccountsConfigError(f"{what} is not readable") from exc
    try:
        meta = os.fstat(fd)
        if (
            not stat.S_ISREG(meta.st_mode)
            or meta.st_nlink != 1
            or meta.st_uid not in {0, os.getuid()}
            or stat.S_IMODE(meta.st_mode) & 0o022
            or meta.st_size > _MAX_FILE
        ):
            raise AccountsConfigError(f"{what} is unsafe (type, owner, mode or size)")
        return os.read(fd, _MAX_FILE + 1)
    finally:
        os.close(fd)


def bootstrap_marker_path(grants_file: Path) -> Path:
    """Where enrollment leaves the single-use permission to create the users anchor."""
    return grants_file.with_name(_BOOTSTRAP_MARKER)


# ------------------------------------------------------------------ trust adapters


class _OnceBootstrap:
    """Single-use permission to create the missing ``users`` anchor.

    Enrollment (admin token) leaves a marker next to the grants file when Vault has
    no ``users`` record. Consuming removes it, so the permission cannot be replayed.
    Vault's own ``cas=0`` refuses to overwrite an existing record regardless.
    """

    def __init__(self, marker: Path, scope: str) -> None:
        self._marker = marker
        self._scope = scope

    def consume(self, scope: str) -> bool:
        if scope != self._scope:
            return False
        try:
            self._marker.unlink()
        except OSError:
            return False
        return True


class _ProcessActor:
    """Authenticates one opaque object that only this process holds.

    arcui routes already authenticate and audit the human actor (``ui.mutation``);
    the account store's own actor is the serving deployment, derived from the
    operator public key under the tenant org. Being in-process, identity comparison
    is the whole check: nothing a request carries can equal the proof.
    """

    def __init__(self, proof: object, did: str) -> None:
        self._proof = proof
        self._did = did

    def authenticate(self, proof: object, *, action: str) -> str:
        if proof is not self._proof or action != "users.mutate":
            raise PermissionError("account actor proof refused")
        return self._did


class _StrictAudit:
    """Adapts a chain sink to the durable contract; a failed append always raises."""

    def __init__(self, sink: object) -> None:
        self._sink = sink

    def write_durable(self, event: AuditEvent) -> None:
        append = getattr(self._sink, "write_durable", None)
        if append is None:
            raise AccountsConfigError("audit sink cannot append durably")
        append(event)


class _AppRoleCredentials:
    """Logs in with the AppRole of the requested capability and installs the token.

    The token goes straight onto the lease's client header; it is never returned,
    logged or stored here. The bootstrap secret is read once from its source
    (``credential:`` / ``file:`` / ``env:`` / ``fd:``) and held in memory only, so a
    re-opened lease after a lease error can log in again.
    """

    def __init__(self, roles: dict[Capability, tuple[str, str]], url: str, ca_pem: bytes) -> None:
        self._roles = roles
        self._url = url
        self._context = ssl.create_default_context(cadata=ca_pem.decode("ascii"))
        self._secrets: dict[str, str] = {}
        self._lock = threading.Lock()

    def login(self, role_id: str, secret_source: str) -> str:
        with self._lock:
            if secret_source not in self._secrets:
                self._secrets[secret_source] = read_secret_source(secret_source)
            secret_id = self._secrets[secret_source]
        session = TokenSession(
            login_path="/v1/auth/approle/login",
            login_body=lambda: {"role_id": role_id, "secret_id": secret_id},
            post=self._post,
        )
        try:
            return session.token()
        except TransitUnavailableError:
            raise VaultLeaseError("Vault AppRole login refused") from None

    def _post(self, path: str, body: dict[str, Any], token: str | None) -> dict[str, Any]:
        headers = {"X-Vault-Token": token} if token else {}
        try:
            with httpx.Client(base_url=self._url, verify=self._context) as client:
                response = client.post(
                    path, json=body, headers=headers, timeout=5.0, follow_redirects=False
                )
            response.raise_for_status()
            answer = response.json()
        except (httpx.HTTPError, ValueError):
            raise TransitUnavailableError("Vault login unavailable") from None
        if not isinstance(answer, dict):
            raise TransitUnavailableError("Vault login answer is invalid")
        return answer

    def authorize(
        self, client: httpx.Client, *, subject: str, policy_name: str, capability: Capability
    ) -> None:
        role = self._roles.get(capability)
        if role is None:
            raise VaultLeaseError("capability has no AppRole")
        client.headers["X-Vault-Token"] = self.login(*role)


# ---------------------------------------------------------------------------- opening


def _config_anchor_mount(cfg: Any, ca_pem: bytes) -> str:
    """Anchor mount derived by arctrust from the same IDs, before the config is read."""
    import hashlib

    probe = DeploymentAuthorityConfig(
        deployment_id=cfg.deployment_id,
        tenant_id=cfg.tenant_id,
        revision=1,
        vault_url=cfg.vault_url,
        vault_ca_sha256=hashlib.sha256(ca_pem).hexdigest(),
    )
    return probe.anchor_mount


def _load_grants(path: Path) -> dict[Capability, dict[str, Any]]:
    try:
        raw = json.loads(read_private_file(path, what="account grants file"))
        grants = {Capability(name): envelope for name, envelope in raw.items()}
    except (ValueError, AttributeError, TypeError) as exc:
        raise AccountsConfigError("account grants file is malformed") from exc
    if set(grants) != set(_ACCOUNT_CAPABILITIES):
        raise AccountsConfigError("account grants file must hold issuer, cipher and anchor")
    return grants


class _AuthorityFactory:
    """The zero-argument factory arcui and ``arc user`` call per operation.

    One :class:`AccountAuthority` is opened lazily and shared; it is re-opened (once
    per failing call) only after a lease error, so renewal or revocation of a Vault
    token heals without a restart. Anchor errors never trigger a re-open: a fresh
    authority would forget the highest version seen and weaken rollback detection.
    """

    def __init__(self, cfg: Any, audit_sink: AuditSink, users_path: Path | None) -> None:
        self._cfg = cfg
        self._audit = audit_sink
        self._users_path = users_path
        self._proof = object()
        self._authority: AccountAuthority | None = None
        self._lock = threading.Lock()

    def __call__(self) -> UserStore:
        authority = self._current()
        try:
            return authority.user_store(self._proof)
        except VaultLeaseError:
            _logger.warning("accounts.lease_error reopening authority")
            return self._reopen(authority).user_store(self._proof)

    def _current(self) -> AccountAuthority:
        with self._lock:
            if self._authority is None:
                self._authority = self._open()
            return self._authority

    def _reopen(self, stale: AccountAuthority) -> AccountAuthority:
        with self._lock:
            if self._authority is stale:
                stale.close()
                self._authority = self._open()
            return self._authority  # type: ignore[return-value]  # reason: set on the line above or by a peer

    def _open(self) -> AccountAuthority:
        from arccli.commands.operator import operator_public_key

        cfg = self._cfg
        operator_key = operator_public_key()
        if operator_key is None:
            raise AccountsConfigError("operator key is unavailable; accounts cannot be trusted")
        ca_pem = read_private_file(Path(cfg.ca_file).expanduser(), what="Vault CA file")
        grants_path = Path(cfg.grants_file).expanduser()
        credentials = _AppRoleCredentials(
            {
                Capability.ISSUER: (cfg.issuer_role_id, cfg.issuer_secret),
                Capability.CIPHER: (cfg.cipher_role_id, cfg.cipher_secret),
                Capability.ANCHOR: (cfg.anchor_role_id, cfg.anchor_secret),
            },
            cfg.vault_url,
            ca_pem,
        )
        anchor_mount = _config_anchor_mount(cfg, ca_pem)
        reader = httpx.Client(
            base_url=cfg.vault_url,
            verify=ssl.create_default_context(cadata=ca_pem.decode("ascii")),
        )
        try:
            reader.headers["X-Vault-Token"] = credentials.login(
                cfg.config_reader_role_id, cfg.config_reader_secret
            )
            return self._open_with(
                cfg,
                operator_key,
                ca_pem,
                grants_path,
                credentials,
                VaultKVAnchor(reader, mount=anchor_mount, record="config"),
                anchor_mount,
            )
        finally:
            _revoke_and_close(reader)

    def _open_with(
        self,
        cfg: Any,
        operator_key: bytes,
        ca_pem: bytes,
        grants_path: Path,
        credentials: VaultCredentialProvider,
        config_anchor: VaultKVAnchor,
        anchor_mount: str,
    ) -> AccountAuthority:
        actor_did = did_from_public_key(operator_key, org=cfg.tenant_id, agent_type="operator")
        return open_account_authority(
            Path(cfg.authority_config).expanduser(),
            trusted_config_key=operator_key,
            expected_deployment=cfg.deployment_id,
            expected_tenant=cfg.tenant_id,
            config_anchor=config_anchor,
            signed_grants=_load_grants(grants_path),
            trusted_grant_key=operator_key,
            credential_provider=credentials,
            ca_pem=ca_pem,
            audit_sink=self._audit,
            strict_audit_sink=_StrictAudit(self._audit),
            actor_verifier=_ProcessActor(self._proof, actor_did),
            users_path=self._users_path or default_users_path(),
            bootstrap_authority=_OnceBootstrap(
                bootstrap_marker_path(grants_path), f"{anchor_mount}/users"
            ),
        )


def _revoke_and_close(client: httpx.Client) -> None:
    try:
        client.post("/v1/auth/token/revoke-self", timeout=5.0, follow_redirects=False)
    except httpx.HTTPError:
        pass
    finally:
        client.close()


# ----------------------------------------------------------------------------- public


def accounts_config() -> Any | None:
    """The validated ``[security.accounts]`` block, or None when absent."""
    from arccli.commands.operator import _machine_security

    try:
        return _machine_security().accounts
    except (ValidationError, ValueError) as exc:
        raise AccountsConfigError(
            f"[security.accounts] is invalid ({type(exc).__name__}); fix arcagent.toml"
        ) from exc


def build_user_store_factory(
    audit_sink: AuditSink, *, users_path: Path | None = None
) -> Callable[[], UserStore] | None:
    """The deployment's account-store factory, or None when no authority is configured.

    ``audit_sink`` must append durably (``write_durable``): a mutation whose audit
    record cannot be written is refused. Raises :class:`AccountsConfigError` when the
    block is present but invalid; connection and trust failures surface on first use.
    """
    cfg = accounts_config()
    if cfg is None:
        _logger.warning(
            "accounts: [security.accounts] is not configured; people management is unavailable"
        )
        return None
    return _AuthorityFactory(cfg, audit_sink, users_path)


__all__ = [
    "AccountsConfigError",
    "bootstrap_marker_path",
    "build_user_store_factory",
    "read_private_file",
]
