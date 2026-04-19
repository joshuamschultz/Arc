"""Unit tests for Capability model and CapabilityStore.

Covers T2.3 (capability issue/verify/expire) and test contract items:
- test_capability_per_turn (item 8)
- test_capability_signed_and_verifiable (item 9)
"""

from __future__ import annotations

import time

import pytest

from arcagent.core.identity import AgentIdentity
from arcagent.modules.memory_acl.capabilities import Capability, CapabilityStore
from arcagent.modules.memory_acl.errors import CapabilityExpired, CapabilityInvalid

# Fixed capability_id used when testing canonical_bytes to avoid UUID randomness
_FIXED_CAP_ID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _store_with_identity() -> tuple[CapabilityStore, AgentIdentity]:
    identity = AgentIdentity.generate(org="test", agent_type="tester")
    store = CapabilityStore(identity=identity)
    return store, identity


def _store_no_identity() -> CapabilityStore:
    return CapabilityStore(identity=None)


def _issue(store: CapabilityStore, *, turn_id: str = "turn-1", ttl: float = 3600.0) -> Capability:
    return store.issue(
        caller_module="test_module",
        target_resource="user:did:arc:org:user/abc:profile",
        allowed_actions=["read"],
        turn_id=turn_id,
        ttl_seconds=ttl,
    )


def _fixed_cap(**kwargs: object) -> Capability:
    """Build a Capability with a fixed capability_id for deterministic tests."""
    defaults: dict[str, object] = {
        "capability_id": _FIXED_CAP_ID,
        "caller_module": "mod",
        "target_resource": "user:x",
        "allowed_actions": ["read"],
        "turn_id": "t1",
        "issued_at": 1.0,
        "expires_at": 2.0,
    }
    defaults.update(kwargs)
    return Capability(**defaults)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Capability model
# ---------------------------------------------------------------------------


class TestCapabilityModel:
    def test_canonical_bytes_deterministic(self) -> None:
        cap = _fixed_cap()
        assert cap.canonical_bytes() == cap.canonical_bytes()

    def test_canonical_bytes_excludes_signature(self) -> None:
        # Same capability_id ensures canonical_bytes differ only if signature
        # were included. Mutable model: set signature, record bytes, clear, compare.
        cap = _fixed_cap(signature="deadbeef")
        bytes_with_sig = cap.canonical_bytes()
        cap.signature = ""
        bytes_no_sig = cap.canonical_bytes()
        # canonical_bytes must be identical regardless of signature field
        assert bytes_with_sig == bytes_no_sig

    def test_is_expired_true(self) -> None:
        cap = _fixed_cap(issued_at=0.0, expires_at=0.0)
        assert cap.is_expired(now=1.0) is True

    def test_is_expired_false(self) -> None:
        cap = Capability(
            caller_module="mod",
            target_resource="user:x",
            allowed_actions=["read"],
            turn_id="t1",
            issued_at=time.monotonic(),
            expires_at=time.monotonic() + 9999,
        )
        assert cap.is_expired() is False

    def test_allows_action(self) -> None:
        cap = Capability(
            caller_module="mod",
            target_resource="user:x",
            allowed_actions=["read", "search"],
            turn_id="t1",
            issued_at=0.0,
            expires_at=9999999.0,
        )
        assert cap.allows("read") is True
        assert cap.allows("search") is True
        assert cap.allows("write") is False


# ---------------------------------------------------------------------------
# Test item 8: test_capability_per_turn
# ---------------------------------------------------------------------------


class TestCapabilityPerTurn:
    def test_capability_valid_within_turn(self) -> None:
        store = _store_no_identity()
        cap = _issue(store, turn_id="turn-1")
        # Should not raise
        assert store.verify(cap) is True

    def test_capability_invalid_after_turn_revoked(self) -> None:
        store = _store_no_identity()
        cap = _issue(store, turn_id="turn-1")
        store.revoke_turn("turn-1")
        with pytest.raises(CapabilityInvalid, match="revoked"):
            store.verify(cap)

    def test_capability_expired_after_ttl(self) -> None:
        store = _store_no_identity()
        cap = _issue(store, turn_id="turn-1", ttl=0.001)
        time.sleep(0.01)
        with pytest.raises(CapabilityExpired):
            store.verify(cap)

    def test_next_turn_does_not_see_previous_turn_capability(self) -> None:
        store = _store_no_identity()
        cap_t1 = _issue(store, turn_id="turn-1")
        store.revoke_turn("turn-1")

        # New turn capability
        cap_t2 = _issue(store, turn_id="turn-2")
        assert store.verify(cap_t2) is True

        # Old capability is revoked
        with pytest.raises(CapabilityInvalid):
            store.verify(cap_t1)

    def test_revoke_turn_returns_count(self) -> None:
        store = _store_no_identity()
        _issue(store, turn_id="turn-42")
        _issue(store, turn_id="turn-42")
        count = store.revoke_turn("turn-42")
        assert count == 2

    def test_revoke_other_turn_does_not_affect_current(self) -> None:
        store = _store_no_identity()
        cap_current = _issue(store, turn_id="turn-current")
        _issue(store, turn_id="turn-other")
        store.revoke_turn("turn-other")
        assert store.verify(cap_current) is True


