"""Tests for PolicyEngine — ACE framework (Reflector + Curator)."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from arcagent.core.config import EvalConfig, MemoryConfig
from arcagent.modules.memory.policy_engine import (
    BulletRewrite,
    BulletUpdate,
    PolicyBullet,
    PolicyDelta,
    PolicyEngine,
)


def _make_telemetry() -> MagicMock:
    t = MagicMock()
    t.audit_event = MagicMock()
    return t


def _make_engine(workspace: Path) -> PolicyEngine:
    return PolicyEngine(
        eval_config=EvalConfig(),
        workspace=workspace,
        telemetry=_make_telemetry(),
        memory_config=MemoryConfig(),
    )


class TestPolicyBulletParsing:
    """T4.3.1: Parse bullets from markdown."""

    def test_parse_structured_bullet(self, tmp_path: Path) -> None:
        engine = _make_engine(tmp_path)
        bullet = (
            "- [P01] Always run tests before claiming success "
            "{score:9, uses:8, reviewed:2026-02-15,"
            " created:2026-01-01, source:sess-abc}\n"
        )
        content = f"# Policy\n\n{bullet}"
        bullets = engine._parse_policy(content)
        assert len(bullets) == 1
        b = bullets[0]
        assert b.id == "P01"
        assert "run tests" in b.text
        assert b.score == 9
        assert b.uses == 8
        assert b.reviewed == "2026-02-15"
        assert b.source == "sess-abc"

    def test_parse_multiple_bullets(self, tmp_path: Path) -> None:
        engine = _make_engine(tmp_path)
        b1 = (
            "- [P01] Lesson one {score:7, uses:3,"
            " reviewed:2026-02-15, created:2026-01-01, source:s1}\n"
        )
        b2 = (
            "- [P02] Lesson two {score:5, uses:1,"
            " reviewed:2026-02-14, created:2026-01-02, source:s2}\n"
        )
        content = f"# Policy\n\n{b1}{b2}"
        bullets = engine._parse_policy(content)
        assert len(bullets) == 2

    def test_parse_empty_policy(self, tmp_path: Path) -> None:
        engine = _make_engine(tmp_path)
        bullets = engine._parse_policy("")
        assert bullets == []

    def test_parse_policy_without_header(self, tmp_path: Path) -> None:
        engine = _make_engine(tmp_path)
        content = (
            "- [P01] Test {score:5, uses:0,"
            " reviewed:2026-02-15, created:2026-02-15, source:s1}\n"
        )
        bullets = engine._parse_policy(content)
        assert len(bullets) == 1


class TestPolicySerialization:
    """T4.3.2: Render bullets back to markdown."""

    def test_serialize_bullet(self, tmp_path: Path) -> None:
        engine = _make_engine(tmp_path)
        bullets = [
            PolicyBullet(
                id="P01", text="Run tests first",
                score=8, uses=5, reviewed="2026-02-15",
                created="2026-01-01", source="sess-abc",
            )
        ]
        result = engine._serialize_policy(bullets)
        assert "# Policy" in result
        assert "[P01]" in result
        assert "Run tests first" in result
        assert "score:8" in result

    def test_serialize_empty_policy(self, tmp_path: Path) -> None:
        engine = _make_engine(tmp_path)
        result = engine._serialize_policy([])
        assert "# Policy" in result


class TestCuratorAdd:
    """T4.3.4: New bullet starts at score 5."""

    @pytest.mark.asyncio()
    async def test_add_new_bullet(self, tmp_path: Path) -> None:
        engine = _make_engine(tmp_path)
        policy_path = tmp_path / "policy.md"
        policy_path.write_text("# Policy\n\n")

        delta = PolicyDelta(
            additions=["Always verify before claiming success"],
            updates=[],
            rewrites=[],
            session_id="sess-new",
        )
        await engine._curate(delta)

        content = policy_path.read_text()
        assert "verify before claiming" in content.lower()
        # Should start at score 5
        assert "score:5" in content


class TestCuratorUpdate:
    """T4.3.5: Apply score_delta, increment uses."""

    @pytest.mark.asyncio()
    async def test_positive_score_update(self, tmp_path: Path) -> None:
        engine = _make_engine(tmp_path)
        policy_path = tmp_path / "policy.md"
        bullet = (
            "- [P01] Run tests {score:7, uses:3,"
            " reviewed:2026-02-14, created:2026-01-01, source:s1}\n"
        )
        policy_path.write_text(f"# Policy\n\n{bullet}")

        delta = PolicyDelta(
            additions=[],
            updates=[BulletUpdate(bullet_id="P01", score_delta=1)],
            rewrites=[],
            session_id="sess-update",
        )
        await engine._curate(delta)

        content = policy_path.read_text()
        assert "score:8" in content
        assert "uses:4" in content

    @pytest.mark.asyncio()
    async def test_negative_score_update(self, tmp_path: Path) -> None:
        engine = _make_engine(tmp_path)
        policy_path = tmp_path / "policy.md"
        bullet = (
            "- [P01] Bad rule {score:5, uses:2,"
            " reviewed:2026-02-14, created:2026-01-01, source:s1}\n"
        )
        policy_path.write_text(f"# Policy\n\n{bullet}")

        delta = PolicyDelta(
            additions=[],
            updates=[BulletUpdate(bullet_id="P01", score_delta=-2)],
            rewrites=[],
            session_id="sess-neg",
        )
        await engine._curate(delta)

        content = policy_path.read_text()
        assert "score:3" in content


class TestCuratorRewrite:
    """T4.3.6: Update text, preserve ID, score, source."""

    @pytest.mark.asyncio()
    async def test_rewrite_preserves_id(self, tmp_path: Path) -> None:
        engine = _make_engine(tmp_path)
        policy_path = tmp_path / "policy.md"
        bullet = (
            "- [P01] Original text {score:6, uses:2,"
            " reviewed:2026-02-14, created:2026-01-01, source:s1}\n"
        )
        policy_path.write_text(f"# Policy\n\n{bullet}")

        delta = PolicyDelta(
            additions=[],
            updates=[],
            rewrites=[BulletRewrite(bullet_id="P01", new_text="Improved text here")],
            session_id="sess-rw",
        )
        await engine._curate(delta)

        content = policy_path.read_text()
        assert "[P01]" in content
        assert "Improved text here" in content
        assert "Original text" not in content


class TestScoreThresholds:
    """T4.3.7: Score-based lifecycle."""

    @pytest.mark.asyncio()
    async def test_score_2_or_below_removed(self, tmp_path: Path) -> None:
        engine = _make_engine(tmp_path)
        policy_path = tmp_path / "policy.md"
        b1 = (
            "- [P01] Good rule {score:8, uses:5,"
            " reviewed:2026-02-15, created:2026-01-01, source:s1}\n"
        )
        b2 = (
            "- [P02] Bad rule {score:2, uses:1,"
            " reviewed:2026-02-10, created:2026-01-01, source:s2}\n"
        )
        policy_path.write_text(f"# Policy\n\n{b1}{b2}")

        delta = PolicyDelta(additions=[], updates=[], rewrites=[], session_id="sess-clean")
        await engine._curate(delta)

        content = policy_path.read_text()
        assert "[P01]" in content
        assert "[P02]" not in content  # Removed due to score <= 2

    @pytest.mark.asyncio()
    async def test_score_clamped_at_10(self, tmp_path: Path) -> None:
        engine = _make_engine(tmp_path)
        policy_path = tmp_path / "policy.md"
        bullet = (
            "- [P01] Top rule {score:10, uses:20,"
            " reviewed:2026-02-15, created:2026-01-01, source:s1}\n"
        )
        policy_path.write_text(f"# Policy\n\n{bullet}")

        delta = PolicyDelta(
            additions=[],
            updates=[BulletUpdate(bullet_id="P01", score_delta=1)],
            rewrites=[],
            session_id="sess-cap",
        )
        await engine._curate(delta)

        content = policy_path.read_text()
        assert "score:10" in content  # Capped at 10

    @pytest.mark.asyncio()
    async def test_score_clamped_at_1(self, tmp_path: Path) -> None:
        engine = _make_engine(tmp_path)
        policy_path = tmp_path / "policy.md"
        bullet = (
            "- [P01] Weak rule {score:3, uses:1,"
            " reviewed:2026-02-15, created:2026-01-01, source:s1}\n"
        )
        policy_path.write_text(f"# Policy\n\n{bullet}")

        delta = PolicyDelta(
            additions=[],
            updates=[BulletUpdate(bullet_id="P01", score_delta=-5)],
            rewrites=[],
            session_id="sess-floor",
        )
        await engine._curate(delta)

        content = policy_path.read_text()
        # Score should be <= 2 so bullet is auto-removed
        assert "[P01]" not in content


class TestAsymmetricScoring:
    """T4.3.7a: +1 positive, -2 negative."""

    @pytest.mark.asyncio()
    async def test_positive_plus_one(self, tmp_path: Path) -> None:
        engine = _make_engine(tmp_path)
        policy_path = tmp_path / "policy.md"
        bullet = (
            "- [P01] Rule {score:5, uses:1,"
            " reviewed:2026-02-15, created:2026-01-01, source:s1}\n"
        )
        policy_path.write_text(f"# Policy\n\n{bullet}")
        delta = PolicyDelta(
            additions=[], updates=[BulletUpdate(bullet_id="P01", score_delta=1)],
            rewrites=[], session_id="s",
        )
        await engine._curate(delta)
        assert "score:6" in policy_path.read_text()

    @pytest.mark.asyncio()
    async def test_negative_minus_two(self, tmp_path: Path) -> None:
        engine = _make_engine(tmp_path)
        policy_path = tmp_path / "policy.md"
        bullet = (
            "- [P01] Rule {score:7, uses:1,"
            " reviewed:2026-02-15, created:2026-01-01, source:s1}\n"
        )
        policy_path.write_text(f"# Policy\n\n{bullet}")
        delta = PolicyDelta(
            additions=[], updates=[BulletUpdate(bullet_id="P01", score_delta=-2)],
            rewrites=[], session_id="s",
        )
        await engine._curate(delta)
        assert "score:5" in policy_path.read_text()


class TestEvalModelFailure:
    """T4.3.10: Skip with fallback_behavior=skip."""

    @pytest.mark.asyncio()
    async def test_skip_on_model_failure(self, tmp_path: Path) -> None:
        engine = _make_engine(tmp_path)
        model = AsyncMock(side_effect=RuntimeError("model down"))
        messages = [{"role": "user", "content": "test"}]

        # Should not raise with default skip behavior
        await engine.evaluate(messages, model)

    @pytest.mark.asyncio()
    async def test_error_on_model_failure_when_configured(self, tmp_path: Path) -> None:
        engine = PolicyEngine(
            eval_config=EvalConfig(fallback_behavior="error"),
            workspace=tmp_path,
            telemetry=_make_telemetry(),
            memory_config=MemoryConfig(),
        )
        model = AsyncMock(side_effect=RuntimeError("model down"))
        messages = [{"role": "user", "content": "test"}]

        with pytest.raises(RuntimeError):
            await engine.evaluate(messages, model)


class TestEmptyPolicy:
    """T4.3.11: First evaluation creates initial bullets."""

    @pytest.mark.asyncio()
    async def test_first_eval_creates_bullets(self, tmp_path: Path) -> None:
        engine = _make_engine(tmp_path)
        policy_path = tmp_path / "policy.md"
        # No policy file exists yet

        model = AsyncMock(return_value=json.dumps({
            "additions": ["Always verify outputs"],
            "updates": [],
            "rewrites": [],
        }))

        messages = [
            {"role": "user", "content": "Do something"},
            {"role": "assistant", "content": "Done, verified"},
        ]
        await engine.evaluate(messages, model)

        assert policy_path.exists()
        content = policy_path.read_text()
        assert "verify" in content.lower()
        assert "score:5" in content


class TestAtomicPolicyWrite:
    """T4.3.12: Write-to-temp + rename."""

    @pytest.mark.asyncio()
    async def test_atomic_write(self, tmp_path: Path) -> None:
        engine = _make_engine(tmp_path)
        policy_path = tmp_path / "policy.md"
        policy_path.write_text("# Policy\n\n")

        delta = PolicyDelta(
            additions=["New lesson"],
            updates=[], rewrites=[], session_id="s1",
        )
        await engine._curate(delta)

        assert policy_path.exists()
        assert not (tmp_path / "policy.md.tmp").exists()


class TestSourceTracking:
    """T4.3.12a: Session ID recorded."""

    @pytest.mark.asyncio()
    async def test_source_set_on_create(self, tmp_path: Path) -> None:
        engine = _make_engine(tmp_path)
        policy_path = tmp_path / "policy.md"
        policy_path.write_text("# Policy\n\n")

        delta = PolicyDelta(
            additions=["Track this"],
            updates=[], rewrites=[], session_id="sess-track",
        )
        await engine._curate(delta)

        content = policy_path.read_text()
        assert "source:sess-track" in content

    @pytest.mark.asyncio()
    async def test_source_updated_on_update(self, tmp_path: Path) -> None:
        engine = _make_engine(tmp_path)
        policy_path = tmp_path / "policy.md"
        bullet = (
            "- [P01] Rule {score:5, uses:1,"
            " reviewed:2026-02-15, created:2026-01-01,"
            " source:old-sess}\n"
        )
        policy_path.write_text(f"# Policy\n\n{bullet}")

        delta = PolicyDelta(
            additions=[],
            updates=[BulletUpdate(bullet_id="P01", score_delta=1)],
            rewrites=[], session_id="new-sess",
        )
        await engine._curate(delta)

        content = policy_path.read_text()
        assert "source:new-sess" in content


class TestNextBulletId:
    """Verify ID generation."""

    def test_sequential_ids(self, tmp_path: Path) -> None:
        engine = _make_engine(tmp_path)
        assert engine._next_id() == "P01"
        assert engine._next_id() == "P02"
        assert engine._next_id() == "P03"


class TestReflectionInvalidJSON:
    """Test _reflect handling of invalid JSON from model."""

    async def test_reflect_invalid_json_returns_none(self, tmp_path: Path) -> None:
        """Invalid JSON from model returns None delta."""
        engine = _make_engine(tmp_path)
        model = AsyncMock(return_value="not valid json {{{")
        messages = [{"role": "user", "content": "test"}]

        delta, policy = await engine._reflect(messages, model)
        assert delta is None
        assert policy == ""  # No policy file exists

    async def test_reflect_type_error_returns_none(self, tmp_path: Path) -> None:
        """TypeError from json.loads returns None delta."""
        engine = _make_engine(tmp_path)
        model = AsyncMock(return_value=None)  # Will cause TypeError in json.loads
        messages = [{"role": "user", "content": "test"}]

        delta, policy = await engine._reflect(messages, model)
        assert delta is None


class TestRewritesWithScore:
    """Test rewrites with score_delta."""

    async def test_rewrite_with_score_delta(self, tmp_path: Path) -> None:
        """Rewrites can include score_delta."""
        engine = _make_engine(tmp_path)
        policy_path = tmp_path / "policy.md"
        bullet = (
            "- [P01] Original {score:5, uses:1,"
            " reviewed:2026-02-14, created:2026-01-01, source:s1}\n"
        )
        policy_path.write_text(f"# Policy\n\n{bullet}")

        delta = PolicyDelta(
            additions=[],
            updates=[],
            rewrites=[BulletRewrite(bullet_id="P01", new_text="Rewritten", score_delta=2)],
            session_id="sess-rw",
        )
        await engine._curate(delta)

        content = policy_path.read_text()
        assert "Rewritten" in content
        assert "score:7" in content  # 5 + 2


class TestEmptyBulletList:
    """Test edge case with empty bullet list."""

    async def test_serialize_empty_list_has_header(self, tmp_path: Path) -> None:
        """Serializing empty list includes header."""
        engine = _make_engine(tmp_path)
        result = engine._serialize_policy([])
        assert "# Policy" in result
        # Should have header and trailing newline
        assert result.strip() == "# Policy"


class TestReflectExistingPolicy:
    """Line 166: _reflect reads existing policy file."""

    async def test_reflect_reads_existing_policy(self, tmp_path: Path) -> None:
        engine = _make_engine(tmp_path)
        # Write existing policy
        policy_path = tmp_path / "policy.md"
        policy_path.write_text("# Policy\n- existing rule score:5\n")

        model = AsyncMock(return_value=json.dumps({
            "additions": ["new rule"],
            "updates": [],
            "rewrites": [],
        }))

        delta, current_text = await engine._reflect(
            [{"role": "user", "content": "hello"}], model
        )
        assert "existing rule" in current_text
        assert delta is not None
        assert len(delta.additions) == 1


class TestReflectEmptyResult:
    """Line 204: _reflect returns None when no additions/updates/rewrites."""

    async def test_reflect_returns_none_for_empty_delta(self, tmp_path: Path) -> None:
        engine = _make_engine(tmp_path)
        model = AsyncMock(return_value=json.dumps({
            "additions": [],
            "updates": [],
            "rewrites": [],
        }))

        delta, _ = await engine._reflect(
            [{"role": "user", "content": "hello"}], model
        )
        assert delta is None
