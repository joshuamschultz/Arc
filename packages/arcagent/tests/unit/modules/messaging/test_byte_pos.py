"""SPEC-017 R-005 regression — messaging ack passes real byte_pos.

The prior bug: both the messaging tool and module path passed
``byte_pos=0`` on every ack, forcing subsequent polls to rescan the
whole JSONL stream instead of seeking past already-consumed bytes.
"""

from __future__ import annotations

import pytest


class _FakeBackend:
    """Stub that records the arguments passed to ack-adjacent calls."""

    def __init__(self, end_pos: int) -> None:
        self._end_pos = end_pos
        self.calls: list[tuple[str, str]] = []

    async def get_stream_end_byte_pos(self, collection: str, key: str) -> int:
        self.calls.append((collection, key))
        return self._end_pos


class _FakeService:
    def __init__(self, backend: _FakeBackend) -> None:
        self._backend = backend


class TestStreamEndBytePos:
    async def test_returns_real_offset_when_backend_supports_it(self) -> None:
        from arcagent.modules.messaging.tools import _stream_end_byte_pos

        backend = _FakeBackend(end_pos=1024)
        svc = _FakeService(backend)

        result = await _stream_end_byte_pos(svc, "arc.inbox.user:alpha")
        assert result == 1024
        assert backend.calls == [("streams", "arc.inbox.user:alpha")]

    async def test_falls_back_to_zero_when_backend_lacks_helper(self) -> None:
        from arcagent.modules.messaging.tools import _stream_end_byte_pos

        class _LegacyBackend:
            pass

        class _LegacyService:
            _backend = _LegacyBackend()

        result = await _stream_end_byte_pos(_LegacyService(), "any.stream")
        assert result == 0

    async def test_falls_back_on_backend_exception(self) -> None:
        from arcagent.modules.messaging.tools import _stream_end_byte_pos

        class _BrokenBackend:
            async def get_stream_end_byte_pos(self, *_: object) -> int:
                raise RuntimeError("backend down")

        class _Service:
            _backend = _BrokenBackend()

        result = await _stream_end_byte_pos(_Service(), "any.stream")
        assert result == 0


class TestBackendAPI:
    """Backend Protocol contract: ``get_stream_end_byte_pos`` returns the
    running stream length and is consistent with append semantics."""

    async def test_memory_backend_tracks_cumulative_bytes(self) -> None:
        from arcteam.storage import MemoryBackend

        backend = MemoryBackend()
        await backend.append_auto_seq("streams", "s1", {"body": "first"})
        size_after_one = await backend.get_stream_end_byte_pos("streams", "s1")
        assert size_after_one > 0

        await backend.append_auto_seq("streams", "s1", {"body": "second"})
        size_after_two = await backend.get_stream_end_byte_pos("streams", "s1")
        assert size_after_two > size_after_one

    async def test_empty_stream_returns_zero(self) -> None:
        from arcteam.storage import MemoryBackend

        backend = MemoryBackend()
        assert await backend.get_stream_end_byte_pos("streams", "nothing") == 0


# Pytest-asyncio auto mode picks up the coroutines; no markers needed.
pytestmark = pytest.mark.asyncio
