# SPEC-035 — PRD

**Status:** VERIFIED
**Owner:** Josh (product owner)
**Format:** EARS acceptance criteria · monotonic `REQ-NNN` · MoSCoW · every requirement tied to a principled-coder pillar and an OWASP threat.

Steering refs: CLAUDE.md Four Pillars (Identity/Sign/Authorize/Audit), ADR-019 (tier = stringency), OWASP ASI01/ASI05/ASI06/ASI09 + LLM06, NIST AC-3/AC-6/AU-9/AU-10/SC-7.

---

## Problem statement

An Arc agent can (1) rewrite its own goal file (`identity.md`), (2) read private data + ingest untrusted input + egress it in a single turn with no gate, and (3) `bash`-read the operator audit-signing key and forge its WORM chain. Each is a confirmed, unenforced CLAUDE.md security claim. This PRD specifies the confinement floors that make the claims true, split across three sub-scopes (A goal-lock, B trifecta, C bash) that share the theme "confine what the agent can touch."

---

## Sub-scope A — Lock goals (ASI01 goal hijack, ASI06 context poisoning)

Goal/policy files are operator-authored and must be read-only to the agent's own mutating tools at **every tier**.

### REQ-001 — Protected-path denylist (Must)
**Pillar:** Security · Simplicity **Threat:** ASI01
The agent's mutating tools shall refuse to modify a protected path.
- **EARS:** When `write`, `edit`, or `bash` attempts to create, overwrite, append to, delete, move, or change permissions of a file whose resolved path matches the protected-path set, the tool shall deny the operation and return a structured `TOOL_PROTECTED_PATH` error naming the path.
- **AC1 (Security):** `write`/`edit` targeting `workspace/identity.md`, `workspace/policy.md`, or `workspace/context.md` are denied; a read of the same file still succeeds.
- **AC2 (Simplicity):** All three tools consult one shared `is_protected_path` helper; no per-tool copy of the rule.

### REQ-002 — Protected set is operator-declared with secure defaults (Must)
**Pillar:** Security · Modularity **Threat:** ASI01
The protected set shall default to the goal-bearing files and be extendable only by the operator via config, never by the agent.
- **EARS:** The system shall load the protected-path set from operator config (`tools.policy.protected_paths`) unioned with the built-in defaults (`identity.md`, `policy.md`, `context.md`); the set shall be resolved once at agent start and be immutable for the session.
- **AC1:** With no config, the three default files are protected. Adding a path to `protected_paths` protects it; the agent has no tool that can edit `arcagent.toml`'s `protected_paths` (it is itself a protected surface).
- **AC2 (Modularity):** The helper lives beside `resolve_workspace_path` in `arcagent.tools._validation`; policy engine untouched.

### REQ-003 — Goal-lock holds at every tier (Must)
**Pillar:** Security **Threat:** ASI01, ASI06
Goal-lock shall not be a tier-gated feature (ADR-019).
- **EARS:** While the agent runs at personal, enterprise, or federal tier, an attempt by the agent's mutating tools to modify a protected path shall be denied identically.
- **AC1:** The same denial fires at all three tiers in tests. (Personal-tier host-bash caveat is noted in Open Questions — see OQ-2.)

### REQ-004 — Protected-path denial is audited (Must)
**Pillar:** Security **Threat:** ASI01, ASI06
- **EARS:** When a protected-path modification is denied, the system shall emit an audit event (`tool.protected_path.denied`) carrying the tool name, caller DID, and target path.
- **AC1 (Audit pillar):** A denied `edit` on `identity.md` produces an audit record with the four Pillars' identity + reason fields.

---

## Sub-scope B — Break the lethal trifecta (LLM06 excessive agency, ASI09 human-agent trust; roots in LLM01/LLM02)

Private-data-read + external-comms + untrusted-input must never complete in one turn without explicit human approval.

### REQ-010 — Forbidden-composition enforcement is live (Must)
**Pillar:** Security · Simplicity **Threat:** LLM06
`GlobalLayer.forbidden_compositions` shall be evaluated, not merely stored.
- **EARS:** When `GlobalLayer.evaluate` runs, the system shall union the call's `capability_tags` with `PolicyContext.session_capabilities` and, if any configured forbidden set is a subset of that union, return DENY with `rule_id="global.forbidden_composition"` naming the matched set.
- **AC1 (Security):** A call that would complete the trifecta is DENIED; the same call in isolation (legs not yet accumulated) is ALLOWED.
- **AC2 (Simplicity):** Enforcement reuses the existing `ForbiddenCompositionChecker.first_forbidden`; `GlobalLayer` gains no new algorithm.

