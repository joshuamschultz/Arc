# SDD: ArcTeam Memory (SPEC-008)

**Version**: 1.0
**Date**: 2026-02-21
**Phase**: 1 (Core Service)

---

## 1. Architecture

### 1.1 Module Structure

```
packages/arcteam/src/arcteam/
    memory/                          # NEW — team memory package
        __init__.py                  # Public API + __all__
        config.py                    # TeamMemoryConfig (Pydantic)
        types.py                     # EntityMetadata, SearchResult, Classification
        errors.py                    # TeamMemoryError hierarchy
        storage.py                   # MemoryStorage (YAML frontmatter + markdown I/O)
        index_manager.py             # IndexManager (_index.json lifecycle)
        search_engine.py             # SearchEngine (BM25 + grep + traversal)
        promotion_gate.py            # PromotionGate (validate, classify, write)
        classification.py            # ClassificationChecker (access control)
        service.py                   # TeamMemoryService (facade)
        cli.py                       # CLI commands
```

### 1.2 Existing Code Impact

The existing `arcteam` package is NOT modified. Key reuse:

- `arcteam.storage.StorageBackend` / `FileBackend` — decisions JSONL append
- `arcteam.audit.AuditLogger` — compliance audit events
- `arcteam.messenger.MessagingService` — CUI+ approval queue messages
- `arcteam.config.TeamConfig` — extends for memory config
- `arcteam.types` — `Classification` enum added here or in memory/types.py

New external dependencies:
- `rank-bm25` — BM25 scoring (pure Python)
- `python-frontmatter` — YAML frontmatter I/O (pure Python, PyYAML dep)

### 1.3 Dependency Graph

```
TeamMemoryService (facade)
    ├── MemoryStorage
    │     └── python-frontmatter (YAML + markdown I/O)
    ├── IndexManager
    │     └── MemoryStorage (frontmatter-only reads)
    ├── SearchEngine
    │     ├── IndexManager (entity lookup, backlink computation)
    │     ├── MemoryStorage (file reads)
    │     └── rank-bm25 (BM25Okapi scoring)
    ├── PromotionGate
    │     ├── MemoryStorage (atomic writes)
    │     ├── IndexManager (dirty flag, duplicate check)
    │     ├── ClassificationChecker (validation)
    │     ├── arcteam.audit.AuditLogger
    │     └── arcteam.messenger.MessagingService (CUI+ approval)
    ├── ClassificationChecker
    │     └── arcteam.audit.AuditLogger (denied access logging)
    ├── config.py (TeamMemoryConfig)
    └── arcteam.storage.StorageBackend (decisions JSONL)
```

No imports from `arcagent`. Clean separation.

## 2. Component Design

### 2.1 TeamMemoryService (Facade)

**File**: `service.py`

```python
class TeamMemoryService:
    """Shared team knowledge graph.

    Standalone service — usable by arcagent, langchain, crewai, or direct.
    Facade that delegates to internal components:
    - MemoryStorage: YAML frontmatter + markdown I/O
    - IndexManager: _index.json lifecycle
    - SearchEngine: BM25 + wiki-link traversal
    - PromotionGate: write validation + audit
    - ClassificationChecker: access control
    """

    def __init__(
        self,
        config: TeamMemoryConfig,
        storage_backend: StorageBackend,  # for decisions JSONL
        audit_logger: AuditLogger | None = None,
        messenger: MessagingService | None = None,
    ) -> None: ...

    async def search(
        self,
        query: str,
        agent_classification: Classification = Classification.UNCLASSIFIED,
        max_results: int = 20,
    ) -> list[SearchResult]:
        """BM25 search with wiki-link traversal, classification-filtered."""

    async def promote(
        self,
        entity_id: str,
        content: str,
        metadata: EntityMetadata,
        agent_id: str,
    ) -> PromotionResult:
        """Write entry point. Validates, classifies, audits, writes."""

    async def get_entity(
        self,
        entity_id: str,
        agent_classification: Classification = Classification.UNCLASSIFIED,
    ) -> EntityFile | None:
        """Get entity by ID. Returns None if not found or above clearance."""

    async def list_entities(
        self,
        entity_type: str | None = None,
        agent_classification: Classification = Classification.UNCLASSIFIED,
    ) -> list[IndexEntry]:
        """List entities from index, classification-filtered."""

    async def record_decision(
        self,
        decision: dict[str, Any],
        agent_id: str,
    ) -> None:
        """Append decision to JSONL via StorageBackend."""

    def status(self) -> MemoryStatus:
        """Sync status: entity count, index freshness, last consolidated."""
```

