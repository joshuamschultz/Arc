# SPEC-062 — Team Docs: A Shared Document Corpus Agents Know Exists

**Status:** Draft for review — written 2026-08-09, not yet approved.
**Feature:** `team-docs` (spec folder `SPEC-062-team-docs`)
**Requested by:** Josh, 2026-08-09.

## 1. What this is

arcteam must store **documents at team level**, make them visible and editable in **ArcUI**, and let **every agent in the fleet read them** — and, critically, every agent must **know the store exists** so it reaches for it unprompted. Documents are added through the UI. A **semantic insertion** — an ontology/index of what is where — is injected into every agent's context so the store is discoverable, not just technically readable. The ontology itself is manageable through the UI.

This spec covers exactly that gap. arcteam and ArcFlow (SPEC-061) will be used heavily and must be bulletproof; this is the shared-document/knowledge half of that bar.

Arc already contains most of the individual pieces — a shared file store, a team knowledge graph, an embedding pipeline, and a proven prompt-injection hook. None of them are connected to each other, and none of them reach an agent by default. The design below is deliberately a **connection and packaging job**, not a new subsystem: it names what's missing, minimally.

## 2. Problem statement — what exists, what's broken, what's missing

Every claim below is cited to a file and line in `/Users/joshschultz/Projects/arc`.

### 2.1 The storage-and-access trap

`arcteam.files.TeamFileStore` (`packages/arcteam/src/arcteam/files.py:33-160`) is a real, agent-callable shared file drop. It is wired to two live capability tools, `store_team_file` and `list_team_files` (`packages/arcagent/src/arcagent/modules/messaging/capabilities.py:628-671`), enabled by default whenever `[modules.messaging]` is on. But it has two traps that make it unsuitable as-is for a company document corpus:

1. **The default root is inside one agent's own directory, not the fleet's.** `TeamFileStore`'s `team_root` is resolved by the messaging runtime as `ws.parent / "team"` where `ws` is the calling agent's own workspace (`packages/arcagent/src/arcagent/modules/messaging/_runtime.py:135-136`). Since `ws.parent` is that agent's own directory, every agent gets a **private** `<agent_dir>/team/` folder unless the operator manually sets the same absolute `[team] root` in every agent's `arcagent.toml`. Nothing today enforces or even warns about this — a fleet can run for a long time with N agents each writing to their own island, "sharing" nothing, with no error anywhere.
2. **Even a genuinely shared file is unreadable by the agent's own tools.** The built-in `read`/`ls`/`find`/`grep` tools resolve every path through `resolve_workspace_path` (`packages/arcagent/src/arcagent/tools/_validation.py:202-297`), which accepts a path only inside the agent's own `workspace` or an operator-listed entry in `[tools] allowed_paths` (`packages/arcagent/src/arcagent/core/config.py:126-142`, empty by default). `list_team_files` can hand an agent a path outside its workspace, but the agent's own `read` tool will refuse to open it unless the operator has separately added that shared directory to `allowed_paths`. Two correctly-configured pieces (`list_team_files` returning a path, `read` confined to workspace) compose into a dead end.
3. **The store has no metadata, no search, no versioning, and partitions by uploading agent** (`{team_root}/shared/files/{agent_name}/<filename>`, `files.py:112-160`) — the wrong axis for a company corpus, which is organized by topic and owner, not by who happened to upload it.

Separately, `arcteam.memory` (`packages/arcteam/src/arcteam/memory/`, 1,881 lines, ~90 tests) is a complete, tested, shared team knowledge graph — but its `entity_type` allowlist has no `"document"` type and its `per_entity_budget = 800` tokens is enforced on write (`memory/promotion_gate.py:131-137`, `memory/config.py:19-26`). It is designed for compact entity cards, not documents, and it is reachable only from a separate `arc-memory` CLI console script (`memory/cli.py:149-154`) — no agent tool, no ArcUI route, no arcagent module calls `TeamMemoryService` anywhere in the tree. SPEC-008's own PRD deferred the bridge that would wire it to agents to "Phase 3 — separate spec" (`.claude/specs/SPEC-008-arcteam-memory/PRD.md:248-257`) and it was never written.

### 2.2 No UI surface

