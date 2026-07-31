# SPEC-029 — PRD: Prompt Caching & Context Control

Requirements in EARS format. IDs monotonic. MoSCoW in the priority column. Every requirement
carries an acceptance criterion tied to ≥1 principled-coder pillar.

---

## Stream A — arcllm (provider caching)

### REQ-001 — Anthropic cache breakpoints [Must] · Scalability, Simplicity
WHEN prompt caching is enabled (config flag, default on) AND the provider is Anthropic, the
adapter SHALL place at most 3 `cache_control` breakpoints — on the last tool, the system block,
and the last content block of the last message.
**AC**: A request built with tools+system+messages over the model's min-cache threshold contains
exactly the expected breakpoints in `tools → system → messages` order; a follow-up request with
one appended turn re-reads the prior prefix (mocked usage shows `cache_read_input_tokens > 0`).
Source: D-387.

### REQ-002 — Cacheable system + TTL [Must] · Security
WHEN caching is enabled, the Anthropic adapter SHALL emit `system` as a content-block list (so a
breakpoint can attach); WHEN caching is disabled it SHALL keep the plain-string form. Default TTL
SHALL be 5-minute ephemeral; 1-hour SHALL be opt-in via config.
**AC**: caching-off request body has `system` as `str`; caching-on has a 1-element block list with
`cache_control`. Config `ttl="1h"` produces `{"type":"ephemeral","ttl":"1h"}`. No secret material
is introduced into cached blocks (LLM07). Source: D-387, D-388.

### REQ-003 — Cache concept stays in the adapter [Must] · Modularity
The system SHALL NOT add any `cache_control`/cache field to shared arcllm types
(`Message`/`Tool`/blocks). Breakpoint placement SHALL be internal to `adapters/anthropic.py`.
**AC**: `git grep cache_control` outside `adapters/anthropic.py` returns nothing; OpenAI-wire
adapters are unaffected by the flag. Source: D-387, D-393.

### REQ-004 — OpenAI/Gemini cache telemetry read-back [Must] · Simplicity
WHEN a provider response includes `usage.prompt_tokens_details.cached_tokens`, the OpenAI adapter
SHALL map it to `Usage.cache_read_tokens` in BOTH the non-streaming and streaming usage parsers.
`cache_write_tokens` SHALL remain `None` (implicit caching has no billable write).
**AC**: a response with `cached_tokens: 512` yields `Usage.cache_read_tokens == 512`; absent field
yields `None`; a cache *miss* (`cached_tokens: 0`) yields `0`, not `None`. Stream and non-stream
agree. Source: D-389.

### REQ-005 — Gemini inheritance preserved [Must] · Modularity
`google.py` SHALL continue to inherit OpenAI usage parsing; no parsing fork.
**AC**: `GoogleAdapter` defines no `_parse_usage`; the Gemini compat field flows through the
inherited path. Source: D-389.

### REQ-006 — No cache key / cachedContent [Won't (this spec)] · Scalability
The system SHALL NOT implement `prompt_cache_key` or Gemini `cachedContent`.
**AC**: neither symbol appears in arcllm. Rationale recorded (fleet violates OpenAI's
~15 req/min-per-prefix ceiling; cachedContent is heavyweight). Source: D-390.

### REQ-007 — Ollama residency [Should] · Simplicity
The system SHALL document warming Ollama via server env `OLLAMA_KEEP_ALIVE` (finite TTL on shared
hosts) and SHALL NOT add `keep_alive` to the OpenAI-compat request body.
**AC**: `ollama.py` unchanged except docs; a doc note explains #11458 (compat path ignores
`keep_alive`) and the native-`/api/chat` override as a future option only. Source: D-395.

---

## Stream B — arcrun (loop / tool-set stability)

### REQ-010 — Immutable per-run tool set [Must] · Modularity, Security
WHEN a run has started, the per-run `ToolRegistry` SHALL reject `add`/`remove`; `list_schemas()`
SHALL be deterministic and MAY be memoized.
**AC**: calling `add`/`remove` after construction raises; `list_schemas()` returns byte-identical
output across turns of a run; a mutation attempt emits an anomaly audit event. Source: D-391.

### REQ-011 — No caching in the loop [Must] · Modularity
arcrun SHALL contain no `cache_control` or caching logic. Its contract to arcllm is a stable,
deterministically-ordered message + tool list.
**AC**: `git grep -i cache` in `arcrun/src` returns nothing caching-related. Source: D-393.

### REQ-012 — Append-only transform_context contract [Must] · Modularity, Scalability
The `transform_context` seam SHALL be documented as append-only (prefix-stable; compaction is a
boundary reset, not a per-turn rewrite). A debug-flag-gated assertion MAY verify returned-prefix
== input-prefix; it SHALL NOT run in the default hot path.
**AC**: docstrings on `loop.py`/`streams.py`/`react.py`/`state.py` state the contract; the
assertion is off by default and adds zero per-turn cost when off. Source: D-394.

