# arcmemory — Setup Guide

Steps required to make arcmemory fully functional that live **outside** the arc
codebase (system packages, model downloads, environment). Keep this file current:
**whenever we do something out-of-band to get arcmemory working, record it here.**

---

## 1. The embedder is REQUIRED for dedup and analogical recall

arcmemory's default `embed_backend = "local"` uses **`sentence-transformers`**, which
is an **optional extra** (`arcmemory[local]`) — it is NOT pulled in by a bare
`pip install arcmemory` or by `uv sync` without the extra.

**If the embedder is absent, arcmemory silently degrades** — no crash, but:

- **Entity dedup does nothing.** `merge_entities` clusters candidate duplicate cards by
  name embedding; with no embedder it finds no candidates, so `custom-erp`,
  `custom-erp-project`, `custom-erp-ctg` … accumulate forever. This is the #1 symptom of
  a missing embedder: **multiple cards for the same real-world entity.**
- **Structural / analogical recall degrades to keyword-only** (BM25 + graph). The
  cross-domain "match the pattern with zero surface overlap" channel goes dark.
- **The agentic sleep pass's `search_similar_entity` tool** falls back to lexical only,
  so the consolidation agent can't find near-duplicates to merge either.

> The code now emits a LOUD warning + a `memory.dedup_skipped` (reason `no-embedder`)
> audit event when it runs without an embedder — so this can never silently degrade
> again. But the fix is to install the embedder.

### Install

```bash
# with the package extra (preferred):
pip install "arcmemory[local]"
#   or, into an existing arc venv managed by uv:
uv pip install "sentence-transformers>=3.0"
```

This pulls `torch` + `transformers` (~2 GB). On a CUDA box (e.g. DGX Spark) it uses the
GPU automatically; on CPU it still works, just slower. First use downloads the embedding
model (~100 MB) to the HuggingFace cache.

### Verify

```bash
python - <<'PY'
import asyncio, arcmemory
async def main():
    emb = arcmemory.ArcLLMEmbedder(model=None, backend="local", telemetry={"agent_did": "x"})
    v = await emb.embed_texts(["Custom ERP", "Custom ERP Project"])
    print("embedder OK, dims:", len(v[0]))
asyncio.run(main())
PY
```

If this prints a dimension count, the embedder is live. If it raises
`ModuleNotFoundError: sentence_transformers`, the extra is not installed.

### Turning it off deliberately

If you truly want a keyword-only, embedder-free deployment, set
`[modules.memory.config] embed_backend = "none"` in the agent's `arcagent.toml`. Then the
degrade is intentional and the warning is suppressed. Do NOT leave `embed_backend = "local"`
(the default) with the package uninstalled — that is the silent-degrade trap above.

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

## Change log of out-of-band setup actions

- **2026-07-13** — Installed `sentence-transformers>=3.0` (→ `sentence-transformers 5.6.0`,
  pulled `torch 2.13.0`) into the DGX arc venv. The box had neither installed, so the
  local embedder was dead and entity dedup was a silent no-op — the cause of the
  duplicate-card clusters observed across all fleet agents.