ArcUI's knowledge system is per-agent only, end to end. `routes/knowledge.py` imports `MemoryOperator` exclusively from `arcmemory.operator` (`packages/arcui/src/arcui/routes/knowledge.py:27,49-51`), one SQLite store rooted at a single agent's workspace, and this is structurally enforced by `tests/test_arcui_no_team_imports.py`. Every knowledge route requires `{agent_id}`; the fleet-aggregation module `team_pages.py` exposes only roster/policy/tasks/tools-skills/audit (`team_pages.py:262-267`) — no memory or document surface at all. `MemoryOperator` has no `create_entry` method and arcui's knowledge routes have no `POST` — **new content can only be minted by the agent itself, never uploaded through the UI** (`packages/arcmemory/src/arcmemory/operator.py:149-459`). A repo-wide grep for multipart/file-upload machinery in `routes/` returns nothing — **ArcUI has no upload endpoint anywhere today.** A grep for `arcteam.memory`/`TeamMemory` across all of arcui's sources also returns zero hits: nothing in the dashboard touches team-level knowledge.

### 2.3 No semantic layer, no chunker, no wired ingestion

Arc's only embedding path, `arcllm.embed()` (`packages/arcllm/src/arcllm/embeddings.py:382-390`), is solid and already in production use: a `LocalEmbedder` backend running `sentence-transformers`/MiniLM offline (384-dim, `embeddings.py:46-47,101-145`), baked into the base Docker image at build time (`Dockerfile:41-60`) with `HF_HUB_OFFLINE=1` at runtime (`Dockerfile:96`) — genuinely air-gapped. `arcmemory` already exercises the intended pattern end to end: a `vec0` sqlite-vec virtual table (`packages/arcmemory/src/arcmemory/db.py:176-179`, `DEFAULT_DIMS=384` at `db.py:30`), an FTS5 BM25 mirror, and `rrf_fuse()` combining the two (`packages/arcmemory/src/arcmemory/fusion.py:16`). None of this is arcteam- or document-aware.

Two gaps block reusing it directly for a document corpus:

1. **No chunker exists anywhere in Arc.** `iter_source_chunks()` treats one whole markdown file (or one raw event) as one atomic chunk (`packages/arcmemory/src/arcmemory/index/source.py:42-75`) — no windowing, no overlap, no token-budget splitting. Embedding a 30-page policy as a single vector is poor retrieval by construction.
2. **PDF/DOCX/XLSX extraction exists but is dead code.** `packages/arcagent/src/arcagent/utils/file_handler.py:276-355` has working extractors for all three formats, but `pdfplumber`, `PyPDF2`, `python-docx`, and `openpyxl` are wrapped in `try/except ImportError` and none of them are declared anywhere — not in `pyproject.toml` (only `memory` and `azure` extras exist, `packages/arcagent/pyproject.toml:40-56`), not in the `Dockerfile`, not in the resolved SBOM. The code path only ever runs if someone installs the libraries by hand, and even then it only truncates extracted text into a chat prompt (`file_handler.py:65,228-246`) — it was never wired into any indexing pipeline.

`arcteam.memory`'s own search (`memory/search_engine.py`) is lexical BM25 + wiki-link graph traversal only — `rank_bm25.BM25Plus`, no embeddings, no vectors, confirmed by grep across the module (`search_engine.py:17-19,118-179`). If team-level content needs semantic search, that integration doesn't exist yet in either direction.

### 2.4 Agents don't know any of this exists

`ContextManager.assemble_system_prompt` (`packages/arcagent/src/arcagent/core/session_internal/context.py:298-368`) emits one bus event, `agent:assemble_prompt`, with a mutable `sections` dict every subscriber can write into (`context.py:345-350`). This is a real, already-used, generic extension point — policy (`modules/policy/capabilities.py:46-56`, priority 60), memory recall (`modules/memory/capabilities.py:75-118`, priority 50), the team roster (`modules/messaging/capabilities.py:251`, priority 50), and task/plan frontiers (`modules/tasks/capabilities.py:1065`, `modules/planning/capabilities.py:251`) all inject this way. **No module currently injects anything about a document corpus.** An agent with perfect read access to a shared docs folder still has no reason to ever look — the "agents must know the store exists" half of the requirement is presently unimplemented by construction, not by bug.

