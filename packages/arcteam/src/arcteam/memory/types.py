"""Shared Pydantic models and type definitions for ArcTeam memory."""

from __future__ import annotations

from enum import IntEnum

from pydantic import BaseModel, Field


class Classification(IntEnum):
    """US Government classification hierarchy."""

    UNCLASSIFIED = 0
    CUI = 1
    CONFIDENTIAL = 2
    SECRET = 3
    TOP_SECRET = 4


class EntityMetadata(BaseModel):
    """YAML frontmatter schema for entity files."""

    entity_type: str
    entity_id: str
    name: str
    status: str = "active"
    last_updated: str = ""  # ISO 8601
    last_verified: str = ""
    created: str = ""
    links_to: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    source_agents: list[str] = Field(default_factory=list)
    classification: str = "unclassified"


class IndexEntry(BaseModel):
    """Single entry in _index.json."""

    entity_id: str
    path: str  # relative to entities/
    entity_type: str
    tags: list[str] = Field(default_factory=list)
    links_to: list[str] = Field(default_factory=list)
    linked_from: list[str] = Field(default_factory=list)
    summary_snippet: str = ""
    last_updated: str = ""
    status: str = "active"
    classification: str = "unclassified"


class SearchResult(BaseModel):
    """Single search result."""

    entity_id: str
    path: str
    snippet: str
    score: float
    hops: int = 0
    entity_type: str = ""
    tags: list[str] = Field(default_factory=list)
    classification: str = "unclassified"


class EntityFile(BaseModel):
    """Complete entity: metadata + body content."""

    metadata: EntityMetadata
    content: str  # markdown body


class PromotionResult(BaseModel):
    """Result of a promote() call."""

    success: bool
    entity_id: str
    action: str  # "created" | "updated" | "queued_approval"
    message: str = ""


class MemoryStatus(BaseModel):
    """Service status snapshot."""

    enabled: bool
    entity_count: int = 0
    index_dirty: bool = False
    last_consolidated: str = ""
    entities_path: str = ""
