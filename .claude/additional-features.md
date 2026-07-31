# Additional Features — Runtime (arcrun)

Features that belong at the runtime level, not yet planned in the current roadmap.

---

## 1. Tool Result Truncation

**What:** Cap tool output before appending to conversation history.

**Why:** Unbounded tool output bloats context, increases cost, and can cause model confusion. Every production agent harness truncates.

**Implementation:**
- Add `max_result_chars: int = 50_000` to `RunConfig`
- Truncate in `react_loop` after `tool_def.execute()` returns
- Append `\n...[truncated {original_len - max} chars]` suffix so the model knows content was cut
- Emit `tool.truncated` event with original and truncated lengths

**Complexity:** Low. ~10 lines in `react.py`.

---

## 2. Parallel Tool Execution

**What:** Execute independent tool calls concurrently within a single turn.

**Why:** Models often emit multiple tool calls in one response. Sequential execution wastes wall-clock time (especially for I/O-bound tools like file reads, API calls).

**Implementation:**
- Use `asyncio.gather(*coros, return_exceptions=True)` to run all tool calls concurrently
- Append results in **original request order** (not completion order) so the model sees a deterministic sequence
- Exceptions become error strings in the tool result, same as today's sequential path
- Add `parallel_tools: bool = True` to `RunConfig` (opt-in initially, flip default later)

**Merge Strategy (from research):**
- Read-only tools: no conflict, always safe to parallelize
- Write tools: use per-resource `asyncio.Lock` keyed by tool name + target path to serialize writes to the same resource
- Mixed: reads run concurrently, writes serialize on their resource key
- Sandbox check (`sandbox.check`) runs sequentially before dispatching the parallel batch (all-or-nothing authorization)

**Complexity:** Medium. New `_execute_parallel()` helper, ~40 lines.

---

## 3. Tool-Level Timeout

**What:** Per-tool execution timeout with configurable default.

**Why:** A hung tool (infinite loop, stalled network call) blocks the entire loop forever. `max_turns` doesn't help because the turn never completes.

**Implementation:**
- Add `tool_timeout_s: float = 120.0` to `RunConfig`
- Allow per-tool override via `Tool.timeout_s: float | None`
- Wrap `tool_def.execute()` in `asyncio.wait_for(coro, timeout)`
- On `asyncio.TimeoutError`, emit `tool.timeout` event and return `"Error: tool timed out after {n}s"` as tool result

**Complexity:** Low. ~8 lines.

---

## 4. Tool Output Sanitization

**What:** Strip or escape control characters, ANSI codes, and prompt-injection markers from tool output before feeding back to the model.

**Why:** Malicious or buggy tools can inject sequences that confuse the model or leak context. Defense-in-depth at the runtime boundary.

**Implementation:**
- Add `sanitize_output: bool = True` to `RunConfig`
- Strip ANSI escape sequences (`\x1b[...m`)
- Strip null bytes and other C0 control chars (keep `\n`, `\t`)
- Optionally strip XML-like tags that could mimic system prompts (`<system>`, `<|im_start|>`, etc.)
- Apply after truncation, before appending to messages

**Complexity:** Low. One utility function, ~15 lines.

---

## 5. Per-Tool Rate Limiting

**What:** Limit how many times a specific tool can be called per run.

**Why:** Prevents runaway loops where the model calls the same tool repeatedly (e.g., infinite read-file cycles). Also useful for expensive tools (API calls with real-world cost).

**Implementation:**
- Add `Tool.max_calls: int | None = None` field
- Add `RunConfig.default_max_tool_calls: int | None = None` for a global default
- Track per-tool call counts in `RunState`
- When limit hit, return `"Error: tool '{name}' rate limit exceeded ({n}/{max})"` and emit `tool.rate_limited` event

**Complexity:** Low. Counter dict + check, ~10 lines.

---

## 6. Consecutive Error Circuit Breaker

**What:** Stop the loop after N consecutive tool errors.

**Why:** If every tool call fails (bad sandbox, misconfigured tools, broken environment), the model will burn tokens looping forever. `max_turns` is too coarse — 10 turns of errors is 10 wasted LLM calls.

**Implementation:**
- Add `max_consecutive_errors: int = 5` to `RunConfig`
- Track consecutive error count in `RunState`, reset on any successful tool call
- When threshold hit, emit `loop.circuit_breaker` event and break out of loop
- Return `LoopResult` with content indicating the circuit breaker fired

**Complexity:** Low. Counter + check, ~8 lines.

---

## 7. Loop Detection

**What:** Detect when the model is stuck in a repetitive pattern (same tool + same args).

**Why:** Models sometimes get stuck calling the same tool with identical arguments, expecting different results. This burns context and cost without progress.

**Implementation:**
- Keep a sliding window (last N tool calls) as `list[tuple[str, dict]]`
- If the same `(tool_name, arguments_hash)` appears K times in the window, inject a system-level nudge or break
- Add `loop_detection_window: int = 6` and `loop_detection_threshold: int = 3` to `RunConfig`
- Emit `loop.repetition_detected` event

**Complexity:** Low-Medium. ~20 lines.

---

## Priority Order

| # | Feature | Effort | Impact |
|---|---------|--------|--------|
| 1 | Tool result truncation | Low | High — prevents context blowup |
| 3 | Tool-level timeout | Low | High — prevents hung loops |
| 6 | Consecutive error breaker | Low | High — prevents burn loops |
| 4 | Output sanitization | Low | Medium — security hardening |
| 5 | Per-tool rate limiting | Low | Medium — safety guardrail |
| 7 | Loop detection | Low-Med | Medium — prevents stuck loops |
| 2 | Parallel tool execution | Medium | High — performance, but can defer |
