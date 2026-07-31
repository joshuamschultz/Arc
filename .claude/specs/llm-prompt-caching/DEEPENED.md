# Prompt Caching — Deepened Design (pre-`/specify`)

> Enrichment of the 5 recommendations from the arcllm/arcrun caching audit.
> Filtered through **principled-coder** pillars in order: Simplicity → Modularity → Security → Scalability.
> Research: 5 parallel Explore agents (external provider docs + internal code). Evidence is file:line.

## Enhancement Summary

The audit found: **arcrun's loop is cache-correct by construction** (append-only, stable
system prompt, deterministic tool order, deterministic parallel-result ordering), but
**arcllm never emits a cache directive**, so Anthropic caching is fully OFF and
OpenAI/Gemini/Ollama caching works only implicitly and is unmeasured.

Deepening changed three of the five items materially:

- **#3 reframed** — arcrun must *not* learn caching. Its real obligation is an
  **immutable, deterministically-ordered per-run tool set**; the cache hit is an emergent
  benefit at the arcllm boundary. This also closes an ASI04/LLM06 security surface.
- **#4 escalated** — there is a **live per-turn cache-buster** in arcagent's
  `ContextManager` (prunes/truncates the prefix every turn above 70% context). This is
  shipping behavior, not hypothetical. The arcrun-side fix is a documented contract; the
  real behavioral fix lives in arcagent.
