"""SPEC-037 F4 — the messaging audit chain is signed by the OPERATOR, not the agent.

The prior ``audit_signer`` minted the chain with the AGENT DID seed (with an
ephemeral-random fallback) — the SPEC-053 anti-pattern (audited subject = audit
authority). The chain must now sign with the resolved operator ``Signer``.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from arctrust import AgentIdentity, OperatorKey

from arcagent.modules.messaging import _runtime
from tests.unit.modules.messaging.conftest import make_config_dict


def _identity() -> AgentIdentity:
    return AgentIdentity.generate(org="local", agent_type="agent")


class TestMessagingAuditAuthority:
    def test_audit_chain_signed_by_operator_not_agent(self, tmp_path: Path) -> None:
        agent = _identity()
        operator = OperatorKey.generate()
        assert operator.public_key != agent.public_key
        try:
            _runtime.configure(
                config=make_config_dict(),
                workspace=tmp_path,
                identity=agent,
                operator_signer=operator.into_signer(),
            )
            audit_signer = _runtime.state().svc._audit._signer
            # The messaging audit chain signs with the OPERATOR authority, never
            # the agent DID seed and never an ephemeral key.
            assert audit_signer.public_key == operator.public_key
            assert audit_signer.public_key != agent.public_key
        finally:
            _runtime.reset()

    def test_missing_operator_signer_fails_closed(self, tmp_path: Path) -> None:
        """No operator signer → refuse to audit with a repudiable key (F4)."""
        with pytest.raises(ValueError, match="operator signer"):
            _runtime.configure(
                config=make_config_dict(),
                workspace=tmp_path,
                identity=_identity(),
                operator_signer=None,
            )
        _runtime.reset()
