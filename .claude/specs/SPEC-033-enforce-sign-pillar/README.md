# SPEC-033 — Enforce the Sign Pillar

**Feature:** Make the **Sign** pillar structurally enforced — no unsigned/unrestricted code path executes at any tier (config-relaxable only at personal).
**Status:** PENDING
**Branch:** `feat/SPEC-033-enforce-sign-pillar`
**Type:** Generic (capability load-path hardening + signature verification + agent-authored artifact signing + TOFU gate)
**Confidence:** High — every gap verified at file:line; the two hardest pieces (`DynamicToolLoader`, `TofuLayer`) already exist and are tested but unwired, so this is mostly wiring.
**Depends on:** **SPEC-036** (Real Code-Execution Sandbox) — execution routing (REQ-040) rides SPEC-036's tier-routed backend and its personal-off contract.

---

## One-liner

The Four Pillars say *"every loaded artifact is verified before use"* — but Arc loads agent-authored capabilities via **plain `exec` with full builtins**, never re-verifies signatures at load, and leaves agent-written skills/tools **unsigned**. SPEC-033 closes the gap: workspace code loads through the **already-built hardened loader**, signatures are **re-verified at load (not just install)**, agent-authored writes and skill-improver mutations are **signed on write**, a real **TOFU approval gate** governs first sight, and execution routes through the **SPEC-036 sandbox** — all fail-closed above personal.

## Why (the problem)

The Sign pillar is a claim the code does not deliver. Verified against source:

- **Agent-authored capabilities load via plain `exec(code, module.__dict__)` with full builtins** — `packages/arcagent/src/arcagent/capabilities/capability_loader.py:443` (`_load_module`). Only the `workspace` root gets AST validation (`_UNTRUSTED_ROOTS = frozenset({"workspace"})`, `capability_loader.py:65`, gated at `:196-198`); `builtins`/`global`/`agent` roots skip validation, and **no** root gets runtime builtin restriction.
- **A hardened `DynamicToolLoader` already exists — with zero production callers.** `packages/arcagent/src/arcagent/tools/_dynamic_loader.py:485-710` — `RESTRICTED_BUILTINS` (`:465`), a wrapped restricted `__import__` (`_make_restricted_import`, `:657`, injected `:606`), CVE-class AST hardening. Built, tested, **unwired** (grep: definition + export only).
- **`TofuLayer` (per-tier signature gate) and `core/os_sandbox.py` have zero production call sites.** `TofuLayer` at `core/tofu_layer.py:56-96` already returns `ALLOW/DENY/NEW_SIGHTING` per tier; nothing calls `evaluate()`. `os_sandbox.make_sandbox` (`os_sandbox.py:80`) is never called (SPEC-036 owns execution isolation).
- **Sigstore/Rekor verification is real but install-time only and skippable below federal.** `packages/arcskill/src/arcskill/hub/verify.py` (775 LOC) does full Fulcio + Rekor verification, but it runs only from `installer.py:345`; there is **no re-verification at capability LOAD/execution**, and non-federal tiers skip when `sigstore` is unavailable (`verify.py:57-63`).
- **Agent-authored skills/tools and improver mutations are unsigned.** `create_skill.py:88` and `create_tool.py:52` `write_text` the artifact with only optional AST validation — no signature. Skill-improver `apply_result` (`skill_improver/engine.py:207,222`) writes the mutated skill and appends **plaintext** audit (`candidate_store.py:136-141`, `audit.jsonl`), not the WORM/`SignedChainSink`.

Net: unsigned agent-generated code executes with full builtins, and "verify before use" is unenforced everywhere the agent actually writes code.

## Decision

**Enforce Sign at the capability load/execute boundary, reusing what already exists.**

- **Workspace `.py` loads through `DynamicToolLoader`** (RESTRICTED_BUILTINS + restricted `__import__`) — never plain `exec`.
- **`TofuLayer` + signature/content-hash verification wire into the load path**; verification **re-runs at load**, not just install; federal-grade verify is the floor.
- **Agent-authored writes (`create_skill`/`create_tool`) and improver mutations are signed on write** with `arctrust.keypair`, verified on (re)load; improver audit moves to the signed WORM chain.
- **A real TOFU approval gate** (consulting `ValidatorsConfig`) governs first sight of workspace code, audited.
- **Execution routes through the SPEC-036 sandbox** — never bare `exec`; personal-off honored.

Rationale: the hard crypto/isolation already exists — wiring it is the smallest correct change (Simplicity); arctrust owns verify primitives, arcagent wires the load path, arcskill owns Sigstore (Modularity); no unsigned exec above personal, fail-closed (Security); per-agent shared-nothing verification, O(1) TOFU hash lookup (Scalability).

## Scope (this spec)

1. **Harden the load path** — replace plain `exec` with `DynamicToolLoader` for workspace code.
2. **Verify at load** — wire `TofuLayer` + load-time signature/content-hash re-verification; federal floor.
3. **Sign agent-authored artifacts** — `create_skill`/`create_tool` + improver mutations signed on write; improver audit → WORM chain.
4. **TOFU approval gate** — first-sight workspace code requires explicit, audited approval.
5. **Sandboxed execution** — route agent-authored execution through SPEC-036.
6. **Config-relaxable / pluggable** — personal-only relaxation; Sigstore-keyless OR org-keypair behind one trust interface.

**Out of scope:** the execution sandbox itself (SPEC-036); Sigstore keyless issuance/UX; signing of first-party `builtins`/`global` shipped artifacts beyond load-time verify (they are release-signed upstream).

## Principled-coder pillars

1. **Simplicity** — the two hardest components (`DynamicToolLoader`, `TofuLayer`) exist and are tested; most tasks are *wiring*, flagged per-task.
2. **Modularity** — arctrust owns verify/sign primitives + WORM chain; arcagent wires the load/execute path; arcskill owns Sigstore/Rekor; trust backend is pluggable.
3. **Security** — fail-closed; no unsigned exec above personal; verify at load, not just install; TOFU on first sight; signed WORM audit.
4. **Scalability** — per-agent shared-nothing verification; O(1) approved-hash lookup; verify-on-change, not a polling storm.

## Files

- `PRD.md` — requirements (EARS, pillar-tagged, MoSCoW)
- `SDD.md` — module boundaries + current-state file:line + component design
- `PLAN.md` — phased TDD tasks (one module per task; WIRE vs NEW marked)

## Learnings

_(captured during /deepen, /implement, /review)_
