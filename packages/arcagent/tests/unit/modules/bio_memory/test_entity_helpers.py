"""Tests for entity_helpers — shared entity file operations."""

from __future__ import annotations

from pathlib import Path

import yaml

from arcagent.modules.bio_memory.entity_helpers import (
    WIKI_LINK_RE,
    EntityIndex,
    add_link_to_frontmatter,
    append_to_section,
    extract_wiki_links,
    normalize_entity_file,
    resolve_entity_path,
    today_str,
    update_frontmatter_field,
    validate_entity_path,
)


def _write_entity(
    entities_dir: Path,
    name: str,
    fm: dict[str, object],
    body: str,
) -> Path:
    fm_text = yaml.dump(fm, default_flow_style=False).strip()
    content = f"---\n{fm_text}\n---\n\n{body}\n"
    path = entities_dir / f"{name}.md"
    path.write_text(content, encoding="utf-8")
    return path


class TestTodayStr:
    def test_returns_date_format(self) -> None:
        result = today_str()
        assert len(result) == 10
        assert result[4] == "-" and result[7] == "-"


class TestResolveEntityPath:
    def test_direct_match(self, tmp_path: Path) -> None:
        entities = tmp_path / "entities"
        entities.mkdir()
        _write_entity(entities, "test", {}, "body")
        result = resolve_entity_path("test", entities, tmp_path)
        assert result is not None
        assert result.stem == "test"

    def test_subdir_match(self, tmp_path: Path) -> None:
        entities = tmp_path / "entities"
        sub = entities / "people"
        sub.mkdir(parents=True)
        _write_entity(sub, "josh", {}, "body")
        result = resolve_entity_path("josh", entities, tmp_path)
        assert result is not None
        assert result.stem == "josh"

    def test_not_found(self, tmp_path: Path) -> None:
        entities = tmp_path / "entities"
        entities.mkdir()
        assert resolve_entity_path("missing", entities, tmp_path) is None

    def test_no_entities_dir(self, tmp_path: Path) -> None:
        entities = tmp_path / "entities"  # doesn't exist
        assert resolve_entity_path("test", entities, tmp_path) is None


class TestValidateEntityPath:
    def test_valid_path(self, tmp_path: Path) -> None:
        path = tmp_path / "entities" / "test.md"
        path.parent.mkdir()
        path.touch()
        assert validate_entity_path(path, tmp_path) is not None

    def test_escaping_workspace(self, tmp_path: Path) -> None:
        path = tmp_path / ".." / "escape.md"
        assert validate_entity_path(path, tmp_path) is None


class TestUpdateFrontmatterField:
    def test_updates_existing_field(self) -> None:
        text = "---\nstatus: active\n---\n\nBody"
        result = update_frontmatter_field(text, "status", "stale")
        assert "stale" in result

    def test_adds_new_field(self) -> None:
        text = "---\nstatus: active\n---\n\nBody"
        result = update_frontmatter_field(text, "verified", "2026-01-01")
        assert "2026-01-01" in result

    def test_no_frontmatter_unchanged(self) -> None:
        text = "Just text, no frontmatter"
        assert update_frontmatter_field(text, "key", "val") == text


class TestAppendToSection:
    def test_appends_to_existing_section(self) -> None:
        text = "## Key Facts\n- old fact\n\n## Summary\nText"
        result = append_to_section(text, "## Key Facts", "- new fact\n")
        assert "- new fact" in result
        assert result.index("new fact") < result.index("## Summary")

    def test_creates_section_if_missing(self) -> None:
        text = "# Entity\n\n## Summary\nText"
        result = append_to_section(text, "## Key Facts", "- fact\n")
        assert "## Key Facts" in result
        assert "- fact" in result

    def test_appends_at_end_when_no_next_section(self) -> None:
        text = "## Key Facts\n- old fact"
        result = append_to_section(text, "## Key Facts", "- new fact\n")
        assert "- new fact" in result


class TestAddLinkToFrontmatter:
    def test_adds_link(self, tmp_path: Path) -> None:
        path = _write_entity(tmp_path, "a", {"links_to": []}, "body")
        assert add_link_to_frontmatter(path, "b") is True
        from arcagent.utils.sanitizer import read_frontmatter

        fm = read_frontmatter(path)
        assert "[[b]]" in fm["links_to"]

    def test_skips_duplicate(self, tmp_path: Path) -> None:
        path = _write_entity(tmp_path, "a", {"links_to": ["[[b]]"]}, "body")
        assert add_link_to_frontmatter(path, "b") is False


