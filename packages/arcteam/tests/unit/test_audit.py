"""Tests for arcteam.audit — AuditLogger with chained HMACs."""

from __future__ import annotations

import pytest

from arcteam.audit import AuditLogger
from arcteam.storage import MemoryBackend


@pytest.fixture
async def audit() -> AuditLogger:
    backend = MemoryBackend()
    al = AuditLogger(backend, hmac_key=b"test-key")
    await al.initialize()
    return al


@pytest.fixture
def backend() -> MemoryBackend:
    return MemoryBackend()


class TestSingleRecord:
    """Single audit record: correct fields, HMAC present."""

    async def test_log_creates_record(self, audit: AuditLogger) -> None:
        await audit.log(
            event_type="message.sent",
            subject="arc.channel.ops",
            actor_id="agent://a1",
            detail="sent message to channel",
            stream="arc.channel.ops",
            msg_seq=1,
        )
        records = await audit._backend.read_stream("audit", "audit", after_seq=0)
        assert len(records) == 1
        r = records[0]
        assert r["audit_seq"] == 1
        assert r["event_type"] == "message.sent"
        assert r["actor_id"] == "agent://a1"
        assert r["hmac_sha256"] != ""
        assert r["classification"] == "UNCLASSIFIED"
        assert r["timestamp_utc"] != ""


class TestChainIntegrity:
    """Chain integrity: 10 records, verify_chain returns True."""

    async def test_valid_chain(self, audit: AuditLogger) -> None:
        for i in range(10):
            await audit.log(
                event_type=f"event_{i}",
                subject="test",
                actor_id="agent://a1",
                detail=f"record {i}",
            )
        valid, last_seq = await audit.verify_chain()
        assert valid is True
        assert last_seq == 10


class TestTamperDetection:
    """Tamper detection: modify a record, verify_chain returns False."""

    async def test_tampered_record(self, backend: MemoryBackend) -> None:
        audit = AuditLogger(backend, hmac_key=b"test-key")
        await audit.initialize()

        for i in range(5):
            await audit.log(
                event_type=f"event_{i}",
                subject="test",
                actor_id="agent://a1",
                detail=f"record {i}",
            )

        # Tamper with the 3rd record
        records = backend._streams["audit"]["audit"]
        records[2]["detail"] = "TAMPERED"

        valid, last_seq = await audit.verify_chain()
        assert valid is False
        assert last_seq == 2  # Verified up to record before tampering


class TestGapDetection:
    """Gap detection: delete a record, verify_chain detects gap."""

    async def test_sequence_gap(self, backend: MemoryBackend) -> None:
        audit = AuditLogger(backend, hmac_key=b"test-key")
        await audit.initialize()

        for i in range(5):
            await audit.log(
                event_type=f"event_{i}",
                subject="test",
                actor_id="agent://a1",
                detail=f"record {i}",
            )

        # Remove the 3rd record (index 2), creating a gap in audit_seq
        del backend._streams["audit"]["audit"][2]

        valid, last_seq = await audit.verify_chain()
        assert valid is False
        assert last_seq == 2  # Last verified before gap


class TestHMACKeyFromEnv:
    """HMAC key from environment variable."""

    def test_load_key_from_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ARCTEAM_HMAC_KEY", "my-secret-key")
        key = AuditLogger.load_hmac_key("ARCTEAM_HMAC_KEY")
        assert key == b"my-secret-key"

    def test_load_key_missing_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("ARCTEAM_HMAC_KEY", raising=False)
        key = AuditLogger.load_hmac_key("ARCTEAM_HMAC_KEY")
        assert key is None

    async def test_session_key_warning(self, backend: MemoryBackend) -> None:
        """When no key provided, uses random session key with warning."""
        audit = AuditLogger(backend)  # No key
        await audit.initialize()
        await audit.log(
            event_type="test",
            subject="test",
            actor_id="agent://a1",
            detail="test",
        )
        valid, _ = await audit.verify_chain()
        assert valid is True  # Session key works within same instance