Two more scraps worth naming because they look like solutions and are not: `Message.refs: list[str]` on the messaging envelope (`packages/arcteam/src/arcteam/types.py:100`) is schema-only — populated only by `arc team send --refs` and **dropped before delivery**, never read by `_format_delivery()` (`packages/arcagent/src/arcagent/modules/messaging/capabilities.py:189-201`). And `arcgateway.fs_reader`'s `scope="team"|"shared"` (`packages/arcgateway/src/arcgateway/fs_reader.py:40,256-259`) is an explicit `NotImplementedError` stub, and even its working `scope="agent"` is arcui's own read-only chokepoint into a single agent's workspace — never called by the agent runtime at all (`packages/arcui/src/arcui/routes/team_pages.py:150-157` is the sole caller).

### 2.5 A related, cited correctness bug worth avoiding, not fixing here

`arcteam.memory.classification.ClassificationChecker` imports its own `Classification` type from `arcteam.memory.types` and fails **open**: an unrecognized classification label logs a warning and defaults to `UNCLASSIFIED` regardless of tier (`packages/arcteam/src/arcteam/memory/classification.py:126-132`). The canonical, tier-aware type lives in `arctrust.classification` — `parse_classification(value, *, strict)` fails **closed** (raises) at federal/enterprise and only warns-and-defaults at personal (`packages/arctrust/src/arctrust/classification.py:43-62`), and `arcmemory` already builds on it correctly. A typo in a document's frontmatter (`SECERT` for `SECRET`) must not silently make it world-readable. This design uses `arctrust.classification` throughout and does not touch or inherit `arcteam.memory`'s fail-open behavior.

## 3. Goals

- Store documents at **team level**, one corpus per fleet, reachable by every agent that opts in.
- Add through the **UI** (the first upload path ArcUI has ever had).
- Give agents both the **means** (extend the read boundary) and the **motive** (an injected ontology) to use the store — "agents must know it exists" is a first-class requirement, not a side effect of access.
- A managed **ontology** — human-and-agent-readable, editable through the UI, regenerated as the corpus changes.
- Documents are **citable artifacts**: content-hashed, classification-gated, audited, versioned rather than silently overwritten.
- Build on `arcteam.files`, `arcteam.memory`'s proven patterns (frontmatter + index + audit), `arcllm.embed()`, and the `agent:assemble_prompt` hook — reuse the rails ArcFlow and arcmemory already proved out, not a new subsystem.

## 4. Non-goals

- Changing per-agent `arcmemory` (episodic/semantic/procedural/entity memory). Untouched.
- A general-purpose RAG product, pluggable vector-DB backends, or anything beyond the in-process `sqlite-vec` pattern Arc already ships. No FAISS/Chroma/LanceDB/Qdrant/Pinecone/Weaviate — repo-wide grep confirms zero such dependency exists today, and this design doesn't add one.
- External vector databases or hosted embedding services as the default path — local MiniLM stays default; a remote `ProviderEmbedder` stays opt-in exactly as arcmemory already treats it.
- Rewriting `arcteam.files.TeamFileStore`, `store_team_file`, or `list_team_files`. They remain as-is — a light, per-agent-name attachment drop for casual sharing, a different and smaller use case than a governed corpus (see §6).
- Implementing `fs_reader`'s `scope="team"|"shared"` stub. See §6 — decided against, for now.

## 5. Design

### 5a. Team document store

**Proposal: a new module, `arcteam.docs`, sibling to `arcteam.memory` and `arcteam.files`.** It reuses `arcteam.files`' containment-check pattern (`files.py:25-30`) and its already-declared dependencies — `python-frontmatter>=1.0` and `rank-bm25>=0.2.2` are already in `packages/arcteam/pyproject.toml:22-28`, so the metadata-sidecar-plus-lexical-index half of this needs **zero new arcteam dependencies**. It does not touch `TeamFileStore`'s existing shape or its two shipped tools; `store_team_file`/`list_team_files` keep working exactly as they do today for lightweight attachment sharing (open question in §9: whether the corpus should instead *be* an evolved `TeamFileStore` — see there for the tradeoff).

Layout:

