# SPEC-029 — SDD: Prompt Caching & Context Control

Design filtered through Simplicity → Modularity → Security → Scalability. Module boundaries are
explicit; no task crosses a package boundary except through a declared contract.

---

## 0. Architecture at a glance

```
┌─ arcagent (owns: context, compaction, memory) ───────────────────────────┐
│  ContextManager.transform_context  → IDENTITY (append-only)              │
│  SessionManager.compact(eval_model, workspace)  ← sole compaction path   │
│     ├─ structured pre-flush → context.md (durable, sanitized)            │
│     ├─ observation-mask kept window (persisted)                          │
│     ├─ structured summary via eval model                                 │
│     └─ write-back: [summary_entry, *masked_kept]                         │
│  maybe_compact(): trigger off reported tokens, compact deep (~50%)       │
└──────────────┬───────────────────────────────────────────────────────────┘
               │ messages + tools (stable, ordered)      summary LLM call
               ▼                                          ▼ (eval model = arcllm)
┌─ arcrun (owns: loop, ordering, immutability) ────────────────────────────┐
│  ToolRegistry: FROZEN per run (no add/remove); list_schemas memoized     │
│  react_loop: append-only; transform_context contract = append-only       │
│  NO caching concept anywhere                                             │
└──────────────┬───────────────────────────────────────────────────────────┘
               │ model.invoke(messages, tools)
               ▼
┌─ arcllm (owns: provider wire format, cache directives) ──────────────────┐
│  AnthropicAdapter: auto-place ≤3 cache_control breakpoints (flag)        │
│  OpenaiAdapter: read prompt_tokens_details.cached_tokens (stream+sync)   │
│  GoogleAdapter: inherits (no fork).  OllamaAdapter: unchanged (env doc)  │
└───────────────────────────────────────────────────────────────────────────┘
```

**The one rule that ties it together**: layers ordered stable→volatile (tools → system →
conversation), append-only between compaction boundaries; one deliberate cache reset per boundary.

---

## 1. Stream A — arcllm

### 1.1 Anthropic breakpoints (`adapters/anthropic.py`)

Add a config-driven helper; no shared-type change.

- **Config**: extend `DefaultsConfig` (or provider config) with `enable_prompt_caching: bool = True`
  and `cache_ttl: Literal["5m","1h"] = "5m"`.
- **`_extract_system`**: when caching on, return a 1-element content-block list
  `[{"type":"text","text":system_text,"cache_control":{"type":"ephemeral"[, "ttl":"1h"]}}]`;
  when off, return the plain string (unchanged path).
- **`_build_request_body`**: when caching on, stamp `cache_control` on (a) the last element of the
  `tools` array, (b) the system block (via `_extract_system`), (c) the last content block of the
  last message. ≤3 breakpoints, under the 4-cap; the tail breakpoint reads tools+system via cascade.
- **`_parse_usage`**: already maps cache tokens — no change (verify with a test).

**Simplicity**: one flag + a small stamping helper; no new public contract. **Modularity**: the
concept never leaves this file. **Security**: 5m default (smaller exfil window + half write cost);
1h opt-in. **Scalability**: fixed 3-breakpoint scheme is O(1) at any history length.

Edge cases: below the model's min-cache threshold the breakpoint is a harmless no-op — do not log
"cached" purely because a breakpoint was emitted; anchor the message breakpoint on a stable
text/tool_result block, not a volatile image block.

### 1.2 OpenAI/Gemini telemetry (`adapters/openai.py`)

In `_parse_usage` and `_parse_openai_sse_line`:
```python
prompt_details = usage_data.get("prompt_tokens_details")
cache_read = prompt_details.get("cached_tokens") if prompt_details else None
# pass cache_read_tokens=cache_read into Usage(...)
```
`.get` guards absence; `0` (miss) is preserved distinct from `None` (unsupported). `google.py`
inherits both. No `prompt_cache_key`.

### 1.3 Ollama (`adapters/ollama.py`)

