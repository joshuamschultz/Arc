"""The sleep loop must obey the operator kill switch.

``ARC_MEMORY_CONSOLIDATE_OFF`` stops both the index-refresh embeds and the
consolidation LLM pass without disabling capture or recall — the "stop the bleed
while we fix the sleep path" switch.
"""

from __future__ import annotations

import asyncio

import pytest

from arcagent.modules.memory import capabilities as cap

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def test_off_switch_reads_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(cap._CONSOLIDATE_OFF_ENV, raising=False)
    assert cap._consolidation_off() is False
    for truthy in ("1", "true", "YES", "on"):
        monkeypatch.setenv(cap._CONSOLIDATE_OFF_ENV, truthy)
        assert cap._consolidation_off() is True
    for falsy in ("0", "", "off", "no"):
        monkeypatch.setenv(cap._CONSOLIDATE_OFF_ENV, falsy)
        assert cap._consolidation_off() is False


class _LoopStop(Exception):  # noqa: N818 — test sentinel, not a real error
    """Break the `while True` after exactly one iteration."""


async def _one_iteration(monkeypatch: pytest.MonkeyPatch) -> dict[str, int]:
    calls = {"refresh": 0, "consolidate": 0}

    async def fake_refresh() -> None:
        calls["refresh"] += 1

    async def fake_poll(*_a: object, **_k: object) -> bool:
        calls["consolidate"] += 1
        return False

    async def fake_sleep(_seconds: float) -> None:
        raise _LoopStop

    monkeypatch.setattr(cap, "refresh_index_once", fake_refresh)
    monkeypatch.setattr(cap, "consolidate_poll_once", fake_poll)
    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    with pytest.raises(_LoopStop):
        await cap.memory_consolidate_loop(None)
    return calls


async def test_loop_skips_all_work_when_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(cap._CONSOLIDATE_OFF_ENV, "1")
    calls = await _one_iteration(monkeypatch)
    assert calls == {"refresh": 0, "consolidate": 0}  # no embeds, no consolidation


async def test_loop_does_its_work_when_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(cap._CONSOLIDATE_OFF_ENV, raising=False)
    calls = await _one_iteration(monkeypatch)
    assert calls == {"refresh": 1, "consolidate": 1}
