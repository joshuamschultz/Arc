# The Module System — Folder-Discovered, Config-Activated Capabilities

An arcagent *module* is a self-contained folder under `arcagent/modules/` that ships a
bundle of tools, hooks, and background tasks plus the per-agent state they share. The
framework **discovers** every such folder by presence on disk, then **activates** only the
ones the agent's config enables. Discovery and activation are deliberately separate: the
full set of shipped modules is always KNOWN, but nothing runs until an operator opts in.

This document is the builder reference: the module contract, how discovery drives loading,
how to add or remove a module, the self-containment rules, and the pluggable-backend
pattern (memory as the worked example). For one module's operator-facing internals, see its
own doc — e.g. `packages/arcagent/docs/tasks-module.md` for `tasks`.

---

## The module contract

Every module is a folder under `arcagent/modules/<name>/` with four files:

```
modules/workpad/
├── __init__.py       # public exports (usually just the Config class)
├── capabilities.py   # @tool / @hook / @background_task / @capability — the discovery signal
├── _runtime.py       # per-agent state via a contextvars.ContextVar + configure()/bind()/state()
└── config.py         # <Name>Config(ModuleConfig) — the [modules.NAME.config] schema
```

- **`capabilities.py`** is where the module's surface lives. Functions stamped with the
  decorators in `arcagent/tools/_decorator.py` — `@tool` (LLM-callable), `@hook` (bus
  subscriber), `@background_task` (interval-driven async task), `@capability` (lifecycle
  class) — are what the `CapabilityLoader` scans and registers. **The presence of this file
  (with `_runtime.py`) is the discovery signal** (see below).

- **`_runtime.py`** holds the module's per-agent state. State is bound to a
  `contextvars.ContextVar`, not a module global, because one process runs many agents and a
  plain global would be clobbered by whichever agent's `asyncio.Task` last called
  `configure()`. The file exposes a fixed shape: `configure(**kwargs)` (build state once at
  startup), `state()` (read it, raising if unconfigured), `bind(state_obj)` (re-set the
  ContextVar into the current task — called at the top of every turn so a hook running in a
  fresh sibling task still sees this agent's state), and `reset()` (test-only). See
  `modules/workpad/_runtime.py` for the canonical shape.

- **`config.py`** defines `<Name>Config` on the shared `ModuleConfig` base
  (`arcagent/core/module_config.py`), which sets `extra="forbid"` so a misspelled config
  key raises a validation error instead of vanishing silently. This is the schema for the
  `[modules.NAME.config]` TOML table.

- **`__init__.py`** typically exports only the Config class. Keep it thin; importing it must
  not have side effects.

### A minimal `capabilities.py`

```python
"""Echo module — trivial example."""
from __future__ import annotations

from typing import Any

from arcagent.modules.echo import _runtime
from arcagent.tools._decorator import tool


@tool(description="Echo a string back", classification="read_only")
async def echo(text: str) -> str:
    st = _runtime.state()          # read this agent's bound state
    return f"{st.config.prefix}{text}"
```

The JSON Schema for `echo` is inferred from the typed signature — module authors never hand-write it.

### The 17 shipped modules

`browser` (CDP web automation) · `memory` (thin Brain wiring — see below) · `messaging`
(inter-agent comms via ArcTeam) · `planning` (Plan-Execute planner) · `policy` (ACE
self-learning adaptation) · `proactive` (proactive execution) · `pulse` (periodic ambient
awareness) · `scheduler` (cron/interval/one-time self-scheduling) · `session` (JSONL store
+ FTS5 `session_search`) · `skills` (SkillAdapter wiring) · `slack` (Slack Socket Mode) ·
`tasks` (mission-control task directory) · `telegram` (Telegram Bot API) · `user_profile`
(per-user profile storage) · `voice` (STT/TTS backends) · `web` (web search + extraction) ·
`workpad` (self-managing `context.md` maintainer).

---

## Discovery + activation

This is the key mechanic. The two steps are independent and live in
`arcagent/core/module_discovery.py`.

**Discovery is folder-driven.** A folder qualifies as a module iff it is a non-underscore
directory containing both `capabilities.py` and `_runtime.py` (`_is_module`).
`discover_modules()` scans `modules/` and returns the sorted names of every qualifying
folder. Because discovery is by presence, no folder can silently contribute nothing for
lack of a config entry — the full present-set is always visible.

**Activation is config-driven and DEFAULT OFF.** A discovered module *loads* only when the
agent's `arcagent.toml` carries an enabled `[modules.NAME]` entry. `active_modules(config)`
returns exactly `discovered ∩ enabled` — the single seam both the real load path
(`agent_lifecycle.configure_module_runtimes` and the scan-root loop) and any listing surface
agree on.

The three states a module name can be in, from `module_statuses()`:

| `discovered` | `enabled` | Meaning |
|---|---|---|
| yes | yes | **Active** — loads: `configure()` runs, capabilities scanned + registered |
| yes | no | **Discovered-inactive** — a valid, listable, known state; nothing loads |
| no | (config enables it) | **Config names an absent folder** — logged as a warning, never loads |

That last row matters: a `[modules.typo]` entry (or a stale name whose folder was removed)
can never load, so `_warn_config_without_folder` surfaces it once at startup as a config
error rather than failing silently.

**Contrast with the always-scanned capability roots.** Modules are one of several capability
sources the `CapabilityLoader` scans. The others — builtins, `~/.arc/capabilities/`, the
per-agent `capabilities/`, and the workspace `capabilities/` — are scanned unconditionally
(builtins) or by user opt-in, independent of `[modules.*]`. Modules are the config-gated
source: `agent_lifecycle.setup_capabilities` appends one `("module:<name>", modules_dir/<name>)`
scan root **per active module only**.

### `[modules.NAME]` schema

From `ModuleEntry` in `arcagent/core/config.py`:

```toml
[modules.workpad]
enabled  = true        # the load gate (default true if the table is present)
priority = 100         # module ordering hint
[modules.workpad.config]
every_n_runs = 20      # forwarded verbatim to WorkpadConfig(**config); extra keys rejected
```

The `[modules.NAME.config]` sub-table becomes `mod_entry.config` (a raw dict), which the
module's `configure()` passes to its `<Name>Config(**config)` — where `extra="forbid"`
validates it.

---

## Adding a module

Nothing in `core/` changes. You touch exactly the new folder plus the agent's TOML:

1. Create `arcagent/modules/<name>/` with the four files:
   `__init__.py`, `capabilities.py` (at least one `@tool`/`@hook`/`@background_task`),
   `_runtime.py` (a `_State` dataclass + `configure`/`state`/`bind`/`reset`), and `config.py`
   (`<Name>Config(ModuleConfig)`). Copy `modules/workpad/` as the template.
2. Enable it in the agent's `arcagent.toml`:
   ```toml
   [modules.<name>]
   enabled = true
   [modules.<name>.config]
   # your config.py fields
   ```

That is the whole loop. Discovery picks the folder up automatically; the scan root and the
`configure()` call follow from `active_modules`. No registry edit, no import in core, no
list to append your name to.

## Removing a module

Disable it in config — delete or set `enabled = false` on the `[modules.<name>]` table. The
folder stays on disk and remains *discovered*, but drops out of `active_modules`, so it goes
inert: `configure()` is never called and its capabilities are never scanned. It sits in the
discovered-inactive state, fully reversible by flipping `enabled` back. Deleting the folder
outright is also fine; just remove any `[modules.<name>]` entry too, or it becomes the
warned "config names an absent folder" case.

---

## Self-containment rules

Modules are shared-nothing by design. The rules that keep them decoupled:

- **No cross-module imports of another module's internals.** A module never reaches into
  another module's `_runtime` or `capabilities`. Modules coordinate through the bus (`@hook`
  events) and through shared stores, not by importing each other.