- **#5 inverted** — the obvious "add `keep_alive` param" fix is a trap: Ollama's
  OpenAI-compat endpoint **silently ignores it** (issue #11458). Correct fix is a server
  env var, zero code.

**Priority (cheapest × highest-value first):** #2 and #5 are trivial; #1 is the big cost
win, contained; #3 and #4 are structural.

---

## Item 1 — Anthropic `cache_control` breakpoints (turn caching ON)

**Current:** `anthropic.py:129` sets `body["system"] = system_text` (a bare string — cannot
carry `cache_control`). `_format_tool` (105-110) and `_format_content_block` (64-95) emit no
`cache_control`. Read side is **already wired**: `_parse_usage` (156-165) maps
`cache_read_input_tokens`/`cache_creation_input_tokens` → `Usage`. So metrics light up the
instant writes are emitted.

### Research Insights

**External mechanics (platform.claude.com):**
- Max **4** breakpoints/request. A breakpoint caches everything **before and including** it,
  and **cascades** — a later breakpoint auto-reads caches from earlier ones. So ≤3 suffices.
- Order is strict: **tools → system → messages**. Changing tools invalidates all three.
- Min cacheable prefix is model-specific and **silently no-ops below threshold**:
  Opus 4.8 / Sonnet 5 = **1,024**; Haiku 4.5 = 4,096; Fable 5 = 512. Breakpoint accepted,
  nothing cached.
- TTL: default 5-min `{"type":"ephemeral"}`; 1-hour `{"type":"ephemeral","ttl":"1h"}`.
  Write = 1.25× (5m) / 2× (1h) base; **read = 0.1× base**.
- `system` **must become a content-block list** to attach `cache_control`.

**Recommended minimal design — auto-placement, ZERO type change:**
Gate behind one flag (`DefaultsConfig.enable_prompt_caching: bool = True`). When on,
`AnthropicAdapter._build_request_body` stamps 3 breakpoints itself:
1. `cache_control` on the **last tool** (caches whole tool array),
2. convert `system` string → one-element block list + `cache_control` (caches system),
3. `cache_control` on the **last content block of the last message** (rolling tail).

- **Simplicity (wins it):** smallest correct change is *no type change at all*. A per-block
  `cache: bool` on `TextBlock`/`Tool`/`Message` is 5 lines + a new public contract for a
  decision the adapter makes correctly on its own. One flag + a helper beats it.
- **Modularity (decisive):** auto-placement keeps `cache_control` — an Anthropic wire
  specific — entirely inside `adapters/anthropic.py`. arcrun/arcagent stay provider-agnostic;
  OpenAI-wire adapters ignore the flag. A shared-type `cache` field would leak an Anthropic
  concept into the cross-provider contract. Reject caller-controlled until a real need
  (three-instances rule).
- **Security posture:** breakpoints ride content arcllm already sends → no new data path.
  Honor "no secrets in system prompts" (LLM07) — a 1h cache widens the exfil window. Keep
  **5-min default** (smaller blast radius, half the write cost); 1h is opt-in. Caches are
  per-API-key/org; a shared key across agents makes an identical poisoned prefix (ASI06)
  readable cross-agent — keep keys per-tenant.
- **Scalability ceiling:** the 4-breakpoint cap + 20-block lookback mean placement must be
  fixed, not fan-out — the 3-breakpoint scheme scales at any conversation length because the
  tail rolls forward. Real ceiling is the **5-min TTL under high concurrency**: an agent whose
  turn cadence exceeds 5 min re-pays the 1.25× write. Expose `ttl` in config for long-lived
  agents.

**Edge cases:** <1,024-token prompts silently no-op (don't log "cached"); keep plain-string
`system` path when caching off; anchor the message breakpoint on a stable text/tool_result
block, not a volatile image; **depends on arcrun emitting tools in stable order** (Item 3).

---

## Item 2 — Read OpenAI/Gemini cache telemetry (~3 lines)

**Current:** `openai.py:_parse_usage` (263-275) reads `completion_tokens_details.reasoning_tokens`
but **drops** the parallel `prompt_tokens_details`. `google.py` inherits it. Billing
(`telemetry_cost.py:28-31`) and pricing config (`config.py:40-41`) already consume
`cache_read_tokens` — the read-back closes the loop with no downstream change.

### Research Insights

- OpenAI field is exactly `usage.prompt_tokens_details.cached_tokens`; auto-cache ≥1,024 tok;
  real discount (up to ~90%) → reading it materially improves cost accuracy.
- **Gemini's OpenAI-compat endpoint returns the identical field** → `google.py` inheritance
  stays correct; **do not fork parsing.** Implicit caching on by default (2.5+, ≥2,048 tok).

**Design:** add to `_parse_usage`:
```python
prompt_details = usage_data.get("prompt_tokens_details")
cache_read = prompt_details.get("cached_tokens") if prompt_details else None
```
pass `cache_read_tokens=cache_read`. **Mirror it in the SSE streaming parser**
(`_parse_openai_sse_line` ~54-59) to avoid a stream/non-stream asymmetry.

- **Simplicity:** ~3 lines/site. No abstraction, no per-provider strategy, no flag.
  Leave `cache_write_tokens = None` (implicit caching has no billable write).
- **Modularity:** the single fix in `OpenaiAdapter` propagates to Gemini for free. Forking
  = boundary violation with zero payoff.
- **Security:** integer counts only, no PII/injection surface. If `prompt_cache_key` is ever
  added it must **not** derive from user content (cross-agent correlation handle under
  zero-trust).
- **Scalability:** the read-back is O(1). **YAGNI on `prompt_cache_key` and `cachedContent`** —
  a shared cache key overflows OpenAI's ~15 req/min-per-prefix ceiling at fleet scale and
  *reduces* hit rate; `cachedContent` is a heavyweight managed resource. Defer both until a
  measured miss problem exists.

**Edge cases:** guard with `.get()` (field absent below threshold / on compat servers);
**preserve `0` vs `None`** — `0` = real cache miss, `None` = unsupported; don't coalesce.

---

## Item 3 — Cache-stable tool definitions (REFRAMED: freeze the registry)

**Current:** `ToolRegistry` (registry.py:12-42) is mutable and `list_schemas()` is re-read
every turn (react.py:174). But grep finds **zero** `.add()`/`.remove()` call sites in the
running loop — the set is *de facto* immutable per run. The risk is a **latent capability**,
not a firing path. `agent.reload()` / `create_tool` mutate the **agent-level** registry and
surface only on the **next** run (snapshotted pre-run). Subagent scoping already gives each
child its own registry + cache prefix (`agent_dispatch.py:69-85`). `use_skill` already appends
skill bodies to the **message tail**, never the tools block.

### Research Insights — the load-bearing modularity call

**arcrun should NOT know about caching.** A `cache_control` breakpoint is an arcllm/provider
concern. Reframe the requirement from "cache-stable tools block" to **"immutable,
deterministically-ordered per-run tool set"** — a legitimate arcrun loop/state invariant. The
provider cache hit then falls out for free at the arcllm boundary. No concept leaks into the
nucleus; resolves the CLAUDE.md "Don't Mix Concerns" tension cleanly.

**Recommended policy (ranked by pillars):**
1. **Freeze `ToolRegistry` per run** — reject `add`/`remove` after construction (or drop them
   from the per-run object; keep mutation only on arcagent's pre-run capability registry).
   Turns byte-stability into a **structural invariant**. Memoize `list_schemas()`.
2. **Keep dynamic capability on the deferred/menu path** (already true — codify it): new
   tools/skills surface via a stable `use_skill`-style meta-tool and land in the **message
   stream**, never the tools block. O(1) tools-block cost regardless of latent capability count.
3. **Scope genuinely-new directly-invocable tool sets to a subagent** (already implemented —
   prefer over append). Parent prefix untouched by construction.
4. **Do NOT adopt "append-only to the live registry"** as the primary mechanism — it still
   grows the tools block unboundedly and forces a cache *write* if the breakpoint trails the
   list. Fallback only.
5. **Breakpoint lives in arcllm** (Item 1), not arcrun.

- **Security posture:** freezing shrinks attack surface — no runtime `add()` for a
  prompt-injected instruction (LLM01) to reach, no smuggling a tool past the human-reviewed
  set (LLM06), no live tool-block rewrite by a poisoned dynamic tool (ASI04/ASI06). Dynamic
  tools already reach the model only via `reload()` → next run — keep it. A mutation event
  *during* a run should be an **anomaly signal**, not a normal path.
- **Scalability ceiling:** tools→system cached once per TTL across thousands of agents vs
  every turn (busting multiplies input tokens by turn count — LLM10). "Append-only" has a
  ceiling (unbounded tools block); **deferred loading is the only pattern with no ceiling.**

---

## Item 4 — `transform_context` stability (ESCALATED: live offender in arcagent)

**Current:** `react.py:168-171` runs `state.transform_context(messages)` every turn on the
full list; the result is sent to the model. The one real production caller is arcagent's
`ContextManager.transform_context` (`arcagent/src/arcagent/core/session_internal/context.py:209-258`)
— and it **is a non-append transform** above threshold (config.py:170-172, ratios 0.70 prune /
0.85 compact / 0.95 emergency over `max_tokens`):
- <0.70 → identity (append-safe, cache-preserving),
- ≥0.70 → `prune_observations` rewrites OLD tool messages to `"[output pruned]"` placeholders
  (prefix rewrite),
- ≥0.95 → `_emergency_truncate` drops oldest messages (prefix rewrite).

Because the "protected recent 40%" boundary **slides** as turns append, a *different* set of
old messages is pruned every turn → the prefix changes **every turn** once over threshold.
Shipping behavior.

### Research Insights

Anthropic's model: caching is a **prefix match**; append-only is the invariant. **Compaction
is a deliberate once-per-task boundary reset** (the `/compact` model) — accept one cache miss,
rebuild a stable prefix from the summary — **not a per-turn rewrite**. arcagent currently does
the opposite.

**Recommended minimal design (split by owner):**
1. **arcrun:** document the append-only contract on the seam (docstrings at `loop.py`
   run/run_async, `streams.py:183`, `react.py` call site, `state.py:36`). One line:
   prefix-stable, append-only, compaction is a boundary reset not a per-turn transform.
2. **arcrun (optional):** a **debug-flag-gated** assertion that returned-prefix == input-prefix;
   warn/raise in dev only. **Never in the default hot path** (<500ms cold-start, 1000s of agents
   — no per-call O(context) diff tax).
3. **arcagent (the real fix, separate scope):** make compaction a **one-time boundary reset**
   (fire `agent:pre_compaction`, rebuild one fresh stable prefix) instead of re-pruning a
   sliding window every turn. Unit test: identity below threshold, bounded single-reset above.
4. **Doc fix:** CLAUDE.md structure diagram says `core/context_manager.py`; the actual file is
   `core/session_internal/context.py` (class `ContextManager`, logger still
   `arcagent.context_manager`).

- **Modularity (load-bearing):** the invariant's **owner is the caller (arcagent)**, not
  arcrun. Hard-enforcing cache semantics in the loop couples the nucleus to a concern it
  doesn't own. arcrun documents + optionally asserts; arcagent guarantees.
- **Security:** `steering/security.md:149` already flags summarizing/pruning transforms as an
  injection-laundering vector (ASI06/LLM01). Append-only slightly *helps* auditability — the
  untransformed `state.messages` stays the tamper-evident record. Assertions fail closed.
- **Scalability ceiling:** long-running/high-turn agents are exactly where caching matters
  most and where the current design fails worst — >70% context turns O(new tokens) cache-read
  into O(full context) re-encode **per turn**. Append-only + one-time compaction keeps
  per-turn cost flat as history grows.

**Not YAGNI** — there's a live offender. But the fix is minimal and split by owner, not a
heavyweight runtime guard.

---

## Item 5 — Ollama `keep_alive` (INVERTED: server env var, zero code)

**Current:** `ollama.py` is a 12-line alias overriding only `name`; it inherits the
OpenAI-compat `/v1/chat/completions` path. `_build_request_body` has **no passthrough / no
`extra_body`** — so "add `keep_alive` to extra params" needs code *and* still wouldn't work.

### Research Insights

- **The compat endpoint silently ignores `keep_alive`** (Ollama issue **#11458**, closed) and
  ignores `options` (num_ctx). They work only on native `/api/chat` or via a Modelfile-baked
  model. Any code that "sets" `keep_alive` on `/v1` looks right and does nothing — verify with
  `ollama ps`, not the response.
- **No cached-token count is returned** (native or compat) → Ollama cache reuse is
  unobservable from the payload; infer from latency + residency.
- KV/context reuse happens automatically while the model stays resident (llama.cpp
  context-shifting). The `/api/generate` `context` token array is deprecated.

**Recommended minimal design:**
1. **Default (recommended, zero arcllm change):** set **`OLLAMA_KEEP_ALIVE`** on the Ollama
   host(s) via deployment config. Finite `30m`–`24h` for shared fleet boxes; `-1` only for
   dedicated single-model hosts. Warm KV cache, adapter untouched, LOC/mypy untouched.
2. **Only if per-request/per-model control is a hard requirement:** add an `OllamaAdapter`
   override targeting native `/api/chat` (its own request/response translation, NDJSON
   streaming, its own tests) — a real adapter, not a body tweak on the compat path.

- **Simplicity:** the smallest change is *not code* — it's an env var on the server.
- **Modularity:** `keep_alive` is an Ollama-runtime concern; putting it in the shared
  `_build_request_body` leaks provider-specific behavior into every OpenAI-compat provider as
  dead weight. Where it belongs, cheapest first: **(a) server env → (b) `ProviderConfig`
  default read by a native override → (c) per-request kwarg on the native override.** Never a
  field on the shared compat path.
- **Security:** local, unauth, no egress — outside the Lethal Trifecta. One note: a pinned
  model **retains KV/context across requests and tenants** — a soft data-residue surface
  (LLM02/ASI06) on a *shared* box. Fine single-tenant; flag if one instance serves multiple
  trust domains.
- **Scalability ceiling:** `keep_alive=-1` **forever disables idle eviction** → OOM /
  eviction-thrash / 503 (`OLLAMA_MAX_QUEUE`) when multiple models are pinned. Memory ≈
  `OLLAMA_MAX_LOADED_MODELS` (default 3) × VRAM, inflated by
  `OLLAMA_NUM_PARALLEL × CONTEXT_LENGTH`. **Prefer a finite generous TTL over `-1`** on shared
  fleet boxes; observability ceiling — no cache metric, SLO on latency + `ollama ps`.

---

## Cross-cutting: cache-busting strategy for subagents / new tools / new skills

The single rule that makes all of the above coherent (from the Claude Code caching model):
**order layers stable → volatile (tools → system → conversation) and only ever append at the
tail.** Concretely for arc:

- **New skill mid-run:** already correct — `use_skill` appends the body to the message tail,
  tools block untouched. Keep it.
- **New tool mid-run:** don't mutate the live tools block. Surface it via a deferred menu
  meta-tool (name only, body on demand into the message stream), **or** spawn a subagent.
- **Subagent:** gets its own system/tools/cache (5-min TTL); the parent prefix is untouched.
  This is the clean way to run experimental/dynamic capability without paying a parent re-read.
  Use a **fork** instead when you want to inherit and read the parent's cache.
- **Deliberate resets** (compaction, model switch, effort switch) are once-per-task boundary
  events — accept one miss, rebuild. Never per-turn.

---

## Recommended `/specify` scoping

- **SPEC A — arcllm caching (Items 1, 2, 5-config):** self-contained in arcllm. Anthropic
  auto-breakpoints behind one flag + OpenAI/Gemini telemetry read-back + Ollama env-var doc.
  Highest ROI, lowest blast radius. TDD against adapter request-body / usage-parse tests.
- **SPEC B — arcrun tool-set immutability (Item 3):** freeze `ToolRegistry` per run + memoize
  `list_schemas()` + codify deferred/subagent policy. Security + cache win; no caching concept
  in arcrun.
- **SPEC C — context-management compaction (Item 4):** arcrun contract docs + optional debug
  assertion; **arcagent** `ContextManager` one-time-boundary-reset behavioral fix + CLAUDE.md
  doc correction. Larger, arcagent-owned.

Dependency: SPEC A Item 1 assumes SPEC B's stable tool ordering to actually land Anthropic
tool-block cache hits (works regardless for system/messages; tool hits need stable order).
