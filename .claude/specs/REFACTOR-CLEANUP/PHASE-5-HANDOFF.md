# Phase 5 — Decomposition: COMPLETE

> All 12 splits committed. Phase 5 is done.

## What's done (12 of 12 splits, committed)

| § | File | Result | Commit |
|---|---|---|---|
| 8.13 | `arcrun/backends/loader.py` | 617 → 226 LOC + `_verifier.py` (229) + `_manifest.py` (29) + `_audit.py` (143) | `9312bc0` |
| 8.11 | `arcllm/modules/telemetry.py` | 620 → 495 LOC + `telemetry_budget.py` (128) + `telemetry_cost.py` (41) | `25ca191` |
| 8.12 | `arcllm/trace_store.py` | 550 → 421 LOC + `trace_query.py` (222) | `c2b2a4c` |
| 8.10 | `arcgateway/pairing.py` | 901 → 877 LOC + `pairing_postgres.py` (29) | `0825b70` |
| 8.8 | `arcskill/hub/dry_run.py` | 902 → 276 LOC + `_firecracker.py` (568) + `_docker.py` (91) + `_result.py` (50) | `83770d4` |
| 8.9 | `arcskill/hub/scanner.py` | 918 → 372 LOC + `_findings.py` (61) + `_secret_patterns.py` (478) + `_ast_scanner.py` (92) | `b30168b` |
| 8.3 | `arcagent/core/tool_registry.py` | 651 → 406 LOC + `tool_transport.py` (203) + `tool_policy_bridge.py` (118) | `492eb48` |
| 8.2 | `arcagent/orchestration/spawn.py` | 992 → 859 LOC + `token_budget.py` (87) + `spawn_handle.py` (84) | `a534888` |
| 8.1 | `arcagent/core/agent.py` | 1146 → **400** LOC + `agent_handle.py` (161) + `agent_lifecycle.py` (289) + `agent_dispatch.py` (298) + `vault_resolver.py` (68) + `model_manager.py` (138) | `6083131` |
| 8.5 | `arccli/commands/agent.py` | 1546 LOC → 17-file subpackage (`__init__`, `_dispatch`, `_common`, `create`, `status`, `skills`, `extensions`, `sessions`, `build`, `tools`, `config`, `reload`, `strategies`, `events`, `run`, `serve`, `chat`) | `5234eac` |
| 8.6 | `arcui/routes/agent_detail.py` | 952 LOC → 8-file subpackage (`__init__`, `_common`, `config`, `skills`, `tools`, `sessions`, `telemetry`, `policy`) | `93a3b2e` |
| 8.7 | `arcui/static/assets/agent-detail.js` | 1408 LOC → 7 IIFE sibling JS files (`-shared`, `-timeline`, `-modules`, `-files`, `-audit`, `-policy`, slim entry); index.html updated | `7d4bde3` |

Each verified ruff clean + tests match HEAD baseline:
- arcllm: 893 passed
- arcgateway: 744 passed
- arcskill: 338 passed
- arcagent: 3375 passed (3 pre-existing `*_real_llm` failures — Cluster D, not introduced)
- arccli: 323 passed
- arcui: 666 passed
- arcrun: (covered by §8.13 prior to this session)

## Methodology that worked across all 12 splits

For each split:

1. **Map structure**: `grep -n '^def \|^class \|^[A-Z_]\+ ='` the source file.
2. **Read** the regions that move; understand dependencies (especially circular-import risk).
3. **For Python files with rich types**: extract a small `_types.py` / `_findings.py` / `_result.py` sibling first to break circular imports — both the new sibling modules and the slim original import from this types module without depending on each other.
4. **Write the new files** with cohesive responsibility:
   - Functions/classes that need test access stay public-named in the new module.
5. **Slim the original** to delegate / re-export.
6. **Add a `__all__` list and re-import** every moved name into the slim original so `from <pkg>.original import X` keeps working unchanged.
7. **For subpackage conversions** (§8.5, §8.6): `git rm` the old `<name>.py`, create `<name>/` directory with `__init__.py` re-exporting the public API. Python prefers package over module; both can't coexist.
8. **Verify**: `uv run --package <pkg> ruff check packages/<pkg>` then `pytest`. Test counts must match HEAD baseline. (Phase 0 baseline log was stale — drift since capture; trust HEAD baseline.)
9. **Update test patches** when tests reach into module-namespaced helpers via `unittest.mock.patch`. Common patterns:
   - `arcskill.hub.dry_run.shutil.which` → `arcskill.hub._firecracker.shutil.which` (or `_docker.shutil.which`) when the function being tested moved.
   - `arcskill.hub.dry_run._DockerBackend` → `arcskill.hub._docker._DockerBackend` because the consumer (`_run_docker`) lives in `_docker.py` and resolves the name through that namespace.
   - `arcagent.core.agent.load_eval_model` → `arcagent.core.model_manager.load_eval_model` (89 occurrences updated via sed in §8.1).
   - `arcagent.core.agent.arcrun_run` / `arcrun_run_async` / `arcrun_run_stream` → `arcagent.core.agent_dispatch.<name>`.
