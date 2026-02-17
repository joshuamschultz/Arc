"""Tests for EntityExtractor — markdown-based entity storage."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
import yaml


def _mock_model(
    *, return_value: Any = None, side_effect: Exception | None = None
) -> MagicMock:
    """Create a mock LLM model with invoke() returning LLMResponse-like object."""
    model = MagicMock()
    if side_effect is not None:
        model.invoke = AsyncMock(side_effect=side_effect)
    else:
        model.invoke = AsyncMock(return_value=MagicMock(content=return_value))
    return model

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


def _read_entity(path: Path) -> tuple[dict[str, Any], str]:
    """Read entity file, return (frontmatter_dict, body_text)."""
    text = path.read_text(encoding="utf-8")
    assert text.startswith("---")
    end = text.find("\n---", 3)
    fm = yaml.safe_load(text[4:end])
    body_start = end + 4
    if body_start < len(text) and text[body_start] == "\n":
        body_start += 1
    return fm, text[body_start:]


class TestTrivialExchangeSkip:
    """Short messages produce no entities."""

    @pytest.mark.asyncio()
    async def test_short_messages_skip(self, tmp_path: Path) -> None:
        ext = _make_extractor(tmp_path)
        model = _mock_model()
        messages = [
            {"role": "user", "content": "ok"},
            {"role": "assistant", "content": "sure"},
        ]
        await ext.extract(messages, model)
        model.invoke.assert_not_called()

    @pytest.mark.asyncio()
    async def test_empty_messages_skip(self, tmp_path: Path) -> None:
        ext = _make_extractor(tmp_path)
        model = _mock_model()
        await ext.extract([], model)
        model.invoke.assert_not_called()


class TestNewEntityCreation:
    """New entity creates a markdown file with frontmatter and facts."""

    @pytest.mark.asyncio()
    async def test_new_entity_creates_md_file(self, tmp_path: Path) -> None:
        ext = _make_extractor(tmp_path)
        model = _mock_model(return_value=json.dumps({
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

        entity_path = tmp_path / "entities" / "josh-schultz.md"
        assert entity_path.exists()

        fm, body = _read_entity(entity_path)
        assert fm["name"] == "Josh Schultz"
        assert fm["type"] == "person"
        assert "Josh" in fm["aliases"]
        assert "role: engineer" in body
        assert "(0.9)" in body


class TestExistingEntityUpdate:
    """Existing entity gets new facts appended, aliases merged."""

    @pytest.mark.asyncio()
    async def test_append_fact_to_existing(self, tmp_path: Path) -> None:
        ext = _make_extractor(tmp_path)

        # Pre-create entity file
        entities_dir = tmp_path / "entities"
        entities_dir.mkdir(parents=True)
        entity_path = entities_dir / "josh-schultz.md"
        entity_path.write_text(
            "---\n"
            "name: Josh Schultz\n"
            "type: person\n"
            "aliases:\n- Josh\n"
            "last_updated: '2026-02-14T10:00:00+00:00'\n"
            "---\n\n"
            "- role: engineer (0.9) [2026-02-14T10:00:00+00:00]\n"
        )

        model = _mock_model(return_value=json.dumps({
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

        content = entity_path.read_text(encoding="utf-8")
        assert "role: engineer" in content
        assert "location: Chicago" in content


class TestContradictionDetection:
    """Contradictions are marked with 'was: old_value'."""

    @pytest.mark.asyncio()
    async def test_contradiction_marks_old_value(self, tmp_path: Path) -> None:
        ext = _make_extractor(tmp_path)

        entities_dir = tmp_path / "entities"
        entities_dir.mkdir(parents=True)
        entity_path = entities_dir / "josh-schultz.md"
        entity_path.write_text(
            "---\n"
            "name: Josh Schultz\n"
            "type: person\n"
            "aliases: []\n"
            "last_updated: '2026-02-14T10:00:00+00:00'\n"
            "---\n\n"
            "- location: NYC (0.9) [2026-02-14T10:00:00+00:00]\n"
        )

        model = _mock_model(return_value=json.dumps({
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

        content = entity_path.read_text(encoding="utf-8")
        assert "location: Chicago" in content
        assert "was: NYC" in content


class TestCaseInsensitiveMatching:
    """Case-insensitive name matching via frontmatter scan."""

    def test_lowercase_matches_titlecase(self, tmp_path: Path) -> None:
        ext = _make_extractor(tmp_path)

        entities_dir = tmp_path / "entities"
        entities_dir.mkdir(parents=True)
        (entities_dir / "josh-schultz.md").write_text(
            "---\nname: Josh Schultz\ntype: person\naliases: []\n---\n"
        )

        slug = ext._resolve_slug("josh schultz")
        assert slug == "josh-schultz"


class TestAliasMatching:
    """Alias matching via frontmatter scan."""

    def test_alias_matches_entity(self, tmp_path: Path) -> None:
        ext = _make_extractor(tmp_path)

        entities_dir = tmp_path / "entities"
        entities_dir.mkdir(parents=True)
        (entities_dir / "josh-schultz.md").write_text(
            "---\nname: Josh Schultz\ntype: person\n"
            "aliases:\n- Mr. Schultz\n- Joshua\n---\n"
        )

        slug = ext._resolve_slug("Mr. Schultz")
        assert slug == "josh-schultz"


class TestAtomicWrite:
    """Entity files are written atomically."""

    @pytest.mark.asyncio()
    async def test_entity_written_atomically(self, tmp_path: Path) -> None:
        ext = _make_extractor(tmp_path)

        model = _mock_model(return_value=json.dumps({
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

        entity_path = tmp_path / "entities" / "test-entity.md"
        assert entity_path.exists()
        # Temp file should not linger
        assert not (tmp_path / "entities" / "test-entity.md.tmp").exists()


class TestEvalModelFailure:
    """Graceful skip on model failure."""

    @pytest.mark.asyncio()
    async def test_skip_on_model_error(self, tmp_path: Path) -> None:
        ext = _make_extractor(tmp_path, fallback_behavior="skip")
        model = _mock_model(side_effect=RuntimeError("model unavailable"))

        messages = [
            {"role": "user", "content": "I work at BlackArc building AI systems"},
            {"role": "assistant", "content": "Interesting work!"},
        ]
        await ext.extract(messages, model)

    @pytest.mark.asyncio()
    async def test_error_on_model_failure_when_configured(self, tmp_path: Path) -> None:
        ext = _make_extractor(tmp_path, fallback_behavior="error")
        model = _mock_model(side_effect=RuntimeError("model unavailable"))

        messages = [
            {"role": "user", "content": "I work at BlackArc building AI systems"},
            {"role": "assistant", "content": "Interesting work!"},
        ]
        with pytest.raises(RuntimeError, match="model unavailable"):
            await ext.extract(messages, model)


class TestMalformedModelResponse:
    """Handle invalid JSON from model."""

    @pytest.mark.asyncio()
    async def test_invalid_json_skipped(self, tmp_path: Path) -> None:
        ext = _make_extractor(tmp_path)
        model = _mock_model(return_value="not valid json {{{")

        messages = [
            {"role": "user", "content": "My name is Josh and I work on ArcAgent"},
            {"role": "assistant", "content": "Cool project!"},
        ]
        await ext.extract(messages, model)

    @pytest.mark.asyncio()
    async def test_missing_entities_key_skipped(self, tmp_path: Path) -> None:
        ext = _make_extractor(tmp_path)
        model = _mock_model(return_value=json.dumps({"other_field": "value"}))

        messages = [
            {"role": "user", "content": "My name is Josh and I work on ArcAgent"},
            {"role": "assistant", "content": "Cool project!"},
        ]
        await ext.extract(messages, model)

        entities_dir = tmp_path / "entities"
        assert not entities_dir.exists() or len(list(entities_dir.glob("*.md"))) == 0


class TestSlugifyEdgeCases:
    """Test _slugify with various edge cases."""

    def test_slugify_empty_string_uses_hash(self, tmp_path: Path) -> None:
        ext = _make_extractor(tmp_path)
        slug = ext._slugify("!!!")
        assert len(slug) == 12  # Hash-based slug

    def test_slugify_cyrillic_name(self, tmp_path: Path) -> None:
        ext = _make_extractor(tmp_path)
        slug = ext._slugify("Владимир")
        assert len(slug) == 12  # Hash-based


class TestEntityExtractionEdgeCases:
    """Test edge cases in entity extraction."""

    @pytest.mark.asyncio()
    async def test_entity_with_no_name_skipped(self, tmp_path: Path) -> None:
        ext = _make_extractor(tmp_path)
        model = _mock_model(return_value=json.dumps({
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

        entities_dir = tmp_path / "entities"
        assert not entities_dir.exists() or len(list(entities_dir.glob("*.md"))) == 0


class TestExtractNoRecentPair:
    """_get_recent_pair returns empty for non-user/assistant messages."""

    async def test_extract_skips_system_only_messages(self, tmp_path: Path) -> None:
        ext = _make_extractor(tmp_path)
        model = _mock_model()
        await ext.extract([{"role": "system", "content": "You are helpful"}], model)
        model.invoke.assert_not_called()


class TestFrontmatterParsing:
    """Test frontmatter reading and stripping."""

    def test_read_frontmatter_valid(self, tmp_path: Path) -> None:
        path = tmp_path / "test.md"
        path.write_text("---\nname: Test\ntype: concept\n---\nbody")

        result = EntityExtractor._read_frontmatter(path)
        assert result == {"name": "Test", "type": "concept"}

    def test_read_frontmatter_missing(self, tmp_path: Path) -> None:
        path = tmp_path / "test.md"
        path.write_text("no frontmatter here")

        result = EntityExtractor._read_frontmatter(path)
        assert result is None

    def test_read_frontmatter_corrupted(self, tmp_path: Path) -> None:
        path = tmp_path / "test.md"
        path.write_text("---\n: : invalid yaml [[\n---\nbody")

        result = EntityExtractor._read_frontmatter(path)
        assert result is None

    def test_strip_frontmatter(self) -> None:
        text = "---\nname: Test\n---\nbody content"
        result = EntityExtractor._strip_frontmatter(text)
        assert result == "body content"


class TestFactParsing:
    """Test fact line parsing for contradiction detection."""

    def test_parse_simple_fact(self) -> None:
        content = "- role: engineer (0.9) [2026-02-14T10:00:00+00:00]"
        facts = EntityExtractor._parse_facts(content)
        assert len(facts) == 1
        assert facts[0]["predicate"] == "role"
        assert facts[0]["value"] == "engineer"

    def test_parse_fact_with_supersession(self) -> None:
        content = "- location: Chicago (0.8) [2026-02-15] | was: NYC"
        facts = EntityExtractor._parse_facts(content)
        assert len(facts) == 1
        assert facts[0]["predicate"] == "location"
        assert facts[0]["value"] == "Chicago"

    def test_parse_ignores_non_fact_lines(self) -> None:
        content = "---\nname: Test\n---\nSome description\n- role: eng (0.9) [2026-02-14]"
        facts = EntityExtractor._parse_facts(content)
        assert len(facts) == 1


class TestFactValueSanitization:
    """ASI-06: Fact values are sanitized against memory poisoning."""

    def test_unicode_nfkc_normalization(self, tmp_path: Path) -> None:
        ext = _make_extractor(tmp_path)
        result = ext._sanitize_fact_text("\uff21\uff22\uff23")
        assert result == "ABC"

    def test_zero_width_characters_stripped(self, tmp_path: Path) -> None:
        ext = _make_extractor(tmp_path)
        result = ext._sanitize_fact_text("hello\u200bworld\u200d")
        assert result == "helloworld"

    def test_control_characters_stripped(self, tmp_path: Path) -> None:
        ext = _make_extractor(tmp_path)
        result = ext._sanitize_fact_text("clean\x00\x01\x08text")
        assert result == "cleantext"

    def test_length_limit_enforced(self, tmp_path: Path) -> None:
        ext = _make_extractor(tmp_path)
        long_text = "x" * 5000
        result = ext._sanitize_fact_text(long_text)
        assert len(result) == 2000

    @pytest.mark.asyncio()
    async def test_stored_facts_are_sanitized(self, tmp_path: Path) -> None:
        """End-to-end: extracted facts have sanitized predicate and value."""
        ext = _make_extractor(tmp_path)
        model = _mock_model(return_value=json.dumps({
            "entities": [{
                "name": "Test",
                "type": "concept",
                "aliases": [],
                "facts": [{
                    "predicate": "has\u200b_type",
                    "value": "injected\x00value\ufeff",
                    "confidence": 0.9,
                }],
            }]
        }))

        messages = [
            {"role": "user", "content": "Tell me about the test concept here"},
            {"role": "assistant", "content": "Here is detailed info about it"},
        ]
        await ext.extract(messages, model)

        entity_path = tmp_path / "entities" / "test.md"
        assert entity_path.exists()
        content = entity_path.read_text(encoding="utf-8")
        assert "\u200b" not in content
        assert "\x00" not in content
        assert "\ufeff" not in content