# ---------------------------------------------------------------------------
# Test item 9: test_capability_signed_and_verifiable
# ---------------------------------------------------------------------------


class TestCapabilitySignedAndVerifiable:
    def test_issued_capability_has_signature(self) -> None:
        store, _ = _store_with_identity()
        cap = _issue(store)
        assert cap.signature != "", "Capability must have a signature when identity is present"

    def test_signature_verifies(self) -> None:
        store, _ = _store_with_identity()
        cap = _issue(store)
        # Verify should not raise
        assert store.verify(cap) is True

    def test_tampered_canonical_bytes_fails_verification(self) -> None:
        store, identity = _store_with_identity()
        cap = _issue(store)
        # Tamper: change the target_resource after signing
        cap.target_resource = "user:did:arc:org:user/VICTIM:profile"
        with pytest.raises(CapabilityInvalid, match="signature mismatch"):
            store.verify(cap)

    def test_tampered_signature_fails_verification(self) -> None:
        store, _ = _store_with_identity()
        cap = _issue(store)
        cap.signature = "00" * 64  # Wrong bytes
        with pytest.raises(CapabilityInvalid):
            store.verify(cap)

    def test_malformed_signature_hex_raises_invalid(self) -> None:
        store, _ = _store_with_identity()
        cap = _issue(store)
        cap.signature = "not-valid-hex"
        with pytest.raises(CapabilityInvalid, match="malformed hex signature"):
            store.verify(cap)

    def test_no_identity_store_issues_without_signature(self) -> None:
        store = _store_no_identity()
        cap = _issue(store)
        # No signature when identity is absent
        assert cap.signature == ""

    def test_no_identity_store_verify_still_passes_without_sig(self) -> None:
        store = _store_no_identity()
        cap = _issue(store)
        assert store.verify(cap) is True


# ---------------------------------------------------------------------------
# CapabilityStore.has_valid_capability (defense-in-depth test item 10)
# ---------------------------------------------------------------------------


class TestHasValidCapability:
    def test_returns_true_when_capability_exists(self) -> None:
        store = _store_no_identity()
        _issue(store, turn_id="t1")
        result = store.has_valid_capability(
            caller_module="test_module",
            target_resource="user:did:arc:org:user/abc:profile",
            action="read",
            turn_id="t1",
        )
        assert result is True

    def test_returns_false_when_action_not_covered(self) -> None:
        store = _store_no_identity()
        _issue(store, turn_id="t1")
        result = store.has_valid_capability(
            caller_module="test_module",
            target_resource="user:did:arc:org:user/abc:profile",
            action="write",
            turn_id="t1",
        )
        assert result is False

    def test_returns_false_when_turn_revoked(self) -> None:
        store = _store_no_identity()
        _issue(store, turn_id="t1")
        store.revoke_turn("t1")
        result = store.has_valid_capability(
            caller_module="test_module",
            target_resource="user:did:arc:org:user/abc:profile",
            action="read",
            turn_id="t1",
        )
        assert result is False

    def test_returns_false_when_different_resource(self) -> None:
        store = _store_no_identity()
        _issue(store, turn_id="t1")
        result = store.has_valid_capability(
            caller_module="test_module",
            target_resource="user:did:arc:org:user/DIFFERENT:profile",
            action="read",
            turn_id="t1",
        )
        assert result is False


# ---------------------------------------------------------------------------
# CapabilityStore cleanup
# ---------------------------------------------------------------------------


class TestCapabilityStoreCleanup:
    def test_clear_expired_removes_expired(self) -> None:
        store = _store_no_identity()
        _issue(store, turn_id="t1", ttl=0.001)
        time.sleep(0.01)
        removed = store.clear_expired()
        assert removed >= 1

    def test_clear_expired_keeps_active(self) -> None:
        store = _store_no_identity()
        _issue(store, turn_id="t1", ttl=9999.0)
        removed = store.clear_expired()
        assert removed == 0
