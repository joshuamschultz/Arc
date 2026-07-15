"""SPEC-021 Task 1.8 — TofuLayer policy gate (R-042 / R-043).

The TOFU layer is a single function: ``evaluate(target) -> TofuDecision``.
Three deployment tiers (passed as plain strings so arctrust stays independent
of arcagent's ``Tier`` enum), three behaviour profiles:

  * **personal** — ``auto_run_agent_code`` toggle is the only gate
  * **enterprise** — first sight prompts via ``NEW_SIGHTING``;
    persisted hash matches → ``ALLOW``; mismatch → ``DENY`` (tamper)
  * **federal** — a valid signature is the floor, then the same human-approval
    gate as enterprise

The trust file is read-only at the agent layer; updates flow through
``arc trust approve``. The agent never writes to it.
"""

from __future__ import annotations

import hashlib

import pytest

from arctrust import (
    CapabilitySource,
    TofuDecision,
    TofuLayer,
    ValidatorEntry,
    ValidatorsConfig,
)

_PERSONAL = "personal"
_ENTERPRISE = "enterprise"
_FEDERAL = "federal"


def _hash(source: str) -> str:
    return "sha256:" + hashlib.sha256(source.encode("utf-8")).hexdigest()


@pytest.fixture
def some_source() -> str:
    return "async def fn(): return 42\n"


class TestPersonalTier:
    def test_auto_run_true_allows(self, some_source: str) -> None:
        layer = TofuLayer(
            tier=_PERSONAL,
            validators=ValidatorsConfig(auto_run_agent_code=True),
        )
        target = CapabilitySource(name="t", source=some_source)
        assert layer.evaluate(target) == TofuDecision.ALLOW

    def test_auto_run_false_denies(self, some_source: str) -> None:
        layer = TofuLayer(
            tier=_PERSONAL,
            validators=ValidatorsConfig(auto_run_agent_code=False),
        )
        target = CapabilitySource(name="t", source=some_source)
        assert layer.evaluate(target) == TofuDecision.DENY

    def test_signed_source_allows_even_with_auto_run_false(self, some_source: str) -> None:
        """Signed agent-authored code loads at personal tier without the
        auto_run_agent_code opt-in.

        The signature IS the trust boundary here: CapabilityLoader only ever
        sets signed=True after re-verifying the artifact against the AGENT'S
        OWN pinned identity key (agent_lifecycle.py's trusted_pubkey =
        agent._identity.public_key) — an attacker who can write files into
        the workspace cannot forge this without the agent's private key, so
        auto-allowing signed sources is still fail-closed for unattributed
        code.
        """
        layer = TofuLayer(
            tier=_PERSONAL,
            validators=ValidatorsConfig(auto_run_agent_code=False),
        )
        target = CapabilitySource(name="t", source=some_source, signed=True)
        assert layer.evaluate(target) == TofuDecision.ALLOW

    def test_unsigned_source_still_denies_by_default(self, some_source: str) -> None:
        """Unsigned code is unaffected by the signed-source relaxation above —
        still gated by the explicit auto_run_agent_code toggle (fail-closed
        default preserved)."""
        layer = TofuLayer(
            tier=_PERSONAL,
            validators=ValidatorsConfig(auto_run_agent_code=False),
        )
        target = CapabilitySource(name="t", source=some_source, signed=False)
        assert layer.evaluate(target) == TofuDecision.DENY


class TestEnterpriseTier:
    def test_unknown_name_emits_new_sighting(self, some_source: str) -> None:
        layer = TofuLayer(tier=_ENTERPRISE, validators=ValidatorsConfig())
        target = CapabilitySource(name="brand-new", source=some_source)
        assert layer.evaluate(target) == TofuDecision.NEW_SIGHTING

    def test_approved_hash_allows(self, some_source: str) -> None:
        layer = TofuLayer(
            tier=_ENTERPRISE,
            validators=ValidatorsConfig(
                approved=(
                    ValidatorEntry(
                        name="known",
                        hash=_hash(some_source),
                        approver="alice@example.com",
                        timestamp="2026-04-28T00:00:00Z",
                    ),
                )
            ),
        )
        target = CapabilitySource(name="known", source=some_source)
        assert layer.evaluate(target) == TofuDecision.ALLOW

    def test_tampered_hash_denies(self, some_source: str) -> None:
        layer = TofuLayer(
            tier=_ENTERPRISE,
            validators=ValidatorsConfig(
                approved=(
                    ValidatorEntry(
                        name="known",
                        hash=_hash(some_source),
                        approver="alice@example.com",
                        timestamp="2026-04-28T00:00:00Z",
                    ),
                )
            ),
        )
        # Same name, different content
        tampered = CapabilitySource(name="known", source=some_source + "x")
        assert layer.evaluate(tampered) == TofuDecision.DENY


class TestFederalTier:
    def test_unsigned_always_denied(self, some_source: str) -> None:
        layer = TofuLayer(tier=_FEDERAL, validators=ValidatorsConfig())
        target = CapabilitySource(name="x", source=some_source, signed=False)
        assert layer.evaluate(target) == TofuDecision.DENY

    def test_signed_first_sight_is_new_sighting_not_allow(self, some_source: str) -> None:
        """A self-signature proves attribution, NOT authorization. Federal must
        route unknown signed code through the human approval gate, exactly like
        enterprise — never auto-allow a compromised agent's own new tool."""
        layer = TofuLayer(tier=_FEDERAL, validators=ValidatorsConfig())
        target = CapabilitySource(name="x", source=some_source, signed=True)
        assert layer.evaluate(target) == TofuDecision.NEW_SIGHTING

    def test_signed_and_approved_hash_allows(self, some_source: str) -> None:
        layer = TofuLayer(
            tier=_FEDERAL,
            validators=ValidatorsConfig(
                approved=(
                    ValidatorEntry(
                        name="known",
                        hash=_hash(some_source),
                        approver="alice@example.com",
                        timestamp="2026-04-28T00:00:00Z",
                    ),
                )
            ),
        )
        target = CapabilitySource(name="known", source=some_source, signed=True)
        assert layer.evaluate(target) == TofuDecision.ALLOW

    def test_signed_but_unapproved_hash_drifts_to_deny(self, some_source: str) -> None:
        layer = TofuLayer(
            tier=_FEDERAL,
            validators=ValidatorsConfig(
                approved=(
                    ValidatorEntry(
                        name="known",
                        hash=_hash(some_source),
                        approver="alice@example.com",
                        timestamp="2026-04-28T00:00:00Z",
                    ),
                )
            ),
        )
        # Approved name, signed, but content drifted → hard stop.
        target = CapabilitySource(name="known", source=some_source + "x", signed=True)
        assert layer.evaluate(target) == TofuDecision.DENY

    def test_approved_hash_but_unsigned_still_denied(self, some_source: str) -> None:
        """Signature is the floor at federal — an approved hash cannot rescue
        an unsigned artifact."""
        layer = TofuLayer(
            tier=_FEDERAL,
            validators=ValidatorsConfig(
                approved=(
                    ValidatorEntry(
                        name="known",
                        hash=_hash(some_source),
                        approver="alice@example.com",
                        timestamp="2026-04-28T00:00:00Z",
                    ),
                )
            ),
        )
        target = CapabilitySource(name="known", source=some_source, signed=False)
        assert layer.evaluate(target) == TofuDecision.DENY
