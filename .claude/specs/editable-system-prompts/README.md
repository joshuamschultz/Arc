# Specification: Editable System Prompts (arcprompt)

**Feature:** `editable-system-prompts`
**Created:** 2026-07-21

## Status

| Doc | Status | Last Update |
|---|---|---|
| PRD | approved | 2026-07-23 |
| SDD | approved | 2026-07-23 |
| PLAN | implemented | 2026-07-23 |

Implementation complete across all four phases. Not yet committed.

## Steering References

- Product: [`../../steering/product.md`](../../steering/product.md)
- Tech: [`../../steering/tech.md`](../../steering/tech.md)
- Structure: [`../../steering/structure.md`](../../steering/structure.md)
- Roadmap: [`../../steering/roadmap.md`](../../steering/roadmap.md)

## Locked Decisions (from PRD open-question answers)

- **Signature storage = detached `.arcsig` sidecar** (T-739). Matches arc's existing convention, zero canonicalization risk. Desync fails closed: a missing/invalid sidecar raises `PromptUnsigned` — never a silent fall-through to stock.
- **Overlay mechanics built; audit trail is the change record.** No arcstore-backed overlay history in v1.
- **`tunable` is inert metadata** in v1 — parsed and preserved, enforces nothing until the v2 optimizer.
- **identity.md integrity check is out of scope** — tracked separately.
- **arcskill ⟂ arcprompt (no shared code).** arcskill houses its own prompts under `arcskill/context/` and loads them with a tiny local loader (no `import arcprompt`), so arcskill works standalone. arcprompt's catalog still discovers/manages those files via the shared `.md` convention.

## Implementation Summary

- **Phase 1 — arcprompt leaf package.** `PromptDocument` (frontmatter + sha256-of-raw-bytes identity), `SignatureVerifier` (mandatory operator-key pinning, `None` pin fails closed), `PromptCatalog` (cross-package discovery), `PromptResolver` (two-layer overlay→stock, first-match, fail-loud), `PromptSnapshot` + one provenance audit event per run. 35 unit tests, 97% coverage, ruff/mypy clean. Architecture import guard (`tests/architecture/test_no_arcprompt_imports_upward.py`), added to `_CANONICAL_PACKAGES` + Makefile.
- **Phase 2 — migration.** 30 prompts externalized byte-identically (RED→GREEN byte-identity harness generated `.md` from live constants, never hand-transcribed): arcrun 9, arcagent 9, arcmemory 7, arcskill 5. All Python constants deleted; two single-prompt arcagent modules removed. Wheel `artifacts` globs added; `tests/architecture/test_prompt_markdown_ships_in_wheels.py` builds each wheel and asserts the markdown ships. Suites: arcrun 543, arcmemory 274, arcskill 702, arcagent 2705 — all green.
- **Phase 3 — arcui + arcagent wiring.** arcui Prompts API (list / detail+diff / write / reset), `SigningAuthority` seam (resolves the operator DID from the request principal, no hardcoded key/DID, resolved DID audited), `prompt:write` policy gate, React Prompts tab + drawer. arcagent constructs the resolver at setup (pinned to the operator key, overlay root `<agent_root>/context/`), freezes the snapshot + emits provenance once per run at `build_run_context`, and resolves strategy + spawn prompts overlay-aware. 14 arcui tests + 9 arcagent tests.
- **Phase 4 — polish + gates.** LOC: arcprompt ceiling 700 (472 actual); arcrun 5067/5400 (the migration *reduced* it). Zero prompt string literals remain in the migrated packages.

## Phase Notes

### T-756: docker overlay visibility — RESOLVED (no change needed)

Overlay visibility is **unaffected by the docker backend**. Prompt resolution runs host-side in the arcagent process at `build_run_context`; the docker backend only sandboxes `contained_execute_python` and bind-mounts the *workspace* — it never runs prompt assembly and never mounts the overlay tree (`<agent_root>/context/`, a sibling of workspace). The snapshot re-reads the host filesystem each run, so an operator's overlay write takes effect on the agent's next run with no restart and no mount change. The REQ-123 "next-run" assumption holds for every backend.

## Learnings

- **Overlays are signed by the operator key, not the agent DID.** arcui's SigningAuthority signs with the deployment operator key, so the arcagent resolver must PIN the operator public key (`OperatorKey.load(default_operator_key_path()).public_key`) — pinning the agent's own DID key would reject every legitimate override. This is the single subtle coupling between the writer (arcui) and the reader (arcagent).
- **Byte-identity migration must generate `.md` from live constant values**, never hand-transcribe. The `render_prompt`/`parse_prompt` newline rule (write `body + "\n"`, strip one trailing `\n`) round-trips any constant including the workpad backslash-continuation case.
- **Architecture tests rot.** The pre-existing `test_arcui_is_not_an_emit_subscriber` used a bare-substring `emit` heuristic that false-positived on `emit_mutation_audit`; tightened to the real anti-pattern (imported `emit` symbol / `audit.emit(` call).

## Follow-on: structured rubric + CLI + arcskill overlay-effectiveness (2026-07-24)

Extended per owner direction ("the rubric needs to be editable as well, maybe a different UI"; "no arcprompt → everything still works, but we can add in and manage the arc system"):

