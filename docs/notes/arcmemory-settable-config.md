# arcmemory settable config — already supported via `dynamics`

**Date:** 2026-08-31
**Status:** No code change needed. Gap is discoverability + a confusing error.

## The problem it looks like

arcmemory has real operator knobs — `consolidate_agent_max_tokens` (default 20k),
`consolidate_agent_max_turns` (16), `consolidate_agent_timeout_seconds` (180),
`distill_max_input_tokens`, the decay/confidence dynamics, etc. — defined on
`arcmemory.config.MemoryConfig`.

Setting one the obvious way **bricks the agent**:

```toml
[modules.memory.config]
consolidate_agent_max_tokens = 40000   # WRONG — extra_forbidden
```

arcagent's `MemoryConfig` (`packages/arcagent/src/arcagent/modules/memory/config.py`)
is a *thin wiring module* built on `ModuleConfig`, which is `extra=forbid`. Any key it
doesn't declare fails Pydantic validation → `Required module 'memory' configuration
failed`. On restart a held session re-fires and the operator sees
`[agent-error] the run failed` in the channel. (This is exactly what took the fleet
down on 2026-08-31, and josh+olivia on 2026-08-29.)

## The path that already works

arcagent's thin module exposes a `dynamics` passthrough. Whatever you put there is
folded into `backend["dynamics"]` and forwarded verbatim to arcmemory's
`build_brain`, which applies it **over** the tier defaults and re-validates against
arcmemory's own `MemoryConfig`:

```toml
[modules.memory.config.dynamics]
consolidate_agent_max_tokens = 40000
consolidate_agent_max_turns = 24
consolidate_agent_timeout_seconds = 300
```

Verified end-to-end (2026-08-31): the value reaches
`arcmemory MemoryConfig.consolidate_agent_max_tokens = 40000` and validates. arcmemory's
`MemoryConfig` is `frozen` but not `extra=forbid`, so a **real** field is applied and an
unknown key is silently ignored (no typo protection — see gap #2).

The same `backend` table also carries the individually-read keys: `embed_backend`,
`embed_model`, `embed_base_url`, `distill_provider`, `distill_model`, `capture_tool_io`.
(`distill_provider` / `distill_model` also have first-class convenience aliases at the
top level of `[modules.memory.config]`.)

## What's actually missing (the real asks)

1. **Docs.** Nothing tells an operator that arcmemory tuning goes through
   `[modules.memory.config.dynamics]`. Document the passthrough + the common knobs.
2. **A friendlier failure.** A flat arcmemory key under `[modules.memory.config]`
   should raise "did you mean `[modules.memory.config.dynamics]`?" instead of a raw
   `extra_forbidden`, and ideally never let one bad key brick the whole module on
   restart. A typo inside `dynamics` is silently ignored — worth a warn.
3. **Optional:** promote the 2–3 most-used consolidation knobs
   (`consolidate_agent_max_tokens/turns/timeout_seconds`) to first-class convenience
   fields on the thin module (like `distill_provider` already is), folded into
   `dynamics` by `_fold_backend_settings`. Pure ergonomics; the passthrough already
   covers the capability.

## Why it matters now

Running the fleet on GLM (a slow local reasoning model, ~40s+/call) means the 20k
consolidation budget and the default turn/timeout caps are tight. Raising them via
`dynamics` is the supported move; no rebuild.
