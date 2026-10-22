# Trusted Wrapper Relocation Guide

The trusted wrapper layer (telemetry, audit, security, etc.) is currently structured for easy relocation. This document describes the three placement options and what moves for each.

## Current Structure

```
arcllm/
├── contract/          # Vendor-free types, protocols (NO Arc imports)
│   ├── types.py       # Message, Delta, LLMResponse, ModelProvider, etc.
│   ├── errors.py      # ModelStreamProtocolError
│   └── middleware.py  # Middleware protocol, ARC_PRIVATE_KWARGS
├── trusted/           # Arc-owned wrapper (depends: contract + arcstore + arctrust)
│   ├── wrapper.py     # TrustedWrapperBase
│   └── (to be added: telemetry, security, injection, guardrails, queue, otel, budget)
├── backends/          # Backend implementations (depend only on contract)
│   ├── direct/        # Built-in HTTP adapters (to be restructured)
│   └── (external backends loaded via dotted path)
├── loader.py          # Single factory: loads backend + applies wrapper
└── types.py           # Re-exports from contract (backward compat)
```

## Three Placement Options

### Option 1: Stay in arcllm (CURRENT)

**No moves required**. The wrapper lives in `arcllm/trusted/` alongside contract and backends.

**Pros:**
- Everything model-related in one package
- Simplest for users: `import arcllm`
- Maintains arcllm → arcrun spine (D-620/621)

**Cons:**
- arcllm becomes heavier (contract + wrapper + backends)
- Wrapper and backends in same package (though cleanly separated)

**Files/Imports:**
- No moves
- Consumers: `from arcllm import load_model` (works today)

---

### Option 2: Move to arcrun

**Move:** `arcllm/trusted/` → `arcrun/model_wrapper/`

**Update:**
```python
# arcllm/loader.py (line ~20, marked RELOCATION)
- from arcllm.trusted.wrapper import apply_trusted_middleware
+ from arcrun.model_wrapper import apply_trusted_middleware
```

**Pros:**
- Wrapper closer to primary consumer (arcrun uses it most)
- arcllm stays focused on contract + backends
- Still maintains clean dependency: arcrun → arcllm.contract

**Cons:**
- arcrun gains wrapper dependencies (arcstore, arctrust)
- arcmemory, arccli, arcui depend on arcrun for wrapped providers
- Cross-package wrapper (arcrun wraps arcllm backends)

**Dependencies after move:**
```
arcrun → arcllm.contract (types, protocols)
arcrun.model_wrapper → arcstore, arctrust (wrapper duties)
arcllm.backends → arcllm.contract (backends stay in arcllm)
```

**Files to move:**
```
arcllm/trusted/wrapper.py              → arcrun/model_wrapper/base.py
arcllm/trusted/telemetry.py           → arcrun/model_wrapper/telemetry.py
arcllm/trusted/security.py            → arcrun/model_wrapper/security.py
arcllm/trusted/injection.py           → arcrun/model_wrapper/injection.py
arcllm/trusted/guardrails.py          → arcrun/model_wrapper/guardrails.py
arcllm/trusted/queue.py                → arcrun/model_wrapper/queue.py
arcllm/trusted/otel.py                 → arcrun/model_wrapper/otel.py
arcllm/trusted/budget.py               → arcrun/model_wrapper/budget.py
```

**Consumers:**
```python
# No change for consumers
from arcllm import load_model  # Still works; factory handles wrapper import
```

---

### Option 3: Move to new package (e.g., `arcwrap`, `arctelem`, `arcprovider`)

**Move:** `arcllm/trusted/` → `newpkg/`

**Update:**
```python
# arcllm/loader.py (line ~20, marked RELOCATION)
- from arcllm.trusted.wrapper import apply_trusted_middleware
+ from newpkg import apply_trusted_middleware
```

**New package dependencies:**
```toml
[project]
name = "arcwrap"  # or arctelem, arcprovider
dependencies = [
    "arcllm",  # for arcllm.contract only
    "arcstore",
    "arctrust",
]
```

**Pros:**
- Clean separation: contract (arcllm) | wrapper (newpkg) | backends (arcllm + external)
- Wrapper can evolve independently
- Other projects (non-Arc) could use the wrapper with their own backends

**Cons:**
- New package to maintain
- Slightly more complex dependency graph
- Need to pick a good name (arcwrap? arctelem? arcprovider?)

**Dependencies after move:**
```
newpkg → arcllm.contract (types, protocols)
newpkg → arcstore, arctrust (wrapper duties)
arcllm.backends → arcllm.contract (backends stay in arcllm)
arcrun, arcmemory, etc. → arcllm (contract + loader)
```

**Files to move:**
```
arcllm/trusted/wrapper.py              → newpkg/wrapper.py
arcllm/trusted/telemetry.py            → newpkg/telemetry.py
arcllm/trusted/security.py             → newpkg/security.py
arcllm/trusted/injection.py            → newpkg/injection.py
arcllm/trusted/guardrails.py           → newpkg/guardrails.py
arcllm/trusted/queue.py                → newpkg/queue.py
arcllm/trusted/otel.py                 → newpkg/otel.py
arcllm/trusted/budget.py               → newpkg/budget.py
```

**Consumers:**
```python
# No change for consumers
from arcllm import load_model  # Still works; factory handles wrapper import
```

---

## What Stays in arcllm Regardless

**Always in arcllm, never moves:**

1. **Contract** (`arcllm/contract/`):
   - Vendor-free types (Message, Delta, LLMResponse, ModelProvider, etc.)
   - Middleware protocol
   - NO imports of other Arc packages

2. **Backends** (`arcllm/backends/`):
   - Built-in `direct` backend (HTTP adapters, router, retry, fallback)
   - External backends loaded via dotted path
   - Depend only on `arcllm.contract`

3. **Loader factory** (`arcllm/loader.py`):
   - `load_model_backend()` function
   - Loads backend + applies wrapper
   - Single import to update for relocation

4. **Backward compat** (`arcllm/types.py`, `arcllm/__init__.py`):
   - Re-exports from contract
   - Legacy aliases (LLMProvider, ArcLLMStreamProtocolError)

## Implementation Checklist for Relocation

When moving the wrapper:

- [ ] Copy `arcllm/trusted/` to new location
- [ ] Update wrapper imports: `from arcllm.contract import ...`
- [ ] Update `arcllm/loader.py` wrapper import (line ~20, marked RELOCATION)
- [ ] If new package: create `pyproject.toml` with dependencies
- [ ] Run architecture tests:
  ```bash
  pytest tests/architecture/ -v
  pytest packages/*/tests/architecture/ -v
  ```
- [ ] Update `tests/architecture/test_no_arcstore_arcteam_upward_imports.py` if needed
- [ ] Update docs: `docs/concepts/decision-index.md`, package pages
- [ ] Delete `arcllm/trusted/` from arcllm
- [ ] Commit with clear relocation message

## Testing After Relocation

All existing tests should pass unchanged:

```bash
# Contract tests (always in arcllm)
pytest packages/arcllm/tests/test_contract_types.py -v

# Wrapper tests (follow the wrapper)
pytest <newlocation>/tests/ -v

# Integration tests (consumers)
pytest packages/arcrun/tests/ -v
pytest packages/arcmemory/tests/ -v
```

## Consumer Impact

**Zero impact** — consumers continue to call:

```python
from arcllm import load_model

provider = load_model("anthropic/claude-sonnet-4-5")
response = await provider.invoke(messages, tools)
```

The factory (`load_model`) handles backend loading and wrapper application internally. Only the factory's import changes during relocation.
