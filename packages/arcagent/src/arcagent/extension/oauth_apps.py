"""P18-3 — the deployment's OAuth app slots: one client id + secret per provider.

A provider's OAuth app is set up ONCE per deployment ("Set up Google sign-in"), and
every connection of that provider uses it: two mailboxes share one app, and no
connection carries a client id or secret of its own (design O4).

Slots live in their own arcstore collection, ``mutable_records/oauth_apps/<provider>``,
NOT in ``connector_credentials``: a slot is not a connection, so the proactive
renewer's scan never meets one and no connection name can collide with one. The
client secret is sealed by the same :class:`~arcagent.extension.custody.CredentialCipher`
as connector credentials, with associated data binding it to ``(app_<provider>,
oauth_app_secret)``, so a sealed secret copied to another provider's slot does not
open. The client id is not a secret (it rides every authorize URL) and is stored
in the clear so a surface can show a hint of it.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from arctrust.audit import AuditEvent, AuditSink, emit
from arctrust.connector_cipher import CredentialCustodyUnavailableError, CredentialSealError

from arcagent.core.errors import ExtensionError
from arcagent.extension.custody import CredentialCipher, custody_unavailable
from arcagent.extension.manifest import OAuthFlow
from arcagent.extension.secrets import Secret
from arcagent.extension.state import MutableConnectionBackend

#: The arcstore collection holding one row per provider.
OAUTH_APP_COLLECTION = "oauth_apps"

#: Refusal code when a connect needs an app slot nobody has set up.
OAUTH_APP_MISSING = "OAUTH_APP_MISSING"

_SEALED_SLOT = "oauth_app_secret"
_PROVIDER = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
#: Client ids are printable tokens; Google's are ~72 characters.
_CLIENT_ID = re.compile(r"^[A-Za-z0-9._~\-]{1,256}$")
_MAX_SECRET_LENGTH = 512
_HINT_LENGTH = 12
#: A directory id is a GUID. ``common``/``organizations``/``consumers`` and domain
#: names are refused: a tenant-bound sign-in must name its one directory.
_TENANT_ID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_CLOUD_KEY = re.compile(r"^[a-z][a-z0-9_]{0,31}$")


@dataclass(frozen=True)
class OAuthApp:
    """One provider's app credentials, the secret still wrapped."""

    provider: str
    client_id: str
    client_secret: Secret
    #: The directory (tenant) GUID, lowercase; "" for a provider with no tenants.
    tenant_id: str = ""
    #: The cloud KEY of the bundle's ``[oauth.clouds]`` table; "" when none applies.
    cloud: str = ""


@dataclass(frozen=True)
class OAuthAppStatus:
    """What a surface may show about a slot: never the secret, only an id hint."""

    provider: str
    configured: bool
    client_id_hint: str = ""
    #: Not secrets: a tenant id and a cloud key are shown so an operator can check them.
    tenant_id: str = ""
    cloud: str = ""


def _app_invalid(message: str, field: str) -> ExtensionError:
    return ExtensionError(code="OAUTH_APP_INVALID", message=message, details={"field": field})


def app_cloud(flow: OAuthFlow, app: OAuthApp) -> str:
    """The cloud KEY this app uses: its own, else the flow's default; "" without clouds.

    Raises:
        ExtensionError: ``OAUTH_APP_INVALID`` for a key the signed manifest does not declare.
    """
    if not flow.clouds:
        return ""
    key = app.cloud or flow.default_cloud
    if key not in flow.clouds:
        raise _app_invalid("that is not a cloud this sign-in app can use", "cloud")
    return key


def bind_flow(flow: OAuthFlow, app: OAuthApp) -> OAuthFlow:
    """``flow`` with its ``{tenant}`` / ``{login_host}`` templates filled from ``app``.

    The host comes from the SIGNED manifest's cloud table, looked up by the slot's
    cloud KEY: no slot value is ever spliced into a URL as a host. The tenant is a
    GUID (checked when the slot was saved, and again here). A flow with no
    templates is returned unchanged.

    Raises:
        ExtensionError: ``OAUTH_APP_INVALID`` — a tenant-bound flow whose slot has no
            valid tenant id, or a cloud key the manifest does not declare.
    """
    if not flow.app_tenant and not flow.clouds:
        return flow
    values: dict[str, str] = {}
    if flow.app_tenant:
        if not _TENANT_ID.fullmatch(app.tenant_id):
            raise _app_invalid(
                f"the {flow.provider} sign-in app has no directory (tenant) ID; set it up again",
                "tenant_id",
            )
        values["tenant"] = app.tenant_id
    cloud = app_cloud(flow, app)
    if cloud:
        values["login_host"] = flow.clouds[cloud].login_host

    def fill(template: str) -> str:
        for name, value in values.items():
            template = template.replace("{" + name + "}", value)
        return template

    return flow.model_copy(
        update={
            "authorize_url": fill(flow.authorize_url),
            "token_url": fill(flow.token_url),
            "id_token_issuers": [fill(issuer) for issuer in flow.id_token_issuers],
        }
    )


