# Future: Vector Embeddings for Memory, Notes, and Entities

> Phase 5 roadmap item. Groundwork already laid in HybridSearch and MemoryConfig.

---

## Current State

The memory module (S002) uses **BM25 keyword search only** via SQLite FTS5.

- `HybridSearch` in `modules/memory/hybrid_search.py` indexes notes, context.md, identity.md, policy.md, and entity files
- Chunks documents at ~400 tokens with heading-boundary preference
- Search is keyword-based: good for exact terms, poor for semantic similarity
- `MemoryConfig` already defines `search_weight_vector=0.3` and `embedding_model="all-MiniLM-L6-v2"` -- these are placeholders waiting for implementation
- `HybridSearch._vec_available` flag exists but is always `False`

---

## What Gets Embedded

| Source | Current Search | With Embeddings |
|--------|---------------|-----------------|
| `notes/*.md` (daily notes) | BM25 keyword | Semantic + keyword hybrid |
| `context.md` (agent context) | BM25 keyword | Semantic + keyword hybrid |
| `identity.md` | BM25 keyword | Semantic + keyword hybrid |
| `policy.md` | BM25 keyword | Semantic + keyword hybrid |
| `entities/*.md` (extracted entities) | BM25 keyword | Semantic + keyword hybrid |

Entities are the strongest candidate for embeddings -- "find entities related to Project X" is a semantic query that BM25 handles poorly.

---

## Implementation Path

### Option A: Local Embeddings (Preferred for Federal)

Use `all-MiniLM-L6-v2` (already in config) via `sentence-transformers`:

```python
from sentence_transformers import SentenceTransformer

model = SentenceTransformer("all-MiniLM-L6-v2")
embeddings = model.encode(chunks)  # Returns numpy arrays, 384 dimensions
```

**Pros**: No network calls, no API keys, works in SCIFs, ~80MB model
**Cons**: Slower than API, moderate quality (but fine for workspace-scale data)

### Option B: API Embeddings (Higher Quality)

Use OpenAI `text-embedding-3-small` (1536 dims) or Anthropic equivalent:

```python
# Through arcllm adapter -- keeps LLM concerns in arcllm
embeddings = await arcllm.embed(chunks, model="text-embedding-3-small")
```

**Pros**: Better semantic quality, smaller index (can quantize)
**Cons**: Requires network, API key, not SCIF-compatible without proxy

### Recommendation

Support both via config, default to local. The `embedding_model` config field already exists for this.

---

## Storage: sqlite-vec

Use `sqlite-vec` extension (successor to `sqlite-vss`). Keeps everything in SQLite -- no new dependency type.

```python
import sqlite_vec

db = sqlite3.connect(":memory:")
db.enable_load_extension(True)
sqlite_vec.load(db)

# Create vector table
db.execute("""
    CREATE VIRTUAL TABLE vec_chunks USING vec0(
        embedding float[384],  -- matches all-MiniLM-L6-v2 output
        +chunk_id TEXT,
        +source_file TEXT,
        +chunk_text TEXT
    )
""")

# Insert
db.execute(
    "INSERT INTO vec_chunks(embedding, chunk_id, source_file, chunk_text) VALUES (?, ?, ?, ?)",
    [embedding_bytes, chunk_id, source, text]
)

# Search (KNN)
rows = db.execute("""
    SELECT chunk_text, source_file, distance
    FROM vec_chunks
    WHERE embedding MATCH ?
    ORDER BY distance
    LIMIT 6
""", [query_embedding_bytes]).fetchall()
```

**Why sqlite-vec**: Same DB engine we already use for FTS5. Single file. No server. Works in airgapped environments.

---

## Hybrid Ranking

The `MemoryConfig` already defines weights:

```python
search_weight_bm25: float = 0.7
search_weight_vector: float = 0.3
```

Combine scores with Reciprocal Rank Fusion (RRF):

```python
def hybrid_rank(bm25_results, vec_results, alpha=0.7):
    """Merge BM25 and vector results using RRF."""
    scores = {}
    for rank, (chunk_id, _) in enumerate(bm25_results):
        scores[chunk_id] = scores.get(chunk_id, 0) + alpha / (rank + 60)
    for rank, (chunk_id, _) in enumerate(vec_results):
        scores[chunk_id] = scores.get(chunk_id, 0) + (1 - alpha) / (rank + 60)
    return sorted(scores.items(), key=lambda x: -x[1])
```

OpenClaw uses similar approach: `vectorWeight=0.7, textWeight=0.3` (they flip the defaults -- they lean vector-heavy). We lean keyword-heavy because our data is structured markdown with clear headings, where BM25 is strong.

---

## What Changes in HybridSearch

Minimal changes to existing class:

1. **Add `_init_vec()` method**: Load sqlite-vec, create vector table, set `_vec_available = True`
2. **Add `_embed_chunks()` method**: Run chunks through embedding model during reindex
3. **Modify `search()` method**: When `_vec_available`, run both BM25 and KNN, merge with RRF
4. **Add `_reindex_vectors()` to `_lazy_reindex()`**: Embed new/changed chunks alongside FTS5 indexing

The existing chunking logic (`_chunk_document`) stays the same -- both BM25 and vector search use the same chunks.

---

## Entity-Specific Considerations

Entities (`entities/*.md`) have YAML frontmatter with structured facts:

```yaml
type: person
aliases: [Josh, Joshua Schultz]
facts:
  - role: CEO at CTG Federal
  - location: Virginia
```

For entities, embed both:
- The full markdown content (for narrative context)
- Individual facts as separate micro-chunks (for precise retrieval)

This lets queries like "who works in Virginia?" match the specific fact, not just documents that happen to mention Virginia.

---

## Security Notes (ASI-06 Defense)

- Embeddings are derived data -- they don't contain raw text, but can leak information via nearest-neighbor attacks
- Apply same boundary markers to vector search results as BM25 results (`<memory-result>` tags)
- Validate that embedding model hasn't been tampered with (checksum the model file)
- In SCIF deployments, embedding model must be pre-loaded and verified -- no runtime downloads
- Rate-limit embedding operations to prevent resource exhaustion

---

## Relationship to Phase 5: Graph Memory

Phase 5 mentions "Graph memory module (temporal knowledge graphs)." Embeddings are a stepping stone:

1. **Phase 1b** (now): BM25 keyword search -- done
2. **Phase 2-3**: Add vector embeddings (this note) -- hybrid BM25 + vector
3. **Phase 5**: Add graph layer on top -- entities become nodes, facts become edges, embeddings enable similarity-based graph traversal

The entity extractor already produces graph-ready data (entity types, relationships, facts). The graph module would formalize these into a queryable structure.

---

## Dependencies

| Dependency | Purpose | Size | Federal-Compatible |
|------------|---------|------|-------------------|
| `sqlite-vec` | Vector storage in SQLite | ~2MB | Yes (C extension, no network) |
| `sentence-transformers` | Local embedding model | ~500MB with model | Yes (offline capable) |
| `numpy` | Vector operations | Already a transitive dep | Yes |

Optional (API route):
| `arcllm` adapter | Remote embedding via API | Already exists | Depends on network policy |

---

## When to Implement

Not urgent. BM25 works well for workspace-scale data (typically <100 files, <50 entities). Vector search becomes valuable when:

- Entity count exceeds ~200 (keyword search starts missing semantic connections)
- Notes accumulate beyond ~6 months (temporal queries benefit from embeddings)
- Multi-agent scenarios where agents share a knowledge base (Phase 3+)
- Users report "I know I wrote about X but search can't find it" -- the classic keyword-gap problem
