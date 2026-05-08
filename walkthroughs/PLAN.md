# Arc Walkthroughs — Master Plan

> **Purpose:** Self-contained reference for rewriting and creating Arc walkthrough notebooks, executable in chunks of 3 by parallel agents, survivable across context clears.
>
> **Owner:** This document is the single source of truth. Update the status table at the bottom as work progresses. If anything contradicts this doc, fix the doc.

---

## 0. Status snapshot

- **As of:** 2026-05-07
- **Total notebooks:** 16 to rewrite + 24 to create = **40 notebooks**
- **Strategy:** 3 parallel agents per chunk. ~14 chunks total.
- **Phase:** Pre-flight (§9) complete. C01 (arctrust 01–03) verified `done`. Ready to dispatch C02.

---

## 1. Goals & quality bar

### Audience
Engineers who've written ≥1 Python project, are familiar with async/await, and want to learn Arc by *running real code*. Not a video script. Not a marketing page. A teach-by-doing notebook.

### What "same depth as existing" means

Profile of existing notebooks (measured 2026-05-07):

| Metric | Min | Median | Max |
|---|---|---|---|
| Total cells | 22 | 45 | 70 |
| Markdown cells | 7 | 18 | 25 |
| Code cells | 15 | 27 | 48 |
| File size | 15 KB | 28 KB | 67 KB |
| Numbered top-level sections | 5 | 7 | 12 |

**Target:** ≥30 cells, ≥7 numbered sections, ≥20 KB. Notebooks that fall below should be flagged for rework, not shipped.

### Voice
- Direct, confident. Show, don't preface.
- Markdown explains the *why*; code shows the *what*.
- No hand-wavy "in production you'd…" — show the production thing.
- Comments in code only when the *why* is non-obvious.

### Runnability
- **Mock-first:** Every notebook must run end-to-end *without an API key* using mock providers/responses. This is a hard requirement.
- **Real-API sections** are clearly labeled `## (live) ...` and gated by an `os.environ.get("ANTHROPIC_API_KEY")` check that prints a skip message instead of crashing.
- Every code cell either runs successfully OR is intentionally a `# this raises` example with the expected error shown in markdown immediately above.
- All cells should have `execution_count: null` and empty `outputs: []` at commit time. The CI/reader will execute fresh.

### Coverage
Cover the full public API of the topic at hand. Reference the package's `__init__.py` exports. If an export exists and is in scope for the notebook topic, it must appear at least once.

---

## 2. Conventions

### Directory layout (current)
```
walkthroughs/
├── PLAN.md                  ← this file
├── arcllm/
│   └── NN-topic.ipynb       ← 13 existing + 4 new
├── arcrun/
│   └── NN-topic.ipynb       ← 3 existing + 4 new
├── arctrust/                ← new, 4 notebooks
├── arcagent/                ← new, 4 notebooks
├── arcteam/                 ← new, 4 notebooks
├── arcui/                   ← new, 2 notebooks
└── arcgateway/              ← new, 2 notebooks
```

### Filename pattern
`NN-topic-with-hyphens.ipynb` — two-digit zero-padded prefix, lowercase, hyphenated. No `step_`, no underscores in the topic.

### Common setup cell (the boilerplate)

**Every notebook's first code cell is exactly this** (copy verbatim):

```python
# Setup: make Arc packages importable from this notebook
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

_here = Path.cwd()
for _p in [_here, *_here.parents]:
    if (_p / "packages").is_dir() and (_p / "pyproject.toml").is_file():
        REPO_ROOT = _p
        break
else:
    raise RuntimeError("Could not locate Arc repo root")

# Add every package's src/ (and tests/, where present) to sys.path
for _pkg in (REPO_ROOT / "packages").iterdir():
    for _sub in ("src", "tests"):
        _path = _pkg / _sub
        if _path.is_dir() and str(_path) not in sys.path:
            sys.path.insert(0, str(_path))

load_dotenv(REPO_ROOT / ".env")
```

Notebooks that need a real API key add a second cell:

```python
# (live) optional — set this to run real-API sections; mock cells run regardless
HAS_LIVE_KEY = bool(os.environ.get("ANTHROPIC_API_KEY"))
print(f"Live API key: {'present' if HAS_LIVE_KEY else 'missing — live cells will skip'}")
```

### Markdown structure

Every notebook follows this skeleton:

```
# <Number>. <Topic Title>
   <one-paragraph hook: what this notebook teaches and why it matters>
   <bulleted "you will learn" list (3–6 items)>

## 1. Setup
   <the boilerplate cell>

## 2. <First concept>
   <markdown explanation>
   <code cell demonstrating it>
   <markdown reflecting on what just happened>
   <code cell exploring an edge case>

## 3. <Second concept>
   ...

...

## N. Summary
   <bulleted recap>
   <"next: see notebook NN-foo" pointer>
   <list of API surface covered>
```

