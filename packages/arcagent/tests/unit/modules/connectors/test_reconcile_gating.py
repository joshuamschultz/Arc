"""The connector reconcile timer must re-attach only when grants change.

Re-attaching every connection on the 5-second timer reloaded every bundle,
re-read every secret, re-verified every signature, and tore down every source
adapter — a fleet-wide hot loop. These tests pin the gate: an unchanged grant
set is a no-op, a changed one reconciles, an unreadable one is left alone, and
the queue drain still runs every tick.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from arcagent.core.errors import ExtensionError
from arcagent.extension.grants import Connection
from arcagent.modules.connectors import capabilities as cap

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _wire_cycle(monkeypatch: pytest.MonkeyPatch, cur: cap.Connectors) -> dict[str, int]:
    """Stub reconcile/drain/state so _reconcile_cycle can run in isolation."""
    calls = {"reconcile": 0, "drain": 0}

    async def fake_reconcile() -> None:
        calls["reconcile"] += 1

    async def fake_drain() -> None:
        calls["drain"] += 1

    monkeypatch.setattr(cap._runtime, "state", lambda: SimpleNamespace())
    monkeypatch.setattr(cur, "reconcile", fake_reconcile)
    monkeypatch.setattr(cur, "_drain_reconcile_commands", fake_drain)
    return calls


async def test_cycle_reconciles_only_on_signature_change(monkeypatch: pytest.MonkeyPatch) -> None:
    cur = cap.Connectors()
    calls = _wire_cycle(monkeypatch, cur)
    signatures = iter(["A", "A", "B", "B"])
    monkeypatch.setattr(cap, "_grant_signature", lambda state: next(signatures))

    await cur._reconcile_cycle()  # None -> "A": changed
    await cur._reconcile_cycle()  # "A" -> "A": unchanged
    await cur._reconcile_cycle()  # "A" -> "B": changed
    await cur._reconcile_cycle()  # "B" -> "B": unchanged

    assert calls["reconcile"] == 2  # only the two real changes rebuilt
    assert calls["drain"] == 4  # the queue is drained every tick regardless


async def test_cycle_keeps_connections_when_grants_unreadable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cur = cap.Connectors()
    cur._grant_signature = "A"  # something is attached
    calls = _wire_cycle(monkeypatch, cur)
    monkeypatch.setattr(cap, "_grant_signature", lambda state: None)

    await cur._reconcile_cycle()

    assert calls["reconcile"] == 0  # a transient read blip must not tear down live grants
    assert cur._grant_signature == "A"  # signature preserved
    assert calls["drain"] == 1


def _state(tmp_path: Path) -> SimpleNamespace:
    return SimpleNamespace(arc_dir=tmp_path, agent_dir=tmp_path / "josh")


def test_signature_is_stable_and_moves_with_the_grant_set(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    grants: dict[str, Connection] = {"gh": Connection(extension="github", agents=("josh",))}

    class _Reg:
        def __init__(self, _arc_dir: object) -> None:
            pass

        def granted_to(self, _agent: str) -> dict[str, Connection]:
            return dict(grants)

    monkeypatch.setattr(cap, "ConnectionRegistry", _Reg)
    state = _state(tmp_path)

    first = cap._grant_signature(state)
    assert first is not None
    assert cap._grant_signature(state) == first  # same grants -> same fingerprint

    grants["dbx"] = Connection(extension="dropbox", agents=("josh",))
    assert cap._grant_signature(state) != first  # a new grant moves it


def test_signature_none_when_grants_unreadable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    class _Reg:
        def __init__(self, _arc_dir: object) -> None:
            pass

        def granted_to(self, _agent: str) -> dict[str, Connection]:
            raise ExtensionError("unreadable", "connections file is corrupt")

    monkeypatch.setattr(cap, "ConnectionRegistry", _Reg)
    assert cap._grant_signature(_state(tmp_path)) is None
