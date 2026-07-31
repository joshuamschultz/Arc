# SPEC-035 — Lock goals + lethal trifecta + confine bash

**Feature:** Three confinement floors on "what the agent can touch": (A) make the agent's own goal/policy files read-only to every mutating tool, (B) break the lethal trifecta by detecting + human-gating any turn that combines private-data-read, external-comms, and untrusted-input, and (C) route `bash` through the SPEC-036 sandbox at enterprise/federal so the shell cannot reach `~/.arc/operator/**` or the `.audit/**` WORM chains.
**Status:** VERIFIED
**Branch:** `feat/SPEC-035-lock-goals-lethal-trifecta`
**Type:** Generic (tool-dispatch confinement + policy composition wiring + sandbox reuse)
**Confidence:** High — every gap is confirmed at file:line; every fix reuses an existing seam (SPEC-036 sandbox, `forbidden_compositions`, `ForbiddenCompositionChecker`, `EgressProxy`, `resolve_workspace_path`) rather than building a parallel mechanism.
**Depends on / references:** SPEC-036 (code-exec sandbox — bash reuses its tier-routed backend), SPEC-053 (audit-authority independence — C is the bash half of its audit-forgery HIGH-close composite), SPEC-034 (policy pipeline — B extends `PolicyContext` + `GlobalLayer` the same way), SPEC-033 (sign pillar), ADR-019 (tier = stringency, not gates), CLAUDE.md Four Pillars + OWASP ASI01/ASI05/ASI06/ASI09, LLM06.

---

## One-liner

The agent can today overwrite its own `workspace/identity.md` (goal file) via `write`/`edit`/`bash`; can read private data, ingest untrusted web content, and egress it in the same turn with nothing stopping it (`GlobalLayer.forbidden_compositions` is stored but never read); and `bash` runs `create_subprocess_shell(cmd, cwd=workspace)` with **cwd as the only scoping**, so `bash("cat ~/.arc/operator/operator.key")` steals the audit-signing seed and forges the WORM log. SPEC-035 closes all three by **wiring existing seams**: a protected-path denylist on the mutating tools, the already-defined `ForbiddenCompositionChecker` + `EgressProxy` + a new human-gate for the trifecta, and routing `bash` through SPEC-036's sandbox (which mounts only the workspace, so host `~/.arc` is simply absent).

## Why (the problem)

Three CLAUDE.md security claims are asserted but not enforced:

- **ASI01 "Immutable goal definitions in identity.md (read-only to agent)."** `context.py:33` reads `workspace/identity.md` + `context.md` every turn as the agent's goal/behavioral spec, but those files live *inside* the workspace, and `write`/`edit` confine to the workspace via `resolve_workspace_path` — so they are writable. `bash` has no path confinement at all. The agent can rewrite its own goals. **No protected-path denylist exists.**
- **"Break the Lethal Trifecta: private data + external comms + untrusted input never coexist without human approval."** `arctrust.policy.GlobalLayer.__init__` accepts `forbidden_compositions` and stores it (`policy.py:353`) but `evaluate()` never references it; `build_pipeline` accepts the param but `agent.py:280` never passes it → **empty, unenforced stub.** `ForbiddenCompositionChecker` (`tool_policy.py:85`) is a real, tested class whose docstring says "used by tool_registry to reject" but it is **instantiated nowhere.** `EgressProxy` (`_egress.py`) is a real per-tool HTTP allowlist gate (SPEC-017) but is **not wired into the built-in tool-dispatch path.** No human-approval primitive exists anywhere in dispatch.
- **ASI05 / audit-forgery composite (SPEC-053).** `tools/bash.py:46` and `builtins/capabilities/bash.py:29` both call `create_subprocess_shell(command, cwd=str(ws))`. cwd is cosmetic — the process sees the whole host filesystem. `bash("cat ~/.arc/operator/operator.key")` exfiltrates the operator seed that signs every WORM chain (SPEC-053), letting the agent forge its own tamper-evident audit log. This is the bash half of the SPEC-033+053 audit-forgery HIGH finding.

## Decision

**Reuse every existing seam; add the smallest confinement that makes each claim true. Delete nothing that works; wire what is already built.**