def app_binding(flow: OAuthFlow, app: OAuthApp) -> dict[str, str]:
    """The non-secret fields a grant is bound to (stored with it, compared on refresh)."""
    bound: dict[str, str] = {}
    if flow.app_tenant:
        bound["tenant_id"] = app.tenant_id
    if flow.clouds:
        bound["cloud"] = app_cloud(flow, app)
    return bound


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _check_provider(provider: str) -> None:
    if not _PROVIDER.fullmatch(provider):
        raise ExtensionError(
            code="OAUTH_APP_INVALID",
            message="that is not a provider name",
            details={},
        )


def _check_binding(tenant_id: str, cloud: str) -> None:
    if tenant_id and not _TENANT_ID.fullmatch(tenant_id):
        raise ExtensionError(
            code="OAUTH_APP_INVALID",
            message=(
                "the tenant ID must be the directory's ID, a GUID like "
                "11111111-2222-3333-4444-555555555555 (not common or a domain name)"
            ),
            details={"field": "tenant_id"},
        )
    if cloud and not _CLOUD_KEY.fullmatch(cloud):
        raise ExtensionError(
            code="OAUTH_APP_INVALID",
            message="that is not a cloud this sign-in app can use",
            details={"field": "cloud"},
        )


def _check_values(client_id: str, client_secret: str) -> None:
    if not _CLIENT_ID.fullmatch(client_id):
        raise ExtensionError(
            code="OAUTH_APP_INVALID",
            message="the client ID has characters a client ID never has; copy it again",
            details={"field": "client_id"},
        )
    if (
        not client_secret
        or len(client_secret) > _MAX_SECRET_LENGTH
        or not client_secret.isprintable()
        or any(character.isspace() for character in client_secret)
    ):
        raise ExtensionError(
            code="OAUTH_APP_INVALID",
            message="the client secret is empty or damaged; copy it again",
            details={"field": "client_secret"},
        )


