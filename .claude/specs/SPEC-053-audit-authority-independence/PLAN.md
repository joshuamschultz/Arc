# SPEC-053 — PLAN: Audit-authority independence

**Status:** PENDING
**Method:** TDD (RED → GREEN → REFACTOR) per task. Each task is scoped to **one
package/module**. Every task lists its REQ(s), the failing test to write first,
and the change. Verify with `pytest`, `mypy --strict`, `ruff check` before
marking a task COMPLETE.

Task counts: 12 tasks / 0 complete / 12 remaining.

---

## Phase 1 — arctrust: operator-key custody primitive

### [ ] T-01 — `OperatorKey` primitive (arctrust) — REQ-001, 003, 004, 006
- **Module:** `packages/arctrust/src/arctrust/operator.py` (new); export via `arctrust/__init__.py`.
- **RED:** `tests/test_operator_key.py` — `generate()` yields a valid Ed25519 seed+pubkey; `save()`→`load()` round-trips; `save()` writes `0600` file / `0700` dir; `load(missing, generate_if_absent=True)` bootstraps; `OperatorKey` has **no** `sign`/`did` attribute (assert `not hasattr`).
- **GREEN:** implement over `arctrust.keypair.KeyPair.from_seed` / `generate_keypair`, mirroring `AgentIdentity.save_keys` permission logic. No new crypto.
- **Boundary check:** grep asserts `operator.py` imports only `arctrust.keypair` + stdlib.

### [ ] T-02 — vault-resolver seam on `OperatorKey.load` — REQ-005, 011
- **Module:** `arctrust/operator.py`.
- **RED:** `load(path, vault_resolver=fake)` reads the key via the resolver and ignores the file; `load(path, vault_resolver=None)` reads the file. Fake resolver mirrors `AgentIdentity._load_from_vault` shape (`resolve_secret(path, id)`).
- **GREEN:** add the `vault_resolver` branch. Document on-disk as interim (CLAUDE.md tension) in the docstring.

### [ ] T-03 — `verify_chain` uses operator pubkey, rejects agent key — REQ-001, 007
- **Module:** `arctrust/audit.py` (test-only; no prod change expected — the sink already verifies with the operator pubkey).
- **RED:** `tests/test_audit.py::test_chain_signed_by_operator_not_agent` — build a `WormSink` with an operator seed, write records, assert `verify_chain(path, operator_pub)` is `True` and `verify_chain(path, agent_pub)` is `False`.
- **GREEN:** if the assertion already holds, this task **locks** existing behavior (no code); if a doc/comment implies agent signing, fix it here.

## Phase 2 — arctrust: federal witness anchor

### [ ] T-04 — `WitnessAnchor` Protocol + `AppendOnlyMediumWitness` — REQ-009, 010
- **Module:** `packages/arctrust/src/arctrust/witness.py` (new); export via `__init__`.
- **RED:** `tests/test_witness.py` — an `AppendOnlyMediumWitness` `submit(checkpoint, sig)` appends to a second custodied file and returns a proof; `verify_inclusion` returns `True` for a submitted head, `False` for a head never submitted; Protocol conformance test.
- **GREEN:** implement Protocol + append-only medium impl. Reuse `_canonical_event_hash`/`read_verified_anchor` shapes; add **no** new anchor format.
- **Boundary check:** `witness.py` imports only arctrust + stdlib.

### [ ] T-05 — rollback-detection round-trip test — REQ-009
- **Module:** `arctrust` integration test.
- **RED:** operator-sign a chain → `read_verified_anchor` head → submit to witness → truncate+re-sign the chain past the anchor with the operator key → assert `verify_against_anchor` (arcllm) against the witnessed head **fails** (forgery caught even with the operator key). (Cross-package read-only usage of `arcllm.trace_retention.verify_against_anchor`; keep the test in whichever package can import both, likely arcagent integration — note if so.)
- **GREEN:** wire the witness read path; ensure the checkpoint payload is the existing `build_checkpoint` dict.

> Note the online `TransparencyLogWitness` (Rekor) is **REQ-010 Should**; if
> descoped from this phase, leave a stub + xfail test and record it in README
> Open Questions. Air-gapped medium is the Must-have path.

## Phase 3 — arcagent: rewire the three WORM sinks (replace, no shim)

### [ ] T-06 — config fields for operator key — REQ-004, 005
- **Module:** `packages/arcagent/src/arcagent/core/config.py` (`SecurityConfig`).
- **RED:** `tests/unit/core/test_config.py` — `operator_key_dir` defaults to `~/.arc/operator`, `operator_vault_path` defaults `""`; both round-trip through TOML load.
- **GREEN:** add the two Pydantic fields with docstrings naming SPEC-053/037.

### [ ] T-07 — load operator key read-only at startup — REQ-002, 004, 006
- **Module:** `arcagent/core/agent.py` (`startup`).
- **RED:** `tests/unit/core/test_agent.py::test_operator_key_loaded_outside_workspace` — after `startup`, the agent holds an `OperatorKey`; its resolved path is **not** under `self._workspace`; personal tier with no key auto-bootstraps one.
- **GREEN:** add `self._operator_key = OperatorKey.load(self._config.security.operator_key_dir, vault_resolver=self._vault_resolver)` between identity (step 3) and tool registry (step 5).

