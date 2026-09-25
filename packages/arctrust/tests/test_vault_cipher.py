"""Vault-backed sealed state never exports a key or changes authority binding."""

from __future__ import annotations

import base64
import json

import httpx
import pytest

from arctrust.vault_cipher import VaultCipher, VaultCipherError

_SAFE_KEY = {
    "type": "aes256-gcm96",
    "exportable": False,
    "derived": False,
    "allow_plaintext_backup": False,
    "deletion_allowed": False,
}


def _client(handler: httpx.MockTransport) -> httpx.Client:
    return httpx.Client(base_url="https://vault.example", transport=handler)


def test_seal_and_open_send_bound_aad_and_no_key_material() -> None:
    seen: list[dict[str, object]] = []

    def respond(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json={"data": _SAFE_KEY})
        body = json.loads(request.content)
        seen.append(body)
        if "/encrypt/" in request.url.path:
            return httpx.Response(200, json={"data": {"ciphertext": "vault:v1:YWJj"}})
        return httpx.Response(
            200, json={"data": {"plaintext": base64.b64encode(b"hello").decode()}}
        )

    cipher = VaultCipher(
        _client(httpx.MockTransport(respond)),
        mount="transit",
        key="users-key",
        scope="tenant/order",
        record_id="users",
    )
    assert cipher.seal(b"hello") == "vault:v1:YWJj"
    assert cipher.open("vault:v1:YWJj") == b"hello"
    assert seen[0]["associated_data"] == seen[1]["associated_data"]
    assert base64.b64decode(str(seen[0]["associated_data"])) == b"arc:v1:tenant/order:users"
    assert "key" not in seen[0]


@pytest.mark.parametrize(
    "unsafe",
    [
        {**_SAFE_KEY, "exportable": True},
        {**_SAFE_KEY, "derived": True},
        {**_SAFE_KEY, "deletion_allowed": True},
        {**_SAFE_KEY, "type": "aes128-gcm96"},
    ],
)
def test_unsafe_key_metadata_is_refused(unsafe: dict[str, object]) -> None:
    client = _client(httpx.MockTransport(lambda _: httpx.Response(200, json={"data": unsafe})))
    with pytest.raises(VaultCipherError, match="unsafe"):
        VaultCipher(
            client, mount="transit", key="users-key", scope="tenant/order", record_id="users"
        )


def test_redirect_and_transport_error_are_sanitized() -> None:
    client = _client(
        httpx.MockTransport(lambda _: httpx.Response(302, headers={"location": "https://evil"}))
    )
    with pytest.raises(VaultCipherError, match="unavailable or unsafe"):
        VaultCipher(
            client, mount="transit", key="users-key", scope="tenant/order", record_id="users"
        )


def test_ciphertext_shape_and_bound_are_checked() -> None:
    client = _client(httpx.MockTransport(lambda _: httpx.Response(200, json={"data": _SAFE_KEY})))
    cipher = VaultCipher(
        client, mount="transit", key="users-key", scope="tenant/order", record_id="users"
    )
    with pytest.raises(VaultCipherError, match="invalid Vault ciphertext"):
        cipher.open("plain")
    with pytest.raises(VaultCipherError, match="exceeds limit"):
        cipher.seal(b"x" * (512 * 1024 + 1))