10. **For static-asset substring tests** (frontend split §8.7): introduce a `_bundle()` helper in the test file that concatenates every `<base>*.js` sibling, then point the substring assertions at the bundle.
11. **For private-method tests** that called `agent._setup_capability_prompt_injection()`, `agent._maybe_compact()`, `agent._create_vault_resolver()`: tests now call the extracted free functions directly (`setup_capability_prompt_injection(agent)`, `maybe_compact(agent, session)`, `create_vault_resolver(config)`).
12. **Commit** with the format: `refactor(<pkg>): §<X.Y> — split <file>` and a body listing the new files + LOC + verification result.

## Package-name reminder (uv workspace)

| Directory | Project name (use with `uv run --package`) |
|---|---|
| `packages/arcagent` | `arc-agent` |
| `packages/arccli` | `arccmd` |
| All others | (matches dir name) |

## Quirks observed

- **zsh doesn't word-split unquoted `for x in $VAR`.** Use explicit list or arrays. Doesn't apply when commands run via this CLI's `Bash` tool — but worth knowing.
- **Mocking module-namespaced helpers across an extract**: `unittest.mock.patch("modA.helper")` only patches `modA.helper`; if `modB` calls `helper` from its own namespace, the patch doesn't hit. Either re-route the test patch to the new namespace OR re-import the helper into the original module so both namespaces resolve to the same object.
- **Cluster D (3 `*_real_llm` failures in `arcagent/test_spawn_e2e.py`)**: pre-existing, not introduced by any phase. Don't chase.
- **Auto-commit hook**: in this session, after editing files for §8.10 a watcher made a commit titled `"added"`. Caught it via `git log --oneline` and amended with the proper `refactor(arcgateway): §8.10 — split pairing.py` message. Watch for spurious commits if you don't see your own commit message immediately.
- **Phase 0 baseline-pytest.log is stale**: pre-dates ~50 unrelated commits. Use HEAD-before-change as the comparison oracle, not the captured baseline.
- **JS IIFE split (§8.7)**: each renderer module pulls helpers from `ARC.AgentDetail._shared` into local aliases (`var escText = _S.escText`) at the top of its IIFE, so renderer bodies stay byte-identical to the originals. Load order in index.html: shared first, then timeline (which exposes shared `renderToolsTable` for modules to reuse), then modules / files / audit / policy / entry.

## ADR-004 core LOC budget recovery

§8.3 + §8.1 alone moved ~990 LOC out of `arcagent/core/`. Combined with §8.2 (orchestration), the arcagent core nucleus is now substantially closer to the 3,500-LOC budget target. Final accounting deferred until Phase 6 ends.

## Items deferred to §8.15 / Phase 6 follow-up

These files are still over the 500-LOC done-definition threshold but the spec for their primary §8.x split was met without further decomposition:

- `arcgateway/pairing.py` (877 LOC after §8.10): the SQLite `PairingStore` class is one cohesive responsibility (per CLAUDE.md "one class, one responsibility"). Splitting further would fight the principle.
- `arcskill/hub/_firecracker.py` (568 LOC): the `FirecrackerSandbox` class itself is one responsibility (microVM lifecycle).
- `arcskill/hub/_secret_patterns.py` (478 LOC): the regex bank registration is data, not logic — flat list under the threshold-by-content if not by raw line count.
- `arcllm/modules/telemetry.py` (495 LOC after §8.11): just under the threshold.
- `arcagent/orchestration/spawn.py` (859 LOC after §8.2): `make_spawn_tool` + `spawn` + `spawn_many` + audit/UI/OTel helpers; could split further but spec §8.2 only requested the data-type extracts.

These are flagged in the §8.15 "smaller wins" follow-up sweep in `README.md`.

## Next phases per `README.md`

- **Phase 6 — Refactor-only Typing & Doc Cleanup**: `arcui` typed response models (Pydantic mirrors of current dict shapes — wire-identical), query-param parsing helpers, document every surviving `# type: ignore` and broad `except`. Pure refactor.
- **Done definition (§15)**: most checkboxes hit by Phase 5. Remaining: "No file in `packages/*/src/` exceeds 500 LOC" — see deferred items above.
