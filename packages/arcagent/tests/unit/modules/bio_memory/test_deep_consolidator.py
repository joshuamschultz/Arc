"""Tests for DeepConsolidator — entity rewrites, graph analysis, merge detection, staleness."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
import yaml

from arcagent.modules.bio_memory.config import BioMemoryConfig
from arcagent.modules.bio_memory.deep_consolidator import DeepConsolidator
from arcagent.modules.bio_memory.entity_helpers import EntityIndex


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
def deep(
    memory_dir: Path, workspace: Path, config: BioMemoryConfig,
    telemetry: MagicMock,
) -> DeepConsolidator:
    return DeepConsolidator(
        memory_dir=memory_dir,
        workspace=workspace,
        config=config,
        telemetry=telemetry,
    )


@pytest.fixture
def idx(entities_dir: Path, workspace: Path) -> EntityIndex:
    return EntityIndex(entities_dir, workspace)


def _write_episode(memory_dir: Path, name: str, date: str, body: str) -> Path:
    episodes = memory_dir / "episodes"
    episodes.mkdir(exist_ok=True)
    fm = yaml.dump({"title": name, "date": date, "tags": [], "entities": []}).strip()
    content = f"---\n{fm}\n---\n\n{body}\n"
    path = episodes / f"{date}-{name}.md"
    path.write_text(content, encoding="utf-8")
    return path


def _write_entity(
    entities_dir: Path, name: str, fm_dict: dict[str, object], body: str,
) -> Path:
    fm_text = yaml.dump(fm_dict, default_flow_style=False).strip()
    content = f"---\n{fm_text}\n---\n\n{body}\n"
    path = entities_dir / f"{name}.md"
    path.write_text(content, encoding="utf-8")
    return path


def _mock_model(response: str) -> AsyncMock:
    model = AsyncMock()
    resp = MagicMock()
    resp.content = response
    model.invoke = AsyncMock(return_value=resp)
    return model


class TestFindRecentEpisodes:
    """Activity check: find episodes from last N days."""

    def test_finds_recent_episodes(self, deep: DeepConsolidator, memory_dir: Path) -> None:
        today = datetime.now(UTC).strftime("%Y-%m-%d")
        _write_episode(memory_dir, "recent", today, "Recent episode.")
        result = deep._find_recent_episodes()
        assert len(result) == 1

    def test_excludes_old_episodes(self, deep: DeepConsolidator, memory_dir: Path) -> None:
        old_date = (datetime.now(UTC) - timedelta(days=30)).strftime("%Y-%m-%d")
        _write_episode(memory_dir, "old", old_date, "Old episode.")
        result = deep._find_recent_episodes(lookback_days=7)
        assert len(result) == 0

    def test_empty_dir_returns_empty(self, deep: DeepConsolidator) -> None:
        result = deep._find_recent_episodes()
        assert result == []


class TestComputeIntensity:
    """Determine consolidation intensity from episode count."""

    def test_zero_episodes_skip(self, deep: DeepConsolidator) -> None:
        assert deep._compute_intensity(0) == "skip"

    def test_few_episodes_light(self, deep: DeepConsolidator) -> None:
        assert deep._compute_intensity(2) == "light"

    def test_many_episodes_full(self, deep: DeepConsolidator) -> None:
        assert deep._compute_intensity(5) == "full"


class TestContentHashGating:
    """Content-hash gating for 80-90% cost reduction."""

    def test_hash_matches_stored(self, deep: DeepConsolidator) -> None:
        deep._update_hash("test-entity", "abc123")
        assert deep._hash_matches("test-entity", "abc123") is True

    def test_hash_mismatch(self, deep: DeepConsolidator) -> None:
        deep._update_hash("test-entity", "abc123")
        assert deep._hash_matches("test-entity", "different") is False

    def test_no_stored_hash(self, deep: DeepConsolidator) -> None:
        assert deep._hash_matches("unknown-entity", "any") is False


class TestValidateRewrite:
    """5-step validation before writing rewritten entity."""

    def test_valid_content_passes(self, deep: DeepConsolidator, entities_dir: Path) -> None:
        _write_entity(entities_dir, "test", {}, "Content")
        path = entities_dir / "test.md"
        assert deep._validate_rewrite("# Updated\n\nNew content.", path) is True

    def test_empty_content_fails(self, deep: DeepConsolidator, entities_dir: Path) -> None:
        _write_entity(entities_dir, "test", {}, "Content")
        path = entities_dir / "test.md"
        assert deep._validate_rewrite("", path) is False

    def test_content_with_frontmatter_fails(
        self, deep: DeepConsolidator, entities_dir: Path,
    ) -> None:
        _write_entity(entities_dir, "test", {}, "Content")
        path = entities_dir / "test.md"
        assert deep._validate_rewrite("---\nfoo: bar\n---\n\nBody", path) is False

    def test_over_budget_content_fails(self, deep: DeepConsolidator, entities_dir: Path) -> None:
        _write_entity(entities_dir, "test", {}, "Content")
        path = entities_dir / "test.md"
        # Config default is 800 tokens, 110% = 880 words max
        long_content = "word " * 900
        assert deep._validate_rewrite(long_content, path) is False


class TestEnforceEntityBudget:
    """Budget enforcement truncates over-budget content."""

    def test_within_budget_unchanged(self, deep: DeepConsolidator) -> None:
        content = "Short content."
        assert deep._enforce_entity_budget(content) == content

    def test_over_budget_truncated(self, deep: DeepConsolidator) -> None:
        content = "x" * 10000
        result = deep._enforce_entity_budget(content)
        max_chars = deep._config.per_entity_budget * 4  # CHARS_PER_TOKEN
        assert len(result) <= max_chars


class TestWriteAheadManifest:
    """Crash safety via write-ahead manifest."""

    def test_write_and_clear_manifest(self, deep: DeepConsolidator) -> None:
        deep._write_manifest(["entity-a", "entity-b"])
        assert deep._manifest_path.exists()

        deep._clear_manifest()
        assert not deep._manifest_path.exists()

    def test_remove_from_manifest(self, deep: DeepConsolidator) -> None:
        deep._write_manifest(["entity-a", "entity-b"])
        deep._remove_from_manifest("entity-a")

        data = json.loads(deep._manifest_path.read_text())
        assert "entity-a" not in data["entities"]
        assert "entity-b" in data["entities"]


class TestEntityCentricPass:
    """Entity-centric pass: rewrite entities touched by episodes."""

    @pytest.mark.asyncio
    async def test_rewrites_touched_entity(
        self, deep: DeepConsolidator, memory_dir: Path, entities_dir: Path,
        idx: EntityIndex,
    ) -> None:
        today = datetime.now(UTC).strftime("%Y-%m-%d")
        _write_episode(
            memory_dir, "session", today,
            "Discussed [[test-entity]] and their project.",
        )
        _write_entity(
            entities_dir, "test-entity",
            {"entity_type": "project", "last_updated": "2026-01-01", "links_to": []},
            "# Test Entity\n\n## Summary\nOld summary.\n",
        )
        idx.refresh()

        model = _mock_model("# Test Entity\n\n## Summary\nUpdated summary with new facts.\n")
        result = await deep._entity_centric_pass(
            [memory_dir / "episodes" / f"{today}-session.md"], model, "test-agent", idx,
        )
        assert result["entities_rewritten"] >= 1

    @pytest.mark.asyncio
    async def test_skips_unchanged_entity_via_hash(
        self, deep: DeepConsolidator, memory_dir: Path, entities_dir: Path,
        idx: EntityIndex,
    ) -> None:
        today = datetime.now(UTC).strftime("%Y-%m-%d")
        ep_path = _write_episode(
            memory_dir, "session", today,
            "Discussed [[test-entity]].",
        )
        entity_path = _write_entity(
            entities_dir, "test-entity",
            {"entity_type": "project", "last_updated": "2026-01-01", "links_to": []},
            "# Test Entity\n",
        )
        idx.refresh()

        # Pre-compute and store hash
        entity_content = entity_path.read_text()
        ep_content = ep_path.read_text()
        input_hash = deep._compute_hash(entity_content + ep_content)
        deep._update_hash("test-entity", input_hash)

        model = _mock_model("")
        result = await deep._entity_centric_pass([ep_path], model, "test-agent", idx)
        assert result["skipped_unchanged"] == 1
        model.invoke.assert_not_called()


class TestGraphCentricPass:
    """Graph-centric pass: discover structural links."""

    @pytest.mark.asyncio
    async def test_discovers_links(
        self, deep: DeepConsolidator, entities_dir: Path, idx: EntityIndex,
    ) -> None:
        # Create a subdirectory with entities
        sub = entities_dir / "projects"
        sub.mkdir()
        _write_entity(sub, "proj-a", {"entity_type": "project", "links_to": []}, "Project A")
        _write_entity(sub, "proj-b", {"entity_type": "project", "links_to": []}, "Project B")
        idx.refresh()

        model = _mock_model(json.dumps([
            {"from": "proj-a", "to": "proj-b", "reason": "same domain"},
        ]))
        result = await deep._graph_centric_pass(model, idx)
        assert not result.get("skipped")

    @pytest.mark.asyncio
    async def test_no_entities_skips(
        self, deep: DeepConsolidator, idx: EntityIndex,
    ) -> None:
        model = _mock_model("[]")
        result = await deep._graph_centric_pass(model, idx)
        assert result.get("skipped") is True


class TestMergeDetection:
    """Find and merge duplicate entities."""

    @pytest.mark.asyncio
    async def test_detects_merge_candidates(
        self, deep: DeepConsolidator, entities_dir: Path, idx: EntityIndex,
    ) -> None:
        # Create entities sharing 3+ links
        shared = ["[[link-1]]", "[[link-2]]", "[[link-3]]"]
        _write_entity(entities_dir, "ent-a", {"links_to": shared}, "Entity A")
        _write_entity(entities_dir, "ent-b", {"links_to": shared}, "Entity B")
        # Create the linked entities
        for i in range(1, 4):
            _write_entity(entities_dir, f"link-{i}", {"links_to": []}, f"Link {i}")
        idx.refresh()

        adj = deep._build_adjacency(idx)
        candidates = deep._find_merge_candidates(adj)
        assert len(candidates) >= 1

    @pytest.mark.asyncio
    async def test_merge_confirmed_by_llm(
        self, deep: DeepConsolidator, entities_dir: Path, workspace: Path,
        idx: EntityIndex,
    ) -> None:
        shared = ["[[link-1]]", "[[link-2]]", "[[link-3]]"]
        _write_entity(entities_dir, "ent-a", {"links_to": shared}, "Entity A about dogs")
        _write_entity(entities_dir, "ent-b", {"links_to": shared}, "Entity B about dogs")
        for i in range(1, 4):
            _write_entity(entities_dir, f"link-{i}", {"links_to": []}, f"Link {i}")
        idx.refresh()

        model = _mock_model(json.dumps({"same_entity": True, "reason": "same topic"}))
        result = await deep._detect_merges(model, idx)
        assert result["merged"] >= 1

        # Archive dir should have the removed entity
        archive = workspace / "archive"
        assert archive.exists()


class TestFlagStaleEntities:
    """Staleness management: flag and archive stale entities."""

    def test_flags_stale_entity(
        self, deep: DeepConsolidator, entities_dir: Path, idx: EntityIndex,
    ) -> None:
        old_date = (datetime.now(UTC) - timedelta(days=100)).strftime("%Y-%m-%d")
        _write_entity(
            entities_dir, "stale-ent",
            {"entity_type": "concept", "last_verified": old_date, "status": "active"},
            "Old entity",
        )
        idx.refresh()
        result = deep._flag_stale_entities(idx)
        assert result["flagged"] == 1

        from arcagent.utils.sanitizer import read_frontmatter
        fm = read_frontmatter(entities_dir / "stale-ent.md")
        assert fm["status"] == "stale"

    def test_archives_very_stale_entity(
        self, deep: DeepConsolidator, entities_dir: Path, workspace: Path,
        idx: EntityIndex,
    ) -> None:
        very_old = (datetime.now(UTC) - timedelta(days=200)).strftime("%Y-%m-%d")
        _write_entity(
            entities_dir, "ancient-ent",
            {"entity_type": "concept", "last_verified": very_old, "status": "active"},
            "Very old entity",
        )
        idx.refresh()
        result = deep._flag_stale_entities(idx)
        assert result["archived"] == 1
        assert not (entities_dir / "ancient-ent.md").exists()
        assert (workspace / "archive" / "ancient-ent.md").exists()

    def test_skips_recently_verified(
        self, deep: DeepConsolidator, entities_dir: Path, idx: EntityIndex,
    ) -> None:
        today = datetime.now(UTC).strftime("%Y-%m-%d")
        _write_entity(
            entities_dir, "fresh-ent",
            {"entity_type": "concept", "last_verified": today, "status": "active"},
            "Fresh entity",
        )
        idx.refresh()
        result = deep._flag_stale_entities(idx)
        assert result["flagged"] == 0
        assert result["archived"] == 0


class TestRotationState:
    """State persistence across consolidation cycles."""

    def test_save_and_load_state(self, deep: DeepConsolidator) -> None:
        deep._save_rotation_state({"last_domain": "projects", "cycle_count": 5})
        state = deep._load_rotation_state()
        assert state["last_domain"] == "projects"
        assert state["cycle_count"] == 5

    def test_load_missing_state_returns_empty(self, deep: DeepConsolidator) -> None:
        state = deep._load_rotation_state()
        assert state == {}


class TestConsolidateOrchestrator:
    """Full consolidation cycle orchestration."""

    @pytest.mark.asyncio
    async def test_skips_when_no_recent_episodes(
        self, deep: DeepConsolidator,
    ) -> None:
        model = _mock_model("")
        result = await deep.consolidate(model, "test-agent")
        assert result["skipped"] is True
        assert result["reason"] == "no_recent_activity"

    @pytest.mark.asyncio
    async def test_runs_with_recent_episodes(
        self, deep: DeepConsolidator, memory_dir: Path,
    ) -> None:
        today = datetime.now(UTC).strftime("%Y-%m-%d")
        _write_episode(memory_dir, "ep1", today, "Session content.")

        # Model for entity pass (stub response)
        model = _mock_model(json.dumps({"updated": False, "content": None}))
        result = await deep.consolidate(model, "test-agent")
        assert "intensity" in result
        assert result["intensity"] in ("light", "full")