No code change. Add a module docstring note: warm via server `OLLAMA_KEEP_ALIVE` (finite on shared
hosts); `keep_alive` is ignored on `/v1/chat/completions` (#11458); a native `/api/chat` override
is a future option if per-request control is ever required.

---

## 2. Stream B — arcrun

### 2.1 Freeze the tool set (`registry.py`)

`ToolRegistry` gains a frozen flag set at end of construction (or the loop wraps the provider tools
in a frozen view). `add`/`remove` raise `RuntimeError` after freeze and emit a `tool.mutation_denied`
anomaly audit event. `list_schemas()` computes once and caches (invalidation is impossible once
frozen). This converts the byte-stability the cache relies on into a **structural invariant** and
closes the mid-run tool-injection surface (ASI04/LLM06).

Pre-run mutation (arcagent's capability registry, `agent.reload()`, `create_tool`) is unaffected —
it operates before the per-run snapshot, surfacing only on the next run.

### 2.2 transform_context contract (`react.py`, `loop.py`, `streams.py`, `state.py`)

Docstring the seam: append-only; prefix-stable; compaction is a boundary reset, not a per-turn
rewrite. Add an optional assertion gated on an existing debug/env flag that checks the returned
list's prefix equals the input prefix (warn/raise in dev only). Default path unchanged — no
per-turn O(context) diff (protects <500ms cold start, fleet scale).

arcrun learns **ordering/immutability**, never caching (REQ-011).

---

## 3. Stream C — arcagent (the reconciliation)

Current split-brain: `transform_context` (per-turn sliding prune/truncate — the cache-buster) AND
`session.compact()` (discrete, write-back). Fix = collapse to one path.

### 3.1 `ContextManager.transform_context` → identity (`session_internal/context.py`)

Replace the body with `return messages`. Delete the per-turn `prune_observations` and
`_emergency_truncate` invocations from the per-turn path. Keep the token-estimate helpers
(`estimate_tokens`, `usage_ratio`, `_estimate_ratio`) as pre-call guards only. The per-turn hook
becomes a no-op that guarantees append-only. (This is the direct fix for D-396/REQ-020.)

`prune_observations` (the masking mechanism) is **retained as a function** but is now called from
the compaction boundary (§3.3), not per turn.

### 3.2 Structured summary (`session_internal/manager.py::_summarize_messages`)

Replace the freeform prompt with a schema-anchored prompt producing:
```
goal:                <verbatim original task — never paraphrased>
constraints:         <security/user constraints — verbatim>
progress:            <quantified, e.g. "6 of 8 done">
key_facts:           [<fact + provenance>]
files_modified:      [<path: change>]
decisions:           [<decision: rationale>]
rejected_approaches: [<what failed + why>]
open_questions:      [<blockers>]
next_step:           <single concrete action>
```
Single-shot (no recursion). Output length-capped (`compaction_summary_max_chars`). Fail-open to
`[Compacted N messages]` on error (unchanged). `_pre_compact_flush` uses the same structured
extraction before writing the sanitized block to `context.md`.

**Simplicity/Security**: structure forces preservation (Factory eval); verbatim goal/constraints
honor LLM07/ASI01; `rejected_approaches` + quantified `progress` are the two research-flagged
must-keep fields.

### 3.3 Masking + deep split at the boundary (`compact`)

- Before summarizing, apply `prune_observations` to the **kept** window and persist the masked
  result into the rebuilt list (so masks are permanent, not re-derived) — REQ-022.
- Adjust the split so the post-compaction ratio is ≤ ~50% of `max_tokens` (compact by measured
  tokens, not a fixed 30% message count) — REQ-024. This is the hysteresis that prevents thrash.
- Write-back stays `self._messages = [summary_entry, *masked_kept]` (already persisted).
- The rebuilt prefix becomes the new cacheable baseline; arcllm's tail breakpoint re-warms it.

### 3.4 Trigger (`agent_dispatch.py::maybe_compact`)

Already keys off `session.token_ratio()` (reported tokens) — REQ-025 satisfied; confirm it does
not fall back to the estimate. Fires at `compact_threshold`; §3.3 ensures it compacts deep so it
won't re-fire next turn.

### 3.5 Boundary correctness

`_summarize_messages`/`_pre_compact_flush` already take an injected `model` (the arcllm eval model
via `agent._ensure_model()`), so the LLM call is on the correct side of the boundary — REQ-026. No
provider SDK import in arcagent context code.

### 3.6 `compaction_summary` → message rendering

The `summary_entry` dict (`type: "compaction_summary"`) must render into a normal user/system
message when the next request is assembled, so the structured summary sits in the prefix as stable
text. Verify the assembly path converts it deterministically (stable field order → stable bytes for
caching).

---

## 4. Security posture (per pillar 3)

- No secrets enter cached blocks (LLM07); 5m default TTL bounds exposure.
- Frozen registry removes the mid-run tool-injection vector (ASI04/LLM06).
- Summary/flush outputs are sanitized before touching `context.md` (ASI06/LLM05) — existing
  `_sanitize_context_output` reused, extended to the structured path.
- Every compaction boundary + registry-mutation-denied + cache config emits an audit event (AU).
- Verbatim `constraints` preservation prevents compaction from laundering away a stated security
  instruction (the injection-laundering vector flagged in `steering/security.md`).

## 5. Scalability posture (per pillar 4)

- Append-only between boundaries → cache read at ~0.1× base across thousands of agents instead of
  full re-prefill per turn (Manus: cache hit rate is THE metric at 100:1 in:out).
- One cache miss per boundary, deep compaction → boundaries are rare.
- O(1) breakpoint scheme; frozen registry memoized. Ceiling: 5m TTL under slow turn cadence
  (1h opt-in for long-lived agents).

## 6. Module-boundary contract summary

| Contract | Producer | Consumer | Invariant |
|----------|----------|----------|-----------|
| Stable ordered messages+tools | arcrun | arcllm | append-only prefix; frozen tool order |
| Cache directive placement | arcllm | provider | internal to anthropic.py only |
| Compaction (identity per turn) | arcagent | arcrun | `transform_context` append-only |
| Summary LLM call | arcagent | arcllm (eval model) | no direct provider call in arcagent |
| Durable memory | arcagent | context.md | sanitized, append, restorable |
