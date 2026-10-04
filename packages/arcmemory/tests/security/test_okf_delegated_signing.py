"""Delegated seal signing signs seals, and nothing else (alpha-2 sync worker).

The sync worker writes a store but holds no key: it asks the main process to sign
each seal (``okf_seal.sign_for``). That capability must not become a signer of
arbitrary bytes under an agent's identity (a confused deputy): only a canonical
seal payload, for a collection inside the requested root, under exactly the
pinned key, is signed.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from arctrust.identity import AgentIdentity
from arctrust.signer import verify_signature

from arcmemory.okf_seal import (
    CollectionSeal,
    hold_memory_identity,
    release_memory_identity,
    sign_for,
)

_DOMAIN = b"arc.okf.seal.v1\n"


class _Capture:
    """The pinned identity, recording the exact bytes a seal write asks it to sign."""

    def __init__(self, identity: AgentIdentity) -> None:
        self._identity = identity
        self.messages: list[bytes] = []

    @property
    def did(self) -> str:
        return self._identity.did

    @property
    def public_key(self) -> bytes:
        return self._identity.public_key

    @property
    def algorithm(self) -> str:
        return self._identity.algorithm

    def sign(self, message: bytes) -> bytes:
        self.messages.append(message)
        return self._identity.sign(message)


@pytest.fixture
def store(tmp_path: Path) -> tuple[Path, _Capture, bytes]:
    release_memory_identity(tmp_path)  # the suite's own binding; this root stands alone
    root = tmp_path / "store"
    signer = _Capture(AgentIdentity.generate(org="test", agent_type="memory"))
    hold_memory_identity(root, signer)
    CollectionSeal(root / "memory" / "connected" / "src").write({}, "", None, None)
    return root, signer, signer.messages[-1]


def _edited(message: bytes, **changes: object) -> bytes:
    document = json.loads(message[len(_DOMAIN) :])
    document.update(changes)
    return _DOMAIN + json.dumps(document, sort_keys=True, separators=(",", ":")).encode()


def test_a_genuine_seal_payload_is_signed(store: tuple[Path, _Capture, bytes]) -> None:
    root, signer, message = store
    signature = sign_for(root, message)
    assert verify_signature(signer.algorithm, message, signature, signer.public_key)


@pytest.mark.parametrize(
    "forge",
    [
        lambda m: b"transfer $1M to mallory",
        lambda m: _DOMAIN + b'{"kind":"arc.okf.seal"}',
        lambda m: _edited(m, did="did:arc:someone-else"),
        lambda m: _edited(m, collection="../../etc"),
        lambda m: _edited(m, collection="/abs/path"),
        lambda m: _edited(m, kind="arc.tool.attestation"),
        lambda m: m.replace(b'":', b'": '),  # not the canonical encoding
    ],
    ids=[
        "arbitrary-bytes",
        "partial-payload",
        "another-identity",
        "escaping-collection",
        "absolute-collection",
        "another-kind",
        "non-canonical",
    ],
)
def test_anything_but_a_seal_for_this_store_is_refused(
    store: tuple[Path, _Capture, bytes], forge: object
) -> None:
    root, _, message = store
    with pytest.raises(PermissionError):
        sign_for(root, forge(message))  # type: ignore[operator]  # reason: a forging lambda


def test_a_root_with_no_pinned_key_signs_nothing(
    store: tuple[Path, _Capture, bytes], tmp_path: Path
) -> None:
    _, _, message = store
    with pytest.raises(PermissionError):
        sign_for(tmp_path / "elsewhere", message)