### [ ] T-08 — rewire policy WORM sink — REQ-002, 008, 012
- **Module:** `arcagent/core/agent.py:182`.
- **RED:** `test_agent.py::test_policy_chain_signed_by_operator_not_agent` — the policy chain written during a dispatch verifies under the operator pubkey and **fails** under the agent DID pubkey; grep test asserts `signing_seed` is absent from the `WormSink(` construction.
- **GREEN:** `WormSink(self._policy_audit_log_path(), self._operator_key.seed)`; **delete** the "signs records with its own DID seed" comment (lines 180-181). No fallback branch.

### [ ] T-09 — rewire skill-improver audit sink (keep skill-signing on DID) — REQ-002, 008
- **Module:** `arcagent/modules/skill_improver/_runtime.py`.
- **RED:** `tests/.../test_skill_improver_runtime.py` — the improver **audit chain** verifies under the operator key; the **mutated-skill signature** (SPEC-033 D3) still uses the agent DID (assert both, to prove the split is intentional and correct).
- **GREEN:** `_build_worm_sink` takes the operator key (thread it from `configure(...)`); `_resolve_signer(identity)` remains ONLY for skill signing. Delete the agent-seed audit path.

### [ ] T-10 — operator-sign the trace checkpoint sink — REQ-002, 008, 009
- **Module:** `arcagent/core/model_manager.py`.
- **RED:** `tests/.../test_model_manager.py` — when a `checkpoint_sink` is wired, checkpoints land in an operator-signed `WormSink`; `read_verified_anchor(chain, operator_pub)` returns the head; federal wires the witness (assert `submit` called).
- **GREEN:** build the operator-signed `WormSink` for checkpoints; wire `JSONLTraceStore(checkpoint_sink=emit-through-operator-worm)`; federal path adds the witness from Phase 2.

## Phase 4 — arccli: generate operator key at init

### [ ] T-11 — `arc init` generates operator key — REQ-003, 015
- **Module:** `packages/arccli/src/arccli/commands/init.py` (`_init`).
- **RED:** `tests/test_cli_init_smoke.py::test_init_creates_operator_key` — after `arc init --tier personal --dir <tmp>`, `<tmp>/operator/operator.key` exists at `0600`; idempotent on re-run; enterprise/federal prints the operator pubkey fingerprint.
- **GREEN:** `OperatorKey.generate().save(arc_dir/"operator"/"operator.key")` when absent; delegate all crypto to arctrust (no key logic in arccli).

## Phase 5 — arctrust.policy hardenings (Findings 2 + 6)

### [ ] T-12 — authenticate before short-circuits + identity-bound cache key — REQ-013, 014
- **Module:** `packages/arctrust/src/arctrust/policy.py` (`PolicyPipeline.evaluate`, `_cache_key`).
- **RED:** `tests/test_policy.py` —
  - Finding 6: in restricted mode, an **unsigned** safe-set call → DENY `identity.unsigned_or_invalid`; a signed safe-set call → ALLOW.
  - Finding 2a: cache an ALLOW for a signed call, then an unsigned call with identical `(tool_name, arguments, agent_did, classification)` → no cache hit, denied by identity.
  - Finding 2b: a de-registered agent's replay within TTL → DENY (not cached ALLOW).
- **GREEN:** run identity verification first in `evaluate` (before `_check_restricted` and `_cache_get`); add `sig_fingerprint = sha256(call.signature or b"")[:16]` to `_cache_key`. Preserve first-DENY-wins / fail-closed / shadow / TTL. Keep the change minimal (one reordering + one key field).

---

## Cross-cutting verification (run before spec close)

- [ ] **Import boundary (REQ-015):** `arctrust` imports none of arcagent/arcllm/arcrun/arcteam (grep + existing boundary test).
- [ ] **No agent-seed WORM path (REQ-008):** grep for `signing_seed` near `WormSink(` returns nothing.
- [ ] **Tier stringency (REQ-012):** personal/enterprise/federal all produce operator-signed chains; only witness/vault are tier-conditional.
- [ ] `mypy --strict`, `ruff check` clean across arctrust/arcagent/arccli; full suites green.

## Traceability (REQ → task)

| REQ | Task(s) |
|-----|---------|
| 001 | T-01, T-03 |
| 002 | T-07, T-08, T-09, T-10 |
| 003 | T-11 |
| 004 | T-01, T-06, T-07 |
| 005 | T-02, T-06 |
| 006 | T-01, T-07 |
| 007 | T-03 |
| 008 | T-08, T-09, T-10 |
| 009 | T-04, T-05, T-10 |
| 010 | T-04 |
| 011 | T-02 |
| 012 | T-08, T-10 |
| 013 | T-12 |
| 014 | T-12 |
| 015 | T-01, T-04, T-11 |

## Notes for the implementer

- `WormSink` already accepts `operator_private_key` and verifies with the derived
  operator pubkey — **do not** re-architect the sink; this is a wiring + custody
  spec. Most tasks are small.
- The skill-improver split (T-09) is the easy thing to get wrong: **audit chain
  → operator key; mutated-skill signature → agent DID.** They are different
  attestations with different authorities.
- Local-only repo: replace old wiring outright (T-08/09/10). No compat flag, no
  fallback, no deprecation shim (repo rule).