### Cell ordering rules
1. Markdown intro → setup code → markdown header → code → markdown reflection → code → repeat
2. Never two consecutive code cells without explanatory markdown between them (unless they're a deliberate "sequence" pattern, in which case one markdown cell explains the sequence above)
3. Last cell is always a markdown "Summary" section

### Output & metadata policy
- `execution_count: null` on every code cell at commit
- `outputs: []` on every code cell at commit
- Notebook-level metadata `kernelspec.display_name = "Python 3"` and `kernelspec.name = "python3"`

---

## 3. Process for each notebook

This is the deterministic recipe an agent runs to produce one notebook. Do not skip steps.

### Step 1 — Surface survey (10 min)
Use the `code-review-graph` MCP tools first; fall back to Read/Grep only if needed.

```
mcp__code-review-graph__semantic_search_nodes_tool(query="<topic>")
mcp__code-review-graph__query_graph_tool(pattern="callers_of", node="<class>")
mcp__code-review-graph__get_review_context_tool(...)
```

Read:
- `packages/<pkg>/src/<pkg>/__init__.py` — public exports for the topic
- `packages/<pkg>/CHANGELOG.md` — what changed since notebook was written
- The 1–3 source files most relevant to the topic
- The existing notebook (if rewriting) — to preserve any structural choices that worked

### Step 2 — Outline
Produce an outline in your scratchpad following the markdown skeleton in §2. Do NOT skip — outlining first prevents flabby notebooks.

Required outline elements:
- Section titles (≥7)
- One sentence per section on what it demonstrates
- Identification of every public API symbol that will be exercised
- Mock vs live boundary marked

### Step 3 — Write
Build the notebook as a JSON file. Use this minimal Python helper (paste into a one-off cell or script):

```python
import json
import nbformat as nbf

nb = nbf.v4.new_notebook()
nb["cells"] = [
    nbf.v4.new_markdown_cell("# 04. Title\n..."),
    nbf.v4.new_code_cell("# Setup: ..."),
    nbf.v4.new_markdown_cell("## 1. Setup"),
    # ...
]
nb["metadata"] = {
    "kernelspec": {"display_name": "Python 3", "language": "python", "name": "python3"},
    "language_info": {"name": "python"},
}
with open("walkthroughs/<pkg>/NN-topic.ipynb", "w") as f:
    nbf.write(nb, f)
```

Or write the JSON directly with the `Write` tool (well-indented, valid JSON). Either is fine — pick whichever is faster for you.

**Do NOT execute the notebook yet.** Cells stay `execution_count: null` and `outputs: []`.

### Step 4 — Lint
```bash
uv run ruff check walkthroughs/<pkg>/NN-topic.ipynb
```
Must report `All checks passed!`. If not, fix the cell sources, not the lint config.

### Step 5 — Smoke run (mock cells only)
```bash
uv run jupyter nbconvert --to notebook --execute --inplace \
  walkthroughs/<pkg>/NN-topic.ipynb \
  --ExecutePreprocessor.timeout=120
```
For notebooks with `(live)` sections that require an API key, gate them with `if not HAS_LIVE_KEY: print("skip"); raise SystemExit` style early-return inside the cell — or set them as `## (live)` markdown sections immediately followed by a single code cell whose first line is `if not HAS_LIVE_KEY: raise SystemExit("requires ANTHROPIC_API_KEY")`. The smoke run should still pass (because raising `SystemExit(0)` is fine in nbconvert).

After smoke run completes, **clear outputs** before commit:
```bash
uv run jupyter nbconvert --clear-output --inplace walkthroughs/<pkg>/NN-topic.ipynb
```

### Step 6 — Commit
One commit per notebook. Format:
```
docs(walkthroughs/<pkg>): rewrite NN-<topic> for <pkg> v<X.Y>

- <bullet what changed materially>
- <bullet new APIs covered>
- Smoke-run passes; ruff clean
```

For new notebooks: `docs(walkthroughs/<pkg>): add NN-<topic> walkthrough`.

### Step 7 — Update PLAN.md status table
Tick the row for this notebook in §7. This is what makes the plan resumable across context clears.

---

## 4. Parallel execution — 3 agents at a time

### Why this is safe (no collisions)
- Each notebook is one file. One agent owns it end-to-end.
- No shared modules, no shared helpers. Each notebook is self-contained.
- The only shared file (`PLAN.md` status table) is touched by the **orchestrator only**, after all 3 agents return.
- Ruff config is frozen — agents must not modify `pyproject.toml`.

### Orchestrator responsibilities (the calling Claude session)
1. Read §7 status table. Pick the next 3 `pending` rows in priority order.
2. Mark them `in-progress` in the status table (one edit, one commit).
3. Spawn 3 `Agent({subagent_type: "general-purpose"})` calls **in a single message** — that's what makes them parallel.
4. Wait for all 3 to return.
5. Verify each by:
   - Running `uv run ruff check <file>` → must pass
   - Reading the notebook with the depth profile script (≥30 cells, ≥7 sections, ≥20 KB)
   - Spot-reading 3–5 cells to confirm quality
6. If a notebook fails verification, dispatch a fresh agent to redo it. Do not silently accept low-quality output.
7. Mark verified rows `done` in the status table. Commit the table update.
8. Repeat until backlog empty.

### Agent prompt template (use exactly this scaffold per notebook)

```
You are writing ONE walkthrough notebook for the Arc project.

NOTEBOOK PATH: walkthroughs/<pkg>/NN-topic.ipynb
TASK TYPE: <REWRITE existing | CREATE new>
TARGET PACKAGE: packages/<pkg>/

READ THESE BEFORE WRITING:
1. /Users/joshschultz/Projects/arc/walkthroughs/PLAN.md sections 1, 2, 3
   — quality bar, conventions, process
2. /Users/joshschultz/Projects/arc/walkthroughs/PLAN.md §5.X.Y (your specific entry)
   — exact scope for THIS notebook
3. packages/<pkg>/src/<pkg>/__init__.py — public exports
4. packages/<pkg>/CHANGELOG.md — recent changes
5. <2–4 specific source files listed in your §5 entry>
<6. Existing notebook if REWRITE>

QUALITY BAR (non-negotiable):
- ≥30 cells, ≥7 numbered sections, ≥20 KB
- First code cell is the EXACT setup boilerplate from PLAN.md §2
- Mock-first: must run end-to-end without API keys
- Cover every public API symbol listed in your §5 entry
- execution_count: null and outputs: [] on every code cell at commit

PROCESS:
1. Outline first (markdown skeleton, ≥7 sections, ≥1 sentence each)
2. Write the notebook (Write tool with valid JSON, OR Python script + nbformat)
3. Lint: `uv run ruff check <file>` — must pass
4. Smoke run: `uv run jupyter nbconvert --to notebook --execute --inplace <file>`
5. Clear outputs: `uv run jupyter nbconvert --clear-output --inplace <file>`
6. Verify: `python3 -c "import json; d=json.load(open('<file>')); print(len(d['cells']))"` ≥ 30

REPORT BACK:
- Final cell count, md/code split, KB size
- Public API symbols covered (matched against your §5 entry)
- Any deviations from the §5 entry and why
- ruff status, smoke-run status

Do NOT modify any file other than the notebook itself.
Do NOT touch pyproject.toml, PLAN.md, or any other notebook.
```

### Chunk schedule
With 3 agents per chunk and 40 notebooks: **14 chunks** (last chunk has 1 notebook + buffer for redos).

Chunks are pre-defined in §6. The orchestrator follows the chunk order strictly — this preserves dependency order (e.g., arctrust 01 lands before any notebook that imports `arctrust.identity`).

---

## 5. Backlog

> Each entry is self-contained — an agent can pick it up cold and execute. Sections include: scope, source files, public API to cover, mock strategy, expected sections.

### 5.1 Existing notebooks to rewrite

#### REWRITE 5.1.1 — `walkthroughs/arcllm/01-core-types.ipynb`
- **Risk:** LOW. Refresh-only.
- **Source files:** `packages/arcllm/src/arcllm/types.py`, `packages/arcllm/src/arcllm/__init__.py`
- **Public API:** `Message`, `Tool`, `ToolCall`, `ContentBlock`, `TextBlock`, `ImageBlock`, `ToolUseBlock`, `ToolResultBlock`, `LLMResponse`, `Usage`, `StopReason`, `LLMProvider`
- **What's stale:** Nothing material. Refresh model names in examples to current defaults (`claude-sonnet-4-6`).
- **Mock strategy:** All examples are pure data construction; no API calls.
- **Sections:** Setup → Messages → Content blocks → Tool/ToolCall → LLMResponse → Usage & StopReason → Provider enum → Summary

#### REWRITE 5.1.2 — `walkthroughs/arcllm/02-config-loading.ipynb`
- **Risk:** MEDIUM.
- **Source files:** `packages/arcllm/src/arcllm/config/__init__.py`, `packages/arcllm/src/arcllm/config/loader.py`, `packages/arcllm/src/arcllm/config/global_config.py`
- **Public API:** `load_global_config`, `load_provider_config`, `GlobalConfig`, `ProviderConfig`, `DefaultsConfig`, `ModuleConfig`, `ProviderSettings`, `ModelMetadata`, `clear_cache`
- **What's new:** Layered config — packaged defaults at `arcllm/config/providers/*.toml` overlay-merged with user config at `${ARC_CONFIG_DIR:-~/.arc}/arcllm.toml`. Deep-merge for dicts, replace for scalars.
- **Mock strategy:** Use `tmp_path` (`tempfile.TemporaryDirectory`) and set `ARC_CONFIG_DIR` env to it.
- **Sections:** Setup → Provider TOML structure → Default load → User overlay precedence → Deep-merge semantics → Cache & invalidation → Common errors → Summary

#### REWRITE 5.1.3 — `walkthroughs/arcllm/03-anthropic-adapter.ipynb`
- **Risk:** MEDIUM.
- **Source files:** `packages/arcllm/src/arcllm/adapters/anthropic.py`
- **Public API:** `AnthropicAdapter`, the Anthropic provider config (TOML)
- **What's new:** Default model now `claude-sonnet-4-6`. New `supports_thinking` metadata field. Catalog refreshed with current models/pricing.
- **Mock strategy:** `unittest.mock.patch` on `httpx.AsyncClient.post` returning canned JSON.
- **Sections:** Setup → Adapter contract → Building requests → Parsing responses → Tool calls → Streaming events → Thinking models → Errors → (live) one real call → Summary

#### REWRITE 5.1.4 — `walkthroughs/arcllm/04-agentic-loop.ipynb`
- **Risk:** HIGH.
- **Source files:** `packages/arcllm/src/arcllm/registry.py` (`load_model`), `packages/arcllm/src/arcllm/loop.py` (if exists), example loops in tests
- **Public API:** `load_model(provider, model, on_event, trace_store, agent_label, routing, circuit_breaker, queue, retry, fallback, rate_limit, audit, security, telemetry, otel)`
- **What's new:** `load_model()` gained 7 new optional params: `on_event`, `trace_store`, `agent_label`, `routing`, `circuit_breaker`, `queue` (in addition to `retry`, `fallback`, etc.). Module stack reorder: Otel → Queue → Telemetry → CircuitBreaker → Audit → Security → Retry → Fallback → RateLimit.
- **Mock strategy:** Use a test adapter or mock `httpx`. `on_event` callbacks logged to a list.
- **Sections:** Setup → Building a loop manually → `load_model` basics → `on_event` callback wiring → Adding `trace_store` → Adding `routing` → Adding `circuit_breaker` + `queue` → Module stack order explained → Multi-tool loop → (live) end-to-end → Summary

#### REWRITE 5.1.5 — `walkthroughs/arcllm/05-openai-adapter.ipynb`
- **Risk:** MEDIUM.
- **Source files:** `packages/arcllm/src/arcllm/adapters/openai.py`
- **Public API:** `OpenaiAdapter`
- **What's new:** Auto system→developer role mapping for o-series (keyed on `supports_thinking`). Catalog includes o1, o3, o4-mini.
- **Mock strategy:** Same as Anthropic — patch `httpx`.
- **Sections:** Setup → Adapter contract → Standard models → o-series role mapping → Tool calls → Streaming → Errors → (live) → Summary

#### REWRITE 5.1.6 — `walkthroughs/arcllm/06-provider-registry.ipynb`
- **Risk:** HIGH.
- **Source files:** `packages/arcllm/src/arcllm/registry.py`, `packages/arcllm/src/arcllm/modules/routing.py`
- **Public API:** `load_model`, `clear_cache`, `RoutingModule` (briefly, deep-dive in 5.1.13's sibling new notebook)
- **What's new:** Provider name regex validation (ASI-04). `RoutingModule` integration via `routing=` kwarg with classification dispatch.
- **Mock strategy:** Mock adapters via the test adapter pattern.
- **Sections:** Setup → Registry lookup flow → Provider validation → Caching behavior → `clear_cache` semantics → Routing kwarg basics → Multi-provider routing → Summary

#### REWRITE 5.1.7 — `walkthroughs/arcllm/07-module-system.ipynb`
- **Risk:** HIGH.
- **Source files:** `packages/arcllm/src/arcllm/modules/base.py` (and all module files)
- **Public API:** `BaseModule`, all 11 modules (`RetryModule`, `FallbackModule`, `RateLimitModule`, `TelemetryModule`, `AuditModule`, `SecurityModule`, `OtelModule`, `QueueModule`, `CircuitBreakerModule`, `RoutingModule`)
- **What's new:** `QueueModule` and `CircuitBreakerModule` added. Stack reordered: Otel → Queue → Telemetry → CircuitBreaker → Audit → Security → Retry → Fallback → RateLimit. The order matters — explain why.
- **Mock strategy:** Custom test module that records hook calls; mock adapter that fails predictably.
- **Sections:** Setup → BaseModule contract → Module hooks (`before_call`, `after_call`, `on_error`) → Stack composition → Default order rationale → Adding a custom module → Removing a module → Stack tracing helpers → Summary

#### REWRITE 5.1.8 — `walkthroughs/arcllm/08-rate-limiter.ipynb`
- **Risk:** LOW.
- **Source files:** `packages/arcllm/src/arcllm/modules/rate_limit.py`
- **Public API:** `RateLimitModule` and its config
- **What's new:** None material. Refresh examples.
- **Mock strategy:** Patch `time.monotonic` or use `freezegun` to control rate-limit timing deterministically.
- **Sections:** Setup → Token bucket basics → RateLimitModule config → Burst behavior → Cooldown behavior → Multi-provider isolation → Position in module stack → Summary

#### REWRITE 5.1.9 — `walkthroughs/arcllm/09-telemetry-module.ipynb`
- **Risk:** MEDIUM.
- **Source files:** `packages/arcllm/src/arcllm/modules/telemetry.py`
- **Public API:** `TelemetryModule`, `on_event` callback, phase sub-timings
- **What's new:** `on_event` callback now threadable through `load_model()`. TraceRecord integration. `agent_label` for per-agent attribution.
- **Mock strategy:** Capture events into a list; assert sequence.
- **Sections:** Setup → Why telemetry → Event types emitted → Phase timings → `on_event` callbacks → `agent_label` attribution → Cost tracking → Latency budgets → Summary

#### REWRITE 5.1.10 — `walkthroughs/arcllm/10-audit-trail.ipynb`
- **Risk:** HIGH.
- **Source files:** `packages/arcllm/src/arcllm/modules/audit.py`, `packages/arctrust/src/arctrust/audit.py` (the new sink)
- **Public API:** `AuditModule`, `arctrust.audit.emit`, `AuditSink`, `JsonlSink`, `SignedChainSink`
- **What's new:** Audit emission migrated to `arctrust.audit.emit` — pluggable sinks. `AuditModule` is now mostly a thin emitter. **TraceStore deep-dive moved to NEW notebook 14.**
- **Mock strategy:** In-memory `AuditSink` that appends events to a list.
- **Sections:** Setup → Audit philosophy → AuditModule basics → arctrust sinks overview → JsonlSink usage → SignedChainSink usage → Custom sink → Pointer to TraceStore (notebook 14) → Summary

#### REWRITE 5.1.11 — `walkthroughs/arcllm/11-otel-export.ipynb`
- **Risk:** MEDIUM.
- **Source files:** `packages/arcllm/src/arcllm/modules/otel.py`
- **Public API:** `OtelModule`, span attributes
- **What's new:** New span attributes from QueueModule (wait time, queue depth). SDK reset in `clear_cache`.
- **Mock strategy:** Use `opentelemetry.sdk.trace.export.InMemorySpanExporter`.
- **Sections:** Setup → OTel basics in arcllm → Span hierarchy → Standard attributes → Queue/wait attributes → Exporters (in-memory, OTLP) → Resource attributes → Summary

#### REWRITE 5.1.12 — `walkthroughs/arcllm/12-security-layer.ipynb`
- **Risk:** MEDIUM.
- **Source files:** `packages/arcllm/src/arcllm/modules/security.py`, `packages/arcllm/src/arcllm/registry.py` (validation hooks)
- **Public API:** `SecurityModule`
- **What's new:** Provider name regex validation at registry layer (ASI-04 hardening). Trace file perms 0o600. PII/CUI redaction patterns refreshed.
- **Mock strategy:** Inputs with embedded secrets; assert redaction.
- **Sections:** Setup → Threat model → Provider name validation → Input redaction → Output redaction → File permission hardening → Combining with audit → Summary

#### REWRITE 5.1.13 — `walkthroughs/arcllm/13-open-providers.ipynb`
- **Risk:** HIGH.
- **Source files:** All 16 adapter files in `packages/arcllm/src/arcllm/adapters/`
- **Public API:** All adapters: Anthropic, OpenAI, Azure_OpenAI, Google, Cohere, xAI, DeepSeek, Groq, Mistral, Moonshot, Fireworks, Together, Ollama, vLLM, HuggingFace, HuggingFace_TGI
- **What's new:** Four new adapters: Azure_OpenAI (gov + commercial), Google Gemini, Cohere, xAI. `supports_thinking` metadata. Layered config affects all.
- **Mock strategy:** One mock per adapter; tabular comparison.
- **Sections:** Setup → Adapter taxonomy → Cloud commercial (Anthropic, OpenAI, Google, Cohere, xAI) → Cloud government (Azure_OpenAI gov) → Fast inference (Groq, Cerebras, Fireworks, Together) → Open source (DeepSeek, Mistral, Moonshot) → Self-hosted (Ollama, vLLM, HuggingFace, TGI) → Capability matrix → Summary

#### REWRITE 5.1.14 — `walkthroughs/arcrun/01-core-react.ipynb`
- **Risk:** MED-HIGH.
- **Source files:** `packages/arcrun/src/arcrun/loop.py`, `packages/arcrun/src/arcrun/strategies/react.py`, `packages/arcrun/src/arcrun/builtins/task_complete.py`, `packages/arcrun/src/arcrun/events.py`
- **Public API:** `run`, `run_async`, `RunHandle`, `Strategy`, `LoopResult`, `Tool`, `ToolContext`, `make_task_complete_tool`, `TaskCompleteArgs`, `Event`, `EventBus`, `verify_chain`
- **What's new:** `task_complete` builtin for structured completion. Budget caps `max_cost_usd`, `max_turns`. `loop.completed` event. Strategy prompt injection.
- **Mock strategy:** `MockModel` from arcrun tests; assert event sequence.
- **Sections:** Setup → ReAct basics → First loop run → Tool integration → `task_complete` for early exit → `max_turns` enforcement → `max_cost_usd` enforcement → Event chain → `verify_chain` integrity → (live) → Summary

#### REWRITE 5.1.15 — `walkthroughs/arcrun/02-tool-executor.ipynb`
- **Risk:** HIGH.
- **Source files:** `packages/arcrun/src/arcrun/executor.py`, `packages/arcrun/src/arcrun/parallel_dispatch.py`, `packages/arcrun/src/arcrun/registry.py`, `packages/arcrun/src/arcrun/sandbox.py`
- **Public API:** `ToolRegistry`, `Tool`, `ToolContext`, `SandboxConfig`, `SandboxError`, sandbox exceptions, classification helpers
- **What's new:** Parallel dispatch for read-only batches. Classification registry. `make_contained_execute_tool` covered separately in 5.1.16.
- **Mock strategy:** Synthetic tools (no I/O).
- **Sections:** Setup → ToolRegistry → Tool definition → SandboxConfig allowlist → SandboxConfig custom checker → Classification (read_only vs state_modifying) → Parallel dispatch → Submission order vs completion order → Errors and retries → Summary

#### REWRITE 5.1.16 — `walkthroughs/arcrun/03-codeexec.ipynb`
- **Risk:** MEDIUM.
- **Source files:** `packages/arcrun/src/arcrun/strategies/code.py`, `packages/arcrun/src/arcrun/builtins/execute.py`, `packages/arcrun/src/arcrun/builtins/contained_execute.py`, `packages/arcrun/src/arcrun/prompts.py`
- **Public API:** Code strategy, `make_execute_tool`, `make_contained_execute_tool`, `get_strategy_prompts`, `prompt_guidance` property
- **What's new:** Strategy selection mechanism. `prompt_guidance` property on strategies. `get_strategy_prompts()` API. Container execution (Docker isolation, mem_limit, cpu_quota).
- **Mock strategy:** Use `MockModel` that returns code blocks; verify execution.
- **Sections:** Setup → CodeExec vs ReAct → When to use which → `prompt_guidance` and `get_strategy_prompts()` → Local sandboxed execute → Container execute (mem/cpu/network limits) → Mid-run task_complete → Strategy selection routing → Summary

### 5.2 New notebooks to create

#### CREATE 5.2.1 — `walkthroughs/arcllm/14-trace-store.ipynb`
- **Source files:** `packages/arcllm/src/arcllm/trace/store.py` (or wherever `TraceStore` lives)
- **Public API:** `TraceStore`, `JSONLTraceStore`, `TraceRecord`
- **Topic:** Hash-chained append-only trace records. RFC 8785 JCS canonical JSON. Daily file rotation. Tamper detection on warm start.
- **Mock strategy:** `tmp_path`. Construct records, pass to JSONLTraceStore, read back, mutate file, verify tamper detection.
- **Sections:** Setup → Why hash chains → TraceRecord schema → JSONLTraceStore basics → Daily rotation → Reading & paginating → Tamper detection demo → Integration with load_model → Summary

#### CREATE 5.2.2 — `walkthroughs/arcllm/15-queue-circuit-breaker.ipynb`
- **Source files:** `packages/arcllm/src/arcllm/modules/queue.py`, `packages/arcllm/src/arcllm/modules/circuit_breaker.py`
- **Public API:** `QueueModule`, `QueueFullError`, `QueueTimeoutError`, `CircuitBreakerModule`
- **Topic:** Bounded concurrency, backpressure. State machine CLOSED/OPEN/HALF_OPEN. Per-provider isolation.
- **Mock strategy:** Mock adapter with controllable latency and failure mode. Drive concurrency via `asyncio.gather`.
- **Sections:** Setup → QueueModule motivation → max_concurrent / max_queued → QueueFullError → QueueTimeoutError → Circuit breaker states → failure_threshold and cooldown → Composing both → Summary

#### CREATE 5.2.3 — `walkthroughs/arcllm/16-config-controller.ipynb`
- **Source files:** `packages/arcllm/src/arcllm/config/controller.py`
- **Public API:** `ConfigController`, `ConfigSnapshot`
- **Topic:** Runtime config mutation with frozen snapshots. `on_change` callbacks. TraceRecord emission for every mutation.
- **Mock strategy:** Capture callbacks; assert snapshot frozen-ness.
- **Sections:** Setup → ConfigController basics → Snapshots are immutable → on_change callbacks → Mutation audit trail → Concurrency safety → Summary

#### CREATE 5.2.4 — `walkthroughs/arcllm/17-routing-module.ipynb`
- **Source files:** `packages/arcllm/src/arcllm/modules/routing.py`
- **Public API:** `RoutingModule`, classification kwarg
- **Topic:** Classification-based provider dispatch. NFKC validation. Enforcement modes.
- **Mock strategy:** Multiple mock adapters, dispatch by tag.
- **Sections:** Setup → Why routing → Classification model → NFKC validation → Enforcement modes (strict/warn) → Multi-provider routing → Failure modes → Summary

#### CREATE 5.2.5 — `walkthroughs/arcrun/04-streaming.ipynb`
- **Source files:** `packages/arcrun/src/arcrun/streams.py`
- **Public API:** `run_stream`, `StreamEvent`, `TokenEvent`, `ToolStartEvent`, `ToolEndEvent`, `TurnEndEvent`
- **Topic:** Real-time streaming runtime. Token chunking semantics. Tool lifecycle events.
- **Mock strategy:** MockModel that yields chunks with realistic timing.
- **Sections:** Setup → Why streaming → run_stream basics → TokenEvent → ToolStartEvent / ToolEndEvent → TurnEndEvent → Backpressure handling → Live REPL pattern → Summary

#### CREATE 5.2.6 — `walkthroughs/arcrun/05-parallel-dispatch.ipynb`
- **Source files:** `packages/arcrun/src/arcrun/parallel_dispatch.py`
- **Public API:** Batch classifier, dispatch_batch helpers
- **Topic:** Concurrent tool dispatch for read-only batches. Classification registry. Semaphore tuning.
- **Mock strategy:** Tools with controlled sleep; measure wall time vs sequential.
- **Sections:** Setup → Why parallel matters → Classification: read_only vs state_modifying → Submission order preservation (`_seq` metadata) → Semaphore tuning → FIPS mode (cap=4) → Race-free audit ordering → Benchmark → Summary

#### CREATE 5.2.7 — `walkthroughs/arcrun/06-task-completion-budgets.ipynb`
- **Source files:** `packages/arcrun/src/arcrun/builtins/task_complete.py`, budget enforcement in `loop.py`
- **Public API:** `make_task_complete_tool`, `TaskCompleteArgs`, `max_cost_usd`, `max_turns`
- **Topic:** Structured task completion. Budget enforcement. Completion payload.
- **Mock strategy:** MockModel that calls task_complete with various status codes.
- **Sections:** Setup → Why structured completion → TaskCompleteArgs schema → success/partial/failed semantics → max_turns enforcement → max_cost_usd enforcement → Budget breach payload → Composing with strategies → Summary

#### CREATE 5.2.8 — `walkthroughs/arcrun/07-event-chain-verification.ipynb`
- **Source files:** `packages/arcrun/src/arcrun/events.py`
- **Public API:** `Event`, `EventBus`, `verify_chain`, `GENESIS_PREV_HASH`, `ChainVerificationResult`
- **Topic:** Hash-chained event audit. Tamper detection. Sequence reconstruction.
- **Mock strategy:** Run a loop, capture events, mutate one, assert chain breaks.
- **Sections:** Setup → Hash chain primer → EventBus basics → Event schema → Computing hashes → verify_chain API → Tamper demo → Compliance use cases → Summary

#### CREATE 5.2.9–12 — `walkthroughs/arctrust/01-04`
- **5.2.9 `01-identity-did.ipynb`** — `AgentIdentity`, `ChildIdentity`, `generate_did`, `parse_did`, `validate_did`. DID format. Parent-child hierarchies. Identity persistence.
- **5.2.10 `02-keypairs-signing.ipynb`** — `KeyPair`, signing operations, public/private key handling, `load_operator_pubkey`, `load_issuer_pubkey`. Ed25519 fundamentals.
- **5.2.11 `03-policy-pipeline.ipynb`** — `PolicyContext`, `PolicyPipeline`, `TierConfig`, `ToolCall`, `build_pipeline`. First-DENY-wins. Five policy layers.
- **5.2.12 `04-audit-sinks.ipynb`** — `AuditEvent`, `AuditSink`, `JsonlSink`, `SignedChainSink`, `emit`. Pluggable sink architecture. Tamper-evident chain.
- **Source files:** `packages/arctrust/src/arctrust/{identity,keypair,policy,audit}.py` (and submodules)
- **Mock strategy:** All cryptography uses real keys (cheap). Files use `tmp_path`.

#### CREATE 5.2.13–16 — `walkthroughs/arcagent/01-04`
- **5.2.13 `01-first-agent.ipynb`** — `ArcAgent` construction, identity binding, basic tool registration, simple run.
- **5.2.14 `02-tool-integration.ipynb`** — Tool registry, transports (in-process, subprocess, MCP, HTTP), `ToolError`, `ToolVetoedError`.
- **5.2.15 `03-policy-and-modules.ipynb`** — Policy enforcement, module bus extensions, `ModuleBusError`.
- **5.2.16 `04-module-bus-events.ipynb`** — Event bus, custom modules, hook lifecycle.
- **Source files:** `packages/arcagent/src/arcagent/core/*.py` (`agent.py`, `tool_registry.py`, `module_bus.py`)
- **Mock strategy:** Test identity, mock tools, in-memory event capture.

#### CREATE 5.2.17–20 — `walkthroughs/arcteam/01-04`
- **5.2.17 `01-team-formation.ipynb`** — `TeamConfig`, `EntityRegistry`, `Entity`, member management.
- **5.2.18 `02-task-distribution.ipynb`** — Task assignment patterns, `Channel`, `MsgType`, `Priority`.
- **5.2.19 `03-messaging-channels.ipynb`** — `MessagingService`, `Message`, inter-agent communication, consensus patterns.
- **5.2.20 `04-team-persistence.ipynb`** — `TeamMemoryService`, `TeamMemoryConfig`, `TeamFileStore`, backends (`FileBackend`, `MemoryBackend`, `StorageBackend`), `AuditLogger`, recovery.
- **Source files:** `packages/arcteam/src/arcteam/*.py`
- **Mock strategy:** In-process team with `MemoryBackend`.

#### CREATE 5.2.21–22 — `walkthroughs/arcui/01-02`
- **5.2.21 `01-dashboard-bringup.ipynb`** — `create_app`, `serve`, three-token auth setup, basic dashboard launch.
- **5.2.22 `02-live-telemetry-attach.ipynb`** — `attach_llm`, WebSocket protocol, telemetry stream, event filtering.
- **Source files:** `packages/arcui/src/arcui/*.py`
- **Mock strategy:** TestClient pattern; subprocess launch with timeout for the bringup demo.

#### CREATE 5.2.23–24 — `walkthroughs/arcgateway/01-02`
- **5.2.23 `01-session-routing.ipynb`** — `GatewayRunner`, `SessionRouter`, `build_session_key`, session lifecycle.
- **5.2.24 `02-platform-adapters.ipynb`** — `InboundEvent`, `Delta`, `DeliveryTarget`, `AsyncioExecutor`, custom adapters.
- **Source files:** `packages/arcgateway/src/arcgateway/*.py`
- **Mock strategy:** Synthetic platform that emits InboundEvents into the runner.

### 5.3 Skipped packages

| Package | Reason | Reconsider when |
|---|---|---|
| `arctui` | v0.0.2 stub, "coming soon" | Implementation lands |
| `arcmodel` | v0.0.2 stub, "coming soon" | Implementation lands |
| `arcprompt` | v0.0.2 stub, "coming soon" | Implementation lands |
| `arcmas` | Meta-package, no public API | N/A — README is the right format |
| `arccli` | CLI tool — `arc --help` + README is better than a notebook | N/A |
| `arcskill` | v0.1.0, only `hub` and `lock` exposed; deferred wave-3 | Wave-3 lands |

---

## 6. Chunked execution schedule

Each chunk is 3 notebooks dispatched in parallel. **Chunks must run in order** because later chunks may import from earlier-shipped code.

| Chunk | Notebooks | Why this grouping | After this chunk |
|---|---|---|---|
| **C01** | `arctrust/01-identity-did`, `arctrust/02-keypairs-signing`, `arctrust/03-policy-pipeline` | arctrust is foundational; do it first so later notebooks can import from it cleanly | arctrust core landed |
| **C02** | `arctrust/04-audit-sinks`, `arcllm/10-audit-trail` (rewrite), `arcllm/14-trace-store` (new) | Audit story is connected — finish the chain | Audit story complete |
| **C03** | `arcagent/01-first-agent`, `arcagent/02-tool-integration`, `arcagent/03-policy-and-modules` | Agent nucleus — enables everything downstream | arcagent core landed |
| **C04** | `arcagent/04-module-bus-events`, `arcrun/01-core-react` (rewrite), `arcrun/06-task-completion-budgets` (new) | Agent loop completion + budget control | arcrun core upgraded |
| **C05** | `arcrun/02-tool-executor` (rewrite), `arcrun/04-streaming` (new), `arcrun/05-parallel-dispatch` (new) | Tool execution surface fully covered | arcrun execution complete |
| **C06** | `arcrun/03-codeexec` (rewrite), `arcrun/07-event-chain-verification` (new), `arcllm/04-agentic-loop` (rewrite) | Strategy + chain + new load_model API | Loop story complete |
| **C07** | `arcllm/06-provider-registry` (rewrite), `arcllm/07-module-system` (rewrite), `arcllm/17-routing-module` (new) | Registry + module + routing — connected | arcllm dispatch story complete |
| **C08** | `arcllm/15-queue-circuit-breaker` (new), `arcllm/16-config-controller` (new), `arcllm/13-open-providers` (rewrite) | Resilience + config + provider catalog | arcllm extensions complete |
| **C09** | `arcllm/01-core-types` (rewrite), `arcllm/02-config-loading` (rewrite), `arcllm/03-anthropic-adapter` (rewrite) | Refresh medium/low-risk arcllm | arcllm refreshes done |
| **C10** | `arcllm/05-openai-adapter` (rewrite), `arcllm/08-rate-limiter` (rewrite), `arcllm/09-telemetry-module` (rewrite) | More arcllm refreshes | arcllm refreshes done |
| **C11** | `arcllm/11-otel-export` (rewrite), `arcllm/12-security-layer` (rewrite), `arcteam/01-team-formation` (new) | Last arcllm + first arcteam | arcllm complete |
| **C12** | `arcteam/02-task-distribution`, `arcteam/03-messaging-channels`, `arcteam/04-team-persistence` | Finish arcteam | arcteam complete |
| **C13** | `arcui/01-dashboard-bringup`, `arcui/02-live-telemetry-attach`, `arcgateway/01-session-routing` | UI + gateway | UI / gateway near-complete |
| **C14** | `arcgateway/02-platform-adapters` (+ buffer slot for any redos) | Wrap up | Done |

**Total:** 14 chunks × ~3 notebooks = ~40 notebooks delivered. Allow ~10–15% redo buffer (1–2 chunks of slack).

---

## 7. Status table

> Update this after every chunk completes. This is what makes the plan resumable across context clears.

| ID | Notebook | Type | Chunk | Status | Notes |
|---|---|---|---|---|---|
| 5.2.9 | arctrust/01-identity-did | CREATE | C01 | done | 57 cells, 41KB, 9 sections |
| 5.2.10 | arctrust/02-keypairs-signing | CREATE | C01 | done | 56 cells, 38KB, 9 sections |
| 5.2.11 | arctrust/03-policy-pipeline | CREATE | C01 | done | 51 cells, 47KB, 10 sections |
| 5.2.12 | arctrust/04-audit-sinks | CREATE | C02 | done | 55 cells, 38KB, 10 sections |
| 5.1.10 | arcllm/10-audit-trail | REWRITE | C02 | done | 46 cells, 37KB, 10 sections |
| 5.2.1 | arcllm/14-trace-store | CREATE | C02 | done | 54 cells, 51KB, 10 sections |
| 5.2.13 | arcagent/01-first-agent | CREATE | C03 | done | 54 cells, 45KB, 10 sections |
| 5.2.14 | arcagent/02-tool-integration | CREATE | C03 | done | 52 cells, 52KB, 10 sections |
| 5.2.15 | arcagent/03-policy-and-modules | CREATE | C03 | done | 72 cells, 51KB, 10 sections |
| 5.2.16 | arcagent/04-module-bus-events | CREATE | C04 | done | 65 cells, 68KB, 10 sections |
| 5.1.14 | arcrun/01-core-react | REWRITE | C04 | done | 75 cells, 51KB, 13 sections |
| 5.2.7 | arcrun/06-task-completion-budgets | CREATE | C04 | done | 48 cells, 52KB, 12 sections |
| 5.1.15 | arcrun/02-tool-executor | REWRITE | C05 | done | 66 cells, 59KB, 11 sections |
| 5.2.5 | arcrun/04-streaming | CREATE | C05 | done | 61 cells, 46KB, 12 sections |
| 5.2.6 | arcrun/05-parallel-dispatch | CREATE | C05 | done | 55 cells, 41KB, 11 sections |
| 5.1.16 | arcrun/03-codeexec | REWRITE | C06 | done | 77 cells, 64KB, 11 sections |
| 5.2.8 | arcrun/07-event-chain-verification | CREATE | C06 | done | 52 cells, 45KB, 11 sections |
| 5.1.4 | arcllm/04-agentic-loop | REWRITE | C06 | done | 71 cells, 63KB, 15 sections |
| 5.1.6 | arcllm/06-provider-registry | REWRITE | C07 | pending | High-risk |
| 5.1.7 | arcllm/07-module-system | REWRITE | C07 | pending | High-risk |
| 5.2.4 | arcllm/17-routing-module | CREATE | C07 | pending | |
| 5.2.2 | arcllm/15-queue-circuit-breaker | CREATE | C08 | pending | |
| 5.2.3 | arcllm/16-config-controller | CREATE | C08 | pending | |
| 5.1.13 | arcllm/13-open-providers | REWRITE | C08 | pending | High-risk |
| 5.1.1 | arcllm/01-core-types | REWRITE | C09 | pending | Low-risk |
| 5.1.2 | arcllm/02-config-loading | REWRITE | C09 | pending | |
| 5.1.3 | arcllm/03-anthropic-adapter | REWRITE | C09 | pending | |
| 5.1.5 | arcllm/05-openai-adapter | REWRITE | C10 | pending | |
| 5.1.8 | arcllm/08-rate-limiter | REWRITE | C10 | pending | Low-risk |
| 5.1.9 | arcllm/09-telemetry-module | REWRITE | C10 | pending | |
| 5.1.11 | arcllm/11-otel-export | REWRITE | C11 | pending | |
| 5.1.12 | arcllm/12-security-layer | REWRITE | C11 | pending | |
| 5.2.17 | arcteam/01-team-formation | CREATE | C11 | pending | |
| 5.2.18 | arcteam/02-task-distribution | CREATE | C12 | pending | |
| 5.2.19 | arcteam/03-messaging-channels | CREATE | C12 | pending | |
| 5.2.20 | arcteam/04-team-persistence | CREATE | C12 | pending | |
| 5.2.21 | arcui/01-dashboard-bringup | CREATE | C13 | pending | |
| 5.2.22 | arcui/02-live-telemetry-attach | CREATE | C13 | pending | |
| 5.2.23 | arcgateway/01-session-routing | CREATE | C13 | pending | |
| 5.2.24 | arcgateway/02-platform-adapters | CREATE | C14 | pending | |

**Status legend:** `pending` → `in-progress` → `verified` → `done`. Use `failed` if a notebook needs redo and queue it for the next available slot.

---

## 8. Acceptance criteria (per notebook)

Before marking `done`, verify:

- [ ] File exists at the path specified in §5.X.Y
- [ ] First code cell is the exact setup boilerplate from §2
- [ ] Cell count ≥ 30
- [ ] Numbered top-level sections ≥ 7
- [ ] File size ≥ 20 KB
- [ ] Every public API symbol listed in §5.X.Y appears at least once in code cells
- [ ] Notebook has a "Summary" markdown section as last cell
- [ ] `uv run ruff check <file>` passes
- [ ] `uv run jupyter nbconvert --to notebook --execute --inplace <file>` succeeds (mock-only path)
- [ ] All `execution_count: null` and `outputs: []` at commit
- [ ] Single commit follows message format from §3 step 6
- [ ] Status table row updated to `done`

If any check fails, the chunk is incomplete. Dispatch a redo agent.

---

## 9. Pre-flight checklist (before C01)

- [ ] Confirm `nbformat` and `jupyter` are installed in the workspace dev deps (`uv add --dev nbformat jupyter`). Add if missing.
- [ ] Confirm `python-dotenv` is in dev deps (already used in setup boilerplate).
- [ ] Pre-existing 58 ruff errors in `scripts/check_loc_budgets.py` and `scripts/coverage_report.py` either fixed or explicitly deferred — they aren't in walkthrough scope and will not block.
- [ ] Sanity-check that `git log --oneline -1 -- walkthroughs/` shows the move commit, so agents know they're working from a clean baseline.

---

## 10. After all 14 chunks

- Add a `walkthroughs/README.md` that's an index linking to every notebook with a one-line description.
- Add a top-level mention of `walkthroughs/` in the repo `README.md`.
- Bump `CHANGELOG.md` with `Added: comprehensive walkthrough notebooks for all production packages`.
- Consider archiving `PLAN.md` to `walkthroughs/.history/PLAN-2026-05-completed.md` once all rows are `done`.
