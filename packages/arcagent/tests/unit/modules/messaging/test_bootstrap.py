"""Unit tests for the messaging module's arcteam service bootstrap."""

from __future__ import annotations

from arctrust import AgentIdentity

from arcagent.modules.messaging import _bootstrap


def _full_identity() -> AgentIdentity:
    return AgentIdentity.generate(org="local", agent_type="agent")


def _verify_only_identity() -> AgentIdentity:
    """An identity with a public key but no signing key (cannot sign)."""
    full = _full_identity()
    return AgentIdentity(did=full.did, public_key=full.public_key, _signing_key=None)


class TestMessageSigner:
    def test_signing_identity_yields_signer_from_identity(self) -> None:
        """A signing identity produces a MessageSigner via from_identity."""
        from arcteam.crypto import MessageSigner

        ident = _full_identity()
        signer = _bootstrap.message_signer(ident)

        assert isinstance(signer, MessageSigner)
        assert signer.did == ident.did
        assert signer.private_key == ident.signing_seed

    def test_none_identity_returns_none(self) -> None:
        assert _bootstrap.message_signer(None) is None

    def test_verify_only_identity_returns_none(self) -> None:
        """A verify-only identity has no seed to sign with — no signer."""
        assert _bootstrap.message_signer(_verify_only_identity()) is None
