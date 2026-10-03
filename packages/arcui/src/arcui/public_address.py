"""The dashboard's public address: the one origin OAuth providers send a browser back to.

The operator states it once in Settings (``[ui] public_base_url`` in ``gateway.toml``).
Every use reads it again from that file, so a change takes effect on the next
sign-in without a restart. It is never taken from a request: a ``Host`` or
``X-Forwarded-Host`` header is attacker-controlled (design O3), so a spoofed header
can not move where a provider delivers an authorization code.

Validation is tier-aware. ``https`` always passes; ``http`` passes only for a
loopback host at the personal tier. The tier is the stricter of the fleet floor
(``arcagent.toml [security] tier``) and ``gateway.toml [gateway] tier``, so neither
file can relax the other.
"""

from __future__ import annotations

import os
import tempfile
import tomllib
from pathlib import Path
from typing import Any

import arcagent
import tomlkit
from arcgateway.config import GatewayConfig, validate_public_base_url
from arctrust.paths import config_file

#: Refusal code when the stored or offered public address is not usable.
PUBLIC_ADDRESS_INVALID = "PUBLIC_ADDRESS_INVALID"


def _gateway_file() -> Path:
    return config_file("gateway.toml")


def effective_tier() -> str:
    """The stricter of the fleet tier floor and the gateway file's tier."""
    gateway_tier = GatewayConfig.load().gateway.tier
    return arcagent.stricter_tier(arcagent.deployment_tier().value, gateway_tier)


def checked_public_address(url: str, tier: str) -> str:
    """The canonical origin for ``url`` at ``tier``, or ``ValueError``.

    Both rules apply: the gateway's (https, or http on loopback at personal; no
    credentials, query or fragment) and the redirect builder's (an origin with no
    path, since the callback page is served at the root).
    """
    canonical = validate_public_base_url(url, tier)
    arcagent.oauth_redirect_uri(canonical)
    return canonical


def _invalid(message: str) -> arcagent.ExtensionError:
    return arcagent.ExtensionError(code=PUBLIC_ADDRESS_INVALID, message=message, details={})


class PublicAddress:
    """Reads and writes the operator's public address; derives the OAuth redirect URI."""

    def __init__(self, *, ui_port: int) -> None:
        self._ui_port = ui_port

    @property
    def ui_port(self) -> int:
        """The port this dashboard serves on (the loopback redirect's port)."""
        return self._ui_port

    def current(self) -> str | None:
        """The stored public address, re-checked at today's effective tier.

        Raises:
            ExtensionError: ``PUBLIC_ADDRESS_INVALID`` when the stored value does not
                pass at the effective tier (a tier raised after it was saved). A
                sign-in then refuses rather than falling back to another address.
        """
        try:
            stored = GatewayConfig.load().ui.public_base_url
            if stored is None:
                return None
            return checked_public_address(stored, effective_tier())
        except ValueError as exc:
            raise _invalid(f"the saved public address is not allowed at this tier: {exc}") from exc

    def redirect_uri(self) -> str:
        """The redirect URI every callback-mode connect uses, derived on each call."""
        return arcagent.oauth_redirect_uri(self.current(), port=self._ui_port)

    def save(self, url: str | None) -> str | None:
        """Validate and store ``url`` (``None`` clears it). Returns the stored origin.

        The rest of ``gateway.toml`` is kept byte-for-byte (tomlkit round trip), the
        whole new file is re-validated before it replaces the old one, and the write
        is atomic.

        Raises:
            ExtensionError: ``PUBLIC_ADDRESS_INVALID`` for an address the tier refuses.
        """
        canonical: str | None = None
        if url is not None and url.strip():
            try:
                canonical = checked_public_address(url, effective_tier())
            except ValueError as exc:
                raise _invalid(str(exc)) from exc
        path = _gateway_file()
        doc = _load_document(path)
        ui = doc.get("ui")
        if canonical is None:
            if isinstance(ui, dict) and "public_base_url" in ui:
                del ui["public_base_url"]
        else:
            if not isinstance(ui, dict):
                ui = tomlkit.table()
                doc["ui"] = ui
            ui["public_base_url"] = canonical
        text = tomlkit.dumps(doc)
        try:
            GatewayConfig.model_validate(tomllib.loads(text))
        except ValueError as exc:
            raise _invalid(str(exc)) from exc
        _atomic_write_text(path, text)
        return canonical


def _load_document(path: Path) -> Any:
    if not path.is_file():
        return tomlkit.document()
    return tomlkit.parse(path.read_text(encoding="utf-8"))


def _atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".toml.tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


__all__ = ["PUBLIC_ADDRESS_INVALID", "PublicAddress", "checked_public_address", "effective_tier"]
