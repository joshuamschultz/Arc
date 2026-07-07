"""Grounded reflection -> existing ACE curator (SPEC-041 Phase 9).

Proves the reflection feeds the REAL ``PolicyEngine`` (no second algorithm),
never targets ``identity.md``, tier-gates the write approval, and audits every
mutation. The SPEC-035 protected-path denial for a *tool* write to ``policy.md``
is proven to still fire.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from arcagent.modules.policy.config import PolicyConfig
from arcagent.modules.policy.policy_engine import PolicyEngine
from arcagent.modules.policy.reflection import ReflectionGrounding, reflect_and_curate


class _FakeModel:
    """Eval model returning a fixed ACE delta (one addition)."""

    def __init__(self, additions: list[str]) -> None:
        self._payload = json.dumps({"additions": additions, "updates": [], "rewrites": []})

    async def invoke(self, messages: list[Any]) -> Any:
        return type("Resp", (), {"content": self._payload})()


class _SpyTelemetry:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, Any]]] = []

    def audit_event(self, event: str, detail: dict[str, Any]) -> None:
        self.events.append((event, detail))


def _engine(tmp_path: Path, tier: str = "personal", telemetry: Any = None) -> PolicyEngine:
    return PolicyEngine(config=PolicyConfig(tier=tier), workspace=tmp_path, telemetry=telemetry)


def test_grounding_is_empty_detection() -> None:
    assert ReflectionGrounding().is_empty is True
    assert ReflectionGrounding(episode_summary="did a thing").is_empty is False
    assert ReflectionGrounding(failures=["boom"]).is_empty is False


async def test_empty_grounding_writes_nothing(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    ran = await reflect_and_curate(engine, _FakeModel(["x"]), ReflectionGrounding())
    assert ran is False
    assert not (tmp_path / "policy.md").exists()


async def test_no_model_writes_nothing(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    ran = await reflect_and_curate(engine, None, ReflectionGrounding(episode_summary="did work"))
    assert ran is False
    assert not (tmp_path / "policy.md").exists()


async def test_grounded_reflection_writes_via_real_engine(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    grounding = ReflectionGrounding(
        episode_summary="retried a transient failure and it worked",
        step_results=["retry succeeded"],
    )
    ran = await reflect_and_curate(
        engine, _FakeModel(["Prefer retry on transient errors"]), grounding
    )
    assert ran is True
    content = (tmp_path / "policy.md").read_text()
    assert "Prefer retry on transient errors" in content
    assert "{score:5" in content  # went through the real curator


async def test_personal_auto_applies_to_policy_md(tmp_path: Path) -> None:
    engine = _engine(tmp_path, tier="personal")
    await reflect_and_curate(
        engine,
        _FakeModel(["lesson A"]),
        ReflectionGrounding(episode_summary="ep"),
        tier="personal",
    )
    assert (tmp_path / "policy.md").exists()
    assert not (tmp_path / "policy.pending").exists()


async def test_federal_stages_to_policy_pending(tmp_path: Path) -> None:
    engine = _engine(tmp_path, tier="federal")
    await reflect_and_curate(
        engine, _FakeModel(["lesson B"]), ReflectionGrounding(episode_summary="ep"), tier="federal"
    )
    assert (tmp_path / "policy.pending").exists()
    assert not (tmp_path / "policy.md").exists()  # staged, not applied


async def test_never_writes_identity_md(tmp_path: Path) -> None:
    (tmp_path / "identity.md").write_text("IMMUTABLE GOAL")
    engine = _engine(tmp_path)
    await reflect_and_curate(
        engine, _FakeModel(["some lesson"]), ReflectionGrounding(episode_summary="ep")
    )
    assert (tmp_path / "identity.md").read_text() == "IMMUTABLE GOAL"


async def test_every_mutation_is_audited(tmp_path: Path) -> None:
    spy = _SpyTelemetry()
    engine = _engine(tmp_path, telemetry=spy)
    await reflect_and_curate(
        engine, _FakeModel(["audited lesson"]), ReflectionGrounding(episode_summary="ep")
    )
    names = [e for e, _ in spy.events]
    assert "policy.reflected" in names
    assert "policy.curated" in names


async def test_crafted_bullet_is_sanitized_by_curator(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    # Zero-width + control chars must be stripped by the reused sanitizer.
    await reflect_and_curate(
        engine,
        _FakeModel(["clean​text\x07here"]),
        ReflectionGrounding(episode_summary="ep"),
    )
    content = (tmp_path / "policy.md").read_text()
    assert "​" not in content
    assert "\x07" not in content
    assert "cleantexthere" in content