class TestNormalizeEntityFile:
    def test_adds_frontmatter(self, tmp_path: Path) -> None:
        path = tmp_path / "test.md"
        path.write_text("# Test Entity\n\nContent.")
        normalize_entity_file(path, tmp_path)
        text = path.read_text()
        assert text.startswith("---\n")

    def test_skips_existing_frontmatter(self, tmp_path: Path) -> None:
        path = tmp_path / "test.md"
        path.write_text("---\ntype: test\n---\n\nContent.")
        normalize_entity_file(path, tmp_path)
        assert path.read_text().count("---") == 2

    def test_infers_type_from_subdirectory(self, tmp_path: Path) -> None:
        entities = tmp_path / "entities"
        sub = entities / "people"
        sub.mkdir(parents=True)
        path = sub / "josh.md"
        path.write_text("# Josh\n\nA person.")
        normalize_entity_file(path, entities)
        from arcagent.utils.sanitizer import read_frontmatter

        fm = read_frontmatter(path)
        assert (
            fm["entity_type"] == "people"
        )  # rstrip("s") not applied since parent is entities_dir check


class TestExtractWikiLinks:
    def test_extracts_links(self) -> None:
        text = "Linked to [[josh]] and [[project-x]]."
        result = extract_wiki_links(text)
        assert "josh" in result
        assert "project-x" in result

    def test_empty_text(self) -> None:
        assert extract_wiki_links("") == []

    def test_no_links(self) -> None:
        assert extract_wiki_links("No links here.") == []


class TestWikiLinkRegex:
    def test_matches(self) -> None:
        assert WIKI_LINK_RE.findall("[[test]]") == ["test"]

    def test_multiple(self) -> None:
        result = WIKI_LINK_RE.findall("[[a]] and [[b]]")
        assert result == ["a", "b"]


class TestEntityIndex:
    def test_resolve_direct_match(self, tmp_path: Path) -> None:
        entities = tmp_path / "entities"
        entities.mkdir()
        _write_entity(entities, "test", {}, "body")
        idx = EntityIndex(entities, tmp_path)
        assert idx.resolve("test") is not None
        assert idx.resolve("test").stem == "test"

    def test_resolve_subdir_match(self, tmp_path: Path) -> None:
        entities = tmp_path / "entities"
        sub = entities / "people"
        sub.mkdir(parents=True)
        _write_entity(sub, "josh", {}, "body")
        idx = EntityIndex(entities, tmp_path)
        assert idx.resolve("josh") is not None

    def test_resolve_not_found(self, tmp_path: Path) -> None:
        entities = tmp_path / "entities"
        entities.mkdir()
        idx = EntityIndex(entities, tmp_path)
        assert idx.resolve("missing") is None

    def test_resolve_no_entities_dir(self, tmp_path: Path) -> None:
        entities = tmp_path / "entities"  # doesn't exist
        idx = EntityIndex(entities, tmp_path)
        assert idx.resolve("any") is None

    def test_all_files(self, tmp_path: Path) -> None:
        entities = tmp_path / "entities"
        entities.mkdir()
        _write_entity(entities, "a", {}, "body a")
        _write_entity(entities, "b", {}, "body b")
        idx = EntityIndex(entities, tmp_path)
        assert len(idx.all_files()) == 2

    def test_refresh_picks_up_new_files(self, tmp_path: Path) -> None:
        entities = tmp_path / "entities"
        entities.mkdir()
        _write_entity(entities, "a", {}, "body")
        idx = EntityIndex(entities, tmp_path)
        assert len(idx.all_files()) == 1

        _write_entity(entities, "b", {}, "body")
        # Before refresh, cache is stale
        assert len(idx.all_files()) == 1
        idx.refresh()
        assert len(idx.all_files()) == 2

    def test_lazy_build(self, tmp_path: Path) -> None:
        entities = tmp_path / "entities"
        entities.mkdir()
        idx = EntityIndex(entities, tmp_path)
        # Cache not built yet
        assert idx._cache is None
        idx.all_files()
        assert idx._cache is not None
