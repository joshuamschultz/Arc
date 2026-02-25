"""Tests for Consolidator — significance, episodes, identity, entity pipeline."""

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
def workspace(tmp_path: Path) -> Path:
    return tmp_path


@pytest.fixture
def memory_dir(workspace: Path) -> Path:
    d = workspace / "memory"
    d.mkdir()
    return d


@pytest.fixture
def entities_dir(workspace: Path) -> Path:
    d = workspace / "entities"
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
    workspace: Path,
) -> Consolidator:
    return Consolidator(
        memory_dir=memory_dir,
        config=config,
        identity=identity,
        working=working,
        telemetry=telemetry,
        workspace=workspace,
    )


def _mock_model(response_content: str) -> AsyncMock:
    """Create a mock LLM model that returns a canned response."""
    model = AsyncMock()
    response = MagicMock()
    response.content = response_content
    model.invoke = AsyncMock(return_value=response)
    return model


def _sample_messages() -> list[dict[str, str]]:
    """Sample messages that pass the pre-filter significance gate.

    Needs >= 3 messages and signal words (e.g., 'change', 'important', 'decide').
    """
    return [
        {"role": "user", "content": "Tell me about the project timeline."},
        {"role": "assistant", "content": "The project is due March 15th."},
        {"role": "user", "content": "Important change. We need to decide on priorities."},
    ]


