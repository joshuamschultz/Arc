"""Vault Transit authenticated sealing with externally custodied keys."""

from __future__ import annotations

import base64
import binascii
import re
from typing import Any

import httpx

_NAME = re.compile(r"^[a-z][a-z0-9-]{1,127}$")
_BOUND = re.compile(r"^[a-zA-Z0-9_/-]{1,256}$")
_MAX_PAYLOAD = 512 * 1024
_MAX_CIPHERTEXT = 1_048_576


class VaultCipherError(RuntimeError):
    """Vault cannot authenticate or recover sealed state."""


class VaultCipher:
    """Seal bounded state under a nonexportable AES-GCM Transit key.

    The injected client already owns scoped, short-lived HTTPS authentication.
    Scope and record ID are fixed at construction and authenticated as AAD on
    every operation, so ciphertext cannot be transplanted across authorities.
    """

    def __init__(
        self, client: httpx.Client, *, mount: str, key: str, scope: str, record_id: str
    ) -> None:
        if client.base_url.scheme != "https" or client.base_url.userinfo:
            raise VaultCipherError("Vault cipher requires credential-free HTTPS URL")
        if not _NAME.fullmatch(mount) or not _NAME.fullmatch(key):
            raise VaultCipherError("invalid Vault cipher key reference")
        if not _BOUND.fullmatch(scope) or not _BOUND.fullmatch(record_id):
            raise VaultCipherError("invalid Vault cipher authority binding")
        self._client = client
        self._mount = mount
        self._key = key
        self._aad = base64.b64encode(f"arc:v1:{scope}:{record_id}".encode()).decode()
        self._validate_key()

    def _validate_key(self) -> None:
        try:
            response = self._client.get(
                f"/v1/{self._mount}/keys/{self._key}",
                timeout=5,
                follow_redirects=False,
            )
            response.raise_for_status()
            data = response.json()["data"]
            if (
                not isinstance(data, dict)
                or data.get("type") != "aes256-gcm96"
                or data.get("exportable") is not False
                or data.get("derived") is not False
                or data.get("allow_plaintext_backup") is not False
                or data.get("deletion_allowed") is not False
            ):
                raise ValueError("unsafe key metadata")
        except (httpx.HTTPError, ValueError, KeyError, TypeError):
            raise VaultCipherError("Vault cipher key unavailable or unsafe") from None

    def seal(self, payload: bytes) -> str:
        """Return versioned Vault ciphertext without exposing the key."""
        if len(payload) > _MAX_PAYLOAD:
            raise VaultCipherError("sealed payload exceeds limit")
        body = self._request(
            "encrypt",
            {
                "plaintext": base64.b64encode(payload).decode(),
                "associated_data": self._aad,
            },
        )
        try:
            ciphertext = body["data"]["ciphertext"]
            self._check_ciphertext(ciphertext)
            if not isinstance(ciphertext, str):
                raise TypeError("ciphertext is not a string")
            return ciphertext
        except (KeyError, TypeError, ValueError):
            raise VaultCipherError("Vault encryption response is invalid") from None

    def open(self, sealed: str) -> bytes:
        """Open only ciphertext bound to this scope and record."""
        self._check_ciphertext(sealed)
        body = self._request(
            "decrypt",
            {
                "ciphertext": sealed,
                "associated_data": self._aad,
            },
        )
        try:
            encoded = body["data"]["plaintext"]
            if not isinstance(encoded, str):
                raise ValueError("plaintext not string")
            payload = base64.b64decode(encoded, validate=True)
            if len(payload) > _MAX_PAYLOAD:
                raise ValueError("plaintext exceeds limit")
            return payload
        except (KeyError, TypeError, ValueError, binascii.Error):
            raise VaultCipherError("Vault decryption response is invalid") from None

    def _request(self, action: str, payload: dict[str, str]) -> dict[str, Any]:
        try:
            response = self._client.post(
                f"/v1/{self._mount}/{action}/{self._key}",
                json=payload,
                timeout=5,
                follow_redirects=False,
            )
            response.raise_for_status()
            body = response.json()
            if not isinstance(body, dict):
                raise ValueError("response is not an object")
            return body
        except (httpx.HTTPError, ValueError, TypeError):
            raise VaultCipherError("Vault cipher unavailable") from None

    @staticmethod
    def _check_ciphertext(value: object) -> None:
        if (
            not isinstance(value, str)
            or len(value) > _MAX_CIPHERTEXT
            or re.fullmatch(r"vault:v[1-9][0-9]*:[A-Za-z0-9+/=]+", value) is None
        ):
            raise VaultCipherError("invalid Vault ciphertext")