**Null Object pattern** (D-033): When `config.enabled = False`, all methods return empty results / no-op. Callers never need conditionals.

### 2.2 TeamMemoryConfig

**File**: `config.py`

```python
class TeamMemoryConfig(BaseModel):
    """Team memory configuration. All fields have defaults."""

    enabled: bool = True
    root: Path = Path.home() / ".arc" / "team"

    # Entity settings
    entity_types: list[str] = Field(
        default=["person", "organization", "project", "domain", "process", "playbook"]
    )
    per_entity_budget: int = 800  # tokens

    # Search settings
    max_hops: int = 3
    bm25_threshold_ratio: float = 0.3
    max_results: int = 20

    # Consolidation (Phase 2 — config ready now)
    consolidation_enabled: bool = True
    consolidation_model: str = ""  # empty = arcllm default

    # Security
    classification_required: bool = True
    encryption_at_rest: bool = False

    # Tier (read from global config)
    tier: str = "personal"  # "federal" | "enterprise" | "personal"
```

### 2.3 Entity Types

**File**: `types.py`

```python
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
    last_updated: str  # ISO 8601
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
```

### 2.4 MemoryStorage

**File**: `storage.py`

Handles YAML frontmatter + markdown I/O for entity files. Separate from existing `StorageBackend` (which handles JSON/JSONL).

```python
class MemoryStorage:
    """YAML frontmatter + markdown file I/O for entity files.

    Atomic writes via tempfile + os.replace (matches FileBackend pattern).
    File locks via fcntl.flock for concurrent access.
    """

    def __init__(self, entities_dir: Path) -> None: ...

    async def read_entity(self, entity_id: str, index: dict[str, IndexEntry]) -> EntityFile | None:
        """Read entity file. Returns None if not found."""

    async def write_entity(self, entity_id: str, metadata: EntityMetadata, content: str) -> Path:
        """Atomic write: entity file with frontmatter + body. Returns written path."""

    async def delete_entity(self, entity_id: str, index: dict[str, IndexEntry]) -> bool:
        """Delete entity file. Returns True if existed."""

    async def read_frontmatter_only(self, path: Path) -> EntityMetadata | None:
        """Read first ~20 lines to extract YAML frontmatter. Skip body."""

    async def list_entity_files(self) -> list[Path]:
        """Discover all .md files in entities directory tree."""

    def _entity_path(self, entity_type: str, entity_id: str) -> Path:
        """Compute path: entities/{entity_type}/{entity_id}.md"""

    def _sync_read(self, path: Path) -> tuple[dict[str, Any], str] | None:
        """Sync: read file, split frontmatter + body via python-frontmatter."""

    def _sync_write(self, path: Path, metadata: dict[str, Any], content: str) -> None:
        """Sync: atomic write via tempfile + os.replace. Uses fcntl.flock."""

    def _sync_read_frontmatter(self, path: Path) -> dict[str, Any] | None:
        """Sync: read only YAML frontmatter block (first 20 lines)."""
```

**Key behaviors**:
- `write_entity()`: validates metadata via Pydantic, generates YAML frontmatter via `python-frontmatter`, atomic write via `tempfile.mkstemp` + `os.replace`
- `_sync_write()`: acquires `fcntl.flock(LOCK_EX)` on entity file before write
- `read_frontmatter_only()`: reads first ~20 lines until closing `---` delimiter, parses YAML only — for index rebuild performance
- All async methods delegate to `asyncio.to_thread()` (matching existing `FileBackend` pattern)
- Token estimation: `len(content.split()) * 1.3` (word-count heuristic from research)

### 2.5 IndexManager

**File**: `index_manager.py`

