"""P18-2F — the Transit row cipher for ``vault_transit`` custody.

The custody key lives in the out-of-process transit (the reference notary here);
this process hands it plaintext + associated data and gets ciphertext back, and
never reads the key. AES-256-GCM (FIPS-approved), AAD bound to the exact
``(connection, field)`` coordinate, exactly as the in-process cipher binds it.
"""

from __future__ import annotations

import base64
import stat
import sys
from pathlib import Path
from typing import Any

import pytest

from arctrust import ArcTrustFipsError, FileNotaryTransit, OperatorKey
from arctrust.connector_cipher import (
    ConnectorSecretCipher,
    CredentialCustodyUnavailableError,
    CredentialSealError,
    TransitConnectorCipher,
)
from arctrust.transit_cipher import TransitDecryptError, TransitUnavailableError

_KEY_REF = TransitConnectorCipher.KEY_REF
_OPENED: list[str] = []
_WATCHING: list[bool] = []


def _audit(event: str, args: tuple[Any, ...]) -> None:
    # Records every in-process open of a transit cipher key file while armed.
    if _WATCHING and event == "open" and args and str(args[0]).endswith(".aes256"):
        _OPENED.append(str(args[0]))


sys.addaudithook(_audit)


@pytest.fixture
def transit(tmp_path: Path) -> FileNotaryTransit:
    return FileNotaryTransit(tmp_path / "notary")


@pytest.fixture
def cipher(transit: FileNotaryTransit) -> TransitConnectorCipher:
    return TransitConnectorCipher(transit)


def _flip(sealed: str, index: int) -> str:
    head, _, body = sealed.rpartition(":")
    raw = bytearray(base64.b64decode(body))
    raw[index] ^= 0x01
    return f"{head}:{base64.b64encode(bytes(raw)).decode()}"


def test_transit_roundtrip(cipher: TransitConnectorCipher) -> None:
    sealed = cipher.seal(b"refresh-abc", scope="blackarc", slot="refresh_token")
    assert sealed.startswith("v1.tr.notary:v1:")
    assert cipher.open(sealed, scope="blackarc", slot="refresh_token") == b"refresh-abc"
    assert cipher.kind == "transit1"


def test_tamper_fails_closed(cipher: TransitConnectorCipher) -> None:
    sealed = cipher.seal(b"refresh-abc", scope="blackarc", slot="refresh_token")
    for index in (0, 12, -1):  # nonce, ciphertext, tag
        with pytest.raises(CredentialSealError):
            cipher.open(_flip(sealed, index), scope="blackarc", slot="refresh_token")


def test_aad_swap_is_refused(cipher: TransitConnectorCipher) -> None:
    sealed = cipher.seal(b"refresh-abc", scope="blackarc", slot="refresh_token")
    with pytest.raises(CredentialSealError):
        cipher.open(sealed, scope="systems", slot="refresh_token")
    with pytest.raises(CredentialSealError):
        cipher.open(sealed, scope="blackarc", slot="client_secret")


def test_process_never_holds_the_transit_key(
    cipher: TransitConnectorCipher, tmp_path: Path
) -> None:
    _OPENED.clear()
    _WATCHING.append(True)
    try:
        sealed = cipher.seal(b"value", scope="a", slot="b")
        assert cipher.open(sealed, scope="a", slot="b") == b"value"
    finally:
        _WATCHING.clear()
    assert _OPENED == [], "this process opened the transit key file"
    key_path = tmp_path / "notary" / f"{_KEY_REF}.aes256"
    assert key_path.exists()
    assert stat.S_IMODE(key_path.stat().st_mode) == 0o600
    key = key_path.read_bytes()
    assert len(key) == 32
    assert all(not isinstance(getattr(cipher, slot), bytes) for slot in cipher.__slots__)
    public = sorted(name for name in dir(cipher) if not name.startswith("_"))
    assert public == ["KEY_REF", "kind", "open", "seal"]


def test_nonce_is_random_per_seal(cipher: TransitConnectorCipher) -> None:
    assert len({cipher.seal(b"same", scope="a", slot="b") for _ in range(5)}) == 5


def test_transit_unreachable_fails_closed(tmp_path: Path) -> None:
    good = TransitConnectorCipher(FileNotaryTransit(tmp_path / "notary"))
    sealed = good.seal(b"v", scope="a", slot="b")
    (tmp_path / "notary" / f"{_KEY_REF}.aes256").unlink()
    with pytest.raises(CredentialCustodyUnavailableError):
        good.open(sealed, scope="a", slot="b")
    gone = TransitConnectorCipher(FileNotaryTransit(tmp_path / "missing" / "deeper"))
    (tmp_path / "missing").write_text("not a directory")
    with pytest.raises(CredentialCustodyUnavailableError):
        gone.seal(b"v", scope="a", slot="b")