- **arcskill prompts are now overlay-EFFECTIVE, not just view-only.** arcskill's loader takes an optional `resolve` callable (`arcskill.context.PromptResolve`) and **never imports arcprompt**: arcagent builds the operator-key-pinned resolver and hands the closure in via `agent_prompt_resolve(agent_root, tier)` (`arcagent.core.prompt_context`), threaded through the skills-module runtime → `ArcSkillImprover` → every improver component. No resolver handed in (arcprompt absent) → shipped stock. So a signed operator override of an arcskill prompt takes effect at runtime; a bare arcskill still runs on defaults.
- **The judge rubric is a structured prompt.** `DIMENSIONS` moved from `evaluator.py` to `arcskill/context/judge_rubric.md` — a prompt whose *body is YAML* (dimension → `{checklist, anti_inflation}`). It rides the exact same sign/overlay/version/resolve rails as any prose prompt (`load_rubric() == DIMENSIONS`); only the parser (YAML) and editor (form) differ. No new "structured document" type in arcprompt.
- **`arc prompt` CLI** (arccli): `list / show / diff / edit / reset` for every prompt across all packages, operator-key-signed overlays, reusing `build_prompt_resolver` + `find_secret` + `sign_artifact`. Stock always read from packaged resources, never a user path.
- **arcui rubric form editor**: a dedicated per-dimension form (checklist rows + calibration field) over a structured `GET/PUT .../rubric` endpoint that reuses the single `_author_signed_overlay` write envelope (secret-scan → policy → confine → operator-sign → audit resolved DID). Prose prompts keep the textarea drawer.

Verification: arcskill 702, arccli 521 (11 new), arcui 668 (22 rubric-route), arcagent 2712, architecture 32 — all green; ruff/mypy clean across all four packages; `judge_rubric.md` ships in the arcskill wheel. Fixed in passing (pre-existing): a brittle `test_command_registry` substring assertion and arcskill's **missing `[project] dependencies`** (declared arctrust/pydantic/PyYAML).

## Review outcome (2026-07-24) — PASS after one blocking fix

`/review` (spec mode) fanned out security/simplicity/coverage/architecture/reliability lenses over the diff.

- **BLOCKING (fixed): SEC-04 path traversal in the prompt READ path.** `arcprompt` joined caller-supplied `package`/`name` into a filesystem path with no `..`/separator rejection (`catalog.stock_path`/`load_stock_document`, `resolver.resolve`/`overlay_path`) — concretely exploitable via `arc prompt show <pkg> ../../../../etc/whatever` (CLI argv is unrestricted). **Fix:** an `_ensure_safe(package, name)` guard at the arcprompt chokepoint rejects any non-single-component identifier (separators / NUL / bare `.`/`..`) → `PromptMissing`; every read caller (arcui routes, arccli argv, agent resolver) is now covered. Regression tests added at the library, CLI, and arcui-route levels; arcprompt coverage 97%→99% (resolver 100%). Re-verified green: arcprompt 51 · arcrun 543 · arcmemory 274 · arcskill 702 · arcui 669 · arccli 524 · arcagent 2712.
- **Advisory (logged, not fixed):** DRY overlaps — arcskill's `_stock_body` duplicates arcprompt's frontmatter split (the deliberate decoupling), `_agent_tier`/`_VALID_TIERS` shared between arccli and arcui, `PromptResolve` alias arcrun could import from arcprompt; and `tunable` inert metadata (deliberate v2 staging).
- **Verified sound by the security lens:** mandatory key-pinning fail-closed, unconditional signature verify (never silent stock fallback), secret-type-only logging, resolved-DID audit (no defaults), write-path confinement, operator-gate-before-policy ordering.

## Open Questions / Residual Findings (for review)

1. **RESOLVED — arcskill `DIMENSIONS` is now fully editable.** Per owner direction it was externalized to `judge_rubric.md` (YAML-body prompt) with a dedicated form editor + CLI, and made overlay-effective. See the "Follow-on" section above.
2. **`decomposer.py` / `_validation.py` protected-names for `context.md` — intentional, not a bug.** `_validation.DEFAULT_PROTECTED_NAMES` (write-time file confinement) includes `context.md`; `decomposer._PROTECTED_NAMES` (plan-time ASI01 goal-hijack, identity artifacts only) excludes it. `context.md` is mutable working memory (workpad rewrites it), so the difference is defensible. Raised rather than silently changed — confirm.
3. **Pre-existing LOC-budget failures, unrelated to this feature.** `arctrust` 3755/3600 and `arcgateway` core 1297/1200 exceed their ceilings at branch HEAD; neither package was touched by this work. Per the "move code, don't raise the ceiling" rule these signal design debt in those packages — a separate refactor, not part of editable-system-prompts. **Flagged for a decision** (fix in a dedicated pass vs. adjust ceilings).
4. **arcui effective-body display does not verify signatures** (arcui holds no pinned key/posture) — a broken/unsigned overlay shows as `effective=stock` in the UI, while the agent's resolver would actually raise at run start. Display-only limitation.
5. **`useOperatorMode` is a client toggle**; the server operator-gate is the real enforcement (every PUT/DELETE 403s a viewer regardless of the toggle).
