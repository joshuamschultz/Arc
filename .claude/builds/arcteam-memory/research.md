# arcteam-memory — build & deepen notes

The `/build` and `/deepen` output for this feature: research insights, architecture diagrams,
component lists, risk registers, open questions and handoff notes. Verbatim, in original order.

**Decisions from this build:** D-088–D-118 (31 total) — see [`.claude/decisions-log.md`](../../decisions-log.md).

---

## ArcTeam Memory — Build Decisions (2026-02-21)

**Phase**: build | **Status**: complete | **Total decisions**: 31 (26 user, 5 auto-applied)
**Priority framework**: simplicity > security > scalability > compliance
**Design doc**: `packages/arcagent/.claude/ARC-Memory-System-v2.1-Final.md` (Section 7)

#### Summary

Team memory is the shared knowledge graph — "Confluence for the team." Wiki-linked markdown entity files, searchable via BM25 with adaptive graph traversal. `TeamMemoryService` is a standalone service in arcteam, framework-agnostic (usable by arcagent, langchain, crewai, etc.). arcagent connects via a thin Module Bus adapter. Consolidation is LLM-driven (via arcllm), triggered automatically by the first agent to start a session when consolidation is due.

#### Auto-Applied (Federal Mandates)

| # | Decision | Mandated Answer | Citation |
|---|----------|----------------|----------|




































#### Open Questions
- None. All categories addressed.

#### Related Design Doc
- `packages/arcagent/.claude/ARC-Memory-System-v2.1-Final.md` — Full ARC Memory System design (agent + team)

#### Research Insights (via /deepen)

**Date**: 2026-02-21
**Topics**: 6 parallel research areas (BM25, wiki-link traversal, YAML/markdown I/O, LLM consolidation, classification access control, file lock concurrency)
**Sources**: rank-bm25 docs, NIST 800-53 rev5 (AC-3, AU-2, RA-2), python-frontmatter, Okapi BM25 literature, fcntl man pages, existing codebase patterns (StorageBackend, Bio-Memory /deepen)

---

##### BM25 Search Research Insights

**rank-bm25 is the right choice.** Pure Python, ~50 LOC internally, zero compiled deps. API: `BM25Okapi(corpus)` where corpus is `list[list[str]]` (tokenized docs). `get_scores(query_tokens)` returns numpy array of scores. `get_top_n(query_tokens, documents, n)` returns ranked results. Memory: ~1KB per doc for 1000 docs = ~1MB total. Negligible.

**Threshold selection is empirical, not universal.** BM25 scores are not normalized (range depends on corpus). Best practice: use relative threshold, not absolute. **Recommended**: take the max score from initial grep results, set branch-stopping threshold at `0.3 * max_score`. This adapts to query specificity. An absolute threshold (e.g., 1.0) fails because BM25 scores scale with IDF — rare terms score high, common terms score low.

**Rebuild vs persist.** For <1000 markdown files, rebuild on search is fast (<50ms). Persisting the BM25 index adds complexity (pickle/JSON serialization, invalidation on writes) for minimal gain. **Recommended**: rebuild per search call, lazy-load tokenized corpus from dirty-flag-gated cache. Cache the tokenized corpus (not scores), invalidate on dirty flag.