```
{team_root}/shared/docs/
├── ontology.md                      # §5d
├── index.db                         # §5c — sqlite-vec + FTS5
├── <doc_id>/
│   ├── meta.json                    # sidecar: owner, added_by, classification,
│   │                                 #   tags, added_at, content_hash, status,
│   │                                 #   superseded_by / supersedes
│   ├── v1/<original-filename>
│   ├── v2/<original-filename>       # a re-upload supersedes, never overwrites
│   └── current -> v2                # symlink or pointer field in meta.json
```

This mirrors ArcFlow's own versioning precedent — signed workflow definitions retain `versions/<n>.toml` rather than overwrite (`SPEC-061-arcflow/DESIGN.md` §4, "Storage, signing, versioning") — applied here to documents: **supersede, never overwrite**, so a citation made against v1 stays resolvable after v2 lands.

**Fixing the `team_root` trap (§2.1.1) is a prerequisite, not optional.** Proposal: when any team-level module is enabled (messaging, or this one), `arcteam_bootstrap` requires an explicit `[team] root` in every agent's config for a multi-agent fleet, and logs a **loud, startup-time warning** — not a debug-level line — whenever it falls back to the per-agent default (`ws.parent / "team"`, `messaging/_runtime.py:136`). A fleet where every agent silently gets its own island is exactly the failure class the corpus research flags repeatedly (arcteam's runner has the identical shape of bug in `ARCTEAM_NATS_URL` mismatches — "a separate island... failure is silent and looks healthy... this feature has produced five times," `workflow_runner_host.py:216-223`). This spec proposes the same discipline: assert convergence loudly at startup rather than fail silently at first use.

### 5b. Agent access

Two changes, both extending existing machinery rather than adding a parallel one:

1. **Extend the read-tool boundary automatically.** `agent_lifecycle.setup_capabilities()` already builds `allowed_paths` from `[tools] allowed_paths` before calling `builtin_runtime.configure()` (`packages/arcagent/src/arcagent/core/agent_lifecycle.py:98,107-115`). Proposal: when `[modules.team_docs]` (or `[modules.messaging]`, if this rides that module instead — open question §9) is enabled, append `{team_root}/shared/docs` to that list automatically, the same way the module already computes `team_root` for messaging. This keeps `resolve_workspace_path`'s deny-by-default posture (`_validation.py:279-296`) intact for everything else — only this one named directory is added, not `{team_root}` wholesale (which also holds `shared/runs/<run_id>/` workflow artifacts, `decisions.jsonl`, and `arcteam.memory`'s `entities/` — none of which this spec grants blanket access to).
2. **New agent tools**, NEW, in the module's `capabilities.py` (mirrors the `store_team_file`/`list_team_files` pattern):

| Tool | Classification | Purpose |
|---|---|---|
| `docs_list(tag=None, classification_max=None)` | read_only | Enumerate corpus entries the agent's clearance covers, with metadata. |
| `docs_read(doc_id, version=None)` | read_only | Return the current (or a specific) version's extracted text, classification-gated. |
| `docs_search(query, top_k=5)` | read_only | Semantic + lexical fused search (§5c), classification-gated at retrieval, not just at read. |
| `docs_store(source_path, title, tags=[], classification="UNCLASSIFIED")` | state_modifying | Ingest a file into the corpus as a new document or a new version of an existing one. Gated — see open question §9. |