### REQ-011 — The trifecta set is configured into production (Must)
**Pillar:** Security · Modularity **Threat:** LLM06
- **EARS:** The system shall pass the trifecta forbidden set (`{private_data, external_comms, untrusted_input}`) into `build_pipeline(forbidden_compositions=…)` at `agent.py`, and shall map built-in tool `capability_tags` to those three legs.
- **AC1:** `build_pipeline` in production receives a non-empty `forbidden_compositions`; a test asserts the trifecta set is present.
- **AC2 (Modularity):** The tag→leg mapping lives in arcagent (deployment knowledge); arctrust receives only the resolved frozensets.

### REQ-012 — Session-scoped capability accumulation (Must)
**Pillar:** Security **Threat:** LLM06, LLM02
The trifecta is a cross-call accumulation within a turn/session, not a single-batch union.
- **EARS:** While a session is active, the system shall accumulate the capability legs of every allowed tool call and inject the accumulated set as `PolicyContext.session_capabilities` on each subsequent evaluation.
- **AC1:** Read-private (turn 1) then egress (turn 3) trips the gate on turn 3, even though neither call alone carries all three legs.
- **AC2:** The accumulator is per-session (shared-nothing across agents/sessions).

### REQ-013 — EgressProxy mediates external comms (Must)
**Pillar:** Security · Modularity **Threat:** LLM02, LLM06
External-comms tools shall reach the network only through `EgressProxy`, making egress both allowlist-gated and observable as the trifecta's second leg.
- **EARS:** When a tool performs outbound network communication, the request shall pass through `EgressProxy`, which denies non-allowlisted origins and records the call as the `external_comms` leg.
- **AC1 (Security):** A non-allowlisted origin is denied (`EGRESS_DENIED`) and audited on both allow and deny paths.
- **AC2 (Modularity):** `EgressProxy` (arcagent.tools._egress) is the single mediation point; no tool opens its own socket.

### REQ-014 — Human-gate on trifecta completion (Must)
**Pillar:** Security **Threat:** ASI09, LLM06
A trifecta-completing action shall pause for explicit human approval, not silently deny or silently allow.
- **EARS:** When the forbidden-composition check fires and the tier permits approval, the system shall halt dispatch of the completing call, surface an approval request labeled as agent-originated, and proceed only upon explicit human approval; on denial or timeout it shall fail closed (deny).
- **AC1 (Security):** No completing action proceeds without a recorded human approval token.
- **AC2 (Audit pillar):** The approval decision (grant/deny/timeout), the human identity, and the completing call are written to the audit chain.
- **AC3 (ASI09):** The approval request is labeled agent-originated; the agent cannot self-approve (its own DID cannot mint an approval token).

### REQ-015 — Approval admits exactly one completing action (Should)
**Pillar:** Security **Threat:** LLM06
- **EARS:** When a human approval is granted, the system shall admit exactly the one approved call; a subsequent trifecta-completing call shall require a new approval.
- **AC1:** A second completing call after a one-shot approval re-triggers the gate.

### REQ-016 — Trifecta gate holds at every tier; approval availability is stringency (Should)
**Pillar:** Security **Threat:** LLM06 **(ADR-019)**
- **EARS:** The forbidden-composition check shall run at every tier; federal shall treat a completed trifecta as hard-deny-unless-approved with no relaxation, while personal may configure auto-approve for named low-risk compositions.
- **AC1:** Federal cannot disable the gate; personal auto-approve is explicit config and audited.

---

## Sub-scope C — Confine bash / subprocess (ASI05 RCE + SPEC-053 audit-forgery composite + ASI03 privilege abuse)

At enterprise/federal the agent's shell must not read or write outside its workspace — specifically not `~/.arc/operator/**` or `.audit/**`.

### REQ-020 — Sandboxed bash at enterprise/federal (Must)
**Pillar:** Security **Threat:** ASI05, ASI03, SPEC-053 composite
`bash` shall execute inside SPEC-036's tier-routed isolation backend at enterprise/federal.
- **EARS:** When the agent invokes `bash` at enterprise or federal tier, the command shall run through arcrun's `resolve_execution_backend(tier, …)` backend (enterprise→container, federal→VM), not a host `create_subprocess_shell`.
- **AC1 (Security):** From sandboxed bash, `cat ~/.arc/operator/operator.key` fails (path absent); `cat /etc/passwd` returns the container's file, not the host's.
- **AC2:** Federal with no VM support refuses (fail closed), consistent with SPEC-036.

### REQ-021 — Audit key + WORM chains are unreachable from bash (Must)
**Pillar:** Security **Threat:** SPEC-053 composite (AU-9/AU-10)
- **EARS:** While bash runs sandboxed, the host `~/.arc/operator/**` (operator signing seed) and `.audit/**` WORM chains shall not be present in the execution filesystem.
- **AC1:** A bash attempt to read or write any path under `~/.arc/operator` or `.audit` from the sandbox returns "no such file"; a test asserts the operator seed cannot be exfiltrated.

