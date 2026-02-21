"""Tests for Consolidator — significance evaluation, episode creation, identity update."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
import yaml

from arcagent.modules.bio_memory.config import BioMemoryConfig
from arcagent.modules.bio_memory.consolidator import Consolidator
from arcagent.modules.bio_memory.identity_manager import IdentityManager
from arcagent.modules.bio_memory.working_memory import WorkingMemory


@pytest.fixture
def memory_dir(tmp_path: Path) -> Path:
    d = tmp_path / "memory"
    d.mkdir()
    return d


@pytest.fixture
def config() -> BioMemoryConfig:
    return BioMemoryConfig()


@pytest.fixture
def telemetry() -> MagicMock:
    return MagicMock()


@pytest.fixture
def identity(
    memory_dir: Path, config: BioMemoryConfig, telemetry: MagicMock,
) -> IdentityManager:
    return IdentityManager(
        memory_dir=memory_dir, config=config, telemetry=telemetry,
    )


@pytest.fixture
def working(memory_dir: Path, config: BioMemoryConfig) -> WorkingMemory:
    return WorkingMemory(memory_dir=memory_dir, config=config)


@pytest.fixture
def consolidator(
    memory_dir: Path,
    config: BioMemoryConfig,
    identity: IdentityManager,
    working: WorkingMemory,
    telemetry: MagicMock,
) -> Consolidator:
    return Consolidator(
        memory_dir=memory_dir,
        config=config,
        identity=identity,
        working=working,
        telemetry=telemetry,
    )


def _mock_model(response_content: str) -> AsyncMock:
    """Create a mock LLM model that returns a canned response."""
    model = AsyncMock()
    response = MagicMock()
    response.content = response_content
    model.invoke = AsyncMock(return_value=response)
    return model


def _sample_messages() -> list[dict[str, str]]:
    return [
        {"role": "user", "content": "Tell me about the project timeline."},
        {"role": "assistant", "content": "The project is due March 15th."},
    ]


class TestLightConsolidate:
    """Consolidator.light_consolidate() orchestrates the full sequence."""

    @pytest.mark.asyncio
    async def test_skips_empty_messages(
        self, consolidator: Consolidator,
    ) -> None:
        model = _mock_model("")
        await consolidator.light_consolidate([], model)
        model.invoke.assert_not_called()

    @pytest.mark.asyncio
    async def test_significant_session_creates_episode(
        self, consolidator: Consolidator, memory_dir: Path,
    ) -> None:
        # Model says significant, then provides episode narrative, then no identity update
        model = AsyncMock()
        responses = [
            MagicMock(content=json.dumps({"significant": True, "reason": "deadline discussed"})),
            MagicMock(content=json.dumps({
                "title": "deadline-discussion",
                "tags": ["deadline"],
                "entities": ["ProjectX"],
                "narrative": "Team discussed the project deadline change.",
            })),
            MagicMock(content=json.dumps({"update_needed": False})),
        ]
        model.invoke = AsyncMock(side_effect=responses)

        await consolidator.light_consolidate(_sample_messages(), model)

        episodes_dir = memory_dir / "episodes"
        assert episodes_dir.exists()
        episode_files = list(episodes_dir.glob("*.md"))
        assert len(episode_files) == 1

    @pytest.mark.asyncio
    async def test_insignificant_session_no_episode(
        self, consolidator: Consolidator, memory_dir: Path,
    ) -> None:
        model = _mock_model(json.dumps({"significant": False, "reason": "trivial"}))
        await consolidator.light_consolidate(_sample_messages(), model)

        episodes_dir = memory_dir / "episodes"
        if episodes_dir.exists():
            assert list(episodes_dir.glob("*.md")) == []

    @pytest.mark.asyncio
    async def test_clears_working_memory(
        self, consolidator: Consolidator, working: WorkingMemory,
        memory_dir: Path,
    ) -> None:
        await working.write(content="Active data", frontmatter={"turn_number": 1})
        model = _mock_model(json.dumps({"significant": False, "reason": "trivial"}))
        await consolidator.light_consolidate(_sample_messages(), model)

        # Working memory should be cleared
        content = await working.read()
        # Either file cleared or still has frontmatter with empty body
        assert "Active data" not in content

    @pytest.mark.asyncio
    async def test_emits_telemetry(
        self, consolidator: Consolidator, telemetry: MagicMock,
    ) -> None:
        model = _mock_model(json.dumps({"significant": False, "reason": "trivial"}))
        await consolidator.light_consolidate(_sample_messages(), model)
        telemetry.audit_event.assert_called()


class TestEvaluateSignificance:
    """Consolidator._evaluate_significance() delegates to LLM."""

    @pytest.mark.asyncio
    async def test_returns_true_for_significant(
        self, consolidator: Consolidator,
    ) -> None:
        model = _mock_model(json.dumps({"significant": True, "reason": "important"}))
        result = await consolidator._evaluate_significance(_sample_messages(), model)
        assert result is True

    @pytest.mark.asyncio
    async def test_returns_false_for_trivial(
        self, consolidator: Consolidator,
    ) -> None:
        model = _mock_model(json.dumps({"significant": False, "reason": "trivial"}))
        result = await consolidator._evaluate_significance(_sample_messages(), model)
        assert result is False

    @pytest.mark.asyncio
    async def test_returns_false_on_parse_error(
        self, consolidator: Consolidator,
    ) -> None:
        model = _mock_model("not valid json")
        result = await consolidator._evaluate_significance(_sample_messages(), model)
        assert result is False


class TestCreateEpisode:
    """Consolidator._create_episode() writes episode files."""

    @pytest.mark.asyncio
    async def test_creates_episode_file(
        self, consolidator: Consolidator, memory_dir: Path,
    ) -> None:
        model = _mock_model(json.dumps({
            "title": "test-episode",
            "tags": ["test"],
            "entities": [],
            "narrative": "A test episode was created.",
        }))
        await consolidator._create_episode(_sample_messages(), model)

        episodes_dir = memory_dir / "episodes"
        assert episodes_dir.exists()
        files = list(episodes_dir.glob("*.md"))
        assert len(files) == 1

    @pytest.mark.asyncio
    async def test_episode_has_frontmatter(
        self, consolidator: Consolidator, memory_dir: Path,
    ) -> None:
        model = _mock_model(json.dumps({
            "title": "test-episode",
            "tags": ["test", "demo"],
            "entities": ["ProjectX"],
            "narrative": "Episode narrative here.",
        }))
        await consolidator._create_episode(_sample_messages(), model)

        episodes_dir = memory_dir / "episodes"
        ep_file = next(episodes_dir.glob("*.md"))
        text = ep_file.read_text(encoding="utf-8")
        assert text.startswith("---\n")
        end = text.find("\n---", 3)
        fm = yaml.safe_load(text[4:end])
        assert "tags" in fm
        assert "test" in fm["tags"]


class TestEvaluateIdentityUpdate:
    """Consolidator._evaluate_identity_update() delegates to LLM."""

    @pytest.mark.asyncio
    async def test_returns_new_content_when_needed(
        self, consolidator: Consolidator,
    ) -> None:
        model = _mock_model(json.dumps({
            "update_needed": True,
            "new_content": "I now prefer detailed answers.",
        }))
        result = await consolidator._evaluate_identity_update(
            _sample_messages(), "I prefer short answers.", model,
        )
        assert result == "I now prefer detailed answers."

    @pytest.mark.asyncio
    async def test_returns_none_when_not_needed(
        self, consolidator: Consolidator,
    ) -> None:
        model = _mock_model(json.dumps({"update_needed": False}))
        result = await consolidator._evaluate_identity_update(
            _sample_messages(), "Current identity.", model,
        )
        assert result is None


class TestEvaluateIdentityPublicAPI:
    """Consolidator.evaluate_identity() — public API wrapping _evaluate_identity_update."""

    @pytest.mark.asyncio
    async def test_public_api_delegates_correctly(
        self, consolidator: Consolidator,
    ) -> None:
        model = _mock_model(json.dumps({
            "update_needed": True,
            "new_content": "Updated identity.",
        }))
        result = await consolidator.evaluate_identity(
            _sample_messages(), "Old identity.", model,
        )
        assert result == "Updated identity."

    @pytest.mark.asyncio
    async def test_public_api_returns_none_when_not_needed(
        self, consolidator: Consolidator,
    ) -> None:
        model = _mock_model(json.dumps({"update_needed": False}))
        result = await consolidator.evaluate_identity(
            _sample_messages(), "Current identity.", model,
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_sanitizes_returned_content(
        self, consolidator: Consolidator,
    ) -> None:
        """Returned identity content is sanitized (ASI-06)."""
        model = _mock_model(json.dumps({
            "update_needed": True,
            "new_content": "Clean\u200bidentity\u200ftext\ufeff",
        }))
        result = await consolidator.evaluate_identity(
            _sample_messages(), "Old identity.", model,
        )
        assert result is not None
        assert "\u200b" not in result
        assert "\u200f" not in result
        assert "\ufeff" not in result


class TestBoundaryMarkers:
    """Consolidator uses UUID-based boundary markers (SEC-11)."""

    def test_boundary_id_is_unique_per_instance(
        self,
        memory_dir: Path,
        config: BioMemoryConfig,
        identity: IdentityManager,
        working: WorkingMemory,
        telemetry: MagicMock,
    ) -> None:
        """Each consolidator instance has a unique boundary ID."""
        c1 = Consolidator(memory_dir, config, identity, working, telemetry)
        c2 = Consolidator(memory_dir, config, identity, working, telemetry)
        assert c1._boundary_id != c2._boundary_id

    def test_boundary_id_length(self, consolidator: Consolidator) -> None:
        assert len(consolidator._boundary_id) == 12
