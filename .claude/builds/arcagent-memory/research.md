# arcagent-memory — build & deepen notes

The `/build` and `/deepen` output for this feature: research insights, architecture diagrams,
component lists, risk registers, open questions and handoff notes. Verbatim, in original order.

**Decisions from this build:** D-202–D-585 (34 total) — see [`.claude/decisions-log.md`](../../decisions-log.md).

---

## Bio-Memory (ArcAgent)

**Date**: 2026-02-21
**Feature**: Biologically-inspired memory module for arcagent
**Source**: ARC-Memory-System-v2.1-Final.md (Section 6: arc-memory / Agent)
**Status**: BUILD COMPLETE — ready for `/deepen` or `/specify`

#### Context

Replaces the existing markdown-memory module as the default memory system. Implements biologically-inspired memory: working memory (scratchpad), identity (how-i-work.md), episodes (significant moments), retrieval (graph traversal), and consolidation (LLM-driven knowledge integration). Team memory is a separate build.

#### Federal Auto-Applied Mandates

| # | Decision | Mandate | Tag |
|---|----------|---------|-----|

#### Architecture Decisions

| # | Category | Question | Decision | Rationale |
|---|----------|----------|----------|-----------|

#### Data Model Decisions

| # | Category | Question | Decision | Rationale |
|---|----------|----------|----------|-----------|

#### Tool Decisions

| # | Category | Question | Decision | Rationale |
|---|----------|----------|----------|-----------|

#### Observability Decisions

| # | Category | Question | Decision | Rationale |
|---|----------|----------|----------|-----------|

#### Security Decisions

| # | Category | Question | Decision | Rationale |
|---|----------|----------|----------|-----------|

#### Integration Decisions

| # | Category | Question | Decision | Rationale |
|---|----------|----------|----------|-----------|

#### Performance Decisions

| # | Category | Question | Decision | Rationale |
|---|----------|----------|----------|-----------|

#### Extensibility Decisions

| # | Category | Question | Decision | Rationale |
|---|----------|----------|----------|-----------|

#### Testing Decisions

| # | Category | Question | Decision | Rationale |
|---|----------|----------|----------|-----------|

#### Deployment Decisions

| # | Category | Question | Decision | Rationale |
|---|----------|----------|----------|-----------|

#### CLI Decisions

| # | Category | Question | Decision | Rationale |
|---|----------|----------|----------|-----------|

#### Tier Variations Summary

| Component | Federal | Enterprise | Personal |
|-----------|---------|------------|----------|
| Audit events | Required (block without) | Default on (warn if disabled) | Off (opt-in) |
| Encryption at rest | AES-256 mandatory | Default on | Off |
| PII filtering | Block storage | Warn | Skip |
| Classification frontmatter | Required | Optional | Skip |
| Memory isolation | Always on | Always on | Always on |
| Content sanitization | Always on | Always on | Always on |

#### Build Order (from Design Doc)

**Phase 1 — Core (v0.1)**: working.md lifecycle, how-i-work.md, grep-based retrieval, token budgets, memory tools, light consolidation
**Phase 2 — Intelligence (v0.2)**: episode recording, deep consolidation (entity + graph pass), promotion gate hooks
**Phase 3 — Scale (v0.3)**: vector search fallback, NATS memory events, adaptive consolidation
**Phase 4 — Harden (v0.4)**: classification access control, encryption at rest, memory provenance, FedRAMP audit

---

#### Research Insights (from `/deepen`)

**Date**: 2026-02-21
**Agents**: 3 parallel web research agents (memory poisoning, grep-based retrieval, LLM consolidation patterns)
**Sources**: 50+ papers, OWASP guides, production systems (Mem0, Zep/Graphiti, SimpleMem, LangMem, Hindsight, A-MEM, MemGPT, MAIF)

---

##### Architecture Research Insights

**Biological memory mapping is well-validated.** Complementary Learning Systems (CLS) theory maps directly: fast hippocampal system = episodes (specific, recent) + slow neocortical system = entity files (generalized, stable). Hindsight (NeurIPS 2025) explicitly implements this. Sleep-like replay reduces catastrophic forgetting (Nature Communications 2022), validating sleep cycle consolidation.

**Reconsolidation trigger.** Neuroscience: memories destabilize when prediction error occurs (PNAS 2022). For bio-memory: trigger entity rewrite when new episodes diverge from existing entity profiles (cosine distance), not on fixed schedule. Low divergence = skip consolidation = save tokens.