### REQ-022 — Sandboxed bash retains workspace access (Must)
**Pillar:** Simplicity · Security **Threat:** — (usability floor for the confinement to be adoptable)
The confinement must not break the agent's normal workflow: bash still edits/builds/tests in the workspace.
- **EARS:** When bash runs sandboxed, the agent's workspace shall be bind-mounted read-write into the execution environment and be the working directory; paths outside the workspace mount shall be inaccessible.
- **AC1:** `bash("echo hi > note.txt")` in the workspace succeeds and the file appears in the host workspace; `bash("cat /Users/**/some-other-project")` fails.
- **AC2 (Modularity):** The bind-mount is implemented in arcrun's backend (honoring its already-declared `supports_bind_mount`); arcagent passes the workspace path, not mount mechanics.

### REQ-023 — Protected paths remain read-only inside the sandbox (Must)
**Pillar:** Security **Threat:** ASI01 (A×C interaction)
Sub-scope A's goal-lock must survive sandboxing: bash inside the workspace mount still cannot rewrite `identity.md`.
- **EARS:** When bash runs sandboxed with the workspace mounted, protected paths within the workspace shall be mounted read-only (or otherwise made unwritable) so the shell cannot bypass REQ-001 via the mount.
- **AC1:** Sandboxed `bash("echo x > identity.md")` fails with a permission/denied error.

### REQ-024 — Personal tier keeps host bash, explicitly (Should)
**Pillar:** Scalability · Security **Threat:** — (ADR-019 stringency)
- **EARS:** While at personal tier, `bash` shall run on the host by default (parallel to SPEC-036 sandbox-off); relaxing enterprise/federal below their sandbox floor shall be refused.
- **AC1:** Personal bash reaches the host machine; enterprise/federal cannot be relaxed to host bash. The choice is audited.

### REQ-025 — Sandbox routing + refusals are audited (Must)
**Pillar:** Security (Audit pillar) **Threat:** ASI05, SPEC-053 composite
- **EARS:** When bash resolves an execution backend, the system shall emit the backend-selection audit event (reusing SPEC-036's `code_exec.backend.selected`), including tier, resolved backend, and any refusal.
- **AC1:** A federal-no-VM refusal and an enterprise container selection each produce an audit record.

---

## MoSCoW summary

| Priority | Requirements |
|----------|--------------|
| **Must** | REQ-001, 002, 003, 004, 010, 011, 012, 013, 014, 020, 021, 022, 023, 025 |
| **Should** | REQ-015, 016, 024 |
| **Could** | Full data-provenance taint tracking for the untrusted-input leg (deferred — see SDD Research Insights + OQ-1); a standing per-session approval grant UX (OQ-3). |
| **Won't (this spec)** | New isolation backend mechanics (SPEC-036); WORM chain internals (SPEC-053); Provider/Team/Sandbox layers (SPEC-034); mTLS/NATS inter-agent comms. |

## Threat-mapping table

| Sub-scope | Requirements | OWASP / compliance |
|-----------|-------------|--------------------|
| A goal-lock | REQ-001..004 | ASI01 (goal hijack), ASI06 (context poisoning); NIST AC-3, AC-6 |
| B trifecta | REQ-010..016 | LLM06 (excessive agency), ASI09 (human-agent trust), LLM01/LLM02 (roots); NIST AC-4 |
| C bash confine | REQ-020..025 | ASI05 (RCE), ASI03 (privilege abuse), **SPEC-053 audit-forgery composite** (AU-9/AU-10); NIST SC-7, AU-9 |

## Open questions (product owner)

- **OQ-1 (untrusted-input leg):** The third trifecta leg — "untrusted input" — is a *data-provenance* property, not a tool capability. Capability tags cleanly give us two legs (private-data-read, external-comms). Proposed MVP: treat "read of web/browser/externally-fetched content this session" as the untrusted-input proxy, deferring full taint tracking. **Confirm the proxy is acceptable for now?**
- **OQ-2 (personal-tier bash vs goal-lock):** At personal, bash runs on the host (REQ-024), so a determined agent could bypass the REQ-001 protected-path denylist by shelling `echo x > identity.md`. Goal-lock is "every tier" (REQ-003) but is only *advisory* at personal without sandboxed bash. **Accept advisory-at-personal, or also apply the path guard inside personal host-bash (adds a shell-parsing wrapper that a shell can partly evade)?**
- **OQ-3 (human-gate UX):** Where does the approval request surface — arcui prompt, blocking CLI prompt, or an arcteam human channel — and what is the default timeout behavior (recommend fail-closed deny)? Is approval per-action (REQ-015) only, or should a standing per-session grant be offered?