def _write_entity(
    entities_dir: Path,
    name: str,
    frontmatter: dict[str, object],
    body: str,
) -> Path:
    """Helper to create an entity file with frontmatter."""
    fm_text = yaml.dump(frontmatter, default_flow_style=False).strip()
    content = f"---\n{fm_text}\n---\n\n{body}\n"
    path = entities_dir / f"{name}.md"
    path.write_text(content, encoding="utf-8")
    return path


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
        # Model: significant → episode → entity analysis → identity eval
        model = AsyncMock()
        responses = [
            MagicMock(content=json.dumps({"significant": True, "reason": "deadline discussed"})),
            MagicMock(content=json.dumps({
                "title": "deadline-discussion",
                "tags": ["deadline"],
                "entities": ["ProjectX"],
                "narrative": "Team discussed the project deadline change.",
            })),
            MagicMock(content=json.dumps({
                "touched_entities": [], "corrections": [],
                "new_entities": [], "co_occurrences": [],
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


class TestPreFilterSignificance:
    """Deterministic gate before LLM significance evaluation."""

    def test_short_session_filtered(self, consolidator: Consolidator) -> None:
        """< 3 messages filtered out."""
        msgs = [{"role": "user", "content": "hi"}]
        assert consolidator._pre_filter_significance(msgs) is False

    def test_long_session_passes(self, consolidator: Consolidator) -> None:
        """> 8 messages always pass."""
        msgs = [{"role": "user", "content": "hi"}] * 10
        assert consolidator._pre_filter_significance(msgs) is True

    def test_signal_words_pass(self, consolidator: Consolidator) -> None:
        """Sessions with signal words pass."""
        msgs = [
            {"role": "user", "content": "we need to change this"},
            {"role": "assistant", "content": "ok"},
            {"role": "user", "content": "important update"},
        ]
        assert consolidator._pre_filter_significance(msgs) is True

    def test_trivial_session_filtered(self, consolidator: Consolidator) -> None:
        """3+ messages without signal words filtered."""
        msgs = [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "hi"},
            {"role": "user", "content": "goodbye"},
        ]
        assert consolidator._pre_filter_significance(msgs) is False


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


class TestEntityAnalysis:
    """Entity analysis single LLM call."""

    @pytest.mark.asyncio
    async def test_analyze_entities_returns_dict(
        self, consolidator: Consolidator,
    ) -> None:
        model = _mock_model(json.dumps({
            "touched_entities": ["josh-schultz"],
            "corrections": [],
            "new_entities": [],
            "co_occurrences": [],
        }))
        result = await consolidator._analyze_entities(_sample_messages(), model)
        assert isinstance(result, dict)
        assert result["touched_entities"] == ["josh-schultz"]

    @pytest.mark.asyncio
    async def test_analyze_entities_handles_failure(
        self, consolidator: Consolidator,
    ) -> None:
        model = _mock_model("not valid json")
        result = await consolidator._analyze_entities(_sample_messages(), model)
        assert result == {}


class TestUpdateTouchedEntities:
    """LC-4: Update last_verified and Recent Activity."""

    @pytest.mark.asyncio
    async def test_updates_existing_entity(
        self, consolidator: Consolidator, entities_dir: Path,
    ) -> None:
        _write_entity(
            entities_dir, "josh-schultz",
            {"entity_type": "person", "last_verified": "2026-01-01"},
            "# Josh Schultz\n\n## Recent Activity\n",
        )
        count = await consolidator._update_touched_entities(["josh-schultz"])
        assert count == 1

        text = (entities_dir / "josh-schultz.md").read_text()
        assert "2026-01-01" not in text or "Referenced in session" in text

    @pytest.mark.asyncio
    async def test_skips_nonexistent_entity(
        self, consolidator: Consolidator,
    ) -> None:
        count = await consolidator._update_touched_entities(["does-not-exist"])
        assert count == 0


class TestApplyCorrections:
    """LC-5: Append corrections to Constraints and Lessons section."""

    @pytest.mark.asyncio
    async def test_applies_correction(
        self, consolidator: Consolidator, entities_dir: Path,
    ) -> None:
        _write_entity(
            entities_dir, "pricing",
            {"entity_type": "concept"},
            "# Pricing\n\n## Constraints and Lessons\n",
        )
        count = await consolidator._apply_corrections([
            {"entity": "pricing", "correction": "Show methodology first"},
        ])
        assert count == 1
        text = (entities_dir / "pricing.md").read_text()
        assert "Show methodology first" in text


class TestCoOccurrenceLinking:
    """LC-6: Bidirectional wiki-link addition."""

    def test_adds_bidirectional_links(
        self, consolidator: Consolidator, entities_dir: Path,
    ) -> None:
        _write_entity(entities_dir, "entity-a", {"links_to": []}, "Entity A")
        _write_entity(entities_dir, "entity-b", {"links_to": []}, "Entity B")

        count = consolidator._add_co_occurrence_links([["entity-a", "entity-b"]])
        assert count == 2  # One in each direction

        from arcagent.utils.sanitizer import read_frontmatter
        fm_a = read_frontmatter(entities_dir / "entity-a.md")
        fm_b = read_frontmatter(entities_dir / "entity-b.md")
        assert "[[entity-b]]" in fm_a["links_to"]
        assert "[[entity-a]]" in fm_b["links_to"]

    def test_skips_existing_links(
        self, consolidator: Consolidator, entities_dir: Path,
    ) -> None:
        _write_entity(entities_dir, "entity-a", {"links_to": ["[[entity-b]]"]}, "A")
        _write_entity(entities_dir, "entity-b", {"links_to": ["[[entity-a]]"]}, "B")

        count = consolidator._add_co_occurrence_links([["entity-a", "entity-b"]])
        assert count == 0  # Already linked

    def test_rate_limits_links(
        self, consolidator: Consolidator, entities_dir: Path,
    ) -> None:
        """Max 10 new links per session."""
        for i in range(8):
            _write_entity(entities_dir, f"ent-{i}", {"links_to": []}, f"Entity {i}")

        # 6 pairs = 12 links attempted, should cap at 10
        pairs = [[f"ent-{i}", f"ent-{i+1}"] for i in range(6)]
        count = consolidator._add_co_occurrence_links(pairs)
        assert count <= 10


class TestCreateEntityStubs:
    """LC-7: New entity stub creation with rate limiting."""

    @pytest.mark.asyncio
    async def test_creates_stub_with_v21_schema(
        self, consolidator: Consolidator, entities_dir: Path,
    ) -> None:
        count = await consolidator._create_entity_stubs([
            {"id": "new-project", "type": "project", "summary": "A new project."},
        ])
        assert count == 1

        # Check file exists
        path = entities_dir / "new-project.md"
        assert path.exists()
        text = path.read_text()
        assert "## Summary" in text
        assert "## Key Facts" in text
        assert "## Constraints and Lessons" in text
        assert "## Recent Activity" in text

    @pytest.mark.asyncio
    async def test_skips_existing_entity(
        self, consolidator: Consolidator, entities_dir: Path,
    ) -> None:
        _write_entity(entities_dir, "existing", {}, "Already exists")
        count = await consolidator._create_entity_stubs([
            {"id": "existing", "type": "project", "summary": "Duplicate"},
        ])
        assert count == 0

    @pytest.mark.asyncio
    async def test_rate_limits_creation(
        self, consolidator: Consolidator, entities_dir: Path,
    ) -> None:
        """Max 3 new entities per session."""
        entities = [
            {"id": f"new-{i}", "type": "concept", "summary": f"Entity {i}"}
            for i in range(5)
        ]
        count = await consolidator._create_entity_stubs(entities)
        assert count == 3  # Rate limited


class TestNormalizeEntityFile:
    """Legacy files get v2.1 frontmatter on first touch."""

    def test_adds_frontmatter_to_legacy_file(
        self, consolidator: Consolidator, entities_dir: Path,
    ) -> None:
        path = entities_dir / "legacy-entity.md"
        path.write_text("# Legacy Entity\n\nSome content here.\n", encoding="utf-8")

        consolidator._normalize_entity_file(path)

        text = path.read_text()
        assert text.startswith("---\n")
        from arcagent.utils.sanitizer import read_frontmatter
        fm = read_frontmatter(path)
        assert fm is not None
        assert fm["entity_id"] == "legacy-entity"
        assert fm["name"] == "Legacy Entity"

    def test_skips_file_with_existing_frontmatter(
        self, consolidator: Consolidator, entities_dir: Path,
    ) -> None:
        path = entities_dir / "existing-fm.md"
        original = "---\nentity_type: person\n---\n\n# Person\n"
        path.write_text(original, encoding="utf-8")

        consolidator._normalize_entity_file(path)

        # Should be unchanged (not double-frontmattered)
        text = path.read_text()
        assert text.count("---") == 2


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
        workspace: Path,
    ) -> None:
        """Each consolidator instance has a unique boundary ID."""
        c1 = Consolidator(memory_dir, config, identity, working, telemetry, workspace=workspace)
        c2 = Consolidator(memory_dir, config, identity, working, telemetry, workspace=workspace)
        assert c1._boundary_id != c2._boundary_id

    def test_boundary_id_length(self, consolidator: Consolidator) -> None:
        assert len(consolidator._boundary_id) == 12
