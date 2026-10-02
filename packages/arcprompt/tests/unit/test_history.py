"""PromptHistory — append-only signed version store (J2 F3)."""

from __future__ import annotations

from pathlib import Path

import pytest
from arctrust.artifact import sign_artifact
from arctrust.keypair import generate_keypair

from arcprompt import (
    PromptHistory,
    PromptMissing,
    PromptUnsigned,
    PromptVersionMissing,
    SignatureVerifier,
    record_if_unseen,
)


def _signed(data: bytes) -> tuple[bytes, str, bytes]:
    pair = generate_keypair()
    manifest = sign_artifact(data, signer_did="did:arc:op", private_key=pair.private_key)
    return data, manifest.to_json(), pair.public_key


def test_record_numbers_versions_and_lists_them_oldest_first(tmp_path: Path) -> None:
    history = PromptHistory(tmp_path, "pkg", "name")
    first = history.record(*_signed(b"one")[:2])
    second = history.record(*_signed(b"two")[:2])
    assert (first.version, second.version) == (1, 2)
    assert [v.version for v in history.versions()] == [1, 2]


def test_record_never_overwrites_an_existing_version(tmp_path: Path) -> None:
    history = PromptHistory(tmp_path, "pkg", "name")
    history.record(*_signed(b"one")[:2])
    stored = tmp_path / "context" / ".history" / "pkg" / "name" / "000001.md"
    stored.with_name("000001.md.arcsig").unlink()  # damaged entry: text without a signature
    history.record(*_signed(b"two")[:2])
    assert stored.read_bytes() == b"one"
    assert [v.version for v in history.versions()] == [2]


def test_read_verified_returns_bytes_only_when_signature_holds(tmp_path: Path) -> None:
    data, sig, public_key = _signed(b"trusted")
    history = PromptHistory(tmp_path, "pkg", "name")
    history.record(data, sig)
    assert history.read_verified(1, SignatureVerifier(public_key)) == b"trusted"
    other = SignatureVerifier(_signed(b"x")[2])
    with pytest.raises(PromptUnsigned):
        history.read_verified(1, other)


def test_tampered_stored_bytes_fail_verification(tmp_path: Path) -> None:
    data, sig, public_key = _signed(b"trusted")
    history = PromptHistory(tmp_path, "pkg", "name")
    history.record(data, sig)
    (tmp_path / "context" / ".history" / "pkg" / "name" / "000001.md").write_bytes(b"forged")
    with pytest.raises(PromptUnsigned):
        history.read_verified(1, SignatureVerifier(public_key))


def test_resolve_ref_takes_a_number_or_a_unique_sha_prefix(tmp_path: Path) -> None:
    history = PromptHistory(tmp_path, "pkg", "name")
    one = history.record(*_signed(b"one")[:2])
    history.record(*_signed(b"two")[:2])
    assert history.resolve_ref("2") == 2
    assert history.resolve_ref(one.sha256[7:15]) == 1
    with pytest.raises(PromptVersionMissing):
        history.resolve_ref("9")
    with pytest.raises(PromptVersionMissing):
        history.resolve_ref("")


def test_record_if_unseen_skips_a_repeat_of_the_newest_version(tmp_path: Path) -> None:
    history = PromptHistory(tmp_path, "pkg", "name")
    data, sig, _ = _signed(b"same")
    record_if_unseen(history, data, sig)
    record_if_unseen(history, data, sig)
    assert len(history.versions()) == 1


@pytest.mark.parametrize("bad", ["../x", "a/b", "a\\b", "", ".."])
def test_unsafe_names_are_refused(tmp_path: Path, bad: str) -> None:
    with pytest.raises(PromptMissing):
        PromptHistory(tmp_path, bad, "name")
    with pytest.raises(PromptMissing):
        PromptHistory(tmp_path, "pkg", bad)
