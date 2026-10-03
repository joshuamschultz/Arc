"""P18-3 — the deployment's OAuth app slots: one client id + secret per provider.

A provider's OAuth app is set up ONCE per deployment ("Set up Google sign-in"), and
every connection of that provider uses it: two mailboxes share one app, and no
connection carries a client id or secret of its own (design O4).

Slots live in their own arcstore collection, ``mutable_records/oauth_apps/<provider>``,
NOT in ``connector_credentials``: a slot is not a connection, so the proactive
renewer's scan never meets one and no connection name can collide with one. The
client secret is sealed by the same :class:`~arcagent.extension.custody.CredentialCipher`
as connector credentials, with associated data binding it to ``(app_<provider>,
oauth_app_secret)``, so a sealed secret copied to another provider's slot does not open. The client id is
not a secret (it rides every authorize URL) and is stored in the clear so a surface
can show a hint of it.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from arctrust.audit import AuditEvent, AuditSink, emit
from arctrust.connector_cipher import CredentialSealError

from arcagent.core.errors import ExtensionError
from arcagent.extension.custody import CredentialCipher
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


@dataclass(frozen=True)
class OAuthApp:
    """One provider's app credentials, the secret still wrapped."""

    provider: str
    client_id: str
    client_secret: Secret


@dataclass(frozen=True)
class OAuthAppStatus:
    """What a surface may show about a slot: never the secret, only an id hint."""

    provider: str
    configured: bool
    client_id_hint: str = ""


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _check_provider(provider: str) -> None:
    if not _PROVIDER.fullmatch(provider):
        raise ExtensionError(
            code="OAUTH_APP_INVALID",
            message="that is not a provider name",
            details={},
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
        self, provider: str, *, client_id: str, client_secret: str, actor_did: str
    ) -> None:
        """Set (or replace) a provider's app. Values are checked before anything is stored."""
        _check_provider(provider)
        client_id = client_id.strip()
        client_secret = client_secret.strip()
        _check_values(client_id, client_secret)
        row = {
            "provider": provider,
            "client_id": client_id,
            "client_secret": self._seal(provider, client_secret),
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
            client_secret=Secret(self._open(provider, row)),
        )

    async def status(self, provider: str) -> OAuthAppStatus:
        """Configured or not, and the first 12 characters of the client id."""
        _check_provider(provider)
        row = await self._backend.mutable_read(OAUTH_APP_COLLECTION, provider)
        if row is None:
            return OAuthAppStatus(provider=provider, configured=False)
        hint = str(row.get("client_id") or "")[:_HINT_LENGTH]
        return OAuthAppStatus(provider=provider, configured=True, client_id_hint=hint)

    async def forget(self, provider: str, *, actor_did: str) -> bool:
        """Delete a provider's slot. Its connections then need a new app to refresh."""
        _check_provider(provider)
        removed = await self._backend.mutable_delete(
            OAUTH_APP_COLLECTION, provider, actor_did=actor_did, sink=self._sink
        )
        if removed:
            self._audit("oauth_app.delete", provider, actor_did)
        return removed

    async def client_for(self, flow: OAuthFlow) -> tuple[str, Secret] | None:
        """The renewer's :class:`~arcagent.extension.credentials.ClientCredentialSource`."""
        app = await self.get(flow.provider)
        return None if app is None else (app.client_id, app.client_secret)

    def _seal(self, provider: str, value: str) -> str:
        try:
            return self._cipher.seal(
                value.encode("utf-8"), scope=_scope(provider), slot=_SEALED_SLOT
            )
        except CredentialSealError:
            raise ExtensionError(
                code="SECRET_VALUE_INVALID",
                message="the client secret could not be sealed",
                details={"provider": provider},
            ) from None

    def _open(self, provider: str, row: dict[str, Any]) -> str:
        if row.get("cipher") != self._cipher.kind:
            raise _unreadable(provider)
        try:
            opened = self._cipher.open(
                str(row.get("client_secret") or ""), scope=_scope(provider), slot=_SEALED_SLOT
            )
            return opened.decode("utf-8")
        except (CredentialSealError, UnicodeDecodeError):
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
]