def test_unavailable_is_not_a_seal_error() -> None:
    assert not issubclass(CredentialCustodyUnavailableError, CredentialSealError)


def test_loose_key_file_is_refused(cipher: TransitConnectorCipher, tmp_path: Path) -> None:
    sealed = cipher.seal(b"v", scope="a", slot="b")
    (tmp_path / "notary" / f"{_KEY_REF}.aes256").chmod(0o644)
    with pytest.raises(CredentialCustodyUnavailableError):
        cipher.open(sealed, scope="a", slot="b")


def test_symlinked_key_file_is_refused(tmp_path: Path) -> None:
    store = tmp_path / "notary"
    store.mkdir()
    target = tmp_path / "elsewhere.bin"
    target.write_bytes(b"k" * 32)
    target.chmod(0o600)
    (store / f"{_KEY_REF}.aes256").symlink_to(target)
    with pytest.raises(CredentialCustodyUnavailableError):
        TransitConnectorCipher(FileNotaryTransit(store)).seal(b"v", scope="a", slot="b")


def test_errors_never_echo_material(cipher: TransitConnectorCipher) -> None:
    sealed = cipher.seal(b"plaintext-marker-123", scope="a", slot="b")
    with pytest.raises(CredentialSealError) as caught:
        cipher.open(sealed, scope="a", slot="c")
    text = f"{caught.value!s} {caught.value!r} {caught.value.args}"
    assert "plaintext-marker-123" not in text
    assert sealed[-20:] not in text


def test_refuses_foreign_prefix_bad_coordinates_and_oversize(
    cipher: TransitConnectorCipher, tmp_path: Path
) -> None:
    key = OperatorKey.load(tmp_path / "op" / "operator.key", generate_if_absent=True)
    xc = ConnectorSecretCipher.for_operator_key(key).seal(b"v", scope="a", slot="b")
    with pytest.raises(CredentialSealError):
        cipher.open(xc, scope="a", slot="b")
    with pytest.raises(CredentialSealError):
        cipher.seal(b"x", scope="Bad Scope", slot="b")
    with pytest.raises(CredentialSealError):
        cipher.seal(b"x" * (64 * 1024 + 1), scope="a", slot="b")
    with pytest.raises(CredentialSealError):
        cipher.open("v1.tr.vault:v1:%%%", scope="a", slot="b")


def test_two_transit_keys_do_not_open_each_other(tmp_path: Path) -> None:
    first = TransitConnectorCipher(FileNotaryTransit(tmp_path / "one"))
    second = TransitConnectorCipher(FileNotaryTransit(tmp_path / "two"))
    sealed = first.seal(b"v", scope="a", slot="b")
    second.seal(b"mint", scope="a", slot="b")  # second transit now has its own key
    with pytest.raises(CredentialSealError):
        second.open(sealed, scope="a", slot="b")


def test_fips_requires_a_validated_backend(
    transit: FileNotaryTransit, monkeypatch: pytest.MonkeyPatch
) -> None:
    with pytest.raises(ArcTrustFipsError):
        TransitConnectorCipher(transit, require_fips=True)
    monkeypatch.setattr("arctrust.fips.fips_backend_active", lambda: True)
    approved = TransitConnectorCipher(transit, require_fips=True)
    sealed = approved.seal(b"v", scope="a", slot="b")
    assert approved.open(sealed, scope="a", slot="b") == b"v"


def test_notary_transit_reports_decrypt_and_unavailable_distinctly(
    transit: FileNotaryTransit, tmp_path: Path
) -> None:
    sealed = transit.encrypt(_KEY_REF, b"v", aad=b"x")
    with pytest.raises(TransitDecryptError):
        transit.decrypt(_KEY_REF, sealed, aad=b"y")
    with pytest.raises(TransitUnavailableError):
        transit.decrypt("absent-key", sealed, aad=b"x")
    with pytest.raises(TransitUnavailableError):
        transit.encrypt("Bad Ref", b"v", aad=b"x")


def test_notary_child_never_imports_arctrust() -> None:
    import arctrust.transit_cipher as module

    child = Path(module.__file__).with_name("_notary_cipher.py").read_text()
    assert "import arctrust" not in child and "from arctrust" not in child


def test_exported_from_the_package_root() -> None:
    import arctrust

    assert arctrust.TransitConnectorCipher is TransitConnectorCipher
    assert arctrust.CredentialCustodyUnavailableError is CredentialCustodyUnavailableError