**Entity files should be derived artifacts.** Hindsight's key pattern: profiles synthesized fresh from source facts, not continuously rewritten. Episode store is append-only. Lossy consolidation becomes recoverable. This changes the failure model.

**Tiered fidelity for retrieval.** Adaptive Focus Memory (AFM): Full/Compressed/Placeholder per retrieved item based on relevance and decay. Preserves more information than uniform truncation under fixed token budgets.

---

##### Data Model Research Insights

**Frontmatter-first search is a production pattern.** Two-pass: (1) grep frontmatter block for tag/entity matches, (2) full-text on matched subset. Same effect as inverted index without database.

**Dendron schema reference.** Fields: `id` (UUID), `title`, `desc` (search abstract), `tags`, `created`/`updated`, `parent`/`children`. The `desc` field for search display is worth adopting.

**`links_to` best practices:**
- Store explicit outbound links as YAML list (O(1) lookup)
- Include `entity_type` (person, concept, decision, event)
- Include `last_accessed` for temporal decay
- Store search-optimized summary in frontmatter

**Bidirectional links.** Compute backlinks at read-time via reverse index (frontmatter grep on startup), not written into files. On delete: scan for `[[deleted-slug]]`, replace with tombstone. On rename: ripgrep-replace old slug across all files.

---

##### Security Research Insights (Memory Poisoning)

**Memory poisoning != prompt injection.** Prompt injection affects one response. Memory poisoning reshapes all future behavior permanently.

**Confirmed attacks (2025):** Google Gemini memory attack (hidden document prompts), Gemini calendar invite poisoning (73% High-Critical), MINJA (95-100% injection success via normal queries, bypasses all moderation).

**Wiki-link injection vectors:**
- Dangling link injection: `[[AttackerEntity]]` auto-creates on traversal
- Homoglyph: `[[Jоhn Smith]]` (Cyrillic о) creates shadow node. NFKC at write time is only defense.
- Link flood: expands traversal surface and storage
- Path traversal: peripheral entity links to core entity, consolidation blends content
- Link-as-instruction: `[[SYSTEM: ignore...]]` if links treated as commands

**Defense: Entity registry.** Only registered entity names followed. Unknown links flagged, not auto-created. Rate-limit entity creation per session.

**Boundary markers are mitigation only.** 480 scenarios: "very little difference between Markdown and XML." Best practice: randomize marker strings per-session (UUID-embedded tags).

**Promptware Kill Chain maps to bio-memory:** Initial Access → Persistence in episode → Privilege Escalation via consolidation → Lateral Movement via wiki-links → Command & Control.

**MINJA is largely undefended.** Plausible reasoning chains bypass all published moderation. Semantic outlier detection is the only partial defense — open research problem.

**MAIF pattern.** Ed25519 signatures per write + hash chain between versions + agent-ID provenance. Verification <0.1ms.

**Layered defense model:**
- L0: Input ingestion (strip Unicode, normalize, reject marker strings)
- L1: Write validation (Pydantic schema, field limits, entity allowlist, source trust tags)
- L2: Cryptographic integrity (Ed25519 + SHA-256 hash chain)
- L3: Consolidation security (provenance-weighted, immutable fields, contradiction detection, semantic diff)
- L4: Graph traversal (entity registry, depth limit, cycle detection, TTL)
- L5: Monitoring (behavioral baseline, trend tracking, human review for identity changes)

---

##### Integration Research Insights (Consolidation)

**Don't rely on 1-10 scoring.** Absolute scoring most vulnerable to adversarial manipulation (46-68% attack success). Two-gate approach: (1) deterministic pre-filter (entropy, semantic divergence, novel entity count), (2) LLM judgment only for passing content. SimpleMem: `H(W_t) = α·|E_new|/|W_t| + (1-α)·(1-cos(E(W_t), E(H_prev)))`, discard below τ=0.35.

**Entity rewrite safety.** Prompt pattern: "for each fact you drop, explicitly state why. Do not drop unless directly superseded." Forces LLM to justify lossy compression.

**Contradiction handling strategies:**
1. Bi-temporal invalidation (Zep/Graphiti): old fact gets `t_invalid`, both persist. Best for entity files.
2. CRUD resolution (Mem0): ADD/UPDATE/DELETE/NOOP. Simpler, loses history.
3. Confidence decay (Hindsight): penalty rather than replacement. Best for `how-i-work.md`.