### REQ-013 — Dynamic capability path [Should] · Scalability
New tools/skills mid-run SHALL surface via a deferred menu meta-tool (name in a stable
description, body into the message tail) OR a subagent with its own registry — never by mutating
the live tools block.
**AC**: `use_skill` remains append-to-tail; no code path appends to the live per-run tool list.
Source: D-392.

---

## Stream C — arcagent (context control / compaction)

### REQ-020 — transform_context is identity [Must] · Scalability
The arcagent `ContextManager.transform_context` SHALL return messages unchanged (append-only).
Per-turn `prune_observations`/`_emergency_truncate` calls SHALL be removed from the per-turn path.
`session.compact()` SHALL be the sole compaction path.
**AC**: across N turns below the compaction threshold, the message prefix sent each turn is
byte-stable (test); `transform_context` performs no masking/truncation. Source: D-396, D-398.

### REQ-021 — Structured summary schema [Must] · Simplicity, Security
Compaction summarization SHALL use a structured schema — `goal`, `constraints`, `progress`,
`key_facts[]`, `files_modified[]`, `decisions[]`, `rejected_approaches[]`, `open_questions[]`,
`next_step` — not freeform prose. `goal` and `constraints` SHALL be preserved verbatim (not
paraphrased). Single-shot generation (no recursive re-summarization).
**AC**: the summary prompt enumerates the fields; a compaction run yields a summary containing the
fields; `goal`/`constraints` text matches source verbatim. Source: D-399.

### REQ-022 — Masking at the boundary, persisted [Must] · Scalability
Observation masking SHALL be applied at the compaction boundary and persisted into the rebuilt
message list, keeping tool-call name/args and replacing stale tool-output bodies with a
placeholder. It SHALL NOT run as a per-turn sliding view.
**AC**: after a compaction, masked tool results in the kept window are placeholders and remain so
on subsequent turns (persisted); no per-turn re-derivation. Source: D-400.

### REQ-023 — Structured pre-compaction flush [Must] · Security
The pre-compaction flush to `context.md` SHALL use structured extraction and the existing
sanitization (NFKC, zero-width strip, control-char strip, length cap — ASI-06/LLM-05).
**AC**: flush output is sanitized and sectioned; malicious zero-width/control chars are stripped;
flush failure fails open (logs, continues). Source: D-399, D-401.

### REQ-024 — Deep/debounced compaction [Must] · Scalability
Compaction SHALL reduce context enough that the post-compaction ratio is ≤ ~50% of `max_tokens`,
so many append-only turns follow before the next boundary (no threshold thrash).
**AC**: after compaction from ≥85%, measured ratio ≤ ~50%; a second compaction does not fire on
the immediately following turn. Source: D-396.

### REQ-025 — Reported-token trigger [Must] · Scalability
Compaction SHALL trigger off provider-reported tokens (`session.token_ratio()`); the char-estimate
SHALL be a pre-call guard only.
**AC**: `maybe_compact` decision uses reported usage; with zero reported usage it does not fire on
estimate alone. Source: D-396.

### REQ-026 — Summarization crosses the boundary correctly [Must] · Modularity
The summary LLM call SHALL go through arcllm (the eval model), never a direct provider call inside
arcagent.
**AC**: `_summarize_messages`/`_pre_compact_flush` invoke the injected `model` (arcllm provider);
no `httpx`/provider SDK import appears in arcagent context code. Source: CLAUDE.md boundary.

### REQ-027 — Doc correction [Should] · Simplicity
The CLAUDE.md structure diagram SHALL name `core/session_internal/context.py` (not the
nonexistent `core/context_manager.py`).
**AC**: both root and arcagent CLAUDE.md structure blocks are corrected. Source: D-397.

---

## Cross-cutting

### REQ-030 — Audit + observability [Must] · Security
Each new operation SHALL emit an audit event: compaction boundary (counts, ratio before/after),
masking applied, and cache configuration in effect. Cache telemetry SHALL flow through the
existing Usage/OTel path.
**AC**: a compaction produces an audit event with before/after ratios; cache read/write tokens are
visible in telemetry. Source: CLAUDE.md Four Pillars (Audit).

### REQ-031 — Quality gates [Must] · all pillars
At done: `ruff check` = 0, `mypy --strict` = 0, line coverage ≥80% (core ≥90%), branch ≥75%, all
existing tests pass across arcllm/arcrun/arcagent. Any pre-existing lint/type error surfaced in
touched files SHALL be fixed (CLAUDE.md "leave it correct").
**AC**: fresh gate output attached in /verify. Source: CLAUDE.md quality gates.

---

## Out of scope (explicit)

- `prompt_cache_key`, Gemini `cachedContent` (REQ-006).
- Native Ollama `/api/chat` adapter (documented as future; REQ-007).
- Recursive/hierarchical summarization (D-398 — single-shot only).
- Retrieval/RAG memory as an in-loop compaction lever (orthogonal; cross-session only, D-401).
- Changing arcrun's loop semantics beyond registry immutability + the transform contract.
