# SPEC-035 — PLAN

**Status:** VERIFIED
**Method:** TDD (RED → GREEN → REFACTOR). Each task is scoped to one module and traces REQ → component → task. Tasks within a sub-scope are ordered by dependency. `[P]` = parallelizable with siblings (no shared file).

Legend: **owner package** in brackets. Every task: write failing test first, implement minimal, verify `ruff` + `mypy --strict` + `pytest` clean before check-off.

---

## Phase A — Lock goals (REQ-001..004) · [arcagent]

- [ ] **A1 — `is_protected_path` helper** `[arcagent.tools._validation]`
  RED: test that a resolved path in the protected set returns True; a normal workspace path returns False; case/symlink-normalized.
  GREEN: add `is_protected_path(resolved, protected)`.
  Trace: REQ-001.

- [ ] **A2 — `ToolPolicy.protected_paths` config + resolution** `[arcagent.core.config, core.agent_lifecycle]`
  RED: config with `protected_paths=["secret.md"]` resolves to a frozenset unioned with defaults `{identity.md, policy.md, context.md}`; exposed via builtin `_runtime`.
  GREEN: add field; resolve in `agent_lifecycle` beside `allowed_paths`; store on `_runtime`.
  Trace: REQ-002.

- [ ] **A3 — Guard `write` + `edit` (both copies)** `[arcagent.tools.write/edit, builtins/capabilities/write/edit]`
  RED: `write`/`edit` to `identity.md` raises `TOOL_PROTECTED_PATH`; `read` of it still works; write to a normal file still works.
  GREEN: after `resolve_workspace_path`, call `is_protected_path`, raise on match. [P] across the 4 files.
  Trace: REQ-001, REQ-003.

- [ ] **A4 — Guard host-`bash` path arg (personal)** `[arcagent.tools.bash, builtins/capabilities/bash]`
  RED: personal-tier `bash` that writes a protected path is blocked (best-effort host wrapper — documented as advisory per OQ-2).
  GREEN: pre-scan obvious redirections to protected paths; note limitation in docstring.
  Trace: REQ-001, OQ-2. (Real enforcement for ent/fed comes from Phase C.)

- [ ] **A5 — Audit protected-path denials** `[arcagent tools + telemetry]`
  RED: a denied protected-path op emits `tool.protected_path.denied` with tool, caller DID, path.
  GREEN: emit via the tool's audit sink.
  Trace: REQ-004.

---

## Phase B — Lethal trifecta (REQ-010..016) · [arctrust + arcagent]

- [ ] **B1 — `ToolCall.capability_tags` + `PolicyContext.session_capabilities`** `[arctrust.policy]`
  RED: construct `ToolCall` with tags and `PolicyContext` with `session_capabilities`; existing 3-field/no-tag constructions still valid (defaults).
  GREEN: add `capability_tags: frozenset[str] = frozenset()` and `session_capabilities: frozenset[str] | None = None`.
  Trace: REQ-010, REQ-012.

- [ ] **B2 — `GlobalLayer.forbidden_composition` enforcement** `[arctrust.policy]`
  RED: a `GlobalLayer` with `forbidden_compositions=[{a,b,c}]` DENYs a call whose `capability_tags ∪ session_capabilities ⊇ {a,b,c}` (rule_id `global.forbidden_composition`); allows when the union is missing a leg.
  GREEN: inline the subset check into `evaluate` (delete the arcagent `ForbiddenCompositionChecker` duplicate in the same edit).
  Trace: REQ-010.

- [ ] **B3 — One-shot approval token honored by `GlobalLayer`** `[arctrust.policy]`
  RED: a call carrying a valid operator-signed approval token skips the composition deny exactly once; an agent-DID-signed token is rejected; a second call re-denies.
  GREEN: verify token signature (operator/human key, not agent DID) + one-shot binding to the call hash.
  Trace: REQ-014 AC3, REQ-015.

- [ ] **B4 — `SessionCapabilityLedger` + tag→leg map** `[arcagent.core.session_internal.capability_ledger]`
  RED: after allowed calls carrying `file_read` then `web` then `network_egress`, the ledger yields `{private_data, untrusted_input, external_comms}`; per-session isolated.
  GREEN: implement accumulation + the deployment tag→leg table.
  Trace: REQ-011 (map), REQ-012.

- [ ] **B5 — Inject ledger + pass trifecta set to `build_pipeline`** `[arcagent.core.agent, agent_lifecycle]`
  RED: production `build_pipeline` receives `forbidden_compositions` containing the trifecta set; dispatch injects `session_capabilities` from the ledger each evaluation.
  GREEN: wire ledger into dispatch; pass the set at `agent.py`.
  Trace: REQ-011, REQ-012.

- [ ] **B6 — `EgressProxy` mediation seam** `[arcagent.core.agent_lifecycle, tools._egress consumers]`
  RED: a network-touching tool call to a non-allowlisted origin is denied (`EGRESS_DENIED`) and audited; an allowlisted call records the `external_comms` leg into the ledger.
  GREEN: instantiate one per-agent `EgressProxy`; route external-comms tools through it; feed `egress.allowed` into the ledger.
  Trace: REQ-013.

