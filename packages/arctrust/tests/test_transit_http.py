"""Vault Transit key creation never exports private material."""

from __future__ import annotations

import base64

import httpx
import pytest
from nacl.signing import SigningKey

from arctrust.signer import SignerError
from arctrust.transit_http import VaultTransitHTTP


def test_issues_nonexportable_key_and_signs_by_reference() -> None:
    signing = SigningKey.generate()
    public = signing.verify_key.encode()
    seen: list[httpx.Request] = []

    def transport(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.method == "POST" and request.url.path.endswith("/keys/arc-user-1"):
            assert request.content == (
                b'{"type":"ed25519","derived":false,"exportable":false,'
                b'"allow_plaintext_backup":false}'
            )
            return httpx.Response(204)
        if request.method == "GET":
            return httpx.Response(
                200,
                json={
                    "data": {
                        "type": "ed25519",
                        "exportable": False,
                        "derived": False,
                        "allow_plaintext_backup": False,
                        "deletion_allowed": False,
                        "latest_version": 1,
                        "keys": {"1": {"public_key": base64.b64encode(public).decode()}},
                    }
                },
            )
        return httpx.Response(
            200,
            json={
                "data": {
                    "signature": "vault:v1:"
                    + base64.b64encode(signing.sign(b"proof").signature).decode()
                }
            },
        )

    with httpx.Client(
        base_url="https://vault.test", transport=httpx.MockTransport(transport)
    ) as client:
        transit = VaultTransitHTTP(client)
        assert transit.create_user_key("arc-user-1") == public
        assert transit.sign("arc-user-1", b"proof") == signing.sign(b"proof").signature
    assert len(seen) == 4


def test_rejects_plaintext_transport_and_exportable_key() -> None:
    with httpx.Client(base_url="http://vault.test") as client:
        with pytest.raises(SignerError, match="HTTPS"):
            VaultTransitHTTP(client)

    def transport(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": {"type": "ed25519", "exportable": True}})

    with httpx.Client(
        base_url="https://vault.test", transport=httpx.MockTransport(transport)
    ) as client:
        with pytest.raises(SignerError, match="metadata"):
            VaultTransitHTTP(client).public_key("arc-user-1")


@pytest.mark.parametrize(
    "field,value",
    [
        ("exportable", True),
        ("derived", True),
        ("allow_plaintext_backup", True),
        ("deletion_allowed", True),
        ("type", "rsa-2048"),
    ],
)
def test_refuses_unsafe_existing_key_metadata(field: str, value: object) -> None:
    metadata = {
        "type": "ed25519",
        "exportable": False,
        "derived": False,
        "allow_plaintext_backup": False,
        "deletion_allowed": False,
        "latest_version": 1,
        "keys": {"1": {"public_key": base64.b64encode(b"p" * 32).decode()}},
    }
    metadata[field] = value
    with httpx.Client(
        base_url="https://vault.test",
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={"data": metadata})
        ),
    ) as client:
        with pytest.raises(SignerError, match="metadata"):
            VaultTransitHTTP(client).public_key("arc-user-1")


def test_redirect_refused_without_following_and_error_is_sanitized() -> None:
    calls = 0

    def transport(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(302, headers={"Location": "https://attacker.test/steal"})

    with httpx.Client(
        base_url="https://vault.test",
        follow_redirects=True,
        transport=httpx.MockTransport(transport),
    ) as client:
        with pytest.raises(SignerError, match="unavailable") as raised:
            VaultTransitHTTP(client).public_key("arc-user-1")
    assert calls == 1
    assert "attacker" not in str(raised.value)


def test_invalid_reference_cannot_escape_vault_mount() -> None:
    with httpx.Client(base_url="https://vault.test") as client:
        with pytest.raises(SignerError, match="reference"):
            VaultTransitHTTP(client).public_key("../other-key")
