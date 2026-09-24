# Skills and reports checkpoint handoff

> **Superseded status notice — 2026-09-23:** This is a historical implementation and verification record. Continue from the [business reliability consolidation handoff](business-reliability-consolidation.md); the branch-isolation and do-not-merge/resume instructions below no longer apply because the user has directed consolidation on main. Preserve the technical evidence below. Final merge hash, gates, and cleanup await root verification.

Snapshot: `codex/checkpoint-skills-auth-wip` at `68115322`. This branch preserves the skills, report, and account work from shared main. The implementation is a tested foundation, not a deployable hosted feature. Do not copy the branch wholesale to main without resolving the composition and authority gaps below.

## Implemented in this snapshot

- ArcAgent has a public `SkillArtifactResolver` protocol and default direct resolver. The anchored revision implementation is an optional lazy leaf; physically removing `revisions.py` leaves standalone `import arcagent` and capability loader startup working.
- The anchored resolver signs a complete bounded bundle, verifies inherited signed resources before first activation, supports binary assets, binds agent/skill/parent/version in a manifest, and advances an external monotonic anchor by CAS. Runtime reads reverify the active bundle. Missing or regressed anchor authority refuses use. Rollback creates a new forward activation of the entire selected bundle, including resources.
- Skill revision, version, rollback, eval, and golden-case routes use verified per-agent bundles and reload the live capability registry before reporting success. Same-named skills on different agents do not share history or bodies. A reviewed complete-bundle promotion port is exposed as `ReviewedSkillBundle`, `reviewed_bundle_digest`, and `AnchoredSkillRevisionResolver.promote_reviewed_bundle(..., runtime: SkillRuntime)`. It requires the reviewed digest and expected active digest, signs/CAS activates, reloads, and checks active bundle and registered skill bytes. Selection, scoring, event capture, and authorization belong to the caller.
- Report preview requires an identified account DID matching the user store, operator role, and a roster agent. It reads via pinned no-follow file descriptors, refuses linked/special/key material, audits refusal and errors, sanitizes bounded HTML, and applies CSP plus an empty iframe sandbox. The UI cancels stale requests, refreshes after save, and resets the editor when the selected file changes.

## Verification at checkpoint

- 61 focused Python tests passed across revisions, routes, report preview, and FD reader.
- 17 ArcAgent/ArcGateway architecture tests passed.
- 50 frontend Vitest tests passed; ESLint passed. New behavior tests cover stale/out-of-order preview fetch, AbortSignal/timeout, source toggle and save refresh, file switch while editing or save pending, and stale-digest skill edit.
- Strict mypy passed for 465 ArcLLM/ArcRun/ArcAgent source files. Scoped Ruff, format, and `git diff --check` passed.
- The curated adversarial battery passed 513 tests when localhost socket binding was allowed. Its manifest does not yet include the new skills/report abuse suites.
- An installed Chrome/Playwright run using `/private/tmp/arc-report-browser-test.py` confirmed that script execution, network requests, form submission, and parent navigation were refused inside the report iframe. That proof script is outside this branch; the repository tests assert sanitizer/CSP behavior but do not run a browser.

## Required production composition

1. Before agent startup, build a real independently monotonic anchor factory scoped by agent DID and skill name, plus an approved operator signer. Inject `AnchoredSkillRevisionResolver` through `ArcAgent(skill_artifact_resolver=...)` for editable skills. Direct resolution remains the explicit standalone mode. A missing, reset, or older anchored head must remain unavailable; never fall back to mutable original. First activation requires an authenticated operator preview/enrollment action. No production anchor factory or startup selection is wired in this snapshot.
2. `arcui.server.create_app` accepts `operator_signer_factory`, `user_store_factory`, and `skill_revision_anchor_factory`, but the CLI/startup composition must supply them. The real customer skill and report routes cannot work merely by patching `app.state` in tests. The account work in this snapshot must be integrated with those factories and the canonical authenticated session.
3. Report reads need a typed deployment read policy applied to the opened object and the authenticated account/agent scope. Current FD root identity, roster, and operator checks do not grant explicit deployment read permission. Wire the policy through production composition; require configured policy in hosted mode if no safe default is available. Keep authorization and key classification tied to the actual opened file, not a path precheck.
4. Auto-improvement integration must call the reviewed promotion port only after evaluation and authorization. A separate worker owns the ArcRun/ArcAgent tool event and outcome bridge: current `tool.end` lacks the arguments expected by the skills hook, and `tool.error` is unmapped. Do not claim that usage evidence or automatic promotion is live until that bridge and route-to-runtime proof pass.

## Remaining semantic review

- The local signed-revision witness is a fail-closed regression detector, not anchor authority. A failed CAS can leave an orphan signed revision that may cause conservative denial; removing all local evidence cannot detect rollback by a compromised external anchor. Define recovery for orphan candidates and rely on a genuinely monotonic external backend.
- If activation succeeds but runtime reload fails, the promotion port returns an error and does not report success. The new anchored head remains active; recovery/retry behavior needs an operational path and a regression with a real running agent.
- The curated adversarial manifest should include the new refusal suites before declaring the security-sensitive feature complete. Hosted composition and browser automation remain acceptance gates.

No skill/report changes from this branch were deployed or merged into main at this checkpoint.