- **Per-module facts are declared by the module's own `configure()` signature — core names
  no module.** `configure_module_runtimes` offers every module a fixed menu of framework
  values (`config`, `telemetry`, `workspace`, `bus`, `identity`, `policy_pipeline`,
  `egress_proxy`, `operator_signer`, …) and delivers only the ones the module's
  `configure()` actually declares as parameters:

  ```python
  sig = inspect.signature(configure_fn)
  kwargs = {name: value for name, value in available.items() if name in sig.parameters}
  configure_fn(**kwargs)
  ```

  So whether a module needs, say, the `operator_signer` is a fact the module states by
  naming that parameter — not something core special-cases. A generic module cannot harvest
  signing authority unless it explicitly asks for it (SPEC-053/037); the WORM-sink modules
  that do ask sign by reference under vault_transit, never the seed. `configure()` is
  fail-open: an exception is logged and the loop continues.

- **Shared framework pieces live in `core/` and `utils/`, not in a module.** The
  `ModuleConfig` base (`core/module_config.py`), discovery (`core/module_discovery.py`),
  the module bus, and utilities like `utils/audit.safe_audit`, `utils/io.atomic_write_text`,
  and `utils/model_helpers` are the common ground. Three modules use a pattern before it is
  extracted to `utils/`. Core stays ignorant of any specific module.

---

## The pluggable-backend pattern (memory)

`memory` is the template for a module that fronts a **pluggable backend** without arcagent
knowing the backend: a thin generic adapter in arcagent, the real implementation in a
separate installable package, selected by TOML.

The module holds no memory logic. `modules/memory/config.py` names no backend — it exposes
a `brain` selector string and an **opaque `backend` dict** forwarded verbatim to whatever
backend is chosen:

```toml
[modules.memory]
enabled = true
[modules.memory.config]
brain = "arcmemory"        # "none" | an installed backend name | "module:Class" (BYO)
[modules.memory.config.backend]
# opaque, backend-defined — arcagent never reads a key here; the backend validates it
embedder = "nomic-embed-text"
```

`configure()` calls `select_brain(cfg.brain, …, backend_config=dict(cfg.backend))`
(`arcagent/brain/select.py`). The selector maps the setting to a concrete `Brain`:

- `"none"` → `NullBrain` (default; memory off, zero files, every hook short-circuits).
- a backend name → lazy-imports that package and calls its well-known
  `build_brain(context)` factory (`_PROVIDER_ENTRYPOINT = "build_brain"`). arcagent has no
  static dependency on any memory package; a missing install degrades to `NullBrain` with a
  warning rather than crashing.
- a dotted `module:Class` path → a bring-your-own `Brain`, refused before import above the
  personal tier unless operator-allowlisted (ASI04).

`Brain` itself (`arcagent/brain/protocol.py`) is a **structural Protocol** — arcagent
depends on no memory package, only on the shape. So `memory` can front `arcmemory` or any
alternative, and swapping the backend is a TOML change: the module, the module contract, and
core are all unchanged.

This is the model for future pluggable subsystems: a thin generic adapter module + a
`Protocol` boundary + a config-selected `build_<thing>` provider in a separate package. The
generic dispatch, BYO allowlist gate, dotted-path importer, and provider call all live once
in `arcagent/extension/select.py` (`select_extension` / `ExtensionPoint`), so a new seam
declares its point and reuses that machinery.
