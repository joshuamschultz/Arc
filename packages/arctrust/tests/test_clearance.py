"""SPEC-038 — clearance bound to identity + delegation narrowing."""

from __future__ import annotations

from arctrust.classification import Classification
from arctrust.identity import AgentIdentity, derive_child_identity


class TestIdentityClearance:
    def test_default_clearance_is_unclassified(self) -> None:
        ident = AgentIdentity.generate(org="test", agent_type="executor")
        assert ident.clearance == Classification.UNCLASSIFIED

    def test_clearance_reported(self) -> None:
        ident = AgentIdentity.generate(org="test", agent_type="executor")
        cleared = AgentIdentity(
            did=ident.did,
            public_key=ident.public_key,
            _signing_key=None,
            clearance=Classification.SECRET,
        )
        assert cleared.clearance == Classification.SECRET


class TestDelegationNarrowing:
    def test_child_clamped_to_parent(self) -> None:
        parent = AgentIdentity.generate(org="test", agent_type="executor")
        child = derive_child_identity(
            parent_sk_bytes=parent.signing_seed,
            spawn_id="spawn-1",
            parent_clearance=Classification.SECRET,
            requested_clearance=Classification.TOP_SECRET,
        )
        assert child.clearance == Classification.SECRET

    def test_cui_parent_cannot_mint_secret(self) -> None:
        parent = AgentIdentity.generate(org="test", agent_type="executor")
        child = derive_child_identity(
            parent_sk_bytes=parent.signing_seed,
            spawn_id="spawn-2",
            parent_clearance=Classification.CUI,
            requested_clearance=Classification.SECRET,
        )
        assert child.clearance == Classification.CUI

    def test_default_child_inherits_unclassified(self) -> None:
        parent = AgentIdentity.generate(org="test", agent_type="executor")
        child = derive_child_identity(
            parent_sk_bytes=parent.signing_seed,
            spawn_id="spawn-3",
        )
        assert child.clearance == Classification.UNCLASSIFIED