**Link discovery grounding.** Graphiti uses 5 separate specialized prompts (not one combined). Separation reduces hallucinated relationships. Rule: never find connections without actual episode text. Names alone produce hallucinations.

**Crash safety.** Single-file: `os.replace()` + `os.fsync()`. Multi-file: write-ahead manifest (pending → write each file → complete). On restart: re-run pending.

**Idempotency.** Content-hash gating: hash all input episodes, skip if hash matches stored hash. Use `temperature=0.0` for consolidation.

---

##### Performance Research Insights (Retrieval & Scale)

**Grep wins at small scale.** LlamaIndex 2026: filesystem/grep correctness 8.4 vs RAG 6.4, relevance 9.6 vs 8.0. Crossover ~100 docs. GrepRAG: ripgrep 38.61% exact match vs GraphCoder 19.44% for named entities. 14x faster latency.

**Grep failure modes:**
- Vocabulary mismatch ("athletic footwear" misses "shoes"). BM25: ~0.72 recall. Hybrid: ~0.91.
- Keyword ambiguity: high-frequency tokens produce noise
- Context fragmentation: overlapping matches without deduplication

**Scale ceiling.** ~500-2K files for <100ms without caching. With BM25 pre-indexing: 10K+ files at microsecond queries.

**Token budget overflow strategy:**
1. Entity names + scores only (near-zero tokens)
2. Full content for top-N by relevance
3. Tiered compression (full → summary → pointer)
4. Truncate lowest-degree nodes in traversal subgraph

**Skip-least-connected caveat.** Safe only if low relevance AND low betweenness centrality. Degree-1 node may be sole bridge between clusters.

**Cost optimization (ranked):**
1. Skip unchanged entities (content-hash) — 80-90% reduction
2. Entropy pre-filter before LLM calls
3. Adaptive retrieval depth (k=3 simple, k=20 cross-entity)
4. Model tiering (cheap for significance/dedup, expensive for contradictions)
5. Batch consolidation at cluster similarity >0.85
6. All consolidation offline/async

---

##### Key References

| Paper/System | Relevance | URL |
|---|---|---|
| MINJA (2025) | Memory injection via queries, 95%+ success | arxiv.org/html/2503.03704v4 |
| Promptware Kill Chain (2026) | 7-stage attack framework for persistent agents | arxiv.org/pdf/2601.09625 |
| OWASP AI Agent Security | SecureAgentMemory reference class | cheatsheetseries.owasp.org |
| MAIF | Cryptographic memory format (Ed25519 + hash chain) | github.com/mbhatt1/maif |
| Unit42 Memory Persistence | Indirect prompt injection to long-term memory | unit42.paloaltonetworks.com |
| GrepRAG (2026) | grep vs vector empirical benchmark | arxiv.org/html/2601.23254v1 |
| REMINDRAG (2025) | 58.8% token reduction via guided traversal | arxiv.org/pdf/2510.13193 |
| AFM (2025) | Tiered fidelity (Full/Compressed/Placeholder) | arxiv.org/html/2511.12712 |
| SimpleMem (2026) | 30x token reduction, entropy filtering | arxiv.org/html/2601.02553v1 |
| Hindsight (NeurIPS 2025) | Belief confidence, background synthesis | arxiv.org/html/2512.12818v1 |
| Zep/Graphiti (2025) | Bi-temporal contradiction handling, 5 prompts | arxiv.org/html/2501.13956v1 |
| A-MEM (NeurIPS 2025) | Zettelkasten-style dynamic linking | arxiv.org/abs/2502.12110 |
| CLS + Sleep Replay (2022) | Biological basis for sleep consolidation | Nature Communications |
| Reconsolidation/PE (2022) | Prediction error triggers memory update | PNAS |

---

##### Research Gaps

1. **MINJA is undefended** — plausible malicious reasoning bypasses all moderation. Open research problem.
2. **No wiki-link-specific graph injection research** — extrapolated from text-level GIAs (NeurIPS 2024). Need purpose-built red teaming.
3. **Consolidation LLM injection defenses are immature** — multi-model committee (3-7x cost) is most effective but expensive. Sleep-cycle amortizes cost.
4. **No empirical grep recall for natural-language notes** — GrepRAG covers code. Vocabulary mismatch rate for prose likely higher than 28%.
5. **Multi-file atomic transactions** — no turnkey solution. All systems use single-file writes or eventual consistency with manifests.
6. **Concurrent writes** — all reviewed systems are single-writer. Race conditions on backlinks under multi-agent scenarios are untested.

---

---
