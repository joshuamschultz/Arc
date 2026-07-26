# ADR-023: Capability Resolution — Unified `CapabilityProvider`, Layered Roots, Last-Wins Precedence, Signed-to-Load Trust

**Status**: Accepted
**Date**: 2026-05-31
**Builds on**: ADR-019 (Four Pillars Universal), ADR-021 (Agent Self-Description via TOML), SPEC-021 (`CapabilityLoader`)
**Relates to**: the forthcoming "everything runs through arcrun" unification (separate plan)

## Context

`arcrun` is the execution loop and the single runtime path to `arcllm`. Today it
receives a **flat `list[Tool]`** (`{name, input_schema, call}`); `arcagent`
flattens tools, skills, and memory into that list via `to_arcrun_tools()`. Two
gaps:

1. **No unified contract.** A skill either dumps its whole body into a tool
   description (burns context every turn) or is lost. There is no "advertise
   cheaply, fetch the body only when actually used."
2. **Capabilities come from several places** and the resolution/trust rules
   were implicit. `arcagent` already discovers them via `CapabilityLoader`
   (`agent_lifecycle.setup_capabilities`) over named `scan_roots`:

   | Root | Location | Source |
   |---|---|---|
   | `builtins` | `arcagent/builtins/capabilities/` | shipped in-package (a few, not a lot) |
   | `builtins-skills` | `arcagent/builtins/capabilities/skills/` | shipped skills |
   | `global` | `~/.arc/capabilities/` | extensions installed out-of-workspace |
   | `agent` | `<agent_root>/capabilities/` | the agent's own declared files |
   | `workspace` | `<workspace>/.capabilities/` | agent-authored at runtime |
   | `module:<name>` | `arcagent/modules/<name>/` | per **enabled** module (`arcagent.toml`) |

We allow extensions (install tools/skills into `~/.arc`), agent-created
capabilities (workspace), and ship a small built-in set. The open questions were:
the contract `arcrun` should expect, how the body is fetched for context, who
wins a name collision, and what is allowed to load at all.

## Decision

### 1. A unified `CapabilityProvider` Protocol, owned by arcrun

`arcrun.run(messages, capabilities: CapabilityProvider)` replaces the flat tool
list. The Protocol lives in `arcrun` (the consumer); `arcagent` implements it.

```python
@runtime_checkable
class CapabilityProvider(Protocol):
    def advertise(self) -> list[CapabilitySpec]:
        """Lean manifest for the model: name · kind · 'use when…' · input_schema.
        This is ALL that enters the prompt/tool list. No bodies, no file contents."""
    async def load(self, name: str, *, caller_did: str) -> str | None:
        """Lazily fetch the heavy context for one capability (a skill's full
        instructions) — only when the model reaches for it. None for plain tools."""
    async def invoke(self, name: str, args: dict, *, caller_did: str) -> CapabilityResult:
        """Dispatch a call. Runs through arctrust policy (caller_did) first."""
```

- **Lazy load = model-driven retrieval.** The model's own tool-call is the
  trigger: it selects a capability, `arcrun` calls `load(name)`, splices the body
  into the next turn. You pay context only for what's used — the same progressive
  disclosure as Claude Code skills (SKILL.md → deeper tiers on use). `arcrun`
  never guesses what to fetch.
- **No new discovery.** `advertise()` *is* the `capability_registry` after
  `CapabilityLoader.scan_and_register()`; `load()` reads the body from whichever
  root the capability resolved from (the registry records the source path). The
  provider is a thin Protocol view over the loader + registry that already exist.
- `arcrun` owns the loop, not the capabilities. Tools/skills/memory are opaque
  handlers passed in (CLAUDE.md: "Don't have arcrun do things that belong to
  agent or arcllm").

### 2. Precedence = last-wins (most-specific overrides)

Resolution order, least- to most-specific:

```
shipped (in-package)  <  extensions (~/.arc)  <  agent files  <  agent-created (workspace)
```

On a name collision the **more-specific layer wins** — an agent-created
`read_file` overrides the shipped one. This is the convention developers expect
(npm nearest-`node_modules`-wins; Claude Code project-skill over user-skill).
`arcagent` may extend or override; it is not locked out of its own core.

### 3. Trust = signed-to-load (a *separate* axis from precedence)

"Who wins a collision" and "what may load at all" are answered independently.
Last-wins governs precedence; **signature + policy** governs trust. Only a
verified artifact ever enters the resolution set, so a malicious unsigned file
dropped in the workspace can never silently shadow a builtin — it does not load.

- **Shipped** — trusted by construction (it is the package).
- **Extensions** (`~/.arc`) — signed at install, recorded in a lockfile (skills
  already do this: `~/.arc/skills/.hub/lock.json`). The lockfile is the
  supply-chain gate (ASI04 / LLM03).
- **Agent-created** (`workspace/.capabilities/`) — signed **and** sandboxed
  (Firecracker) before callable (ASI05). Least trust, highest precedence — fine,
  *because* it had to be signed to get in.
- **Audit-on-override.** When a lower-trust tier shadows a higher one, emit a
  capability-override event (`capability.override name=… source=workspace`).
  Last-wins, but never *silent* — every override is visible to review (ASI01/ASI06).

### 4. Enablement is explicit; discovery is automatic

Roots are scanned automatically, but `arcagent.toml`'s `[modules]` block gates
which module dirs are scanned (`if mod_entry.enabled`). A capability is never
active merely because a file exists in a folder — least-privilege allowlist
(LLM06 / ASI03).

## Consequences

**Positive**
- One contract for arcrun, not three parallel lists. Context stays lean (lazy,
  model-driven). Concern split holds (arcrun = Protocol + loop; arcagent = roots,
  trust ordering, lazy loads). Security holds: every `load`/`invoke` carries
  `caller_did` → arctrust policy, fail-closed; every artifact is signed-to-load;
  every override is audited. Matches established framework behavior.

**Negative / accepted**
- Last-wins lets an agent override a builtin. Accepted because the override must
  be signed and is audited — a deliberate, reviewable act, not a silent hijack.
- A `use_skill(name)` round-trip (model signals it wants a skill body) costs one
  extra turn vs. pre-stuffing context. Accepted: legible, keeps arcrun dumb, and
  far cheaper than paying every skill's body on every turn.

**Open (deferred)**
- Whether `workspace/.capabilities` is callable at all in **federal tier** or
  strictly personal-tier-with-audit. Default: gate by tier; federal may disable
  agent-authored capabilities entirely.
- The exact override signal (`use_skill` meta-tool vs. auto-inject on manifest
  reference). Leaning explicit `use_skill`.

## Alternatives considered

- **Trust-ordered lock (builtins immutable, lower tiers may only add).** More
  conservative, but contradicts the expected last-wins convention and blocks
  legitimate agent self-extension. Rejected in favor of last-wins + signed-to-load,
  which gets the safety from the trust axis instead of the precedence axis.
- **Keep the flat `list[Tool]`.** Simplest, but forces skills to burn context or
  be dropped, and offers no lazy retrieval. Rejected.
