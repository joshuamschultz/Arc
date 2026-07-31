# SPEC-035 — SDD

**Status:** VERIFIED
Traces: PRD REQ-001..025. Pillars: Simplicity → Modularity → Security → Scalability.

---

## 1. Architecture overview

Three confinement floors, each wiring an existing seam into the tool-dispatch path. Nothing here is a new subsystem.

```
                          arcagent (WIRES)                    arctrust (DECIDES)        arcrun (ISOLATES)
                          ────────────────                    ──────────────────        ─────────────────
 write / edit / bash ──▶ is_protected_path (A) ──deny─────────────────────────────────────
                                │
 tool dispatch ─────────▶ SessionCapabilityLedger (B) ──inject──▶ PolicyContext
                                │                                        │
                                │                          GlobalLayer.evaluate (B)
                                │                          └─ ForbiddenCompositionChecker
                                │                                        │ DENY(forbidden_composition)
                                ▼                                        ▼
                          HumanGate (B) ◀── approval-required ── PolicyDenied(rule=forbidden_composition)
                                │
 external comms ────────▶ EgressProxy (B, allowlist + leg signal)
                                │
 bash @ ent/fed ───────▶ arcrun backend call (C) ──────────────────────▶ resolve_execution_backend
                                                                          + workspace bind-mount (NEW)
```

**Concern boundaries (explicit, per CLAUDE.md "don't mix concerns"):**
- **arcagent** owns tool wiring: the protected-path guard, the session capability ledger, the human-gate orchestration, the EgressProxy mediation seam, and the `bash → arcrun backend` call. It does *not* implement isolation or policy-decision algorithms.
- **arctrust** owns the policy *decision*: `GlobalLayer.forbidden_compositions` enforcement and the `PolicyContext` schema extension. It receives resolved frozensets + accumulated legs as injected state (same pattern SPEC-034 set for Provider/Team/Sandbox) and imports no arcagent/arcrun.
- **arcrun** owns execution *isolation*: the tier-routed backend (SPEC-036) and the new workspace bind-mount. It never sources tier or contains agent logic — `tier`/`workspace` arrive as parameters.

---

## 2. Sub-scope A — Protected-path denylist

### 2.1 Component: `is_protected_path` (arcagent.tools._validation)
Add beside the existing `resolve_workspace_path`:

```python
def is_protected_path(resolved: Path, protected: frozenset[Path]) -> bool:
    """True if a resolved, workspace-relative path is operator-protected (read-only to the agent)."""
```

- Consumers: `tools/write.py`, `tools/edit.py`, `builtins/capabilities/write.py`, `builtins/capabilities/edit.py`, and both bash copies (host-bash path only — sandboxed bash gets it via REQ-023 mount).
- Each mutating tool, after `resolve_workspace_path(...)`, calls `is_protected_path` and raises `ToolError(code="TOOL_PROTECTED_PATH")` on match. **One helper, N call-sites — no duplicated rule (REQ-001 AC2).**

### 2.2 Config: `tools.policy.protected_paths` (arcagent.core.config)
`ToolPolicy` (config.py:98) gains `protected_paths: list[str] = []`. Resolved once in `agent_lifecycle` alongside `allowed_paths`, unioned with defaults `{identity.md, policy.md, context.md}`, and handed to the builtin `_runtime` (which already carries `workspace`/`allowed_paths`). `protected_paths` itself is not agent-editable because `arcagent.toml` is outside the workspace / itself protected (REQ-002).

### 2.3 Audit
Denials emit `tool.protected_path.denied` via the existing telemetry/audit sink the tools already have access to (REQ-004).

**Boundary note:** goal-lock is arcagent-local (tool surface). It is *not* a policy layer — the policy pipeline decides tool *calls*; this guards *path arguments*, which is a tool responsibility. Keeping it in `_validation` avoids inflating the arctrust engine.

---

## 3. Sub-scope B — Lethal-trifecta gate

### 3.1 Component: `GlobalLayer.forbidden_compositions` enforcement (arctrust.policy) — make the stub live
`GlobalLayer.evaluate` currently only checks `deny_rules`. Extend it:

