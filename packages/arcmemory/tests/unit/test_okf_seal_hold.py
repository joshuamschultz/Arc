"""Counted seal bindings for a root several owners share (alpha-2 D7).

A connection's shared store is opened by every subscribed agent in a process.
Each owner holds the store principal's key while it uses the store; the key is
unbound only when the LAST holder lets go, so one agent's teardown never leaves
another agent's writer unable to sign.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from arctrust.identity import AgentIdentity

from arcmemory.okf_seal import CollectionSeal, hold_memory_identity, release_memory_identity


@pytest.fixture(autouse=True)
def _no_ancestor_binding(tmp_path: Path) -> None:
    # The suite binds an agent key to tmp_path; the shared root must stand alone.
    release_memory_identity(tmp_path)


def test_the_last_holder_unbinds(tmp_path: Path) -> None:
    root = tmp_path / "shared" / "store"
    signer = AgentIdentity.generate(org="test", agent_type="knowledge")
    seal = CollectionSeal(root / "memory")

    first = hold_memory_identity(root, signer)
    second = hold_memory_identity(root, signer)
    assert seal.can_sign

    first()
    first()  # a release is idempotent: it drops its own hold once
    assert seal.can_sign, "another holder still uses the store"

    second()
    assert not seal.can_sign


def test_a_hold_under_another_key_replaces_the_binding(tmp_path: Path) -> None:
    root = tmp_path / "store"
    old = hold_memory_identity(root, AgentIdentity.generate(org="test", agent_type="a"))
    new = hold_memory_identity(root, AgentIdentity.generate(org="test", agent_type="b"))

    old()  # the stale holder's release must not unbind the new key
    assert CollectionSeal(root / "memory").can_sign

    new()
    assert not CollectionSeal(root / "memory").can_sign
