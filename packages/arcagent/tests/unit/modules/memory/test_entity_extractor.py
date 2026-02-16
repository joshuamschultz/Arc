"""Tests for EntityExtractor — async LLM-driven entity extraction."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from arcagent.core.config import EvalConfig
from arcagent.modules.memory.entity_extractor import EntityExtractor


def _make_telemetry() -> MagicMock:
    t = MagicMock()
    t.audit_event = MagicMock()
    return t


def _make_extractor(
    workspace: Path,
    *,
    fallback_behavior: str = "skip",
) -> EntityExtractor:
    return EntityExtractor(
        eval_config=EvalConfig(fallback_behavior=fallback_behavior),
        workspace=workspace,
        telemetry=_make_telemetry(),
    )


class TestTrivialExchangeSkip:
    """T4.1.2: Short messages produce no entities."""

    @pytest.mark.asyncio()
    async def test_short_messages_skip(self, tmp_path: Path) -> None:
        ext = _make_extractor(tmp_path)
        model = AsyncMock()
        messages = [
            {"role": "user", "content": "ok"},
            {"role": "assistant", "content": "sure"},
        ]
        await ext.extract(messages, model)
        model.assert_not_called()

    @pytest.mark.asyncio()
    async def test_empty_messages_skip(self, tmp_path: Path) -> None:
        ext = _make_extractor(tmp_path)
        model = AsyncMock()
        await ext.extract([], model)
        model.assert_not_called()


class TestNewEntityCreation:
    """T4.1.3: New entity creates directory, facts.jsonl, index update."""

    @pytest.mark.asyncio()
    async def test_new_entity_creates_files(self, tmp_path: Path) -> None:
        ext = _make_extractor(tmp_path)
        model = AsyncMock(return_value=json.dumps({
            "entities": [{
                "name": "Josh Schultz",
                "type": "person",
                "aliases": ["Josh"],
                "facts": [{"predicate": "role", "value": "engineer", "confidence": 0.9}],
            }]
        }))

        messages = [
            {"role": "user", "content": "I'm Josh Schultz, a software engineer"},
            {"role": "assistant", "content": "Nice to meet you, Josh!"},
        ]
        await ext.extract(messages, model)

        # Entity directory created
        entity_dir = tmp_path / "entities" / "josh-schultz"
        assert entity_dir.exists()

        # Facts file created
        facts_file = entity_dir / "facts.jsonl"
        assert facts_file.exists()
        fact = json.loads(facts_file.read_text().strip())
        assert fact["predicate"] == "role"
        assert fact["value"] == "engineer"

        # Index updated
        index_file = tmp_path / "entities" / "index.json"
        assert index_file.exists()
        index = json.loads(index_file.read_text())
        assert "josh-schultz" in index["entities"]


class TestExistingEntityUpdate:
    """T4.1.4: Existing entity appends fact, updates index."""

    @pytest.mark.asyncio()
    async def test_append_fact_to_existing(self, tmp_path: Path) -> None:
        ext = _make_extractor(tmp_path)

        # Pre-create entity
        entity_dir = tmp_path / "entities" / "josh-schultz"
        entity_dir.mkdir(parents=True)
        facts_file = entity_dir / "facts.jsonl"
        existing_fact = {"predicate": "role", "value": "engineer", "confidence": 0.9,
                         "timestamp": "2026-02-14T10:00:00Z", "status": "active"}
        facts_file.write_text(json.dumps(existing_fact) + "\n")

        index_file = tmp_path / "entities" / "index.json"
        index_file.write_text(json.dumps({
            "version": 1,
            "entities": {
                "josh-schultz": {
                    "name": "Josh Schultz",
                    "type": "person",
                    "aliases": ["Josh"],
                    "last_updated": "2026-02-14T10:00:00Z",
                    "fact_count": 1,
                }
            }
        }))

        model = AsyncMock(return_value=json.dumps({
            "entities": [{
                "name": "Josh Schultz",
                "type": "person",
                "aliases": ["Josh"],
                "facts": [{"predicate": "location", "value": "Chicago", "confidence": 0.8}],
            }]
        }))

        messages = [
            {"role": "user", "content": "I live in Chicago"},
            {"role": "assistant", "content": "Chicago is a great city!"},
        ]
        await ext.extract(messages, model)

        # Should have 2 facts now
        lines = facts_file.read_text().strip().split("\n")
        assert len(lines) == 2

        # Index fact count updated
        updated_index = json.loads(index_file.read_text())
        assert updated_index["entities"]["josh-schultz"]["fact_count"] == 2


class TestContradictionDetection:
    """T4.1.5: Contradictions supersede old facts."""

    @pytest.mark.asyncio()
    async def test_contradiction_marks_old_as_superseded(self, tmp_path: Path) -> None:
        ext = _make_extractor(tmp_path)

        entity_dir = tmp_path / "entities" / "josh-schultz"
        entity_dir.mkdir(parents=True)
        facts_file = entity_dir / "facts.jsonl"
        old_fact = {"predicate": "location", "value": "NYC", "confidence": 0.9,
                    "timestamp": "2026-02-14T10:00:00Z", "status": "active"}
        facts_file.write_text(json.dumps(old_fact) + "\n")

        index_file = tmp_path / "entities" / "index.json"
        index_file.write_text(json.dumps({
            "version": 1,
            "entities": {
                "josh-schultz": {
                    "name": "Josh Schultz", "type": "person",
                    "aliases": [], "last_updated": "2026-02-14T10:00:00Z", "fact_count": 1,
                }
            }
        }))

        model = AsyncMock(return_value=json.dumps({
            "entities": [{
                "name": "Josh Schultz",
                "type": "person",
                "aliases": [],
                "facts": [{"predicate": "location", "value": "Chicago", "confidence": 0.9}],
            }]
        }))

        messages = [
            {"role": "user", "content": "I moved to Chicago recently"},
            {"role": "assistant", "content": "Hope you like Chicago!"},
        ]
        await ext.extract(messages, model)

        lines = facts_file.read_text().strip().split("\n")
        # Old fact + supersede marker + new fact
        assert len(lines) >= 2
        # New fact should reference supersession
        new_fact = json.loads(lines[-1])
        assert new_fact["value"] == "Chicago"
        assert "supersedes" in new_fact


class TestCaseInsensitiveMatching:
    """T4.1.6: Case-insensitive name matching."""

    @pytest.mark.asyncio()
    async def test_lowercase_matches_titlecase(self, tmp_path: Path) -> None:
        ext = _make_extractor(tmp_path)

        index_file = tmp_path / "entities" / "index.json"
        (tmp_path / "entities" / "josh-schultz").mkdir(parents=True)
        (tmp_path / "entities" / "josh-schultz" / "facts.jsonl").touch()
        index_file.write_text(json.dumps({
            "version": 1,
            "entities": {
                "josh-schultz": {
                    "name": "Josh Schultz", "type": "person",
                    "aliases": [], "last_updated": "2026-02-14T10:00:00Z", "fact_count": 0,
                }
            }
        }))

        result = ext._find_existing_entity("josh schultz", json.loads(index_file.read_text()))
        assert result == "josh-schultz"


class TestAliasMatching:
    """T4.1.7: Alias matching."""

    @pytest.mark.asyncio()
    async def test_alias_matches_entity(self, tmp_path: Path) -> None:
        ext = _make_extractor(tmp_path)

        index = {
            "version": 1,
            "entities": {
                "josh-schultz": {
                    "name": "Josh Schultz", "type": "person",
                    "aliases": ["Mr. Schultz", "Joshua"],
                    "last_updated": "2026-02-14T10:00:00Z", "fact_count": 0,
                }
            }
        }
        result = ext._find_existing_entity("Mr. Schultz", index)
        assert result == "josh-schultz"


class TestAtomicIndexWrite:
    """T4.1.8: Write-to-temp + rename."""

    @pytest.mark.asyncio()
    async def test_index_written_atomically(self, tmp_path: Path) -> None:
        ext = _make_extractor(tmp_path)
        (tmp_path / "entities").mkdir(parents=True)

        model = AsyncMock(return_value=json.dumps({
            "entities": [{
                "name": "Test Entity",
                "type": "concept",
                "aliases": [],
                "facts": [{"predicate": "is", "value": "test", "confidence": 0.5}],
            }]
        }))

        messages = [
            {"role": "user", "content": "Tell me about the test entity concept"},
            {"role": "assistant", "content": "Here is info about the test entity"},
        ]
        await ext.extract(messages, model)

        index_file = tmp_path / "entities" / "index.json"
        assert index_file.exists()
        # Temp file should not exist after atomic rename
        assert not (tmp_path / "entities" / "index.json.tmp").exists()


class TestEvalModelFailure:
    """T4.1.10: Graceful skip on model failure."""

    @pytest.mark.asyncio()
    async def test_skip_on_model_error(self, tmp_path: Path) -> None:
        ext = _make_extractor(tmp_path, fallback_behavior="skip")
        model = AsyncMock(side_effect=RuntimeError("model unavailable"))

        messages = [
            {"role": "user", "content": "I work at BlackArc building AI systems"},
            {"role": "assistant", "content": "Interesting work!"},
        ]
        # Should not raise
        await ext.extract(messages, model)

    @pytest.mark.asyncio()
    async def test_error_on_model_failure_when_configured(self, tmp_path: Path) -> None:
        ext = _make_extractor(tmp_path, fallback_behavior="error")
        model = AsyncMock(side_effect=RuntimeError("model unavailable"))

        messages = [
            {"role": "user", "content": "I work at BlackArc building AI systems"},
            {"role": "assistant", "content": "Interesting work!"},
        ]
        with pytest.raises(RuntimeError, match="model unavailable"):
            await ext.extract(messages, model)


class TestMalformedModelResponse:
    """Extra safety: handle invalid JSON from model."""

    @pytest.mark.asyncio()
    async def test_invalid_json_skipped(self, tmp_path: Path) -> None:
        ext = _make_extractor(tmp_path)
        model = AsyncMock(return_value="not valid json {{{")

        messages = [
            {"role": "user", "content": "My name is Josh and I work on ArcAgent"},
            {"role": "assistant", "content": "Cool project!"},
        ]
        # Should not raise
        await ext.extract(messages, model)

    @pytest.mark.asyncio()
    async def test_missing_entities_key_skipped(self, tmp_path: Path) -> None:
        """Model response missing 'entities' key is handled."""
        ext = _make_extractor(tmp_path)
        model = AsyncMock(return_value=json.dumps({"other_field": "value"}))

        messages = [
            {"role": "user", "content": "My name is Josh and I work on ArcAgent"},
            {"role": "assistant", "content": "Cool project!"},
        ]
        # Should not raise, should skip
        await ext.extract(messages, model)

        # No entities created
        entities_dir = tmp_path / "entities"
        assert not entities_dir.exists() or len(list(entities_dir.glob("*"))) == 0


class TestSlugifyEdgeCases:
    """Test _slugify with various edge cases."""

    def test_slugify_empty_string_uses_hash(self, tmp_path: Path) -> None:
        """Empty or non-ASCII-only names use hash-based slugs."""
        ext = _make_extractor(tmp_path)
        # Only special characters, no alphanumeric
        slug = ext._slugify("!!!")
        assert len(slug) == 12  # Hash-based slug

    def test_slugify_cyrillic_name(self, tmp_path: Path) -> None:
        """Non-ASCII names fall back to hash."""
        ext = _make_extractor(tmp_path)
        slug = ext._slugify("Владимир")
        assert len(slug) == 12  # Hash-based


class TestEntityExtractionEdgeCases:
    """Test edge cases in entity extraction."""

    @pytest.mark.asyncio()
    async def test_entity_with_no_name_skipped(self, tmp_path: Path) -> None:
        """Entity data missing 'name' field is skipped."""
        ext = _make_extractor(tmp_path)
        model = AsyncMock(return_value=json.dumps({
            "entities": [{
                "type": "concept",
                "facts": [{"predicate": "is", "value": "test"}],
            }]
        }))

        messages = [
            {"role": "user", "content": "Tell me about the concept"},
            {"role": "assistant", "content": "Here is the info"},
        ]
        await ext.extract(messages, model)

        # No entity should be created
        entities_dir = tmp_path / "entities"
        assert not entities_dir.exists() or len(list(entities_dir.glob("*"))) == 0

    @pytest.mark.asyncio()
    async def test_malformed_facts_jsonl_handling(self, tmp_path: Path) -> None:
        """Existing facts.jsonl with invalid JSON lines is handled."""
        ext = _make_extractor(tmp_path)

        # Pre-create entity with malformed facts
        entity_dir = tmp_path / "entities" / "test-entity"
        entity_dir.mkdir(parents=True)
        facts_file = entity_dir / "facts.jsonl"
        facts_file.write_text('{"predicate": "valid", "value": "ok"}\ninvalid json line\n')

        index_file = tmp_path / "entities" / "index.json"
        index_file.write_text(json.dumps({
            "version": 1,
            "entities": {
                "test-entity": {
                    "name": "Test Entity",
                    "type": "concept",
                    "aliases": [],
                    "last_updated": "2026-02-15T10:00:00Z",
                    "fact_count": 1,
                }
            }
        }))

        model = AsyncMock(return_value=json.dumps({
            "entities": [{
                "name": "Test Entity",
                "type": "concept",
                "aliases": [],
                "facts": [{"predicate": "new", "value": "fact"}],
            }]
        }))

        messages = [
            {"role": "user", "content": "Tell me about Test Entity"},
            {"role": "assistant", "content": "Here is new info"},
        ]

        # Should handle malformed line gracefully
        await ext.extract(messages, model)

        # New fact should be appended
        lines = facts_file.read_text().strip().split("\n")
        assert len(lines) >= 2  # Original valid + new fact


class TestExtractNoRecentPair:
    """Line 92: _get_recent_pair returns empty for non-user/assistant messages."""

    async def test_extract_skips_system_only_messages(self, tmp_path: Path) -> None:
        ext = EntityExtractor(
            eval_config=EvalConfig(provider="test", model="test"),
            workspace=tmp_path,
            telemetry=_make_telemetry(),
        )
        model = AsyncMock()
        # Only system messages — no user/assistant pair
        await ext.extract([{"role": "system", "content": "You are helpful"}], model)
        model.assert_not_called()


class TestLoadIndexJsonDecodeError:
    """Lines 254-255: Corrupted index.json returns default."""

    def test_corrupted_index_returns_default(self, tmp_path: Path) -> None:
        ext = EntityExtractor(
            eval_config=EvalConfig(provider="test", model="test"),
            workspace=tmp_path,
            telemetry=_make_telemetry(),
        )
        entities_dir = tmp_path / "entities"
        entities_dir.mkdir()
        (entities_dir / "index.json").write_text("{bad json!!")

        index = ext._load_index()
        assert index == {"version": 1, "entities": {}}
