# arcmemory — Setup Guide

Steps required to make arcmemory fully functional that live **outside** the arc
codebase (system packages, model downloads, environment). Keep this file current:
**whenever we do something out-of-band to get arcmemory working, record it here.**

---

## 1. The embedder is REQUIRED for dedup and analogical recall

arcmemory's default `embed_backend = "local"` is arcllm's on-device backend, which
needs **`sentence-transformers`**. That library is declared by `arcllm[local]`, pulled
in through `arcmemory[local]`, which the **root `arc` package now depends on** — so a
plain `uv sync --all-packages` installs it. (It used to be an extra nobody's install
command asked for, which is exactly how the fleet ran for months with no embedder.)

**If the embedder is absent, arcmemory degrades** — no crash, but:

- **Hybrid recall loses its semantic third.** Recall fuses vec (semantic) + BM25
  (lexical) + graph (associative) + recency; with no embedder the vec list is dropped
  and only the last three answer. Paraphrase and cross-domain matches are missed.
- **Entity dedup does nothing.** `merge_entities` clusters candidate duplicate cards by
  name embedding; with no embedder it finds no candidates, so `custom-erp`,
  `custom-erp-project`, `custom-erp-ctg` … accumulate forever. This is the #1 symptom of
  a missing embedder: **multiple cards for the same real-world entity.**
- **The agentic sleep pass's `search_similar_entity` tool** falls back to lexical only,
  so the consolidation agent can't find near-duplicates to merge either.

> The degrade is now LOUD: a one-per-process `WARNING` on the `arcmemory.degrade`
> logger, plus the per-query `recall.degraded` audit event, plus a non-zero exit from
> `arc memory status`. It can no longer happen unnoticed.

### Install

```bash
uv sync --all-packages          # the deployment command; installs the embedder
pip install "arcmemory[local]"  # standalone equivalent
```

This pulls `torch` + `transformers` (~2 GB). On a CUDA box (e.g. DGX Spark) it uses the
GPU automatically; on CPU it still works, just slower. First use downloads the embedding
model (~100 MB) to the HuggingFace cache.

### Verify

```bash
arc memory status ~/arc/team
```

`semantic recall: LIVE` means the real embed call answered and the sqlite-vec extension
loaded. `DEGRADED` prints the reason, the fix, and exits 1 (so a deploy check can gate
on it). Pass workspace dirs to also see per-agent `chunks / embedded` counts — an
embedder can be live while an agent's index was never rebuilt.

### The remote alternative

Instead of on-device weights, point at an OpenAI-compatible `/embeddings` endpoint:

```toml
[modules.memory.config.backend]
embed_backend  = "provider"
embed_base_url = "https://your-endpoint/v1"
embed_model    = "text-embedding-3-small"
```

The API key comes from the `ARC_EMBED_API_KEY` environment variable — never from the
TOML, so no credential lands on the filesystem.

### Turning it off deliberately

If you truly want a keyword-only, embedder-free deployment, set
`[modules.memory.config.backend] embed_backend = "none"`. Then the degrade is
intentional and `arc memory status` says so.

---

## 2. Consolidation needs a distiller LLM

The sleep pass (fact/insight/procedure distillation, day summaries, and the LLM
merge-confirmation step) requires an LLM. It is wired through the agent's config:

```toml
[modules.memory.config]
distill_provider = "anthropic"          # empty => consolidation is a no-op
distill_model    = "claude-sonnet-5"
```

With no distiller, capture and keyword recall still work, but nothing is consolidated and
no merges are confirmed. Requires whatever credentials that provider needs (e.g.
`ANTHROPIC_API_KEY` in the environment) — see the provider's own setup.

---

## 3. Deployment checklist (DGX / any host)

On every deploy of an arc host that runs memory:

1. `uv sync` (or `pip install`) the arc packages **including the embedder extra** —
   confirm `python -c "import sentence_transformers"` succeeds in the deployed venv.
2. Confirm each agent's `arcagent.toml` has `distill_provider` set (else consolidation
   is inert) and `embed_backend = "local"` (the default).
3. Ensure the provider API key/env is present for the service.
4. Restart the service and confirm no `memory.dedup_skipped` (reason `no-embedder`) audit
   events appear on the first consolidation.

---

## 4. Postgres + pgvector index backend (SPEC-073, OPTIONAL)

The document/chunk index sits behind a pluggable `IndexBackend` (COMP-007). The default is
SQLite + `sqlite-vec` — nothing below is needed for it. To scale to Postgres + `pgvector`:

**Config vs secret (the rule):** the *selection* is non-secret config —
`[modules.memory.config.backend.dynamics] index_backend = "postgres"` in the agent toml
(the deploy overlay `deploy_node_overlays.py memory-config` writes exactly this). The
*connection string* is a secret — it comes from the `ARC_MEMORY_PG_DSN` env var (in
`~/.arc/config/arc.env`, 0600, sourced into the service), never from any toml. This mirrors
`ARC_EMBED_API_KEY`.

**Install the extra:** `pip install "arcmemory[postgres]"` (pulls `asyncpg` + `pgvector`).
Absent, `open_index_backend("postgres")` raises a clear "install arcmemory[postgres]" error.

**Deploy lanes (both opt-in; a default deploy runs neither):**
- **Host (DGX / Azure VM, systemd):** set `ARC_MEMORY_INDEX_BACKEND=postgres` and
  `POSTGRES_PASSWORD=...` in the deploy `.env`. `deploy-node.sh` then runs
  `scripts/install-postgres.sh` (a pgvector container via Docker, idempotent), writes
  `ARC_MEMORY_PG_DSN` into `arc.env`, and applies the `memory-config` overlay fleet-wide.
- **Docker (cloud compose):** `docker compose --profile postgres up` starts the
  `pgvector/pgvector:pg16` service; set `ARC_MEMORY_PG_DSN` + `POSTGRES_PASSWORD` in `.env`.

**Deploy gate:** confirm connectivity before trusting it —
`arc memory backend --index-backend postgres` exits 0 when pgvector is reachable and the
`vector` extension is present, non-zero otherwise (the same exit-code contract as
`arc memory status`).

---

## 5. Signed fleet-shared knowledge

Shared knowledge is optional and belongs to the existing memory module rather
than a parallel runtime:

```toml
[modules.memory.config]
shared_knowledge_enabled = true
```

The fleet root resolves through `arc_team()` and records are stored below
`shared/knowledge`. Do not compose this path manually. The service account must
be able to create this directory atomically; production storage should be
encrypted at rest. Agent signing keys remain non-exportable and must not be
copied into configuration or the shared tree.

Smoke-test the boundary by saving a personal record, calling
`knowledge_promote(reference)`, retrieving it from a second authorized agent,
and confirming a lower-clearance agent receives no record. Directly changing a
shared document must make signature verification fail rather than silently
accepting the edit.

---

## Change log of out-of-band setup actions

- **2026-08-22** — Added optional PostgreSQL/pgvector indexing and signed,
  classification-gated fleet knowledge promotion under the canonical team root.

- **2026-07-13** — Installed `sentence-transformers>=3.0` (→ `sentence-transformers 5.6.0`,
  pulled `torch 2.13.0`) into the DGX arc venv. The box had neither installed, so the
  local embedder was dead and entity dedup was a silent no-op — the cause of the
  duplicate-card clusters observed across all fleet agents.