```python
union = set(call.capability_tags) | set(ctx.session_capabilities or ())
matched = self._checker.first_forbidden(union)   # ForbiddenCompositionChecker over self._forbidden_compositions
if matched is not None:
    return Decision.deny(layer="global", rule_id="global.forbidden_composition",
                         reason=f"forbidden capability composition {sorted(matched)} …", …)
```

- Requires `ToolCall` to carry `capability_tags` (it does not today — add `capability_tags: frozenset[str] = frozenset()` to the arctrust `ToolCall`; arcagent fills it from the tool's registered tags at dispatch).
- Requires `PolicyContext.session_capabilities: frozenset[str] | None = None` (new optional field — existing constructions unaffected, exactly like SPEC-034's optional-field extension).
- `ForbiddenCompositionChecker` already exists in `arcagent.core.tool_policy`; **move/duplicate the tiny checker into arctrust** (arctrust must not import arcagent) OR inline its 6-line `first_forbidden` into `GlobalLayer`. Prefer inlining — it is a subset test; the arcagent copy is deleted in the same edit (no dead duplicate, per repo standard). (REQ-010)

### 3.2 Component: `SessionCapabilityLedger` (arcagent, session-scoped)
A per-session accumulator: after each ALLOWED call, union that tool's legs into the ledger; on each evaluation, inject the current ledger as `PolicyContext.session_capabilities`. Lives with the session/dispatch wiring (shared-nothing per session). Maps `capability_tags → {private_data, external_comms, untrusted_input}` via a small deployment table in arcagent (REQ-011, REQ-012):

| Leg | Source tags (examples) |
|-----|------------------------|
| `private_data` | `file_read`, `user_profile`, `memory`, `recall` |
| `external_comms` | `network_egress`, `web`, `slack_notify`, `audio`(tts egress) |
| `untrusted_input` | `web`/`browser`/`extract` **content reads** (proxy — see Research Insights + OQ-1) |

### 3.3 Component: `EgressProxy` mediation seam (arcagent.tools._egress) — wire the built class
`EgressProxy` already implements deny-by-default origin allowlist + audit. This spec routes external-comms tools through a shared per-agent `EgressProxy` instance and treats a successful `egress.allowed` as the signal that the `external_comms` leg occurred (feeding the ledger). No change to `EgressProxy`'s logic — only instantiation + injection into the network-touching tools (REQ-013).

### 3.4 Component: `HumanGate` (arcagent)
When dispatch catches `PolicyDenied` with `rule_id="global.forbidden_composition"` and the tier permits approval:
1. Pause the completing call.
2. Emit an approval request (labeled agent-originated, ASI09) through the configured surface (see OQ-3).
3. Block on human decision with a fail-closed timeout.
4. On approval: mint a one-shot approval token (signed by the *human/operator* identity, never the agent DID — REQ-014 AC3), re-dispatch the single call with the token present; `GlobalLayer` honors a valid token to skip the composition deny exactly once (REQ-015).
5. Audit grant/deny/timeout with the human identity + call (REQ-014 AC2).

Federal: no auto-approve. Personal: `auto_approve` config may pre-satisfy named compositions, audited (REQ-016).

**Boundary note:** the *decision* "this composition is forbidden" is arctrust's; the *orchestration* "pause and ask a human" is arcagent's (it owns the loop/dispatch). The token is the clean handoff — arctrust checks token validity, arcagent obtains it.

---

## 4. Sub-scope C — Sandboxed bash (reuses SPEC-036)

### 4.1 How C reuses SPEC-036 (do NOT rebuild)
SPEC-036 already ships in arcrun:
- `resolve_execution_backend(tier, relax, platform_supports_vm)` → `"vm"` (federal) / `"docker"` (enterprise) / `"docker"|"local"` (personal).
- `DockerBackend` / `VmBackend` with `--cap-drop=ALL --security-opt=no-new-privileges --read-only --network=none --tmpfs=/tmp`, and `run_separated()` for stdout/stderr/exit.
- `make_execute_tool(...)` wires `execute_python` through exactly this path with backend-selection audit.

**arcagent's `bash` at enterprise/federal delegates to the same backend** instead of `create_subprocess_shell`. It calls `resolve_execution_backend`/`load_backend` (or a thin arcrun helper mirroring `make_execute_tool`'s shape but running the shell command instead of `python3 -`), passing `tier`, `caller_did`, `audit_sink`, and the workspace to mount. The command runs via `backend.run_separated(command, cwd="/workspace", …)`.

### 4.2 The one real arcrun addition: workspace bind-mount
**Confirmed gap:** `DockerBackend` declares `supports_bind_mount=True` but `_docker_run_detached` mounts nothing except `tmpfs /tmp` — so a container today has **no workspace access at all**. `execute_python` sidesteps this by piping code over stdin into `/tmp`. **bash cannot** — it must read/write workspace files. So reuse requires implementing the declared bind-mount:
- `DockerBackend.__init__` gains an optional `workspace_mount: Path | None`; `_docker_run_detached` adds `-v {workspace}:/workspace:rw` (and mounts protected paths read-only per REQ-023, e.g. `-v {ws}/identity.md:/workspace/identity.md:ro`), sets `--workdir /workspace`. Host `~/.arc/**` is simply never mounted → unreachable (REQ-021).
- `VmBackend` gets the equivalent guest-mount (SPEC-036's VM already isolates the host FS; the workspace is shared in read-write, `~/.arc` excluded).
- This addition lives entirely in arcrun (isolation concern). arcagent passes `workspace=` and the read-only protected subpaths; it does not touch mount flags (REQ-022 AC2).

### 4.3 Personal tier
Personal `bash` keeps `create_subprocess_shell` on the host (the existing code path), explicit and audited, parallel to SPEC-036 `relax=off`. Enterprise/federal cannot relax below the sandbox floor (reuses SPEC-036's `IsolationRelaxationError`). (REQ-024)

### 4.4 Fallback (only if backend routing proves infeasible)
A host-side path-confinement wrapper resolving every path arg and blocking `..`/symlink/abs escapes. **Explicitly inferior:** a shell evades naive path parsing via `$(...)`, env expansion, `eval`, and here-docs. The sandbox mount is the real boundary; the wrapper is a documented fallback, not the design.

---

## 5. Module / file impact

| Package | File | Change | REQ |
|---------|------|--------|-----|
| arcagent | `tools/_validation.py` | add `is_protected_path` | 001 |
| arcagent | `tools/write.py`, `tools/edit.py`, `builtins/capabilities/write.py`, `edit.py` | call the guard | 001 |
| arcagent | `tools/bash.py`, `builtins/capabilities/bash.py` | host-path guard + ent/fed sandbox delegation | 001, 020, 022 |
| arcagent | `core/config.py` | `ToolPolicy.protected_paths` | 002 |
| arcagent | `core/agent_lifecycle.py` | resolve + inject protected set; instantiate EgressProxy + ledger | 002, 012, 013 |
| arcagent | `core/agent.py` | pass `forbidden_compositions` (trifecta set) to `build_pipeline`; wire HumanGate into dispatch | 011, 014 |
| arcagent | new `core/session_internal/capability_ledger.py` | `SessionCapabilityLedger` + tag→leg map | 012 |
| arcagent | new `core/human_gate.py` | `HumanGate` + one-shot token | 014, 015, 016 |
| arctrust | `policy.py` | `GlobalLayer.evaluate` composition check; `ToolCall.capability_tags`; `PolicyContext.session_capabilities`; token honoring | 010, 015 |
| arcagent | `core/tool_policy.py` | delete now-duplicated `ForbiddenCompositionChecker` (inlined into arctrust) | 010 |
| arcrun | `backends/docker.py`, `backends/vm.py` | implement workspace bind-mount (+ ro protected subpaths) | 021, 022, 023 |
| arcrun | `builtins/execute.py` or new thin `run_shell` helper | shell-command entry mirroring `make_execute_tool` | 020, 025 |

---

## 6. Failure modes (fail-closed everywhere)

| Condition | Behavior |
|-----------|----------|
| Protected-path match | Deny + audit (REQ-001/004) |
| Missing `session_capabilities` in ctx | Treated as empty — but arcagent always injects; a *configured* trifecta with a blind ledger is a wiring bug, caught in tests |
| Human-gate timeout / denial | Deny the completing call (REQ-014) |
| Agent attempts self-approval | Rejected — token must be operator/human-signed (REQ-014 AC3) |
| Federal + no VM | Refuse bash (reuse SPEC-036 `IsolationUnavailableError`) (REQ-020 AC2) |
| Sandbox unavailable at enterprise | Fail closed — no silent host fallback (SPEC-036 floor) |

---

## 7. Research Insights (/deepen enrichment)

### A — Goal immutability patterns
- **Read-only config / control-plane vs data-plane split** is the standard pattern: the artifact defining the agent's objective is owned by the control plane (operator) and mounted read-only into the data plane (agent runtime). Mirrors Kubernetes `readOnly: true` volume mounts and immutable ConfigMaps, and OpenClass/least-privilege guidance (NIST AC-6). Our protected-path denylist + REQ-023 read-only mount implements exactly this split.
- **OWASP ASI01 mitigation guidance** explicitly recommends "immutable system prompts / goal files" and "separation of instruction storage from agent-writable memory" — corroborates keeping `identity.md`/`policy.md` operator-authored and agent-unwritable. ASI06 (context poisoning) adds `context.md` integrity to the same protected set.
- **Federal framing:** NIST 800-53 AC-3 (access enforcement) + AC-6 (least privilege) + SI-7 (software/firmware/information integrity) map directly — the goal file is "information" whose integrity is enforced by making it unwritable to the subject it governs.

### B — Lethal-trifecta mitigation
- **Simon Willison's "lethal trifecta"** (2025) is the canonical framing: *access to private data* + *exposure to untrusted content* + *ability to externally communicate* = exfiltration risk; the mitigation is to **break at least one leg** for any given action. Our design breaks the *co-occurrence* (gate when all three accumulate) rather than permanently removing a capability — preserving usefulness while closing the exfil path. Willison's key nuance we honor: the third leg (untrusted content) is about *data provenance*, not a tool — hence OQ-1's taint-proxy caveat.
- **Capability-based egress mediation** (object-capability security; CloudflareD/egress-broker patterns): route all outbound through a single mediating proxy with an explicit origin allowlist, deny-by-default, full audit — precisely what `EgressProxy` already implements. Making it the *sole* external-comms path both enforces the allowlist and gives a reliable "external_comms leg occurred" signal (no side-channel egress).
- **Non-compositional safety** (arXiv:2603.15973, already cited in `tool_policy.py`): two individually-safe capabilities compose into an unsafe outcome; check the *union* of held capabilities against forbidden sets. Our accumulation-across-turns (REQ-012) extends batch-union to session-union, matching how real exfiltration unfolds over multiple tool calls.
- **Human-in-the-loop approval primitives:** the dominant pattern (LangGraph `interrupt`/checkpoints, Anthropic tool-use "human approval" gates, ASI09 guidance) is a *durable pause* — persist state, surface a labeled request, resume only on an authenticated approval that the agent itself cannot issue. Our one-shot operator-signed token (REQ-014/015) implements this with the added federal property that approval authority ≠ the audited subject (consistent with SPEC-053's audit-authority-independence thesis).
- **Federal-friendly:** NIST AC-4 (information flow enforcement) is the controlling control family for the trifecta; the human gate is an AC-4 "human review" flow-control point.

### C — Shell sandboxing approaches
- **Container/VM isolation beats path-parsing** is the consensus: seccomp/namespace containers (gVisor, Docker `--cap-drop=ALL --read-only --network=none`) and microVMs (Firecracker) provide a *filesystem boundary the guest cannot reason around*, whereas allow/deny path regexes are repeatedly defeated by shell metaprogramming (`$(...)`, `eval`, symlinks, `/proc` tricks). SPEC-036 already chose the container/VM path; C simply routes bash through it. This is why the SDD marks the path-regex wrapper as fallback-only.
- **Bind-mount minimality:** mount *only* what the workload needs (the workspace), never the host home or secret stores — standard container-security guidance (CIS Docker Benchmark 5.x). Our `-v {workspace}:/workspace:rw` with `~/.arc` unmounted is the textbook realization; read-only sub-mounts for protected paths (REQ-023) mirror CIS 5.12 (mount root filesystem read-only).
- **Secret isolation / audit-key protection:** keeping signing keys off any surface the workload can read is core to tamper-evident logging (NIST AU-9 "protection of audit information"). SPEC-053 established operator-key independence; C ensures the shell — the widest-reach tool — cannot undo it. Cited jointly as the SPEC-033+053 audit-forgery HIGH-close composite.
- **Firecracker/microVM** for the federal floor matches CLAUDE.md's ASI05 claim ("Firecracker microVM isolation") — SPEC-036's `vm` backend; C inherits it for bash at federal with no extra work beyond the guest workspace mount.

---

## 8. Traceability (REQ → component)

| REQ | Component(s) |
|-----|-------------|
| 001–004 | `is_protected_path`, tool call-sites, `ToolPolicy.protected_paths`, audit event |
| 010, 015 | `GlobalLayer.evaluate` composition check + token honoring, `ToolCall.capability_tags`, `PolicyContext.session_capabilities` |
| 011 | `agent.py` build_pipeline arg + tag→leg map |
| 012 | `SessionCapabilityLedger` |
| 013 | `EgressProxy` instantiation + injection |
| 014, 016 | `HumanGate`, tier config |
| 020, 022, 024, 025 | arcagent bash delegation, arcrun backend routing + audit, personal host path |
| 021, 023 | arcrun workspace bind-mount + read-only protected sub-mounts |

---

## 9. Security-review remediation (post-merge hardening)

Five findings from the SPEC-035 security review were remediated in the same commit.

### 9.1 Goal-lock case-fold / inode bypass (MEDIUM)
`is_protected_path` compared **resolved-path strings**, so on case-insensitive
APFS/NTFS `write(file_path="IDENTITY.md")` slipped past the guard (write/edit are
never sandboxed — this string check is their sole protection at every tier). Now
compares by **inode identity** (`(st_dev, st_ino)`) when the target exists —
defeating case-fold, symlink, and hardlink aliases — and falls back to a
case-normalized path comparison for a to-be-created target.

### 9.2 auto_approve subset match (MEDIUM)
`HumanGate._auto_approvable` used `named.issubset(legs)`; since `legs` is the
tripping union (⊇ the forbidden set), a 2-leg auto-approve entry silently
green-lit the full 3-leg trifecta. Now requires **exact match** (`named == legs`).

### 9.3 Decision-cache stale replay (MEDIUM)
`PolicyPipeline._cache_key` omitted `ctx.session_capabilities`, so a
fixed-argument egress tool ALLOWed under a partial ledger was served the cached
ALLOW after the ledger completed the trifecta — skipping the DENY. The sorted
`session_capabilities` are now folded into the cache key.

### 9.4 Ledger keyed to "" (LOW)
The dispatch ledger key and the egress-proxy leg recording were hardcoded to
`""` (process-global). A `ContextVar` (`current_session_id`) is now bound per
dispatch and read by both, so the ledger is genuinely per-session.

### 9.5 Trifecta gate was INERT — now LIVE (CRITICAL)
Prior state: no built-in tool tagged `external_comms`/`untrusted_input` and
`EgressProxy` had **zero callers**, so the ledger maxed at `{private_data}` and
the gate never fired; the shipped tests used **synthetic tags**. Remediation:
- **`bash` now contributes `untrusted_input`** (via the `subprocess` tag → leg
  map). Deliberately NOT `external_comms`: at ent/fed bash runs `--network=none`,
  so tagging egress would spuriously trip the gate.
- **`EgressProxy` is wired into the sandbox namespace** as the bare name
  `egress` (the only outbound path for agent-authored tools), giving
  `_runtime.egress()` a real caller; `egress.allowed` records the
  `external_comms` leg for the current session.
- A **real end-to-end test** (`tests/security/test_trifecta_e2e.py`) drives a
  real file read + real subprocess + real `EgressProxy` network call and asserts
  the gate FIRES and the human gate engages — replacing the false confidence of
  synthetic tags.

**Honest status:** the trifecta gate is **ARMED and correct** but **dormant for
the current built-in set** — the only leg producers today are `file_read`
(private_data) and `subprocess` (untrusted_input); no built-in emits
`external_comms` on its own. It activates the moment network/egress tools are
added and tagged (SPEC-045 web / SPEC-038 provider tools), or when an
agent-authored tool egresses through the injected proxy. No overclaim: with
only the current built-ins, a production session cannot assemble all three legs
without such a tool.
