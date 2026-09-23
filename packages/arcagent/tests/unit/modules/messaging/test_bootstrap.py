"""Unit tests for the messaging module's arcteam service bootstrap."""

from __future__ import annotations

import logging

import pytest
from arcteam import FleetBackendUnavailableError
from arcteam import composition as _bootstrap
from arctrust import AgentIdentity


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


class TestMakeBackend:
    """Configured NATS failures remain visible; standalone mode uses memory."""

    @pytest.mark.asyncio
    async def test_empty_url_returns_memory_backend_no_warning(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        from arcteam.storage import MemoryBackend

        with caplog.at_level(logging.WARNING, logger="arcagent.modules.messaging"):
            backend = await _bootstrap.make_backend("")

        assert isinstance(backend, MemoryBackend)
        assert caplog.records == []

    @pytest.mark.asyncio
    async def test_connection_refused_does_not_substitute_memory(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A refused fleet connection cannot appear as a working local bus."""
        from arcteam.backends import nats as nats_backend

        async def _refuse(_servers: str) -> object:
            raise ConnectionRefusedError(61, "Connection refused")

        monkeypatch.setattr(nats_backend.NatsBackend, "connect", staticmethod(_refuse))

        with caplog.at_level(logging.WARNING, logger="arcteam.composition"):
            with pytest.raises(FleetBackendUnavailableError):
                await _bootstrap.make_backend("nats://127.0.0.1:4222")

        assert "in-memory" not in caplog.text

    @pytest.mark.asyncio
    async def test_timeout_is_visible(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A bounded-connect timeout remains a connection failure."""

        from arcteam.backends import nats as nats_backend

        async def _timeout(_servers: str) -> object:
            raise TimeoutError

        monkeypatch.setattr(nats_backend.NatsBackend, "connect", staticmethod(_timeout))

        with pytest.raises(FleetBackendUnavailableError):
            await _bootstrap.make_backend("nats://127.0.0.1:4222")

    @pytest.mark.asyncio
    async def test_unexpected_error_still_surfaces(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Only connection errors degrade quietly; an unexpected error propagates."""
        from arcteam.backends import nats as nats_backend

        async def _boom(_servers: str) -> object:
            raise ValueError("bug in bootstrap")

        monkeypatch.setattr(nats_backend.NatsBackend, "connect", staticmethod(_boom))

        with pytest.raises(ValueError, match="bug in bootstrap"):
            await _bootstrap.make_backend("nats://127.0.0.1:4222")