```python
class IndexManager:
    """Manages _index.json — the entity lookup manifest.

    Lazy rebuild via dirty flag. Writes touch .dirty marker.
    Next read checks and rebuilds if dirty.
    """

    def __init__(
        self,
        entities_dir: Path,
        memory_storage: MemoryStorage,
        config: TeamMemoryConfig,
    ) -> None: ...

    async def get_index(self) -> dict[str, IndexEntry]:
        """Get current index. Rebuilds if dirty flag set."""

    async def lookup(self, entity_id: str) -> IndexEntry | None:
        """O(1) entity lookup by ID."""

    async def entity_exists(self, entity_id: str) -> bool:
        """Check if entity exists in index."""

    async def get_backlinks(self, entity_id: str) -> list[str]:
        """Compute backlinks from index (entities that link TO this one)."""

    async def touch_dirty(self) -> None:
        """Set dirty flag (called after writes)."""

    async def rebuild(self) -> dict[str, IndexEntry]:
        """Full index rebuild from entity frontmatter. Atomic write."""

    def _is_dirty(self) -> bool:
        """Check if .dirty marker file exists."""

    def _sync_rebuild(self) -> dict[str, IndexEntry]:
        """Sync: scan all entity files, read frontmatter, build index."""

    def _sync_write_index(self, index: dict[str, IndexEntry]) -> None:
        """Sync: atomic write _index.json via tempfile + os.replace."""
```

**Key behaviors**:
- `get_index()`: checks `.dirty` flag → rebuilds if dirty → caches in memory
- `get_backlinks()`: scans index `links_to` fields to find entities that reference target (O(N) but from cached index)
- `rebuild()`: reads frontmatter only from all `.md` files (not body), computes `linked_from` from all `links_to`, writes atomically
- Federal tier: checksum on index file verified on load

### 2.6 SearchEngine

**File**: `search_engine.py`

```python
class SearchEngine:
    """BM25 search with adaptive wiki-link graph traversal.

    Two-phase search:
    1. Grep + BM25 scoring on entity corpus
    2. Adaptive wiki-link traversal (BFS, BM25-scored hops)
    """

    def __init__(
        self,
        memory_storage: MemoryStorage,
        index_manager: IndexManager,
        config: TeamMemoryConfig,
    ) -> None: ...

    async def search(
        self,
        query: str,
        max_results: int = 20,
    ) -> list[SearchResult]:
        """Full search: BM25 initial → adaptive traversal → ranked results."""

    def _build_corpus(
        self,
        entities: dict[str, IndexEntry],
        file_contents: dict[str, str],
    ) -> tuple[list[list[str]], list[str]]:
        """Tokenize entity contents for BM25. Strip frontmatter, markdown, code blocks."""

    def _bm25_search(
        self,
        query: str,
        corpus: list[list[str]],
        entity_ids: list[str],
        top_k: int,
    ) -> list[tuple[str, float]]:
        """BM25Okapi scoring. Returns (entity_id, score) pairs."""

    async def _traverse_links(
        self,
        initial_results: list[tuple[str, float]],
        query: str,
        corpus_cache: dict[str, list[str]],
    ) -> list[tuple[str, float, int]]:
        """BFS wiki-link traversal with adaptive BM25 stopping.

        Returns (entity_id, score, hops) triples.
        """

    def _tokenize(self, text: str) -> list[str]:
        """Lowercase, split on whitespace + punctuation, strip markdown."""

    def _strip_markdown(self, text: str) -> str:
        """Remove YAML frontmatter, markdown syntax, code blocks, keep wiki-link text."""
```

**Key behaviors**:
- `search()`: load index → read entity contents → build BM25 corpus → initial search → adaptive traversal → merge results → sort by score
- `_traverse_links()`: BFS with `visited` set, threshold = `0.3 * max(initial_scores)`, stops branch if score drops below threshold
- `_build_corpus()`: strip frontmatter (between `---` delimiters), strip markdown (`#`, `*`, `` ` ``), strip code blocks (``` regions), keep wiki-link text (`[[name]]` → `name`)
- `_tokenize()`: lowercase, split on `\s+|[^\w-]`, no stemming
- Corpus caching: tokenized corpus cached in memory, invalidated when index dirty flag is set
- BM25 parameters: k1=1.5, b=0.75 (BM25Okapi defaults)

### 2.7 PromotionGate

**File**: `promotion_gate.py`

