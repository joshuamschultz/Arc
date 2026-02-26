"""Retriever — grep-based search with wiki-link graph traversal.

Two-pass search: frontmatter grep for tag/entity matches, then full-text
on matched subset. Wiki-links followed one hop. Budget enforcement via
configurable overflow strategy. Searches memory/, workspace/entities/,
and optionally team entities.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from arcagent.modules.bio_memory.config import BioMemoryConfig
from arcagent.modules.bio_memory.entity_helpers import WIKI_LINK_RE, EntityIndex
from arcagent.utils.io import CHARS_PER_TOKEN
from arcagent.utils.sanitizer import read_frontmatter, sanitize_wiki_link

# Scoring constants
_FRONTMATTER_BOOST = 5.0
_WIKI_LINK_DECAY = 0.5
_TEAM_SCORE_PENALTY = 0.8


@dataclass
class RetrievalResult:
    """A single search result from memory retrieval."""

    source: str
    content: str
    score: float
    match_type: str  # "frontmatter", "fulltext", "wiki_link"


class Retriever:
    """Grep + wiki-link graph traversal across memory tiers."""

    def __init__(
        self,
        memory_dir: Path,
        config: BioMemoryConfig,
        workspace: Path | None = None,
        team_entities_dir: Path | None = None,
    ) -> None:
        self._memory_dir = memory_dir
        self._config = config
        self._workspace = workspace or memory_dir.parent
        self._entities_dir = self._workspace / config.entities_dirname
        self._team_entities_dir = team_entities_dir

    async def search(
        self,
        query: str,
        top_k: int = 10,
        scope: str | None = None,
    ) -> list[RetrievalResult]:
        """Two-pass search: frontmatter grep, full-text on matches, wiki-link follow."""
        idx = EntityIndex(self._entities_dir, self._workspace)
        files = self._discover_files(scope=scope, idx=idx)
        if not files:
            return []

        # Per-search content cache — each file read once, reused across passes
        content_cache: dict[Path, str] = {}

        # Pass 1: frontmatter grep — filter by tag/entity match
        fm_matches = self._frontmatter_grep(query, files)

        # Pass 2: full-text grep — score all files, boost frontmatter matches
        scored = self._fulltext_grep(query, files, fm_matches, content_cache)

        # Sort by score descending, take top_k
        scored.sort(key=lambda r: r.score, reverse=True)
        results = scored[:top_k]

        # Follow wiki-links from top results (one hop)
        linked = self._follow_wiki_links_from_results(results, idx, content_cache)
        for link_result in linked:
            if not any(r.source == link_result.source for r in results):
                results.append(link_result)

        # Re-sort and trim to top_k
        results.sort(key=lambda r: r.score, reverse=True)
        results = results[:top_k]

        # Enforce token budget
        return self._enforce_budget(results)

    async def recall(self, name: str) -> str | None:
        """Retrieve specific entity/episode by name (exact match on slug).

        Validates resolved path stays within allowed directories to prevent
        path traversal attacks (SEC-9, ASI-06).
        """
        # Check episodes directory
        episodes_dir = self._memory_dir / self._config.episodes_dirname
        if episodes_dir.exists():
            path = (episodes_dir / f"{name}.md").resolve()
            if self._is_within_bounds(path) and path.exists():
                return path.read_text(encoding="utf-8")

        # Check top-level memory files
        path = (self._memory_dir / f"{name}.md").resolve()
        if self._is_within_bounds(path) and path.exists():
            return path.read_text(encoding="utf-8")

        # Check entities directory (index-backed O(1) lookup)
        idx = EntityIndex(self._entities_dir, self._workspace)
        entity_path = idx.resolve(name)
        if entity_path is not None and entity_path.exists():
            return entity_path.read_text(encoding="utf-8")

        return None

    def _is_within_bounds(self, path: Path) -> bool:
        """Verify resolved path is within memory_dir or entities_dir (path traversal defense)."""
        resolved_mem = self._memory_dir.resolve()
        resolved_ent = self._entities_dir.resolve()
        try:
            path.relative_to(resolved_mem)
            return True
        except ValueError:
            pass
        try:
            path.relative_to(resolved_ent)
            return True
        except ValueError:
            pass
        # Also allow team entities
        if self._team_entities_dir:
            try:
                path.relative_to(self._team_entities_dir.resolve())
                return True
            except ValueError:
                pass
        return False

    def _frontmatter_grep(
        self, query: str, files: list[Path],
    ) -> set[Path]:
        """Pass 1: grep YAML frontmatter for tag/entity matches."""
        matches: set[Path] = set()
        query_lower = query.lower()
        terms = query_lower.split()

        for path in files:
            fm = read_frontmatter(path)
            if fm is None:
                continue
            # Check tags, entities, and other list fields
            for key in ("tags", "entities", "entity_refs", "topics"):
                values = fm.get(key, [])
                if isinstance(values, list):
                    for val in values:
                        val_lower = str(val).lower()
                        if any(t in val_lower for t in terms):
                            matches.add(path)
                            break
                if path in matches:
                    break
        return matches

    def _fulltext_grep(
        self,
        query: str,
        files: list[Path],
        fm_matches: set[Path],
        content_cache: dict[Path, str],
    ) -> list[RetrievalResult]:
        """Pass 2: full-text search on files, score by match count."""
        results: list[RetrievalResult] = []
        query_lower = query.lower()
        terms = query_lower.split()

        for path in files:
            try:
                if path in content_cache:
                    content = content_cache[path]
                else:
                    content = path.read_text(encoding="utf-8")
                    content_cache[path] = content
            except (OSError, UnicodeDecodeError):
                continue

            content_lower = content.lower()
            match_count = sum(content_lower.count(t) for t in terms)

            if match_count == 0 and path not in fm_matches:
                continue

            # Score: match count normalized, with frontmatter boost
            score = float(match_count)
            match_type = "fulltext"

            if path in fm_matches:
                score += _FRONTMATTER_BOOST
                match_type = "frontmatter"

            # Apply team penalty for team entity files
            if self._team_entities_dir and self._is_team_entity(path):
                score *= _TEAM_SCORE_PENALTY

            source = self._relative_source(path)
            results.append(RetrievalResult(
                source=source,
                content=content,
                score=score,
                match_type=match_type,
            ))

        return results

    def _is_team_entity(self, path: Path) -> bool:
        """Check if a file belongs to team entities directory."""
        if not self._team_entities_dir:
            return False
        try:
            path.resolve().relative_to(self._team_entities_dir.resolve())
            return True
        except ValueError:
            return False

    def _relative_source(self, path: Path) -> str:
        """Compute relative source path for display."""
        # Try memory_dir first (backward compat)
        try:
            return str(path.relative_to(self._memory_dir))
        except ValueError:
            pass
        # Try workspace
        try:
            return str(path.relative_to(self._workspace))
        except ValueError:
            pass
        return str(path)

    def _follow_wiki_links_from_results(
        self,
        results: list[RetrievalResult],
        idx: EntityIndex,
        content_cache: dict[Path, str],
    ) -> list[RetrievalResult]:
        """Extract wiki-links from results and resolve to files (one hop)."""
        linked: list[RetrievalResult] = []
        seen_sources: set[str] = {r.source for r in results}

        for result in results:
            for link_path in self._follow_wiki_links(result.content, idx=idx):
                source = self._relative_source(link_path)
                if source in seen_sources:
                    continue
                seen_sources.add(source)
                try:
                    if link_path in content_cache:
                        content = content_cache[link_path]
                    else:
                        content = link_path.read_text(encoding="utf-8")
                        content_cache[link_path] = content
                except (OSError, UnicodeDecodeError):
                    continue
                linked.append(RetrievalResult(
                    source=source,
                    content=content,
                    score=result.score * _WIKI_LINK_DECAY,
                    match_type="wiki_link",
                ))
        return linked

    def _follow_wiki_links(
        self, content: str, depth: int = 1, idx: EntityIndex | None = None,
    ) -> list[Path]:
        """Extract [[wiki-links]] from content, resolve to file paths.

        Only follows links that resolve to existing files (entity registry
        defense against dangling link injection — security research).
        """
        if depth <= 0:
            return []

        paths: list[Path] = []
        for match in WIKI_LINK_RE.finditer(content):
            raw_link = match.group(1)
            slug = sanitize_wiki_link(raw_link)
            if slug is None:
                continue

            # Check episodes directory first, then memory top-level
            episodes_dir = self._memory_dir / self._config.episodes_dirname
            candidate = episodes_dir / f"{slug}.md"
            if candidate.exists():
                paths.append(candidate)
                continue
            candidate = self._memory_dir / f"{slug}.md"
            if candidate.exists():
                paths.append(candidate)
                continue

            # Check entities (index-backed O(1) lookup)
            if idx is not None:
                entity_path = idx.resolve(slug)
                if entity_path is not None:
                    paths.append(entity_path)
                    continue
            elif self._entities_dir.exists():
                candidate = self._entities_dir / f"{slug}.md"
                if candidate.exists():
                    paths.append(candidate)
                    continue

            # Check team entities (only existing files — registry defense)
            if self._team_entities_dir and self._team_entities_dir.exists():
                candidate = self._team_entities_dir / f"{slug}.md"
                if candidate.exists():
                    paths.append(candidate)

        return paths

    def _discover_files(
        self, scope: str | None = None, idx: EntityIndex | None = None,
    ) -> list[Path]:
        """Find indexable files, optionally filtered by scope.

        Scope values: "episodes", "daily_notes", "working", "entities", or None (all).
        """
        files: list[Path] = []

        # Episodes
        if scope is None or scope == "episodes":
            episodes_dir = self._memory_dir / self._config.episodes_dirname
            if episodes_dir.exists():
                files.extend(episodes_dir.glob("*.md"))

        # Daily notes
        if scope is None or scope == "daily_notes":
            daily_notes_dir = self._memory_dir / self._config.daily_notes_dirname
            if daily_notes_dir.exists():
                files.extend(daily_notes_dir.glob("*.md"))

        # Working memory
        if scope is None or scope == "working":
            working = self._memory_dir / self._config.working_filename
            if working.exists():
                files.append(working)

        # Entities (index-backed avoids rglob)
        if scope is None or scope == "entities":
            if idx is not None:
                files.extend(idx.all_files())
            elif self._entities_dir.exists():
                files.extend(self._entities_dir.rglob("*.md"))
            # Team entities (lower priority — separate directory, no index)
            if self._team_entities_dir and self._team_entities_dir.exists():
                files.extend(self._team_entities_dir.rglob("*.md"))

        return files

    def _enforce_budget(
        self, results: list[RetrievalResult],
    ) -> list[RetrievalResult]:
        """Apply overflow strategy to fit within retrieved_budget tokens."""
        max_chars = self._config.retrieved_budget * CHARS_PER_TOKEN
        total = 0
        trimmed: list[RetrievalResult] = []

        for result in results:
            remaining = max_chars - total
            if remaining <= 0:
                break
            if len(result.content) <= remaining:
                trimmed.append(result)
                total += len(result.content)
            else:
                # Truncate this result to fit
                trimmed.append(RetrievalResult(
                    source=result.source,
                    content=result.content[:remaining],
                    score=result.score,
                    match_type=result.match_type,
                ))
                total += remaining
                break

        return trimmed
