"""Architecture test (SPEC-028 task 3.6) — spawn observability respects layers.

The model dependency arrow points one way: arcagent → arcrun → arcllm.
- Spawn lineage (``spawn_event``) is emitted by arcagent, never arcrun.
- Telemetry identity is set through ArcRun's public model boundary.
"""

from __future__ import annotations

from pathlib import Path

_ARCRUN_SRC = Path(__import__("arcrun").__file__).resolve().parent
_SPAWN_SRC = Path(__import__("arcagent").__file__).resolve().parent / "orchestration" / "spawn.py"
_OBSERVABILITY_SRC = _SPAWN_SRC.with_name("spawn_observability.py")


def test_spawn_owned_by_arcagent() -> None:
    """arcrun source emits no spawn_event and contains no spawn lineage logic."""
    offenders = [
        py.relative_to(_ARCRUN_SRC).as_posix()
        for py in _ARCRUN_SRC.rglob("*.py")
        if "spawn_event" in py.read_text(encoding="utf-8")
    ]
    assert not offenders, f"spawn_event must be emitted by arcagent, not arcrun: {offenders}"


def test_spawn_emits_lineage_and_uses_arcrun_identity() -> None:
    """arcagent's spawn.py emits the spawn_event and sets (not defines) the contextvar."""
    execution_src = _SPAWN_SRC.read_text(encoding="utf-8")
    observability_src = _OBSERVABILITY_SRC.read_text(encoding="utf-8")
    assert 'kind="spawn_event"' in observability_src
    assert "arcrun.model_identity" in execution_src
    assert "arcllm" not in execution_src + observability_src


def test_agent_identity_exposed_by_arcrun() -> None:
    """ArcAgent consumes identity binding only through ArcRun's root facade."""
    import arcrun

    assert callable(arcrun.model_identity)
