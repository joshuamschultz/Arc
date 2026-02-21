"""TeamMemoryService — shared team knowledge graph facade.

Standalone service usable by arcagent, langchain, crewai, or direct.
Wires internal components and provides unified API.
Null Object pattern: when disabled, all methods return empty/no-op.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from arcteam.memory.classification import ClassificationChecker
from arcteam.memory.config import TeamMemoryConfig
from arcteam.memory.index_manager import IndexManager
from arcteam.memory.promotion_gate import PromotionGate
from arcteam.memory.search_engine import SearchEngine
from arcteam.memory.storage import MemoryStorage
from arcteam.memory.types import (
    Classification,
    EntityFile,
    EntityMetadata,
    IndexEntry,
    MemoryStatus,
    PromotionResult,
    SearchResult,
)

if TYPE_CHECKING:
    from arcteam.audit import AuditLogger

logger = logging.getLogger(__name__)


class TeamMemoryService:
    """Shared team knowledge graph.

    Standalone service — usable by arcagent, langchain, crewai, or direct.
    """

    def __init__(
        self,
        config: TeamMemoryConfig,
        audit_logger: AuditLogger | None = None,
        messenger: object | None = None,
    ) -> None:
        self._config = config
        self._audit = audit_logger

        if not config.enabled:
            # Null Object: components not initialized
            self._storage: MemoryStorage | None = None
            self._index_mgr: IndexManager | None = None
            self._search: SearchEngine | None = None
            self._gate: PromotionGate | None = None
            self._classifier: ClassificationChecker | None = None
            return

        # Wire components
        self._storage = MemoryStorage(config.entities_dir)
        self._index_mgr = IndexManager(config.entities_dir, self._storage, config)
        self._search = SearchEngine(self._storage, self._index_mgr, config)
        self._classifier = ClassificationChecker(config, audit_logger)
        self._gate = PromotionGate(
            self._storage,
            self._index_mgr,
            self._classifier,
            audit_logger,
            messenger,
            config,
        )

    async def search(
        self,
        query: str,
        agent_classification: Classification = Classification.UNCLASSIFIED,
        max_results: int = 20,
        agent_id: str = "",
    ) -> list[SearchResult]:
        """BM25 search with wiki-link traversal, classification-filtered."""
        if self._search is None:
            return []

        results = await self._search.search(
            query, max_results, agent_classification=agent_classification
        )

        # Classification filter
        if self._classifier is not None:
            results = self._classifier.filter_results(
                results, agent_classification
            )

        # Audit log search (NIST 800-53 AU-2)
        if self._audit:
            await self._audit.log(
                event_type="memory.search",
                subject=query,
                actor_id=agent_id,
                detail=f"returned {len(results)} results",
                classification=agent_classification.name,
            )

        return results

    async def promote(
        self,
        entity_id: str,
        content: str,
        metadata: EntityMetadata,
        agent_id: str,
    ) -> PromotionResult:
        """Write entry point. Validates, classifies, audits, writes."""
        if self._gate is None:
            return PromotionResult(
                success=False,
                entity_id=entity_id,
                action="disabled",
                message="Team memory is disabled",
            )

        return await self._gate.promote(entity_id, content, metadata, agent_id)

    async def get_entity(
        self,
        entity_id: str,
        agent_classification: Classification = Classification.UNCLASSIFIED,
    ) -> EntityFile | None:
        """Get entity by ID. Returns None if not found or above clearance."""
        if self._storage is None or self._index_mgr is None:
            return None

        index = await self._index_mgr.get_index()
        entry = index.get(entity_id)
        if entry is None:
            return None

        # Classification check
        if self._classifier is not None:
            if not self._classifier.check_access(
                entry.classification, agent_classification, entity_id=entity_id
            ):
                return None

        return await self._storage.read_entity(entity_id, index)

    async def list_entities(
        self,
        entity_type: str | None = None,
        agent_classification: Classification = Classification.UNCLASSIFIED,
    ) -> list[IndexEntry]:
        """List entities from index, classification-filtered."""
        if self._index_mgr is None:
            return []

        index = await self._index_mgr.get_index()
        entries = list(index.values())

        # Filter by type
        if entity_type is not None:
            entries = [e for e in entries if e.entity_type == entity_type]

        # Classification filter
        if self._classifier is not None:
            entries = self._classifier.filter_results(
                entries, agent_classification
            )

        return entries

    async def rebuild_index(self) -> dict[str, IndexEntry]:
        """Force a full index rebuild. Returns the rebuilt index."""
        if self._index_mgr is None:
            return {}
        return await self._index_mgr.rebuild()

    async def record_decision(
        self,
        decision: dict[str, Any],
        agent_id: str,
    ) -> None:
        """Append decision to decisions JSONL log."""
        if not self._config.enabled:
            return

        record = {
            "timestamp": datetime.now(UTC).isoformat(),
            "agent_id": agent_id,
            **decision,
        }
        decisions_path = self._config.root / "decisions.jsonl"
        await asyncio.to_thread(self._append_jsonl, decisions_path, record)
        logger.info("Decision recorded by %s: %s", agent_id, decision.get("title", ""))

    @staticmethod
    def _append_jsonl(path: Any, record: dict[str, Any]) -> None:
        """Append a JSON record to a JSONL file."""
        from pathlib import Path

        p = Path(str(path))
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")

    async def status(self) -> MemoryStatus:
        """Service status snapshot."""
        if self._index_mgr is None:
            return MemoryStatus(enabled=False, entity_count=0)

        index = await self._index_mgr.get_index()
        return MemoryStatus(
            enabled=self._config.enabled,
            entity_count=len(index),
        )