```python
class PromotionGate:
    """Write validation, classification enforcement, audit trail.

    All entity writes flow through promote(). No direct writes to MemoryStorage
    except from this gate and IndexManager.
    """

    def __init__(
        self,
        memory_storage: MemoryStorage,
        index_manager: IndexManager,
        classification_checker: ClassificationChecker,
        audit_logger: AuditLogger | None,
        messenger: MessagingService | None,
        config: TeamMemoryConfig,
    ) -> None: ...

    async def promote(
        self,
        entity_id: str,
        content: str,
        metadata: EntityMetadata,
        agent_id: str,
    ) -> PromotionResult:
        """Validate → classify → duplicate-check → write or queue.

        1. Validate metadata via Pydantic
        2. Check classification label present (tier-gated)
        3. Check if entity exists (update vs create)
        4. UNCLASSIFIED: write immediately
        5. CUI+: queue for human approval via messaging
        6. Audit-log all outcomes
        7. Touch dirty flag
        """

    async def _validate(self, metadata: EntityMetadata) -> None:
        """Schema validation. Raises TeamMemoryError on invalid."""

    async def _check_duplicate(self, entity_id: str) -> IndexEntry | None:
        """Check index for existing entity."""

    async def _queue_approval(
        self,
        entity_id: str,
        content: str,
        metadata: EntityMetadata,
        agent_id: str,
    ) -> PromotionResult:
        """Send approval request to memory-approval channel."""
```

**Key behaviors**:
- All writes go through `promote()` — no other component writes entity files directly
- Schema validation catches malformed metadata before any I/O
- Duplicate detection via `IndexManager.entity_exists()` — update path vs create path
- Classification enforcement: federal blocks without label, enterprise warns, personal skips
- Audit trail on every call (success, failure, queued)

### 2.8 ClassificationChecker

**File**: `classification.py`

```python
class ClassificationChecker:
    """Classification access control enforcement.

    Checks agent clearance against entity classification on every read/search.
    Tier-gated: federal=hard block, enterprise=warn+block, personal=no enforcement.
    """

    def __init__(
        self,
        config: TeamMemoryConfig,
        audit_logger: AuditLogger | None = None,
    ) -> None: ...

    def check_access(
        self,
        entity_classification: str,
        agent_classification: Classification,
        entity_id: str = "",
        agent_id: str = "",
    ) -> bool:
        """Check if agent has clearance. Returns True if access allowed."""

    def filter_results(
        self,
        results: list[SearchResult] | list[IndexEntry],
        agent_classification: Classification,
        agent_id: str = "",
    ) -> list[SearchResult] | list[IndexEntry]:
        """Silently filter results above agent clearance. Audit-logs denials."""

    @staticmethod
    def parse_classification(value: str) -> Classification:
        """Parse classification string to enum. Defaults to UNCLASSIFIED."""
```

### 2.9 Error Types

**File**: `errors.py`

```python
class TeamMemoryError(Exception):
    """Base exception for team memory operations."""
    def __init__(self, code: str, message: str) -> None: ...

class EntityNotFoundError(TeamMemoryError):
    """Entity not found in index or filesystem."""

class EntityValidationError(TeamMemoryError):
    """Entity metadata failed Pydantic validation."""

class ClassificationError(TeamMemoryError):
    """Classification access denied."""

class IndexCorruptionError(TeamMemoryError):
    """Index file corrupted or checksum mismatch."""

class PromotionError(TeamMemoryError):
    """Promotion gate rejected the write."""

class LockTimeoutError(TeamMemoryError):
    """Could not acquire file lock within timeout."""
```

## 3. Data Flow

### 3.1 Search Flow

```
TeamMemoryService.search(query, agent_classification)
    → IndexManager.get_index()
        → check .dirty flag → rebuild if needed
    → MemoryStorage.read_entity() for each indexed entity
    → SearchEngine.search(query)
        → _build_corpus() → strip markdown, tokenize
        → _bm25_search() → BM25Okapi scoring
        → _traverse_links() → BFS with adaptive stopping
    → ClassificationChecker.filter_results()
        → silently remove above-clearance results
        → audit-log denials (federal)
    → Return list[SearchResult]
```

### 3.2 Promotion Flow

```
TeamMemoryService.promote(entity_id, content, metadata, agent_id)
    → PromotionGate.promote()
        → _validate(metadata) → Pydantic schema check
        → ClassificationChecker: label present? (tier-gated)
        → _check_duplicate(entity_id) → update vs create
        → if UNCLASSIFIED:
            → MemoryStorage.write_entity()
            → IndexManager.touch_dirty()
            → AuditLogger.log("entity.promoted")
            → return PromotionResult(success=True, action="created"|"updated")
        → if CUI+:
            → _queue_approval() → MessagingService.send()
            → AuditLogger.log("entity.promotion_queued")
            → return PromotionResult(success=True, action="queued_approval")
```

### 3.3 Entity Read Flow