class OAuthAppStore:
    """Sealed per-provider OAuth app credentials over arcstore."""

    def __init__(
        self,
        backend: MutableConnectionBackend,
        cipher: CredentialCipher,
        *,
        sink: AuditSink | None = None,
        clock: Callable[[], datetime] = _utcnow,
    ) -> None:
        self._backend = backend
        self._cipher = cipher
        self._sink = sink
        self._clock = clock

    async def put(
        self,
        provider: str,
        *,
        client_id: str,
        client_secret: str,
        actor_did: str,
        tenant_id: str = "",
        cloud: str = "",
    ) -> None:
        """Set (or replace) a provider's app. Values are checked before anything is stored.

        ``tenant_id`` / ``cloud`` are shape-checked here; whether the bundle needs
        them, and whether ``cloud`` is one it declares, is the caller's check
        (:meth:`arcagent.connections.Connections.set_oauth_app`), which knows the flow.
        """
        _check_provider(provider)
        client_id = client_id.strip()
        client_secret = client_secret.strip()
        tenant_id = tenant_id.strip().lower()
        cloud = cloud.strip()
        _check_values(client_id, client_secret)
        _check_binding(tenant_id, cloud)
        row = {
            "provider": provider,
            "client_id": client_id,
            "tenant_id": tenant_id,
            "cloud": cloud,
            "client_secret": await self._seal(provider, client_secret),
            "cipher": self._cipher.kind,
            "updated_at": self._clock().isoformat(),
        }
        existing = await self._backend.mutable_read(OAUTH_APP_COLLECTION, provider)
        if existing is None:
            await self._backend.mutable_create_batch(
                OAUTH_APP_COLLECTION, [(provider, row)], actor_did=actor_did, sink=self._sink
            )
        else:
            await self._backend.mutable_merge(
                OAUTH_APP_COLLECTION, provider, row, actor_did=actor_did, sink=self._sink
            )
        self._audit("oauth_app.write", provider, actor_did)

    async def get(self, provider: str) -> OAuthApp | None:
        """The provider's app, or ``None`` when no slot is set up.

        Raises:
            ExtensionError: ``CREDENTIAL_UNREADABLE`` for a slot sealed by another
                deployment's key or cipher.
        """
        _check_provider(provider)
        row = await self._backend.mutable_read(OAUTH_APP_COLLECTION, provider)
        if row is None:
            return None
        return OAuthApp(
            provider=provider,
            client_id=str(row.get("client_id") or ""),
            client_secret=Secret(await self._open(provider, row)),
            tenant_id=str(row.get("tenant_id") or ""),
            cloud=str(row.get("cloud") or ""),
        )

    async def status(self, provider: str) -> OAuthAppStatus:
        """Configured or not, and the first 12 characters of the client id."""
        _check_provider(provider)
        row = await self._backend.mutable_read(OAUTH_APP_COLLECTION, provider)
        if row is None:
            return OAuthAppStatus(provider=provider, configured=False)
        hint = str(row.get("client_id") or "")[:_HINT_LENGTH]
        return OAuthAppStatus(
            provider=provider,
            configured=True,
            client_id_hint=hint,
            tenant_id=str(row.get("tenant_id") or ""),
            cloud=str(row.get("cloud") or ""),
        )

    async def forget(self, provider: str, *, actor_did: str) -> bool:
        """Delete a provider's slot. Its connections then need a new app to refresh."""
        _check_provider(provider)
        removed = await self._backend.mutable_delete(
            OAUTH_APP_COLLECTION, provider, actor_did=actor_did, sink=self._sink
        )
        if removed:
            self._audit("oauth_app.delete", provider, actor_did)
        return removed

    async def client_for(self, flow: OAuthFlow) -> OAuthApp | None:
        """The renewer's :class:`~arcagent.extension.credentials.ClientCredentialSource`."""
        return await self.get(flow.provider)

    @property
    def cipher_kind(self) -> str:
        """The cipher new slots are sealed with (``xc1`` in process, ``transit1`` in Vault)."""
        return self._cipher.kind

    async def providers(self) -> list[str]:
        """Every provider with a slot (what a re-seal walks)."""
        rows = await self._backend.mutable_query(OAUTH_APP_COLLECTION)
        return sorted(str(row["provider"]) for row in rows if "provider" in row)

    async def sealed_by(self, provider: str) -> str | None:
        """The cipher kind a provider's slot is sealed with, or ``None`` when absent."""
        row = await self._backend.mutable_read(OAUTH_APP_COLLECTION, provider)
        return None if row is None else str(row.get("cipher") or "")

    async def reseal(self, provider: str, *, source: CredentialCipher, actor_did: str) -> bool:
        """Move one slot sealed by ``source`` under this store's cipher (P18-2F).

        The new ciphertext is opened again before it is written, so a slot is never
        replaced by something this deployment cannot read. False when the slot is
        absent or already under this cipher (a re-run is a no-op).

        Raises:
            ExtensionError: ``CREDENTIAL_UNREADABLE`` (sealed under neither cipher)
                or ``CREDENTIAL_CUSTODY_UNAVAILABLE`` (the vault cannot answer).
        """
        _check_provider(provider)
        row = await self._backend.mutable_read(OAUTH_APP_COLLECTION, provider)
        if row is None or row.get("cipher") == self._cipher.kind:
            return False
        if row.get("cipher") != source.kind:
            raise _unreadable(provider)
        secret = await _run(provider, source.open, str(row.get("client_secret") or ""))
        resealed = await self._seal(provider, secret.decode("utf-8"))
        moved = {**row, "client_secret": resealed, "cipher": self._cipher.kind}
        if await self._open(provider, moved) != secret.decode("utf-8"):
            raise _unreadable(provider)
        await self._backend.mutable_merge(
            OAUTH_APP_COLLECTION,
            provider,
            {"client_secret": resealed, "cipher": self._cipher.kind},
            actor_did=actor_did,
            sink=self._sink,
        )
        self._audit("oauth_app.resealed", provider, actor_did)
        return True

    async def _seal(self, provider: str, value: str) -> str:
        """Seal off the event loop: a transit cipher is a blocking round trip."""
        try:
            sealed = await asyncio.to_thread(
                self._cipher.seal, value.encode("utf-8"), scope=_scope(provider), slot=_SEALED_SLOT
            )
        except CredentialCustodyUnavailableError:
            raise custody_unavailable(_scope(provider)) from None
        except CredentialSealError:
            raise ExtensionError(
                code="SECRET_VALUE_INVALID",
                message="the client secret could not be sealed",
                details={"provider": provider},
            ) from None
        return sealed

    async def _open(self, provider: str, row: dict[str, Any]) -> str:
        if row.get("cipher") != self._cipher.kind:
            raise _unreadable(provider)
        opened = await _run(provider, self._cipher.open, str(row.get("client_secret") or ""))
        try:
            return opened.decode("utf-8")
        except UnicodeDecodeError:
            raise _unreadable(provider) from None

    def _audit(self, action: str, provider: str, actor_did: str) -> None:
        if self._sink is None:
            return
        emit(
            AuditEvent(
                actor_did=actor_did,
                action=action,
                target=f"oauth_app:{provider}",
                outcome="allow",
                extra={"provider": provider},
            ),
            self._sink,
        )


async def _run(provider: str, operation: Callable[..., bytes], sealed: str) -> bytes:
    """Open one sealed slot value off the loop, mapping failures to custody refusals."""
    try:
        return await asyncio.to_thread(
            operation, sealed, scope=_scope(provider), slot=_SEALED_SLOT
        )
    except CredentialCustodyUnavailableError:
        raise custody_unavailable(_scope(provider)) from None
    except CredentialSealError:
        raise _unreadable(provider) from None


def _scope(provider: str) -> str:
    return f"app_{provider}"


def _unreadable(provider: str) -> ExtensionError:
    return ExtensionError(
        code="CREDENTIAL_UNREADABLE",
        message=f"the {provider} sign-in app could not be opened under this deployment's key",
        details={"provider": provider},
    )


__all__ = [
    "OAUTH_APP_COLLECTION",
    "OAUTH_APP_MISSING",
    "OAuthApp",
    "OAuthAppStatus",
    "OAuthAppStore",
    "app_binding",
    "app_cloud",
    "bind_flow",
]