- **A — protected-path denylist.** One shared resolver-level check (`is_protected_path`) that `write`, `edit`, and `bash` consult before any mutation. Goal-bearing files (`identity.md`, `policy.md`, `context.md`, and any operator-declared path) are read-only to the agent at **every tier** (ADR-019: goal-lock is universal). Reads still succeed — the files are operator-authored and operator-edited out-of-band.
- **B — trifecta gate.** Populate `forbidden_compositions` with the real trifecta set and make `GlobalLayer.evaluate` actually run the `ForbiddenCompositionChecker` over the **session-accumulated** capability legs (carried on `PolicyContext`, filled by arcagent — same injected-state pattern SPEC-034 established for the other layers). `EgressProxy` becomes the mediation point for the external-comms leg. When the check fires, a **human-gate** primitive pauses dispatch for explicit approval; approval is audited (ASI09 — labeled, never impersonated) and lets exactly one completing action through.
- **C — sandboxed bash.** At enterprise/federal, `bash` executes through SPEC-036's `resolve_execution_backend(tier, …)` backend, which mounts **only the workspace** — the host `~/.arc/operator/**` and `.audit/**` are not present in the container/VM, so they are unreachable by construction. Personal keeps host bash (explicit relax, parallel to SPEC-036 sandbox-off). The one real arcrun addition is a workspace bind-mount on the backend (declared `supports_bind_mount=True` but not implemented today).

Rationale: A is a 3-line guard shared by 3 tools (Simplicity). B makes a stored-but-dead field live and wires two already-built classes — no new policy engine (Modularity: arctrust owns the decision + schema, arcagent injects state + runs the human-gate). C reuses the sandbox rather than inventing path-regex confinement a shell can evade (Security: true isolation is the boundary). Every gate is O(1) over injected state or a set membership test (Scalability).

## Scope (this spec)

1. **Protected-path denylist** — shared `is_protected_path` guard consulted by `write`, `edit`, `bash` (both `tools/` and `builtins/capabilities/` copies). Config-declared protected set with sane defaults.
2. **`GlobalLayer.forbidden_compositions` enforcement** — make `evaluate()` union the call's `capability_tags` with `PolicyContext.session_capabilities` and DENY on any forbidden subset, using `ForbiddenCompositionChecker`.
3. **Session capability accumulation** — arcagent tracks legs seen this turn/session and injects them on `PolicyContext`; `build_pipeline` receives the trifecta `forbidden_compositions`.
4. **EgressProxy mediation seam** — external-comms tools route through `EgressProxy`, making egress both allowlist-gated and observable as the trifecta's second leg.
5. **Human-gate primitive** — a distinct "approval required" outcome that pauses dispatch, surfaces to the human, audits the decision, and admits one completing action on explicit approval; fail-closed on timeout/denial.
6. **Sandboxed bash (enterprise/federal)** — arcagent `bash` delegates to arcrun's tier-routed backend; arcrun backend gains a **workspace-only bind-mount**; personal relaxes to host bash explicitly.

**Out of scope (owned elsewhere — referenced, not duplicated):**
- The isolation backend mechanics + tier routing — **SPEC-036** (this spec mounts the workspace into it and calls it for bash).
- The WORM chain + operator-key independence — **SPEC-053** (C protects the key SPEC-053 relies on).
- Provider/Team/Sandbox policy layers — **SPEC-034**.
- Full data-provenance taint tracking for the untrusted-input leg — a fuller design is deferred; this spec uses a capability-tag proxy (see Open Questions).

## Principled-coder pillars

1. **Simplicity** — one shared path guard for 3 tools; one stored field made live; bash delegates to a backend that already exists. No new engine.
2. **Modularity** — arcagent wires (path guard, session accumulation, human-gate, bash→sandbox call); arctrust owns the policy decision + `PolicyContext` schema; arcrun owns isolation + the mount. No package reaches across its boundary.
3. **Security** — goal files unwritable at every tier; trifecta broken by construction with a human gate; bash cannot read the audit seed at enterprise/federal. Fail-closed throughout (missing approval = deny; unavailable federal isolation = refuse).
4. **Scalability** — set-membership + subset checks, no I/O in the policy hot path; the sandbox path is the same long-lived per-agent container SPEC-036 already amortizes.

## Documents

- [PRD.md](./PRD.md) — EARS requirements, MoSCoW, threat mapping, pillar-tied acceptance criteria across A/B/C.
- [SDD.md](./SDD.md) — components, module boundaries, SPEC-036 reuse, Research Insights.
- [PLAN.md](./PLAN.md) — TDD tasks, one module each, REQ→component→task traceable.
