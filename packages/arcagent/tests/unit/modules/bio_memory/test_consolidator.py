"""Tests for Consolidator — significance, episodes, daily notes, entity pipeline."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
import yaml

from arcagent.modules.bio_memory.config import BioMemoryConfig
from arcagent.modules.bio_memory.consolidator import Consolidator
from arcagent.modules.bio_memory.daily_notes import DailyNotes
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
def daily_notes(
    memory_dir: Path,
    config: BioMemoryConfig,
) -> DailyNotes:
    return DailyNotes(memory_dir=memory_dir, config=config)


@pytest.fixture
def working(memory_dir: Path, config: BioMemoryConfig) -> WorkingMemory:
    return WorkingMemory(memory_dir=memory_dir, config=config)


@pytest.fixture
def consolidator(
    memory_dir: Path,
    config: BioMemoryConfig,
    daily_notes: DailyNotes,
    working: WorkingMemory,
    telemetry: MagicMock,
    workspace: Path,
) -> Consolidator:
    return Consolidator(
        memory_dir=memory_dir,
        config=config,
        working=working,
        daily_notes=daily_notes,
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


class TestPeriodicConsolidate:
    """Consolidator.periodic_consolidate() orchestrates the full sequence."""

    @pytest.mark.asyncio
    async def test_skips_empty_messages(
        self,
        consolidator: Consolidator,
    ) -> None:
        model = _mock_model("")
        await consolidator.periodic_consolidate([], model)
        model.invoke.assert_not_called()

    @pytest.mark.asyncio
    async def test_significant_session_creates_episode(
        self,
        consolidator: Consolidator,
        memory_dir: Path,
    ) -> None:
        # Model: daily note → significance → episode → entity analysis
        model = AsyncMock()
        responses = [
            MagicMock(content=json.dumps({"entries": ["Discussed deadline"]})),
            MagicMock(content=json.dumps({"significant": True, "reason": "deadline discussed"})),
            MagicMock(
                content=json.dumps(
                    {
                        "title": "deadline-discussion",
                        "tags": ["deadline"],
                        "entities": ["ProjectX"],
                        "narrative": "Team discussed the project deadline change.",
                    }
                )
            ),
            MagicMock(
                content=json.dumps(
                    {
                        "touched_entities": [],
                        "corrections": [],
                        "new_entities": [],
                        "co_occurrences": [],
                    }
                )
            ),
        ]
        model.invoke = AsyncMock(side_effect=responses)

        await consolidator.periodic_consolidate(_sample_messages(), model)

        episodes_dir = memory_dir / "episodes"
        assert episodes_dir.exists()
        episode_files = list(episodes_dir.glob("*.md"))
        assert len(episode_files) == 1

    @pytest.mark.asyncio
    async def test_insignificant_session_no_episode(
        self,
        consolidator: Consolidator,
        memory_dir: Path,
    ) -> None:
        # Model: daily note → significance (false)
        model = AsyncMock()
        responses = [
            MagicMock(content=json.dumps({"entries": ["Trivial chat"]})),
            MagicMock(content=json.dumps({"significant": False, "reason": "trivial"})),
        ]
        model.invoke = AsyncMock(side_effect=responses)

        await consolidator.periodic_consolidate(_sample_messages(), model)

        episodes_dir = memory_dir / "episodes"
        if episodes_dir.exists():
            assert list(episodes_dir.glob("*.md")) == []

    @pytest.mark.asyncio
    async def test_always_creates_daily_note(
        self,
        consolidator: Consolidator,
        memory_dir: Path,
    ) -> None:
        """Daily note is always appended, even for trivial sessions."""
        # Model: daily note → (pre-filter will catch trivial)
        model = _mock_model(json.dumps({"entries": ["Did some work"]}))

        # Short trivial messages — will be pre-filtered (no signal words, < 3 msgs)
        trivial = [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "hello"},
            {"role": "user", "content": "bye"},
        ]
        await consolidator.periodic_consolidate(trivial, model)

        daily_notes_dir = memory_dir / "daily-notes"
        assert daily_notes_dir.exists()
        note_files = list(daily_notes_dir.glob("*.md"))
        assert len(note_files) == 1

    @pytest.mark.asyncio
    async def test_clears_working_memory(
        self,
        consolidator: Consolidator,
        working: WorkingMemory,
        memory_dir: Path,
    ) -> None:
        await working.write(content="Active data", frontmatter={"turn_number": 1})
        # Model: daily note → (pre-filter catches trivial)
        model = _mock_model(json.dumps({"entries": ["Worked on task"]}))

        await consolidator.periodic_consolidate(_sample_messages(), model)

        # Working memory should be cleared
        content = await working.read()
        assert "Active data" not in content

    @pytest.mark.asyncio
    async def test_emits_telemetry(
        self,
        consolidator: Consolidator,
        telemetry: MagicMock,
    ) -> None:
        # Model: daily note → (pre-filter catches trivial)
        model = _mock_model(json.dumps({"entries": ["Some work"]}))
        await consolidator.periodic_consolidate(_sample_messages(), model)
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
        self,
        consolidator: Consolidator,
    ) -> None:
        model = _mock_model(json.dumps({"significant": True, "reason": "important"}))
        result = await consolidator._evaluate_significance(_sample_messages(), model)
        assert result is True

    @pytest.mark.asyncio
    async def test_returns_false_for_trivial(
        self,
        consolidator: Consolidator,
    ) -> None:
        model = _mock_model(json.dumps({"significant": False, "reason": "trivial"}))
        result = await consolidator._evaluate_significance(_sample_messages(), model)
        assert result is False

    @pytest.mark.asyncio
    async def test_returns_false_on_parse_error(
        self,
        consolidator: Consolidator,
    ) -> None:
        model = _mock_model("not valid json")
        result = await consolidator._evaluate_significance(_sample_messages(), model)
        assert result is False


class TestCreateEpisode:
    """Consolidator._create_episode() writes episode files."""

    @pytest.mark.asyncio
    async def test_creates_episode_file(
        self,
        consolidator: Consolidator,
        memory_dir: Path,
    ) -> None:
        model = _mock_model(
            json.dumps(
                {
                    "title": "test-episode",
                    "tags": ["test"],
                    "entities": [],
                    "narrative": "A test episode was created.",
                }
            )
        )
        await consolidator._create_episode(_sample_messages(), model)

        episodes_dir = memory_dir / "episodes"
        assert episodes_dir.exists()
        files = list(episodes_dir.glob("*.md"))
        assert len(files) == 1

    @pytest.mark.asyncio
    async def test_episode_has_frontmatter(
        self,
        consolidator: Consolidator,
        memory_dir: Path,
    ) -> None:
        model = _mock_model(
            json.dumps(
                {
                    "title": "test-episode",
                    "tags": ["test", "demo"],
                    "entities": ["ProjectX"],
                    "narrative": "Episode narrative here.",
                }
            )
        )
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
        self,
        consolidator: Consolidator,
    ) -> None:
        model = _mock_model(
            json.dumps(
                {
                    "touched_entities": ["josh-schultz"],
                    "corrections": [],
                    "new_entities": [],
                    "co_occurrences": [],
                }
            )
        )
        result = await consolidator._analyze_entities(_sample_messages(), model)
        assert isinstance(result, dict)
        assert result["touched_entities"] == ["josh-schultz"]

    @pytest.mark.asyncio
    async def test_analyze_entities_handles_failure(
        self,
        consolidator: Consolidator,
    ) -> None:
        model = _mock_model("not valid json")
        result = await consolidator._analyze_entities(_sample_messages(), model)
        assert result == {}


class TestUpdateTouchedEntities:
    """LC-4: Update last_verified and Recent Activity."""

    @pytest.mark.asyncio
    async def test_updates_existing_entity(
        self,
        consolidator: Consolidator,
        entities_dir: Path,
    ) -> None:
        _write_entity(
            entities_dir,
            "josh-schultz",
            {"entity_type": "person", "last_verified": "2026-01-01"},
            "# Josh Schultz\n\n## Recent Activity\n",
        )
        count, mutated = await consolidator._update_touched_entities(["josh-schultz"])
        assert count == 1
        assert "josh-schultz" in mutated

        text = (entities_dir / "josh-schultz.md").read_text()
        assert "2026-01-01" not in text or "Referenced in session" in text

    @pytest.mark.asyncio
    async def test_skips_nonexistent_entity(
        self,
        consolidator: Consolidator,
    ) -> None:
        count, mutated = await consolidator._update_touched_entities(["does-not-exist"])
        assert count == 0
        assert len(mutated) == 0


class TestAppendEntityFacts:
    """LC-4.5: Append compact fact triplets to entity Key Facts."""

    def test_appends_facts_to_entity(
        self,
        consolidator: Consolidator,
        entities_dir: Path,
    ) -> None:
        _write_entity(
            entities_dir,
            "josh",
            {"entity_type": "person"},
            "# Josh\n\n## Key Facts\n\n## Summary\nA person.\n",
        )
        count, mutated = consolidator._append_entity_facts(
            {
                "josh": [{"p": "works_at", "v": "Anthropic", "c": 0.9}],
            }
        )
        assert count == 1
        assert "josh" in mutated
        text = (entities_dir / "josh.md").read_text()
        assert "works_at" in text
        assert "Anthropic" in text

    def test_detects_contradiction(
        self,
        consolidator: Consolidator,
        entities_dir: Path,
    ) -> None:
        _write_entity(
            entities_dir,
            "josh",
            {"entity_type": "person"},
            "# Josh\n\n## Key Facts\n- works_at: IBM .8 2023-01-01\n\n## Summary\n",
        )
        count, mutated = consolidator._append_entity_facts(
            {
                "josh": [{"p": "works_at", "v": "Anthropic", "c": 0.9}],
            }
        )
        assert count == 1
        assert "josh" in mutated
        text = (entities_dir / "josh.md").read_text()
        assert "was: IBM" in text

    def test_skips_nonexistent_entity(
        self,
        consolidator: Consolidator,
    ) -> None:
        count, mutated = consolidator._append_entity_facts(
            {
                "missing": [{"p": "role", "v": "engineer", "c": 0.5}],
            }
        )
        assert count == 0
        assert len(mutated) == 0

    def test_skips_empty_predicate_or_value(
        self,
        consolidator: Consolidator,
        entities_dir: Path,
    ) -> None:
        _write_entity(
            entities_dir,
            "test",
            {"entity_type": "concept"},
            "# Test\n\n## Key Facts\n",
        )
        count, mutated = consolidator._append_entity_facts(
            {
                "test": [{"p": "", "v": "val", "c": 0.5}, {"p": "key", "v": "", "c": 0.5}],
            }
        )
        assert count == 0
        assert len(mutated) == 0

    def test_multiple_facts_for_entity(
        self,
        consolidator: Consolidator,
        entities_dir: Path,
    ) -> None:
        _write_entity(
            entities_dir,
            "proj",
            {"entity_type": "project"},
            "# Project\n\n## Key Facts\n\n## Summary\n",
        )
        count, mutated = consolidator._append_entity_facts(
            {
                "proj": [
                    {"p": "status", "v": "active", "c": 0.95},
                    {"p": "language", "v": "Python", "c": 0.9},
                ],
            }
        )
        assert count == 2
        assert "proj" in mutated


class TestApplyCorrections:
    """LC-5: Append corrections to Constraints and Lessons section."""

    @pytest.mark.asyncio
    async def test_applies_correction(
        self,
        consolidator: Consolidator,
        entities_dir: Path,
    ) -> None:
        _write_entity(
            entities_dir,
            "pricing",
            {"entity_type": "concept"},
            "# Pricing\n\n## Constraints and Lessons\n",
        )
        count, mutated = await consolidator._apply_corrections(
            [
                {"entity": "pricing", "correction": "Show methodology first"},
            ]
        )
        assert count == 1
        assert "pricing" in mutated
        text = (entities_dir / "pricing.md").read_text()
        assert "Show methodology first" in text


class TestCoOccurrenceLinking:
    """LC-6: Bidirectional wiki-link addition."""

    def test_adds_bidirectional_links(
        self,
        consolidator: Consolidator,
        entities_dir: Path,
    ) -> None:
        _write_entity(entities_dir, "entity-a", {"links_to": []}, "Entity A")
        _write_entity(entities_dir, "entity-b", {"links_to": []}, "Entity B")

        count, mutated = consolidator._add_co_occurrence_links([["entity-a", "entity-b"]])
        assert count == 2  # One in each direction
        assert "entity-a" in mutated
        assert "entity-b" in mutated

        from arcagent.utils.sanitizer import read_frontmatter

        fm_a = read_frontmatter(entities_dir / "entity-a.md")
        fm_b = read_frontmatter(entities_dir / "entity-b.md")
        assert "[[entity-b]]" in fm_a["links_to"]
        assert "[[entity-a]]" in fm_b["links_to"]

    def test_skips_existing_links(
        self,
        consolidator: Consolidator,
        entities_dir: Path,
    ) -> None:
        _write_entity(entities_dir, "entity-a", {"links_to": ["[[entity-b]]"]}, "A")
        _write_entity(entities_dir, "entity-b", {"links_to": ["[[entity-a]]"]}, "B")

        count, mutated = consolidator._add_co_occurrence_links([["entity-a", "entity-b"]])
        assert count == 0  # Already linked
        assert len(mutated) == 0

    def test_rate_limits_links(
        self,
        consolidator: Consolidator,
        entities_dir: Path,
    ) -> None:
        """Max 10 new links per session."""
        for i in range(8):
            _write_entity(entities_dir, f"ent-{i}", {"links_to": []}, f"Entity {i}")

        # 6 pairs = 12 links attempted, should cap at 10
        pairs = [[f"ent-{i}", f"ent-{i + 1}"] for i in range(6)]
        count, _mutated = consolidator._add_co_occurrence_links(pairs)
        assert count <= 10


class TestCreateEntityStubs:
    """LC-7: New entity stub creation with rate limiting."""

    @pytest.mark.asyncio
    async def test_creates_stub_with_v21_schema(
        self,
        consolidator: Consolidator,
        entities_dir: Path,
    ) -> None:
        count = await consolidator._create_entity_stubs(
            [
                {"id": "new-project", "type": "project", "summary": "A new project."},
            ]
        )
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
        self,
        consolidator: Consolidator,
        entities_dir: Path,
    ) -> None:
        _write_entity(entities_dir, "existing", {}, "Already exists")
        count = await consolidator._create_entity_stubs(
            [
                {"id": "existing", "type": "project", "summary": "Duplicate"},
            ]
        )
        assert count == 0

    @pytest.mark.asyncio
    async def test_rate_limits_creation(
        self,
        consolidator: Consolidator,
        entities_dir: Path,
    ) -> None:
        """Max 3 new entities per session."""
        entities = [
            {"id": f"new-{i}", "type": "concept", "summary": f"Entity {i}"} for i in range(5)
        ]
        count = await consolidator._create_entity_stubs(entities)
        assert count == 3  # Rate limited


class TestNormalizeEntityFile:
    """Legacy files get v2.1 frontmatter on first touch."""

    def test_adds_frontmatter_to_legacy_file(
        self,
        entities_dir: Path,
    ) -> None:
        from arcagent.modules.bio_memory.entity_helpers import normalize_entity_file

        path = entities_dir / "legacy-entity.md"
        path.write_text("# Legacy Entity\n\nSome content here.\n", encoding="utf-8")

        normalize_entity_file(path, entities_dir)

        text = path.read_text()
        assert text.startswith("---\n")
        from arcagent.utils.sanitizer import read_frontmatter

        fm = read_frontmatter(path)
        assert fm is not None
        assert fm["entity_id"] == "legacy-entity"
        assert fm["name"] == "Legacy Entity"

    def test_skips_file_with_existing_frontmatter(
        self,
        entities_dir: Path,
    ) -> None:
        from arcagent.modules.bio_memory.entity_helpers import normalize_entity_file

        path = entities_dir / "existing-fm.md"
        original = "---\nentity_type: person\n---\n\n# Person\n"
        path.write_text(original, encoding="utf-8")

        normalize_entity_file(path, entities_dir)

        text = path.read_text()
        assert text.count("---") == 2


class TestBoundaryMarkers:
    """Consolidator uses UUID-based boundary markers (SEC-11)."""

    def test_boundary_id_is_unique_per_instance(
        self,
        memory_dir: Path,
        config: BioMemoryConfig,
        daily_notes: DailyNotes,
        working: WorkingMemory,
        telemetry: MagicMock,
        workspace: Path,
    ) -> None:
        """Each consolidator instance has a unique boundary ID."""
        c1 = Consolidator(memory_dir, config, working, daily_notes, telemetry, workspace=workspace)
        c2 = Consolidator(memory_dir, config, working, daily_notes, telemetry, workspace=workspace)
        assert c1._boundary_id != c2._boundary_id

    def test_boundary_id_length(self, consolidator: Consolidator) -> None:
        assert len(consolidator._boundary_id) == 12


class TestBatchTeamPromote:
    """Batch team promotion of mutated entities."""

    @pytest.mark.asyncio
    async def test_batch_promote_calls_team_service(
        self,
        memory_dir: Path,
        config: BioMemoryConfig,
        daily_notes: DailyNotes,
        working: WorkingMemory,
        telemetry: MagicMock,
        workspace: Path,
        entities_dir: Path,
    ) -> None:
        """Mutated entities are read from disk and promoted to team store."""
        import sys

        # Provide a mock EntityMetadata so the arcteam import succeeds
        mock_types = MagicMock()
        mock_types.EntityMetadata = lambda **kw: MagicMock(**kw)
        sys.modules["arcteam"] = MagicMock()
        sys.modules["arcteam.memory"] = MagicMock()
        sys.modules["arcteam.memory.types"] = mock_types

        try:
            mock_svc = AsyncMock()
            mock_svc.promote = AsyncMock()

            consolidator = Consolidator(
                memory_dir=memory_dir,
                config=config,
                working=working,
                daily_notes=daily_notes,
                telemetry=telemetry,
                workspace=workspace,
                team_service_factory=lambda: mock_svc,
                agent_id="test-agent",
            )

            _write_entity(
                entities_dir,
                "josh-schultz",
                {"entity_type": "person", "entity_id": "josh-schultz"},
                "# Josh\n\n## Key Facts\n",
            )

            promoted = await consolidator._batch_team_promote({"josh-schultz"})
            assert promoted == 1
            assert mock_svc.promote.called
        finally:
            sys.modules.pop("arcteam.memory.types", None)
            sys.modules.pop("arcteam.memory", None)
            sys.modules.pop("arcteam", None)

    @pytest.mark.asyncio
    async def test_batch_promote_noop_without_team_service(
        self,
        consolidator: Consolidator,
        entities_dir: Path,
    ) -> None:
        """Without team service, batch promote is a no-op."""
        _write_entity(entities_dir, "test", {"entity_type": "concept"}, "# Test\n")
        promoted = await consolidator._batch_team_promote({"test"})
        assert promoted == 0

    @pytest.mark.asyncio
    async def test_batch_promote_skips_missing_entities(
        self,
        memory_dir: Path,
        config: BioMemoryConfig,
        daily_notes: DailyNotes,
        working: WorkingMemory,
        telemetry: MagicMock,
        workspace: Path,
        entities_dir: Path,
    ) -> None:
        """Missing entities are skipped silently."""
        mock_svc = AsyncMock()
        mock_svc.promote = AsyncMock()

        consolidator = Consolidator(
            memory_dir=memory_dir,
            config=config,
            working=working,
            daily_notes=daily_notes,
            telemetry=telemetry,
            workspace=workspace,
            team_service_factory=lambda: mock_svc,
            agent_id="test-agent",
        )

        promoted = await consolidator._batch_team_promote({"does-not-exist"})
        assert promoted == 0
        assert not mock_svc.promote.called
