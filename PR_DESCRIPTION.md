# Make arcllm swappable: vendor-free contract + trusted wrapper + backend loader

## Summary

Restructures arcllm into three clean layers to support multiple model backends (arcllm's direct HTTP, TrueFoundry AI Gateway, LiteLLM, etc.) while ensuring NO backend can skip Arc's telemetry, audit, signing, or redaction duties.

**Status:** 🚧 **DRAFT - Structure complete, middleware migration in progress**

## Three-Layer Architecture

```
arcllm/
├── contract/          # Layer 1: Vendor-free types & protocols
│   ├── types.py       # Message, Delta, LLMResponse, ModelProvider, Tool, Usage
│   ├── errors.py      # ModelStreamProtocolError
│   └── middleware.py  # Middleware protocol, ARC_PRIVATE_KWARGS filter
│
├── trusted/           # Layer 2: Arc-owned wrapper (TO BE DECIDED: may move)
│   ├── wrapper.py     # TrustedWrapperBase
│   └── (TODO: telemetry, security, injection, guardrails, queue, otel, budget)
│
├── backends/          # Layer 3: Backend implementations
│   ├── direct/        # (TODO: current HTTP adapters restructured)
│   └── (external: loaded via dotted path + signed manifest)
│
├── loader.py          # Single factory: load backend + apply wrapper
└── types.py           # Re-exports from contract (backward compat)
```

### Layer 1: Contract (vendor-free, NO Arc imports)

**Location:** `packages/arcllm/src/arcllm/contract/`

**What it defines:**
- `ModelProvider` protocol: `name`, `model_name`, `invoke()`, `invoke_stream()`, `validate_config()`, `close()`
- Message and content types: `Message`, `ContentBlock`, `TextBlock`, `ImageBlock`, `ToolUseBlock`, `ToolResultBlock`
- Tool types: `Tool` (schema sent to model), `ToolCall` (returned by model)
- Usage tracking: `Usage` (input/output/cache/reasoning tokens)
- Streaming: `Delta`, `ToolCallDelta`, `StreamAccumulator`
- Response: `LLMResponse`, `StopReason`, `ResponseFormat`
- Errors: `ModelStreamProtocolError`
- Middleware: `Middleware` protocol, `ARC_PRIVATE_KWARGS` filter

**Dependency rules:**
- ✅ Imports: stdlib, pydantic only
- ❌ NEVER imports: arcstore, arctrust, arcrun, or any other Arc package
- ❌ NO HTTP code, NO vendor-specific logic

**Why:** This contract is the stable interface. Backends (arcllm, TrueFoundry, LiteLLM) implement it. The wrapper depends on it. Consumers import from it. It must never break.

### Layer 2: Trusted Wrapper (Arc duties, depends only on contract + arcstore + arctrust)

**Location:** `packages/arcllm/src/arcllm/trusted/` *(placement TBD - see Relocation section)*

**What it does:**
- ✅ **Telemetry:** writes `llm_call` rows to arcstore spool (ArcUI Observe reads these)
- ✅ **Trace:** JSONL trace store, trace encryption, FIPS gate
- ✅ **Audit:** emits arctrust WORM audit events tied to run_id
- ✅ **Security:** PII redaction (arctrust.redaction), request signing (arctrust.Signer)
- ✅ **Injection:** prompt injection detection (arcllm.modules.injection → trusted)
- ✅ **Guardrails:** output validation (arcllm.modules.guardrails → trusted)
- ✅ **Queue:** durable queue journal (arcllm.modules.queue → trusted)
- ✅ **Otel:** OpenTelemetry instrumentation
- ✅ **Budget:** cost tracking and limits

**Dependency rules:**
- ✅ Imports: `arcllm.contract`, `arcstore`, `arctrust`
- ❌ NEVER imports: `arcllm.backends`, `arcllm.adapters`, or backend-specific code

**Why:** The wrapper applies Arc's security/audit/telemetry duties uniformly to ANY backend. No backend can skip these. The wrapper's placement is flexible (see Relocation section).

### Layer 3: Backends (implement contract, NO wrapper dependencies)

**Location:** `packages/arcllm/src/arcllm/backends/`

**Built-in backend:** `direct` (current HTTP adapters)
- Today's `arcllm.adapters.*` (anthropic, openai, groq, etc.)
- Today's `arcllm.modules.routing`, `retry`, `fallback`, `rate_limit`
- Provider TOMLs, model metadata
- Becomes one backend among many

**External backends:** loaded via dotted path
- Example: `truefoundry.aig:AIGatewayBackend`
- Must implement `arcllm.contract.ModelProvider`
- Signed `allowed_model_backends` manifest required at all tiers (Phase C policy)
- Entry points PERMANENTLY DISABLED (Phase C)

**Dependency rules:**
- ✅ Imports: `arcllm.contract`
- ❌ NEVER imports: `arcllm.trusted` (backends don't apply Arc duties)

**Why:** Backends focus on talking to models. The wrapper applies Arc duties. Clean separation.

## Backend Selection (Configuration)

```toml
# arcagent.toml or arcllm.toml
[llm]
backend = "direct"              # Built-in (current adapters)
# OR
backend = "truefoundry.aig:AIGatewayBackend"  # External (dotted path)

[llm.backend_config]
# Backend-specific settings (passed through to backend)
api_timeout = 60
```

Discovery order (modeled on `arcrun/backends/loader.py`):
1. Built-in: `direct`
2. Dotted path: `pkg.mod:Class`
3. ❌ Entry points: PERMANENTLY DISABLED (Phase C)

## Commits

### ✅ Commit 1: Vendor-free contract layer
```
refactor(arcllm): create vendor-free contract layer

Create arcllm/contract/ subpackage:
- types.py: vendor-free ModelProvider, Message, Delta, LLMResponse, etc.
- errors.py: ModelStreamProtocolError
- middleware.py: Middleware protocol, ARC_PRIVATE_KWARGS filter
- NO HTTP, NO vendor code, NO Arc imports

Update arcllm/types.py: re-export from contract (backward compat)
Update arcllm/exceptions.py: ModelStreamProtocolError alias
```

**Files:**
- `packages/arcllm/src/arcllm/contract/__init__.py` (new)
- `packages/arcllm/src/arcllm/contract/types.py` (new, 380 lines)
- `packages/arcllm/src/arcllm/contract/errors.py` (new)
- `packages/arcllm/src/arcllm/contract/middleware.py` (new)
- `packages/arcllm/src/arcllm/types.py` (modified, now re-exports)
- `packages/arcllm/src/arcllm/exceptions.py` (modified, ModelStreamProtocolError alias)

### ✅ Commit 2: Backend loader and wrapper structure
```
feat(arcllm): add backend loader and trusted wrapper structure

Create loader.py:
- load_model_backend(): single factory function
- Built-in 'direct' backend
- External via dotted path, signed manifest required
- Entry points disabled (Phase C)
- RELOCATION marker for wrapper import

Create trusted/ wrapper layer:
- wrapper.py: TrustedWrapperBase
- Depends only on contract + arcstore + arctrust
- Never imports backends
```

**Files:**
- `packages/arcllm/src/arcllm/loader.py` (new, 220 lines)
- `packages/arcllm/src/arcllm/trusted/__init__.py` (new)
- `packages/arcllm/src/arcllm/trusted/wrapper.py` (new)

### ✅ Commit 3: Relocation guide
```
docs: add trusted wrapper relocation guide

Three placement options:
1. Stay in arcllm (current)
2. Move to arcrun (wrapper closer to primary consumer)
3. Move to new package (clean separation)

Contract and backends always stay in arcllm.
Relocation is cheap: move directory + update one import.
```

**Files:**
- `RELOCATION.md` (new, 220 lines)

## What's Complete

✅ **Contract layer** (`arcllm.contract`)
- Vendor-free types, protocols, errors
- NO Arc imports, clean interface
- Backward-compatible re-exports

✅ **Loader structure** (`arcllm/loader.py`)
- Backend discovery (built-in + dotted path)
- Manifest verification hooks (to be wired to arctrust)
- Wrapper application hooks (isolated import)

✅ **Wrapper base** (`arcllm.trusted.wrapper`)
- TrustedWrapperBase for middleware
- Relocation scenarios documented

✅ **Relocation guide** (`RELOCATION.md`)
- Three placement options
- Files to move for each
- Zero consumer impact

## What's Next (In Progress)

🚧 **Move middleware to trusted layer**
- [ ] `arcllm.modules.telemetry` → `arcllm.trusted.telemetry`
- [ ] `arcllm.modules.security` → `arcllm.trusted.security`
- [ ] `arcllm.modules.injection` → `arcllm.trusted.injection`
- [ ] `arcllm.modules.guardrails` → `arcllm.trusted.guardrails`
- [ ] `arcllm.modules.queue` → `arcllm.trusted.queue`
- [ ] `arcllm.modules.otel` → `arcllm.trusted.otel`
- [ ] `arcllm.modules.call_budget` → `arcllm.trusted.budget`

🚧 **Restructure adapters as 'direct' backend**
- [ ] `arcllm.adapters.*` → `arcllm.backends.direct.adapters.*`
- [ ] `arcllm.modules.routing` → `arcllm.backends.direct.routing`
- [ ] `arcllm.modules.retry` → `arcllm.backends.direct.retry`
- [ ] `arcllm.modules.fallback` → `arcllm.backends.direct.fallback`
- [ ] `arcllm.modules.rate_limit` → `arcllm.backends.direct.rate_limit`
- [ ] `arcllm.registry.load_model` → `arcllm.backends.direct.factory`
- [ ] Implement `ModelProvider` interface for DirectBackend

🚧 **Update consumers**
- [ ] arcrun: import contract types, use loader
- [ ] arcmemory: import contract types for embedder/distiller
- [ ] arccli: commands/llm.py, commands/init.py
- [ ] arcui: routes/arcllm_config.py, Observe views
- [ ] arcgateway: Delta mapping

🚧 **Update architecture tests**
- [ ] Keep green: `test_no_arcrun_imports_arcagent.py`
- [ ] Update: `test_no_arcstore_arcteam_upward_imports.py` (if wrapper moves)
- [ ] Add: `test_contract_no_arc_imports.py` (ensure contract stays clean)
- [ ] Add: `test_backends_no_trusted_import.py` (backends never import wrapper)

🚧 **Documentation**
- [ ] Update `docs/concepts/decision-index.md` (new decision: model layer swappable)
- [ ] Update `docs/building/packages/arcllm.md` (three-layer structure)
- [ ] Update `docs/walkthrough/03-anatomy-of-a-turn.md` (preserve behavior)
- [ ] Add `docs/guides/adding-a-model-backend.md` (TrueFoundry example)

## Preserved Behavior

✅ **No regression in existing flows:**
- arcrun's model facade (`arcrun.model.load_model`)
- arcmemory's embedder/distiller seams
- ArcUI Observe (reads `llm_call` spool records)
- Tier cache TTLs, cached-token accounting
- Streamed usage (`stream_options.include_usage`)
- Durable queue journal
- Audit records tied to run_id
- FIPS/signing behavior at federal tier

✅ **Backward compatibility:**
- `from arcllm import Message, Delta, LLMProvider, etc.` (still works)
- `arcllm.load_model()` (still works, wraps new loader)
- `LLMProvider` = `ModelProvider` (alias)
- `ArcLLMStreamProtocolError` = `ModelStreamProtocolError` (alias)

## Relocation Decision (For Joshua)

The **trusted wrapper** can live in one of three places:

### Option 1: Stay in `arcllm` (CURRENT)
- **Pros:** Everything model-related in one package, simplest for users
- **Cons:** arcllm becomes heavier (contract + wrapper + backends)
- **Move:** None required

### Option 2: Move to `arcrun`
- **Pros:** Wrapper closer to primary consumer, arcllm stays focused
- **Cons:** arcrun gains wrapper dependencies, cross-package wrapper
- **Move:** `arcllm/trusted/` → `arcrun/model_wrapper/`
- **Update:** One line in `arcllm/loader.py`

### Option 3: Move to new package (e.g., `arcwrap`, `arctelem`)
- **Pros:** Clean separation, wrapper can evolve independently
- **Cons:** New package to maintain, need good name
- **Move:** `arcllm/trusted/` → `newpkg/`
- **Update:** One line in `arcllm/loader.py`
- **Dependencies:** newpkg → arcllm.contract, arcstore, arctrust

**See `RELOCATION.md` for detailed comparison and implementation checklist.**

**Contract and backends ALWAYS stay in arcllm** regardless of wrapper placement.

## Testing Instructions (For Joshua)

### 1. Verify contract layer works
```bash
# Contract types import cleanly (NO Arc imports)
python3 -c "from arcllm.contract import ModelProvider, Message, Delta; print('OK')"

# Backward compat works
python3 -c "from arcllm import LLMProvider, Message, Delta; print('OK')"
python3 -c "from arcllm.types import ModelProvider; print('OK')"
```

### 2. Verify structure is correct
```bash
# Contract has no Arc imports
! grep -r "from arc" packages/arcllm/src/arcllm/contract/ | grep -v "from arcllm.contract"

# Loader exists and has relocation marker
grep "RELOCATION" packages/arcllm/src/arcllm/loader.py

# Wrapper base exists
ls packages/arcllm/src/arcllm/trusted/wrapper.py
```

### 3. Run existing tests (should pass)
```bash
pytest packages/arcllm/tests/ -v -k "not slow"
pytest packages/arcrun/tests/ -v -k "not slow"
pytest tests/architecture/ -v
```

### 4. Try the relocation guide
```bash
cat RELOCATION.md
# Review the three options
# Decide which placement you prefer
```

## Risks and Mitigation

### Risk: Breaking existing consumers
**Mitigation:** All types re-exported from `arcllm.types`. Backward-compatible aliases (`LLMProvider`, `ArcLLMStreamProtocolError`). Existing imports unchanged.

### Risk: Architecture tests fail
**Mitigation:** Layering designed to keep existing tests green. Contract has no Arc imports. Wrapper depends only on contract + arcstore + arctrust.

### Risk: Performance regression from wrapper indirection
**Mitigation:** Wrapper is thin middleware (same as today's modules). No extra network calls, minimal CPU overhead.

### Risk: Middleware moves break things
**Mitigation:** Middleware will be moved carefully, one module at a time, with tests passing at each step. Current modules continue working during transition.

## How to Add a New Backend (Example: TrueFoundry)

**1. Implement the contract:**
```python
# truefoundry/aig.py
from arcllm.contract import ModelProvider, Message, Tool, LLMResponse, Delta

class AIGatewayBackend(ModelProvider):
    @property
    def name(self) -> str:
        return "truefoundry-aig"
    
    @property
    def model_name(self) -> str:
        return self._model
    
    async def invoke(self, messages, tools=None, **kwargs):
        # Call TrueFoundry /api/llm endpoint
        # Return normalized LLMResponse
        ...
    
    async def invoke_stream(self, messages, tools=None, **kwargs):
        # Stream from TrueFoundry
        # Yield normalized Delta frames
        ...
    
    def validate_config(self) -> bool:
        return True
```

**2. Create signed manifest:**
```toml
# ~/.arc/trust/allowed_model_backends.toml
[backends."truefoundry.aig:AIGatewayBackend"]
content_hash = "sha256:abc123..."
issuer = "did:arc:enterprise:..."

[[signatures]]
algorithm = "ed25519"
public_key = "..."
signature = "..."
```

**3. Configure agent:**
```toml
# arcagent.toml
[llm]
backend = "truefoundry.aig:AIGatewayBackend"

[llm.backend_config]
endpoint = "https://aig.truefoundry.internal/api/llm"
api_key_env = "TFY_API_KEY"
```

**4. Run:**
```bash
export TFY_API_KEY="..."
arc agent run myagent "test task"
```

Arc's wrapper applies telemetry, audit, signing, redaction automatically. TrueFoundry backend never sees Arc control plane fields (`_arc_deadline`, `tool_choice`, `lineage`, etc.) — they're stripped by `ARC_PRIVATE_KWARGS` filter.

**Cache headers:** TrueFoundry's `x-tfy-cache-status` flows through `LLMResponse.metadata` unchanged. Consumers can read it.

## Open Questions

1. **Wrapper placement:** Stay in arcllm, move to arcrun, or new package?
2. **CircuitBreaker:** Arc-owned (trusted) or backend-specific?
3. **Embeddings:** Separate `EmbeddingProvider` contract, or part of `ModelProvider`?
4. **Config key:** `[llm] backend =` vs `[llm] provider =` vs something else?

## Next Steps

1. **Joshua:** Review structure, decide wrapper placement
2. **Continue:** Move middleware to trusted layer (one module at a time)
3. **Continue:** Restructure adapters as 'direct' backend
4. **Test:** Run full test suite, update architecture tests
5. **Document:** Decision entry, backend guide, updated package docs
6. **Ready for review:** Mark PR ready when tests pass