Rich `@tool` metadata (`description`, `when_to_use`, `capability_tags`) on every one of these, following the existing convention (`packages/arcagent/tools/__init__.py`'s public `tool`/`ToolMetadata`, as used at `messaging/capabilities.py:628-671`) — description alone is not enough to make a model reach for a tool it's never been told exists; that job belongs to §5d's ontology injection, and the tool descriptions carry the second half of the "and will use it" bar.

### 5c. Semantic layer

Reuse the exact pattern arcmemory already proved, applied to the new corpus:

- **Embedding**: `arcllm.embed(texts, model=DEFAULT_EMBED_MODEL, backend="local")` — same call, same offline MiniLM/384-dim default, same Docker-baked weights, zero new embedding infrastructure (`arcllm/embeddings.py:382-390`, `Dockerfile:41-60,93-96`).
- **Storage**: a `vec0` sqlite-vec virtual table at `{team_root}/shared/docs/index.db`, same shape as `arcmemory/db.py:176-179` (`chunk_id TEXT PRIMARY KEY, embedding float[384]`), plus an FTS5 mirror for lexical search, fused with `rrf_fuse()` (`arcmemory/fusion.py:16`) — reused verbatim, not reimplemented.
- **Chunking — NEW, because none exists anywhere in Arc** (§2.3.1). A small, pure-Python token/char-window chunker with overlap, living in `arcteam.docs`. No new dependency: character or whitespace-token windowing needs nothing beyond the standard library. Exact window/overlap size is an open question (§9); a reasonable starting point mirrors common RAG practice (roughly 500–800 tokens per chunk, ~10–15% overlap) but should be validated against real documents before locking it.
- **Extraction — the dead-code path becomes real.** `pdfplumber`, `python-docx`, and `openpyxl` (already coded against in `arcagent/utils/file_handler.py:276-355` but never installed anywhere) need to be **actually declared** as a real extras group — e.g. `arcteam[docs]` or wherever the ingestion code ultimately lives — and added to the relevant install path (base image or an optional layer). This is a real, named decision this spec is surfacing, not a detail to discover during implementation: today those libraries are aspirational, not shipped, confirmed by grep across `pyproject.toml`, every `Dockerfile`, and the resolved SBOM.
- Ingestion (extract → chunk → embed → index) runs at `docs_store` time — synchronous for v1 given expected corpus sizes (a policy document, not a data lake); revisit as a background job only if upload latency becomes a real complaint.

### 5d. Ontology / semantic insertion — the "agents must know it exists" half

A single human-and-agent-readable file, `{team_root}/shared/docs/ontology.md`: what categories exist, which documents are load-bearing, and when an agent should reach for the corpus versus its own knowledge. Not the corpus content — a map of it, small by design (see the size discipline `arcteam.memory` already enforces via `per_entity_budget=800` tokens per card, `memory/config.py:19-26` — the ontology should be held to a comparable budget, capped in the low thousands of tokens, not proportional to corpus size).

Injection follows the exact, already-proven pattern (§2.4): a NEW `@hook(event="agent:assemble_prompt", priority=N)` subscriber in the new module, writing `sections["team_docs"] = <ontology text>` into the mutable dict every other section-injecting module already writes into (`context.py:345-350`). Cached at the `session` tier (`context.py`'s `_tier_of`/`_SESSION_SECTIONS` — the ontology changes rarely enough that re-rendering it every turn is waste; unknown-key sections default to the `run` tier per `context.py:229`, so this needs an explicit tier assignment, not the default). Regeneration is triggered by any successful `docs_store` call (mark-dirty, lazy-rebuild-on-next-read — the same discipline `arcteam.memory.index_manager` already uses for its own index, `index_manager.py:48-76`), and the file remains directly hand-editable through the ArcUI ontology editor (§5e) so an operator's curation is never clobbered by the next auto-regenerate without being asked (open question §9 covers the exact merge policy).

### 5e. ArcUI surface

New team-level routes, living beside `team_pages.py`/`team_chat.py`/`workflow_plane.py` — all of which **already import `arcteam`** (`team_pages.py:150-157`, `messaging.py:37-214`, `team_chat.py:182-252`, `workflow_plane.py:30,168,180,463-464`) — never beside `routes/knowledge.py`, which is deliberately kept arcteam-free by `tests/test_arcui_no_team_imports.py:` this is a **team**-level surface by definition and belongs with arcteam's other team routes, not the per-agent knowledge module.

| Route | Method | Notes |
|---|---|---|
| `/api/team/docs` | GET | List corpus entries with metadata. |
| `/api/team/docs` | POST | **NEW — the first multipart/upload endpoint ArcUI has ever had** (§2.2 confirmed zero exist today). Operator-gated, size-capped, secret-content-scanned reusing the pattern already shipped for workspace file saves (`routes/agent_detail/files_write.py:104-195`). |
| `/api/team/docs/{id}` | GET | Fetch one document's metadata + content/preview. |
| `/api/team/docs/search` | GET | `?q=` — the fused semantic+lexical search from §5c. |
| `/api/team/docs/ontology` | GET, PUT | Read/edit `ontology.md` directly. |

A new **Team Docs** page in the web UI: list + upload + preview + an ontology editor (a plain markdown textarea is enough for v1 — no new editor dependency). Reuses the existing two-role model (`viewer` read, `operator` control) exactly as every other ArcUI surface does (`auth.py:225-276`); mutating routes re-check `role == "operator"` at the route level in addition to the auth middleware, the same defense-in-depth pattern `knowledge.py:70-77` already uses for memory edits.

**Classification is respected, no-read-up, same discipline as arcmemory's retrieval gate** (`arcmemory/security.py:140,159-166`) — but note it applies differently on the two access paths: an agent's `docs_read`/`docs_search` calls check the calling agent's `Entity.clearance` (already a real field on the entity registry, `arcteam/types.py:145-156`) against the document's classification via `arctrust.classification.dominates()`; an **operator** browsing through ArcUI sees by role, not by a simulated clearance — operator access is already the highest tier in the two-role model, consistent with how `knowledge.py`'s operator-gated memory mutations work today.

### 5f. Audit + trust

- Every `docs_store` call, every UI upload, and every ontology edit is audited — reusing arcteam's `AuditLogger` (already used by messaging and workflow signing) and ArcUI's `emit_mutation_audit` pattern (already used for memory PATCH/DELETE, `knowledge.py`).
- Every stored document is **content-hashed** (SHA-256) at ingestion — this is what makes "supersede, not overwrite" (§5a) meaningful: a citation naming a document + hash is checkable against tampering the same way ArcFlow's manifest hashing protects a signed workflow definition.
- **Classification uses `arctrust.classification` exclusively** (`parse_classification(value, strict=tier != "personal")`, `dominates()`) — not `arcteam.memory.classification`'s own fail-open copy (§2.5). A malformed or missing classification label on a document must not silently default to world-readable at enterprise/federal tier; it should behave exactly the way `arcmemory` already behaves for the equivalent case.

## 6. Layering (who owns what)

| Package | Gets | Why |
|---|---|---|
| `arcteam` (NEW `docs` module) | The corpus engine: storage layout, metadata sidecar, chunker, index (`sqlite-vec` + FTS5), fused search, versioning, ontology regeneration trigger, audit writes | Team-level shared state is arcteam's layer already (`arcteam.memory`, `arcteam.files` both live here); a leaf module, no `arcagent` import, same as `arcteam.memory` today |
| `arcagent/modules/team_docs/` (NEW) | Thin tool surface (`docs_list/read/search/store`), the `agent:assemble_prompt` ontology-injection hook, the `allowed_paths` extension at capability setup | Agents author/consume; removable module, mirrors `modules/messaging`'s shape |
| `arcui` | New `/api/team/docs*` routes beside `team_pages.py`/`team_chat.py`; the Team Docs page | Operator-facing upload/ontology-edit surface; never touches `routes/knowledge.py`'s arcteam-free boundary |
| `arcllm` / `arctrust` | Nothing new | `embed()` and `classification` reused as-is |
| `arcmemory` | Nothing changed | Per-agent brain stays per-agent; this spec does not create a "team brain" (arcmemory's own research already rejected sharing an agent workspace across agents — colliding identity, sessions, and `workspace_path` on the entity registry) |

## 7. What this deliberately does not change

- **`TeamFileStore`, `store_team_file`, `list_team_files`** — unchanged. They remain the lightweight per-agent-name attachment drop; `arcteam.docs` is a different, governed thing (metadata, versioning, classification, search) built alongside it, not on top of it.
- **`arcmemory`** — per-agent episodic/semantic/procedural/entity memory is completely out of scope. No merging into a "team brain."
- **`arcteam.memory`** (the entity/knowledge-graph service) — also unchanged. It remains the right tool for compact, linked entity cards (people, projects, playbooks); this spec is for documents, a different shape of content with a different budget (§5a vs. `per_entity_budget=800`, `memory/config.py:19-26`). Wiring `arcteam.memory` to agents (SPEC-008's deferred "TeamMemoryBridge") is explicitly a separate spec, not folded in here.
- **`fs_reader`'s `scope="team"|"shared"` stub — decided against, for now.** The brief asked this to be decided and stated, not left open. `fs_reader` is arcui's own read-only chokepoint into a **single agent's own workspace**, used by the dashboard's per-agent file browser (`team_pages.py:150-157`); it is never called by the agent runtime at all. Implementing its `team`/`shared` scope would only help a human browsing team content through the generic per-agent files tab — which the new dedicated Team Docs page (§5e) already serves better, with metadata, search, and the ontology editor. Wiring the stub here would be redundant scope creep against a UI surface this spec is building anyway. If a future need for "browse team docs through the generic per-agent file tree" appears, revisit as its own small follow-up.

## 8. Requirements

EARS-style, mechanically testable. Numbered from REQ-260 (REQ-259 is the highest in use across existing specs).

**Storage**

- **REQ-260** (Must): The system SHALL store team documents under a single fleet-wide `{team_root}/shared/docs/` corpus, distinct from the existing per-agent-name `TeamFileStore` layout.
- **REQ-261** (Must): The system SHALL attach a metadata sidecar to every document recording owner, added_by, classification, tags, added_at, and content hash.
- **REQ-262** (Must): WHEN a document is re-uploaded under an existing document id THEN the system SHALL create a new version and SHALL NOT overwrite or delete the prior version's content.
- **REQ-263** (Must): WHEN any team-level document module is enabled on more than one agent in a fleet THEN the system SHALL require an explicit shared `[team] root` and SHALL emit a startup-time warning, at a visible log level, whenever it falls back to a per-agent-private default path.
- **REQ-264** (Should): The system SHALL compute a SHA-256 content hash for every stored document version and SHALL make that hash resolvable from the document's metadata.
- **REQ-265** (Must): The system SHALL NOT modify or remove the existing `TeamFileStore`, `store_team_file`, or `list_team_files` behavior.

**Access**

- **REQ-266** (Must): WHEN the team-docs module is enabled on an agent THEN the system SHALL add `{team_root}/shared/docs` to that agent's `[tools] allowed_paths` automatically, without requiring manual TOML edits.
- **REQ-267** (Must): The system SHALL NOT add `{team_root}` or any of its other subdirectories (`shared/runs`, `entities`, message audit storage) to an agent's `allowed_paths` as a side effect of enabling team docs.
- **REQ-268** (Must): The system SHALL expose `docs_list`, `docs_read`, and `docs_search` as read-only agent tools and `docs_store` as a state-modifying agent tool, each with a description and `when_to_use` sufficient to select it without prior context.
- **REQ-269** (Must): WHEN an agent calls `docs_read` or `docs_search` on a document whose classification exceeds the agent's clearance THEN the system SHALL deny the request using `arctrust.classification.dominates()` and SHALL NOT let a denied document influence search ranking.
- **REQ-270** (Must): The system SHALL use `arctrust.classification.parse_classification` for every classification comparison in this feature and SHALL NOT use or replicate `arcteam.memory.classification`'s fail-open unknown-label behavior.

**Semantic layer**

- **REQ-271** (Must): The system SHALL chunk a document into overlapping windows before embedding, rather than embedding a whole file as one vector.
- **REQ-272** (Must): The system SHALL embed chunks using `arcllm.embed()` with the local backend by default, requiring no network call at index or query time in the default configuration.
- **REQ-273** (Must): The system SHALL fuse vector and lexical (FTS5/BM25) search results via reciprocal rank fusion, reusing `arcmemory.fusion.rrf_fuse` rather than a new fusion implementation.
- **REQ-274** (Must): WHERE a document requires PDF, DOCX, or XLSX extraction THEN the system SHALL declare the extraction libraries as a real, installed dependency (not a bare `try/except ImportError` on an undeclared package).
- **REQ-275** (Should): The index SHALL be rebuildable from stored documents and metadata alone, without depending on any external service.

**Ontology / discoverability**

- **REQ-276** (Must): The system SHALL maintain a single ontology document describing the corpus's categories and key documents, kept within an explicit, small token budget rather than growing proportionally with corpus size.
- **REQ-277** (Must): The system SHALL inject the ontology into every enabled agent's system prompt via the `agent:assemble_prompt` hook, and SHALL NOT require any other agent action to make the corpus discoverable.
- **REQ-278** (Must): WHEN a document is stored, superseded, or its metadata changes THEN the system SHALL mark the ontology for regeneration rather than silently going stale.
- **REQ-279** (Must): The ontology SHALL be directly editable through the ArcUI Team Docs page, and an operator's hand edit SHALL be preserved rather than silently discarded by the next automatic regeneration.

**ArcUI**

- **REQ-280** (Must): The system SHALL provide an operator-gated upload endpoint for team documents, enforcing a size cap and the same secret-content scan already applied to workspace file writes.
- **REQ-281** (Must): The system SHALL provide list, detail, and search routes for team documents, readable by both viewer and operator roles.
- **REQ-282** (Must): The system SHALL provide a GET/PUT route for the ontology document, edit access gated to the operator role.
- **REQ-283** (Must): The system SHALL NOT import `arcteam` from `routes/knowledge.py` or any module covered by `tests/test_arcui_no_team_imports.py`; team-docs routes SHALL live alongside `team_pages.py`/`team_chat.py`/`workflow_plane.py`.

**Audit and trust**

- **REQ-284** (Must): The system SHALL audit every document store, every UI upload, and every ontology edit with actor identity and timestamp.
- **REQ-285** (Must): The system SHALL fail closed on an unrecognized or missing classification label at enterprise and federal tier, and SHALL default to UNCLASSIFIED with a warning only at personal tier — matching `arctrust.classification`'s existing tier behavior exactly.

## 9. Open questions for Josh

- **Corpus engine shape**: a new sibling module `arcteam.docs` (this design's default), or should the corpus literally *be* an evolved `TeamFileStore` with richer capabilities bolted on? The sibling-module route avoids changing the already-shipped `store_team_file`/`list_team_files` contract; the evolved-`TeamFileStore` route means one fewer concept but a riskier change to code already in use.
- **Is `docs_store` gated by human approval?** As currently proposed it's a plain `state_modifying` tool any agent with the module enabled can call. Should writes instead route through the existing `HumanGate`/`ApprovalGrant` machinery (SPEC-061's `arc approve` pattern), at least above personal tier?
- **Ontology auto-regen policy**: full auto-regenerate on every `docs_store` (simplest, risks clobbering operator curation), or an auto-suggested diff the operator must accept, or a purely operator-authored file that the system only ever proposes additions to?
- **Chunk sizing**: the ~500–800 token / ~10–15% overlap default in §5c is a starting guess, not validated against real documents — worth confirming against Josh's actual corpus before locking it.
- **Naming**: `arcteam.docs` / `team_docs` module name, and `{team_root}/shared/docs/` directory name — any preference, or does this collide with something in mind for a different feature?
- **Where does extraction run?** At the ArcUI upload handler (synchronous, simplest for v1-sized corpora), or inside the `arcteam.docs` ingestion function called from there? Affects whether the new PDF/DOCX/XLSX dependency (§5c, REQ-274) lands on `arcui` or `arcteam`.

## 10. Test plan sketch

**Unit**

- Chunker: deterministic windowing and overlap on a range of document lengths, including documents smaller than one window.
- Metadata sidecar: schema validation, rejecting an unlisted classification label at federal/enterprise strict mode.
- Versioning: re-upload creates a new version, prior version content is still readable, `current` pointer updates.
- Classification gating: `dominates()` denies a low-clearance agent's `docs_read`/`docs_search` against a higher-classification document; a denied document never surfaces in fused search ranking.
- Ontology budget: injected text stays within the configured token cap regardless of corpus size.
- `allowed_paths` extension: enabling the module adds exactly `{team_root}/shared/docs` and nothing else from `{team_root}`.

**Integration — one full-path proof, mirroring ArcFlow's own anti-dead-wiring discipline** (arcteam's `tests/e2e/` directory is currently empty — this spec should not repeat that gap):

1. Upload a real document (including at least one PDF, to exercise REQ-274's newly-declared extraction dependency) through the ArcUI Team Docs page as an operator.
2. Confirm the corpus's `meta.json`, content-hash, and index entries exist on disk.
3. Start a fresh agent with the module enabled; assert the ontology text is present in that agent's assembled system prompt — proof of REQ-277, not just that the hook fires.
4. Have the agent call `docs_search` with a query whose only good match is the uploaded document; assert the document is returned and its content is retrievable via `docs_read`.
5. Edit the document's classification to exceed the agent's clearance; repeat step 4 and assert the document is now denied, not just deprioritized.

This is the same shape of test that caught ArcFlow's own silent-failure classes (a healthy-looking system that in fact resolves nothing) — the corpus research here flags multiple analogous "looks fine, does nothing" failure modes (the `team_root` island trap, §5a; the workspace-confinement dead end, §5b) that only a real end-to-end run through all three surfaces (UI, storage, agent) will actually catch.