- [ ] **B7 — `HumanGate` orchestration** `[arcagent.core.human_gate + dispatch]`
  RED: a trifecta-completing call raises approval-required; on human approval a one-shot token is minted and the single call proceeds; on timeout/denial it fails closed; grant/deny/timeout audited with human identity.
  GREEN: catch `PolicyDenied(rule="global.forbidden_composition")`, run the gate, re-dispatch with token.
  Trace: REQ-014, REQ-015.

- [ ] **B8 — Tier stringency for the gate** `[arcagent.core.human_gate, config]`
  RED: federal cannot disable the gate / no auto-approve; personal `auto_approve` config pre-satisfies named compositions and is audited.
  GREEN: tier branch on approval availability.
  Trace: REQ-016.

---

## Phase C — Sandboxed bash (REQ-020..025) · [arcrun + arcagent]

- [ ] **C1 — Workspace bind-mount in `DockerBackend`** `[arcrun.backends.docker]`
  RED: a backend created with `workspace_mount=ws` runs a command that reads/writes a workspace file successfully AND cannot read a host path outside the mount (e.g. a temp file under a fake `~/.arc`); protected sub-paths mount read-only (write fails).
  GREEN: add `workspace_mount` param; `-v {ws}:/workspace:rw`, `--workdir /workspace`, read-only `-v` for protected subpaths; leave `~/.arc` unmounted.
  Trace: REQ-021, REQ-022, REQ-023.

- [ ] **C2 — Workspace mount in `VmBackend`** `[arcrun.backends.vm]`
  RED: VM guest sees the workspace read-write, host `~/.arc` absent, protected paths read-only.
  GREEN: implement the guest workspace share equivalent.
  Trace: REQ-021, REQ-022, REQ-023 (federal).

- [ ] **C3 — Shell entry through the backend** `[arcrun.builtins.execute or new run_shell helper]`
  RED: a `run_shell(command, tier, workspace, …)` routes through `resolve_execution_backend` and returns stdout/stderr/exit; emits `code_exec.backend.selected`; federal-no-VM refuses.
  GREEN: mirror `make_execute_tool`'s selection/audit shape but run the shell command via `run_separated(command, cwd="/workspace")`.
  Trace: REQ-020, REQ-025.

- [ ] **C4 — arcagent `bash` delegates at enterprise/federal** `[arcagent.tools.bash, builtins/capabilities/bash]`
  RED: enterprise/federal `bash("cat ~/.arc/operator/operator.key")` fails (path absent); workspace `bash("echo hi > note.txt")` writes to the host workspace; personal `bash` still runs on host.
  GREEN: branch on tier — ent/fed call arcrun `run_shell` with `workspace=`; personal keeps `create_subprocess_shell`; delete no host path (personal still needs it).
  Trace: REQ-020, REQ-022, REQ-024.

- [ ] **C5 — Relaxation refusal + audit parity** `[arcagent.tools.bash]`
  RED: attempting to relax enterprise/federal bash to host is refused (reuse SPEC-036 `IsolationRelaxationError`); the personal host choice is audited.
  GREEN: reuse SPEC-036 relax semantics; emit the selection/downgrade audit.
  Trace: REQ-024, REQ-025.

---

## Phase D — Cross-cutting verification

- [ ] **D1 — A×C interaction test** `[tests/security]`
  Sandboxed `bash("echo x > identity.md")` at enterprise fails via the read-only protected sub-mount (REQ-023) — goal-lock survives sandboxing.

- [ ] **D2 — Full trifecta E2E** `[tests/e2e]`
  Read private file (turn 1) → fetch untrusted web content (turn 2) → attempt egress (turn 3): the third call trips the gate, human-gate pauses, approval admits exactly one call, a second egress re-triggers. All decisions in the audit chain.

- [ ] **D3 — Audit-forgery negative test** `[tests/security]`
  At enterprise/federal, no `bash` invocation can read the operator seed or write under `.audit/**` (REQ-021) — asserts the SPEC-053 composite is closed on the bash surface.

- [ ] **D4 — Gates green** — `ruff check`, `mypy --strict` (arcagent + arctrust + arcrun), full `pytest` with coverage ≥ thresholds; no new `# type: ignore` without justification; LOC budgets respected (move code, don't raise ceilings).

---

## Traceability matrix (REQ → task)

| REQ | Tasks |
|-----|-------|
| 001 | A1, A3, A4 |
| 002 | A2 |
| 003 | A3, D1 |
| 004 | A5 |
| 010 | B1, B2 |
| 011 | B4, B5 |
| 012 | B1, B4, B5 |
| 013 | B6 |
| 014 | B3, B7 |
| 015 | B3, B7 |
| 016 | B8 |
| 020 | C3, C4 |
| 021 | C1, C2, D3 |
| 022 | C1, C2, C4 |
| 023 | C1, C2, D1 |
| 024 | C4, C5 |
| 025 | C3, C5 |

## Boundary guardrails (do not cross)

- arctrust tasks (B1–B3) add policy **decision** + schema only — no arcagent/arcrun imports.
- arcrun tasks (C1–C3) add isolation/mount only — `tier` + `workspace` are **parameters**, never sourced; no agent logic.
- arcagent tasks **wire**: path guard, ledger, egress, human-gate, bash delegation. No isolation mechanics, no composition algorithm (that's arctrust's inlined subset test).
- Every added/duplicated seam deletes its dead predecessor in the same edit (e.g., arcagent `ForbiddenCompositionChecker` removed when its logic lands in arctrust `GlobalLayer`).