**Markdown preprocessing matters.** Strip YAML frontmatter before indexing body text. Strip markdown syntax (`#`, `*`, `**`, `` ` ``). Keep wiki-link text (`[[entity-name]]` → `entity-name`). Remove code blocks entirely (they pollute term frequencies with variable names). Use simple tokenization: lowercase, split on whitespace + punctuation, no stemming needed for entity-name matching.

**Edge cases:**
- Empty query → return empty, don't score (division by zero in IDF)
- Single-token query → BM25 still works, but consider exact-match boost
- Very long documents → BM25 naturally handles via length normalization (k1=1.5, b=0.75 defaults are good)
- Code-heavy docs → strip code blocks, index only prose sections

**rank-bm25 vs from-scratch.** rank-bm25 is 47 lines of core code. Writing from scratch saves a dependency but gains nothing. The library handles edge cases (empty corpus, zero-length docs) that a hand-roll might miss. **Recommendation**: use rank-bm25 with `BM25Okapi` (not `BM25Plus` or `BM25L`). Okapi is the standard.

---

##### Wiki-Link Graph Traversal Research Insights

**Regex for wiki-links.** Pattern: `r'\[\[([^\]|]+)(?:\|([^\]]+))?\]\]'` — captures `entity_id` in group 1 and optional `display_text` in group 2. Edge cases to handle: (1) skip matches inside fenced code blocks (``` regions), (2) skip matches inside inline code (`` ` ``), (3) handle escaped brackets `\[\[` gracefully. Best approach: strip code blocks before regex, not with negative lookbehind (complex, fragile).

**BFS is correct for relevance-bounded traversal.** BFS explores closest neighbors first — when you stop a branch at low relevance, you've already covered the most connected nodes. DFS would dive deep into one branch before visiting close neighbors, wasting budget on distant irrelevant nodes. **Recommended**: BFS with per-node scoring.

**Bidirectional traversal strategy.** Forward links (links_to) represent "this entity references that." Backlinks (linked_from) represent "that entity references this." For search: traverse forward links first (author → their projects), then backlinks (project → who else works on it). In practice, traverse both in BFS, treating the graph as undirected. The BM25 threshold handles relevance — direction matters less than content match.

**Cycle detection.** Simple `visited: set[str]` is sufficient. For 1000 entities × 5 links avg = 5000 edges, the visited set is trivially small. No need for Tarjan's or Floyd's. **Worst-case BFS with max_hops=3**: 1 + 5 + 25 + 125 = 156 nodes visited (with no deduplication). With deduplication and early stopping, typically 10-30 nodes.

**Adaptive stopping implementation:**
```
threshold = 0.3 * max_initial_score
queue = [(entity_id, hop_count) for each initial result]
while queue:
    entity_id, hops = queue.popleft()
    if hops >= max_hops or entity_id in visited: continue
    visited.add(entity_id)
    score = bm25.score(entity_content, query)
    if score < threshold: continue  # prune this branch
    results.append((entity_id, score, hops))
    for linked_id in entity.links_to + entity.linked_from:
        queue.append((linked_id, hops + 1))
```

**Link consistency on write.** When entity A adds `[[B]]`:
1. A's frontmatter gets `links_to: [B]`
2. B's frontmatter gets `linked_from: [A]` added
3. Both updates happen in same write transaction (or rebuild index with dirty flag)

**Simpler alternative**: don't maintain `linked_from` in individual files. Compute backlinks from `_index.json` which already stores all `links_to` relationships. O(N) scan of index, but only during search — not on every write. **Recommended**: this approach. It eliminates write-time cross-file updates entirely.

---

##### YAML Frontmatter + Markdown I/O Research Insights

**python-frontmatter is reliable.** `frontmatter.load(path)` returns `Post` object with `.metadata` (dict) and `.content` (str). `frontmatter.dumps(post)` serializes back. Handles multi-line strings, lists, dates, Unicode. One dependency (PyYAML). Battle-tested in static site generators.

**Pydantic validation pattern:**
```python
raw = frontmatter.load(path)
meta = EntityMetadata.model_validate(raw.metadata)  # Pydantic v2
content = raw.content
```
This gives you type-safe metadata + raw markdown body in two lines.

**Atomic write — match existing FileBackend pattern.** The existing `storage.py:113-123` already does `tempfile.mkstemp + os.replace`. MemoryStorage should use the identical pattern:
```python
fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
try:
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(frontmatter.dumps(post))
    os.replace(tmp, path)
except BaseException:
    if os.path.exists(tmp): os.unlink(tmp)
    raise
```

**Token counting.** tiktoken is accurate but requires downloading tokenizer data and adds a dependency. For budget enforcement on entity files (800 token limit), a **word-count heuristic is sufficient**: `len(content.split()) * 1.3 ≈ tokens`. The 1.3 multiplier accounts for subword tokenization. For consolidation prompts where exact counts matter, use arcllm's token counting if available. **Recommendation**: word-count heuristic for write-time budget checks, exact counting deferred to consolidation.

**Frontmatter edge cases:**
- Special characters in YAML: strings with `:`, `#`, `{`, `}` must be quoted. python-frontmatter handles this automatically via PyYAML's `safe_dump`.
- Multi-line strings: use YAML literal blocks (`|`) for summaries. python-frontmatter handles via `default_flow_style=False`.
- Lists in frontmatter: `links_to: [entity-a, entity-b]` — PyYAML handles natively.
- Dates: use ISO 8601 strings, not YAML date type (avoids timezone issues).

**Index rebuild without reading full bodies.** python-frontmatter has no "metadata-only" mode. Two options:
1. Read first ~20 lines until `---` closing delimiter (fast, ~0.1ms/file)
2. Use regex to extract between `---` delimiters without full parse

**Recommended**: option 1 — read file, split on first two `---`, parse only the YAML block. Skip body entirely. For 1000 files: ~100ms total.

**Encoding.** Always `encoding="utf-8"`. No BOM handling needed — python-frontmatter strips BOM if present. Normalize newlines to `\n` on write (avoid CRLF in cross-platform scenarios).

---

##### LLM Consolidation Engine Research Insights

**arcllm integration pattern.** Call `load_model(model_name)` from arcllm to get an adapter. Use adapter.invoke() with structured messages. If `consolidation_model` is set in team memory config, pass it to `load_model()`. Otherwise, `load_model()` uses arcllm's default routing.

**Entity rewrite prompt pattern (from Bio-Memory research):**
```
You are updating a knowledge base entity file.

Current file:
{current_content}

New information from recent sessions:
{new_episodes}

Rules:
1. Integrate new facts into the existing structure
2. For each fact you drop, state why (superseded, redundant, or contradicted)
3. Preserve all wiki-links [[entity-id]] that still reference valid entities
4. Stay under {budget} words (~{token_budget} tokens)
5. Do not invent facts not present in the source material
6. Output ONLY the updated markdown body (no frontmatter)
```

**Batch processing.** Sequential is safer for Phase 1. Parallel introduces coordination complexity (two entities referencing each other during simultaneous rewrite). **Recommended**: process entities sequentially, prioritized by: (1) staleness (oldest `last_updated` first), (2) number of pending episodes, (3) link count (highly-connected entities first).

**Crash safety — write-ahead manifest.**
```
1. Write manifest: pending_entities.json = [entity_a, entity_b, ...]
2. For each entity: rewrite → atomic write → remove from manifest
3. On restart: read manifest → re-process remaining entities
4. On complete: delete manifest, update .last_consolidated timestamp
```
This matches the existing FileBackend pattern. The manifest is the crash recovery checkpoint.

**Cost estimation.** Per entity rewrite: ~500 input tokens (current) + ~500 (episodes) + ~200 (prompt) = ~1200 input + ~800 output. At $3/M input, $15/M output (Claude Sonnet): ~$0.016/entity. For 100 entities: ~$1.60 per consolidation. **Recommendation**: log estimated cost before running, configurable max-cost-per-consolidation.

**Diff-based consolidation.** Only send entities with pending episodes (flagged during promote()). Track `last_consolidated_at` per entity in frontmatter. Compare against episode timestamps. Skip unchanged entities. From Bio-Memory research: content-hash gating gives 80-90% reduction.

**Link discovery.** Two-phase approach from Bio-Memory research: (1) entity rewrite discovers links within episode context naturally, (2) optional graph pass discovers cross-domain links by showing entity summaries to LLM. Phase 2 is expensive — defer to Phase 2 of implementation.

**Validation of LLM output.** Before writing:
1. Parse output as markdown (no syntax errors)
2. Check word count against budget (reject if >110% of budget)
3. Verify no frontmatter in output (prompt says "no frontmatter")
4. Extract wiki-links from output, verify all reference valid entities in index
5. If validation fails: retry once with explicit error, then skip entity and log warning

---

##### Classification Access Control Research Insights

**US Government classification hierarchy (codified):**
```python
class Classification(IntEnum):
    UNCLASSIFIED = 0
    CUI = 1          # Controlled Unclassified Information (NIST SP 800-171)
    CONFIDENTIAL = 2
    SECRET = 3
    TOP_SECRET = 4
```
CUI is not a classification level per se — it's a handling category for unclassified info that requires safeguarding. In practice, it sits between UNCLASSIFIED and CONFIDENTIAL for access control purposes. Sub-levels (TS//SCI, TS//SAP) exist but are handled as TOP_SECRET for our purposes.

**NIST 800-53 AC-3 (Access Enforcement):**
- "The system enforces approved authorizations for logical access to information and system resources"
- Requires: (1) defined access control policy, (2) enforcement mechanism, (3) audit of enforcement decisions
- For file-based: every read/search must check agent clearance vs entity classification. Denied access must be audit-logged (AU-2 cross-reference).

**Mixed-classification search results.** NIST guidance: **filter silently, log the denial.** Do not inform the requesting agent that higher-classified results exist (that itself is information leakage — "there IS something classified about this topic"). Return count of results found, not count of results filtered. The audit log captures the filtered results for compliance review.

**Classification inheritance during traversal.** An UNCLASSIFIED entity linking to a CUI entity does NOT inherit CUI classification. However, during graph traversal, if following a link would lead to a CUI entity and the agent lacks CUI clearance, that branch is pruned. The link itself (the fact that a connection exists) is at the classification of the linking entity. **Recommendation**: prune at traversal time, don't propagate classification upward.

**Downgrade/declassification.** Requires human approval at all tiers. Classification can only be lowered, never raised automatically (an agent cannot classify something as SECRET — only a human can). Audit trail must record: who downgraded, when, from what to what, authorization reference.

**Tier-gated enforcement pattern:**
```python
def check_access(entity_cls: Classification, agent_cls: Classification, tier: str) -> bool:
    if tier == "personal":
        return True  # no enforcement
    if entity_cls <= agent_cls:
        return True
    if tier == "enterprise":
        logger.warning("Access denied: %s > %s", entity_cls, agent_cls)
    # federal: silent deny + audit
    audit_log.log("access_denied", entity=entity_id, agent=agent_id, reason="classification")
    return False
```

**Data spillage detection.** Two approaches:
1. **Write-time**: when promoting content, scan for patterns (CUI markings, classification banners) in content destined for lower-classified entities. Regex-based.
2. **Consolidation-time**: LLM prompt includes instruction "flag any content that appears to be classified higher than {entity_classification}."
**Recommendation**: regex patterns at write-time (zero cost), LLM check during consolidation (amortized cost).

---

##### File Lock Concurrency Research Insights

**fcntl.flock behavior.** On macOS and Linux: advisory locks (processes must cooperate). `LOCK_EX` blocks until acquired. **Auto-releases on file descriptor close AND on process death.** This means crashed processes don't leave stale locks — the kernel cleans up. Threads sharing an fd share the lock (no intra-process exclusion). This is fine for asyncio since we use `asyncio.to_thread()` which runs in a thread pool — each thread gets its own fd via `open()`.

**NFS caveat.** `flock()` does NOT work on NFS (some implementations silently succeed without locking). For NFS: use `fcntl.lockf()` instead. For our use case (local filesystem): `flock` is correct.

**Per-entity-file lock pattern (matching existing FileBackend).** Lock the entity file itself — no separate `.lock` files needed. The existing `storage.py:136-143` pattern is correct:
```python
with open(entity_path, "ab") as f:
    fcntl.flock(f.fileno(), fcntl.LOCK_EX)
    try:
        # write via tempfile + os.replace
    finally:
        fcntl.flock(f.fileno(), fcntl.LOCK_UN)
```
For new entity files (file doesn't exist yet): create parent dir, then lock on the tempfile or use directory-level lock briefly. **Simpler**: use `open(entity_path, "a+b")` which creates the file if missing.

**asyncio.to_thread safety.** Two coroutines in the same process calling `await asyncio.to_thread(write_with_lock, path)` will run in separate threads. Each thread opens its own fd → gets its own lock → flock serializes them correctly. No deadlock risk because each lock is on a single file. **Deadlock possible only if**: one thread holds lock A and waits for lock B while another holds B and waits for A. Our pattern is one lock per operation → no deadlock.

**Lock timeout.** `flock(LOCK_NB)` returns immediately with `BlockingIOError` if lock unavailable. **Recommended pattern**:
```python
for attempt in range(max_retries):
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        break
    except BlockingIOError:
        await asyncio.sleep(0.1 * (attempt + 1))
else:
    raise TimeoutError(f"Could not acquire lock on {path}")
```
Default: 5 retries × backoff = ~1.5s max wait. If a write takes >1.5s something is seriously wrong.

**Consolidation global lock.** `.consolidation.lock` file with `flock(LOCK_EX | LOCK_NB)`. If lock unavailable → another process is consolidating → skip. This is the "first agent wins" pattern from D-097.

**_index.json rebuild atomicity.** Same tempfile+rename pattern. Multiple writers setting dirty flag simultaneously is fine — flag is idempotent (file exists = dirty). Rebuild reads all entity frontmatter, writes new index atomically. If two processes rebuild simultaneously: both produce correct output, last writer wins (os.replace is atomic). No corruption possible.

---

##### Cross-Cutting Insights

**Alignment with Bio-Memory /deepen.** The Bio-Memory research (already in this log) directly supports several arcteam-memory decisions:
- Frontmatter-first search (Data Model Insights) validates our two-pass approach
- Crash safety via `os.replace() + os.fsync()` for single files, write-ahead manifest for multi-file (Integration Insights)
- Content-hash gating for 80-90% consolidation cost reduction (Performance Insights)
- Layered defense model (Security Insights) maps to our classification + audit approach
- BFS traversal with visited set matches Zep/Graphiti patterns

**New risks discovered:**
1. **BM25 threshold gaming.** An attacker who understands the scoring model could craft entity content to always score high (keyword stuffing). Mitigation: per-entity content validation at write time, word frequency caps.
2. **Consolidation prompt injection.** Malicious content in entity files becomes part of consolidation prompts. Mitigation: randomized boundary markers per consolidation run (from Bio-Memory research), content sanitization before prompt construction.
3. **Classification downgrade via consolidation.** If entity A (CUI) is consolidated and rewritten, the LLM might produce output without CUI markers. Mitigation: frontmatter classification is NEVER changed by consolidation — only body content is rewritten. Classification changes require human approval (D-110).
4. **Index poisoning.** Corrupt `_index.json` could route queries to wrong entities. Mitigation: integrity checksum on index (federal tier), rebuild from source files on checksum mismatch.

**Implementation priority (based on research):**
1. MemoryStorage (YAML+markdown I/O) — foundation everything depends on
2. Entity model + _index.json — data layer
3. BM25 search + grep — retrieval
4. Wiki-link traversal — graph layer
5. Promotion gate + classification — security layer
6. ConsolidationEngine — LLM integration
7. TeamMemoryBridge (arcagent adapter) — integration layer

---

---
