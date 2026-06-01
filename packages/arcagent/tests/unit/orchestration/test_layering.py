"""Architecture test (SPEC-028 task 3.6) — spawn observability respects layers.

The dependency arrow points one way: arcagent → {arcrun, arcllm, arcstore}.
- Spawn lineage (``spawn_event``) is emitted by arcagent, never arcrun.
- Telemetry identity (the ``agent_identity`` contextvar) is defined by arcllm;
  arcagent only *sets* it. arcrun never learns about either concern.
"""

from __future__ import annotations

from pathlib import Path

_ARCRUN_SRC = Path(__import__("arcrun").__file__).resolve().parent
_SPAWN_SRC = Path(__import__("arcagent").__file__).resolve().parent / "orchestration" / "spawn.py"


def test_spawn_owned_by_arcagent() -> None:
    """arcrun source emits no spawn_event and contains no spawn lineage logic."""
    offenders = [
        py.relative_to(_ARCRUN_SRC).as_posix()
        for py in _ARCRUN_SRC.rglob("*.py")
        if "spawn_event" in py.read_text(encoding="utf-8")
    ]
    assert not offenders, f"spawn_event must be emitted by arcagent, not arcrun: {offenders}"


def test_spawn_emits_lineage_and_uses_arcllm_identity() -> None:
    """arcagent's spawn.py emits the spawn_event and sets (not defines) the contextvar."""
    src = _SPAWN_SRC.read_text(encoding="utf-8")
    assert 'kind="spawn_event"' in src
    # Identity contextvar is an arcllm concern that arcagent imports and uses.
    assert "from arcllm.modules.telemetry import agent_identity" in src


def test_agent_identity_defined_in_arcllm() -> None:
    """The contextvar identity primitive lives in arcllm.telemetry (not arcagent)."""
    from arcllm.modules.telemetry import agent_identity

    assert callable(agent_identity)
