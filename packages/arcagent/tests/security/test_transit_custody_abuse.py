"""P18-2F abuse cases — connector credentials sealed in the Vault Transit.

Attacker models: a database reader/writer (with or without the old in-process
operator seed), a ciphertext transplanter, an environment poisoner on the host,
and an outage that tempts a fallback. Every case must fail closed.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from arctrust import FileNotaryTransit, TransitConnectorCipher
from arctrust.transit_cipher import TransitUnavailableError
from packages.arcagent.tests.custody_fakes import InterleavingBackend, make_cipher

from arcagent.core.errors import ExtensionError
from arcagent.extension.custody import CREDENTIAL_COLLECTION, CredentialRowStore

ACTOR = "did:arc:operator:test"


class DownTransit:
    def encrypt(self, key_ref: str, plaintext: bytes, *, aad: bytes) -> str:
        raise TransitUnavailableError("down")

    def decrypt(self, key_ref: str, ciphertext: str, *, aad: bytes) -> bytes:
        raise TransitUnavailableError("down")


@pytest.fixture
def transit(tmp_path: Path) -> FileNotaryTransit:
    return FileNotaryTransit(tmp_path / "notary")


async def _rows(transit: FileNotaryTransit) -> tuple[InterleavingBackend, CredentialRowStore]:
    backend = InterleavingBackend()
    rows = CredentialRowStore(backend, TransitConnectorCipher(transit))
    await rows.put_fields("blackarc", {"refresh_token": "victim-refresh"}, actor_did=ACTOR)
    await rows.put_fields("systems", {"refresh_token": "attacker-refresh"}, actor_did=ACTOR)
    return backend, rows


async def test_holder_of_the_old_operator_seed_cannot_open_transit_rows(
    transit: FileNotaryTransit,
) -> None:
    backend, _ = await _rows(transit)
    stolen_seed_store = CredentialRowStore(backend, make_cipher())
    row = await stolen_seed_store.read("blackarc")
    assert row is not None
    with pytest.raises(ExtensionError) as caught:
        await stolen_seed_store.open_field(row, "refresh_token")
    assert caught.value.code == "CREDENTIAL_UNREADABLE"
    assert "victim-refresh" not in str(await backend.mutable_query(CREDENTIAL_COLLECTION))


async def test_transplanted_transit_ciphertext_does_not_open(
    transit: FileNotaryTransit,
) -> None:
    backend, rows = await _rows(transit)
    attacker = await backend.mutable_read(CREDENTIAL_COLLECTION, "systems")
    assert attacker is not None
    victim = await backend.mutable_read(CREDENTIAL_COLLECTION, "blackarc")
    assert victim is not None
    victim["fields"]["refresh_token"] = attacker["fields"]["refresh_token"]
    await backend.update_if(
        CREDENTIAL_COLLECTION, "blackarc", {"fields": victim["fields"]}, {}, actor_did=ACTOR
    )
    row = await rows.read("blackarc")
    assert row is not None
    with pytest.raises(ExtensionError) as caught:
        await rows.open_field(row, "refresh_token")
    assert caught.value.code == "CREDENTIAL_UNREADABLE"
    assert "attacker-refresh" not in caught.value.message


async def test_downgrade_to_an_in_process_row_planted_with_the_seed_is_refused(
    transit: FileNotaryTransit,
) -> None:
    """A DB writer holding the old seed plants an xc1 value and flips ``cipher``."""
    backend, rows = await _rows(transit)
    planted = make_cipher().seal(b"attacker-token", scope="blackarc", slot="refresh_token")
    await backend.update_if(
        CREDENTIAL_COLLECTION,
        "blackarc",
        {
            "cipher": "xc1",
            "fields": {"refresh_token": {"sealed": planted, "updated_at": "2026-01-01"}},
        },
        {},
        actor_did=ACTOR,
    )
    row = await rows.read("blackarc")
    assert row is not None
    with pytest.raises(ExtensionError) as caught:
        await rows.open_field(row, "refresh_token")
    assert caught.value.code == "CREDENTIAL_UNREADABLE"


async def test_outage_never_falls_back_to_an_in_process_seal(transit: FileNotaryTransit) -> None:
    backend = InterleavingBackend()
    rows = CredentialRowStore(backend, TransitConnectorCipher(DownTransit()))
    with pytest.raises(ExtensionError) as caught:
        await rows.put_fields("blackarc", {"refresh_token": "value"}, actor_did=ACTOR)
    assert caught.value.code == "CREDENTIAL_CUSTODY_UNAVAILABLE"
    assert await backend.mutable_query(CREDENTIAL_COLLECTION) == []


def test_poisoned_pythonpath_does_not_reach_the_notary_child(
    transit: FileNotaryTransit, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The child runs isolated (-I): a planted ``cryptography`` on PYTHONPATH is ignored."""
    evil = tmp_path / "evil" / "cryptography"
    evil.mkdir(parents=True)
    marker = tmp_path / "pwned"
    (evil / "__init__.py").write_text(f"open({str(marker)!r}, 'w').write('x')\n")
    monkeypatch.setenv("PYTHONPATH", str(evil.parent))
    cipher = TransitConnectorCipher(transit)

    sealed = cipher.seal(b"v", scope="a", slot="b")

    assert cipher.open(sealed, scope="a", slot="b") == b"v"
    assert not marker.exists()