```
TeamMemoryService.get_entity(entity_id, agent_classification)
    → IndexManager.lookup(entity_id)
        → not found → return None
    → ClassificationChecker.check_access()
        → denied → audit-log, return None
    → MemoryStorage.read_entity()
    → Return EntityFile
```

## 4. Disk Layout

```
{root}/                              # default: ~/.arc/team/
    entities/
        _index.json                  # Derived manifest
        .dirty                       # Dirty flag (empty file)
        .last_consolidated           # Timestamp file
        .consolidation.lock          # Global consolidation lock
        person/
            sarah-chen.md
            john-doe.md
        organization/
            nnsa.md
            doe.md
        project/
            genesis-mission.md
        domain/
            procurement.md
            pricing.md
        playbook/
            contract-review-sop.md
    decisions/
        00000000.log                 # Append-only JSONL (via StorageBackend)
```

## 5. Concurrency Model

| Operation | Lock | Pattern |
|-----------|------|---------|
| Read entity | None | Lock-free file read |
| Write entity | `fcntl.flock(LOCK_EX)` on entity file | Matches `FileBackend._sync_append` |
| Delete entity | `fcntl.flock(LOCK_EX)` on entity file | Lock → delete → unlock |
| Rebuild index | `fcntl.flock(LOCK_EX)` on `_index.json` | Atomic write (tempfile + os.replace) |
| Touch dirty | None | Idempotent file create |
| Consolidation | `fcntl.flock(LOCK_EX | LOCK_NB)` on `.consolidation.lock` | Non-blocking; skip if locked |
| Append decision | `fcntl.flock(LOCK_EX)` on JSONL stream | Existing `FileBackend` pattern |

All sync I/O wrapped in `asyncio.to_thread()`.

Lock timeout pattern (from research):
```python
for attempt in range(5):
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        break
    except BlockingIOError:
        await asyncio.sleep(0.1 * (attempt + 1))
else:
    raise LockTimeoutError(...)
```

## 6. Error Handling

| Error | Type | Recovery |
|-------|------|----------|
| Entity not found | `EntityNotFoundError` | Return None |
| Metadata validation failure | `EntityValidationError` | Reject promotion |
| Classification denied | Silent filter | Return empty / None + audit log |
| Disk write failure | `TeamMemoryError` | Atomic write prevents partial writes |
| Index corruption | `IndexCorruptionError` | Force rebuild from source files |
| Lock timeout | `LockTimeoutError` | Raise to caller (retry at their discretion) |
| BM25 empty query | Return empty list | No scoring attempted |
| Corrupted frontmatter | Skip file | Log warning, exclude from index |
| Missing entity type dir | Auto-create | `mkdir(parents=True, exist_ok=True)` |

## 7. Telemetry Events

| Event | When | Data |
|-------|------|------|
| `team_memory.search` | Each search call | query_hash, results_count, hops_explored, duration_ms |
| `team_memory.entity.promoted` | Entity written | entity_id, entity_type, action (create/update), agent_id |
| `team_memory.entity.promotion_queued` | CUI+ queued | entity_id, classification, agent_id |
| `team_memory.access_denied` | Classification filter | entity_id, entity_classification, agent_classification |
| `team_memory.index.rebuilt` | Index rebuilt | entity_count, duration_ms |
| `team_memory.decision.recorded` | Decision appended | decision_id, agent_id |

## 8. Security Considerations

### 8.1 Memory Poisoning Defense

| Layer | What | Implementation |
|-------|------|----------------|
| L0 | Input sanitization | Pydantic validation on all metadata; length limits on content |
| L1 | Wiki-link injection | Only follow links to entities that exist in `_index.json` |
| L2 | BM25 keyword stuffing | Per-entity content validation at write time; word frequency caps |
| L3 | Classification boundary | Silent filter, never reveal existence of classified entities |
| L4 | Index poisoning | Federal: integrity checksum; all tiers: rebuild from source on mismatch |
| L5 | Consolidation injection | Phase 2: randomized boundary markers, content sanitization before prompt |

### 8.2 Prompt Injection Defense (Phase 2 — Consolidation)

- Entity content sanitized before inclusion in consolidation prompts
- System/user message separation in LLM calls
- Randomized boundary markers per consolidation run
- LLM output validated before writing (budget check, no frontmatter, valid links)

### 8.3 Data Spillage Prevention

- Write-time: regex scan for classification banners in content destined for lower-classified entities
- Consolidation-time (Phase 2): LLM prompt includes spillage detection instruction
