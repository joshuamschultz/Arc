"""Vault Transit key issuance and sign-by-reference over injected HTTPS."""

from __future__ import annotations

import base64
import binascii
import re
from typing import Any

import httpx

from arctrust.signer import ED25519, SignerError, verify_signature

_KEY_REF = re.compile(r"^[a-z][a-z0-9-]{1,127}$")


class VaultTransitHTTP:
    """Vault Transit adapter whose caller owns an authenticated HTTPS client.

    The client must already carry a scoped, short-lived Vault authorization
    capability. This class never reads tokens from files or environment and
    never includes a response body or URL in an error.
    """

    def __init__(self, client: httpx.Client, *, mount: str = "transit") -> None:
        if client.base_url.scheme != "https":
            raise SignerError("Vault Transit requires HTTPS")
        if not _KEY_REF.fullmatch(mount):
            raise SignerError("invalid Vault Transit mount")
        self._client = client
        self._mount = mount

    def create_user_key(self, key_ref: str) -> bytes:
        """Create one nonexportable Ed25519 key and return its verified public key."""
        self._check_ref(key_ref)
        try:
            response = self._client.post(
                f"/v1/{self._mount}/keys/{key_ref}",
                json={
                    "type": "ed25519",
                    "derived": False,
                    "exportable": False,
                    "allow_plaintext_backup": False,
                },
                timeout=5,
                follow_redirects=False,
            )
            response.raise_for_status()
        except httpx.HTTPError:
            raise SignerError("Vault Transit unavailable") from None
        return self.public_key(key_ref)

    def public_key(self, key_ref: str) -> bytes:
        """Read a nonexportable Ed25519 public key from Vault metadata."""
        self._check_ref(key_ref)
        body = self._request("GET", f"/v1/{self._mount}/keys/{key_ref}")
        try:
            data = body["data"]
            if (
                data["type"] != ED25519
                or data["exportable"] is not False
                or data["derived"] is not False
                or data["allow_plaintext_backup"] is not False
                or data["deletion_allowed"] is not False
            ):
                raise ValueError("unsafe key configuration")
            latest = data["latest_version"]
            if type(latest) is not int or latest < 1:
                raise ValueError("invalid key version")
            encoded = data["keys"][str(latest)]["public_key"]
            if not isinstance(encoded, str):
                raise ValueError("invalid public key")
            raw = base64.b64decode(encoded, validate=True)
            if len(raw) != 32:
                raise ValueError("invalid Ed25519 public key length")
            return raw
        except (KeyError, TypeError, ValueError, AttributeError, binascii.Error):
            raise SignerError("Vault Transit key metadata is invalid") from None

    def sign(self, key_ref: str, message: bytes) -> bytes:
        """Sign without materializing the private key in Arc."""
        self._check_ref(key_ref)
        body = self._request(
            "POST",
            f"/v1/{self._mount}/sign/{key_ref}",
            json_body={"input": base64.b64encode(message).decode("ascii")},
        )
        try:
            signature = str(body["data"]["signature"])
            prefix, version, encoded = signature.split(":", 2)
            if prefix != "vault" or not version.startswith("v"):
                raise ValueError("invalid signature envelope")
            raw = base64.b64decode(encoded, validate=True)
            if len(raw) != 64:
                raise ValueError("invalid Ed25519 signature")
            if not verify_signature(ED25519, message, raw, self.public_key(key_ref)):
                raise ValueError("signature does not match key")
            return raw
        except (KeyError, TypeError, ValueError, binascii.Error):
            raise SignerError("Vault Transit signature is invalid") from None

    def _request(
        self, method: str, path: str, json_body: dict[str, str] | None = None
    ) -> dict[str, Any]:
        try:
            response = self._client.request(
                method, path, json=json_body, timeout=5, follow_redirects=False
            )
            response.raise_for_status()
            body: dict[str, Any] = response.json()
            if not isinstance(body, dict):
                raise ValueError("invalid Vault response")
            return body
        except (httpx.HTTPError, ValueError, TypeError):
            raise SignerError("Vault Transit unavailable") from None

    @staticmethod
    def _check_ref(key_ref: str) -> None:
        if not _KEY_REF.fullmatch(key_ref):
            raise SignerError("invalid Vault Transit key reference")
