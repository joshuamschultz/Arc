# editable-system-prompts — build & deepen notes

The `/build` and `/deepen` output for this feature: research insights, architecture diagrams,
component lists, risk registers, open questions and handoff notes. Verbatim, in original order.

**Decisions from this build:** D-459–D-481 (23 total) — see [`.claude/decisions-log.md`](../../decisions-log.md).

---

## Editable System Prompts (arcprompt) — Build Decisions (2026-07-21)

**Phase**: build | **Status**: complete | **Total decisions**: 23 (20 user, 3 auto-applied)
**ID range**: D-459 to D-481
**Priority framework**: simplicity → modularity → security → scalability

#### Summary
Externalize every system prompt across arc packages into per-package context/ markdown, loaded through arcprompt (a leaf package), overridable per agent via signed operator-written overlays, and viewable/editable in arcui. v1 is externalize + view/edit; evals and GEPA-style self-tuning are v2 on this spine.

#### Research Insights

**From Solutions Archive:**
- _(none)_

**Best Practices:**
- _(none)_

**Edge Cases:**
- _(none)_

**Performance:**
- _(none)_

**References:**
- _(none)_

#### Auto-Applied (Compliance Mandates)
| ID | Category | Decision | Mandated Answer | Citation |
|---|---|---|---|---|

#### Research Insights

**From Solutions Archive:**
- _(none)_

**Best Practices:**
- D-472's secret scanner is confirmed present and reusable: _find_secret (files_write.py:60-67) iterates arcllm._secrets.SECRET_PATTERNS then falls back to a keyword-anchored generic token regex, returning a secret_type label that becomes the audit detail.

**Edge Cases:**
- _(none)_

**Performance:**
- _(none)_

**References:**
- packages/arcui/src/arcui/routes/agent_detail/files_write.py:49-67






#### Research Insights

**From Solutions Archive:**
- security-issues/2026-04-18-tier-must-flow-through-construction.md (SPEC-017) — DIRECT HIT on D-471. Audit trails recorded tier=personal in federal deployments because the enforcement path saw the real tier while the annotating context saw a hardcoded fallback. Enforcement was correct; the audit lied. arcprompt must take tier/posture at CONSTRUCTION, never resolve it per-call.
- security-issues/2026-04-18-ast-validator-is-not-enough-defense-in-depth.md — a single gate is not a control; corroborates pairing D-466 (placement) with D-467 (verification) rather than relying on either alone.

**Best Practices:**
- Version identity should be auto-derived, with a separate mutable POINTER expressing deployment intent. Langfuse uses auto-increment + protected labels; LangSmith uses commit hash + tags; both warn against pinning ':latest' in production.
- arcprompt's own README already reserves the package for exactly this purpose ('prompt content lives outside runtime code, versioned independently and swappable without recompiling') and names arcrun.prompts.get_strategy_prompts as the migration source — D-459 follows an existing intent rather than inventing one.
- No context/ directory exists anywhere in the repo, so D-459's layout collides with nothing.
- Follow blueprints/loader.py:51's Path(__file__).parent convention for locating packaged files — the repo's established pattern, not importlib.resources.

**Edge Cases:**
- Two-layer stock/overlay chains cannot 3-way-merge upstream changes into a locally-edited overlay — the documented dpkg conffiles limitation. Recording the stock hash an overlay forked from would let arcui surface 'stock changed since you overrode this'. RECOMMENDATION, not a decision change to D-465.
- Kustomize's strategic-merge-patch cannot delete array items and breaks silently when list items lack merge keys. D-460's whole-file replace semantics dodge this class entirely — worth stating explicitly so a future 'just patch a section' proposal is refused on purpose.
- Rails-style multi-file config cascades produce load-order-dependent precedence bugs; a further argument for D-460's two layers over three.

**Performance:**
- Moving prompt text out of arcrun/src reduces NCLOC (docstrings and triple-quoted constants count per check_loc_budgets.py:22), so the arcrun 5,400 foundation budget gets slack rather than pressure.
- A new arcprompt package carries no LOC ceiling (check_loc_budgets.py:62-64) — consider adding one for consistency with the foundation tier.

**References:**
- https://langfuse.com/docs/prompt-management/features/prompt-version-control
- https://raphaelhertzog.com/2010/09/21/debian-conffile-configuration-file-managed-by-dpkg/
- https://github.com/kubernetes-sigs/kustomize/issues/4596
- packages/arcagent/src/arcagent/blueprints/loader.py:51





#### Research Insights

**From Solutions Archive:**
- _(none — no archive entry covers prompt or config schema design)_

**Best Practices:**
- Independent convergence on D-462: Langfuse auto-increments an integer per edit, LangSmith assigns a commit hash, Humanloop and Agenta both hash parameters to derive a version ID. None trusts a hand-typed version field, and the cited reason is exactly ours — humans edit content and forget to bump.
- Markdown + YAML frontmatter is the converging portable-prompt format: Microsoft Prompty (.prompty) and GitHub Copilot (.prompt.md / .chatmode.md). Frontmatter fields recurring across ecosystems: name, description, version/tags, model+provider+params, inputs/outputs schema, tools allowlist. D-462's name/description/tunable is a strict subset — defensible as YAGNI, and the surveyed supersets show where growth would go.
- _FRONTMATTER_RE already exists at arcui/routes/agent_detail/_common.py:55 and the frontend has a frontmatter.tsx MarkdownFile renderer — the frontmatter path is already paved on both ends.

**Edge Cases:**
- Content hash alone gives no human-readable 'what changed' signal; sources recommend a hybrid. D-462's answer is git history — sound in-repo, but note an OVERLAY lives in team/<agent>/context/ which is gitignored in this repo, so overlays have NO history mechanism at all. This is a genuine gap in D-462's 'history from git' rationale for the overlay half.
- GEPA's candidate object requires per-module prompt text, full parent lineage, and per-instance scores against a maintained Pareto set; SkillOpt requires retaining REJECTED candidates so the optimizer avoids repeating failed edits. Both are materially richer than D-462's schema — fine for v1, but the v2 optimizer will need a store, not just frontmatter.
- Two agents pinned to different versions of the same logical prompt diverge silently ('template drift') — a known config-versioning failure mode that D-460's per-agent overlays make possible by design.

**Performance:**
- _(none)_

**References:**
- https://prompty.ai/core-concepts/file-format/
- https://arxiv.org/abs/2507.19457 (GEPA)
- https://arxiv.org/abs/2605.23904 (SkillOpt)
- https://humanloop.com/docs/explanation/environments



#### Research Insights

**From Solutions Archive:**
- _(none)_

**Best Practices:**
- D-466's placement is API-compatible for free: _VALID_ROOTS = {'workspace','agent'} (_common.py:52) and _compute_write_target (_common.py:111-128) already resolve anything under the agent root, so team/<agent>/context/ needs NO new root registration.
- Follow skill_versions.py's error convention precisely: _error() wrapping ErrorResponse, and _store_unreadable() -> 503 with the exception verbatim, so a 200-with-empty-list is distinguishable from a 503-unreadable. Directly applicable to 'this agent has no overlays' vs 'the package dir could not be read'.
- Route registration is a flat manual manifest (routes/agent_detail/__init__.py:72-112): new module with __all__, alphabetized import block, explicit Route entries per method, plus a docstring bullet. Response models go in arcui/schemas.py, hooks in web/src/lib/queries.ts, types in web/src/lib/types.ts.

**Edge Cases:**
- Partial duplication is real and should be acknowledged rather than denied: identity.md and context.md are ALREADY readable and writable through the generic file surface (GET/PUT /api/agents/{id}/files/read) and edited in the UI via file-tree.tsx FileViewer:141. What genuinely does not exist is stock-vs-effective resolution, prompt-specific listing, reset-to-stock, and any diff rendering — which is exactly D-464's justification, and it survives.
- Note the existing PUT reuses the GET's path (/files/read) rather than a distinct write path — a convention worth deliberately NOT copying for /prompts, where PUT and DELETE have clean REST semantics.

**Performance:**
- skill_versions.py:95-105 memoizes diff generation with @lru_cache(maxsize=256) keyed on (hash_a, hash_b, body_a, body_b) — directly reusable shape for stock-vs-overlay diffs, which are far more cacheable since stock never changes at runtime.

**References:**
- packages/arcui/src/arcui/routes/agent_detail/_common.py:52,111-128
- packages/arcui/src/arcui/routes/agent_detail/skill_versions.py:60-105






#### Research Insights

**From Solutions Archive:**
- security-issues/2026-04-18-ast-validator-is-not-enough-defense-in-depth.md — no single gate is a control. Supports D-466 + D-467 as layered rather than alternative.

**Best Practices:**
- If the signature goes in frontmatter, use git's gpgsig excluded-field pattern: sign every field EXCEPT the reserved signature key, then splice it in; verification strips it to regenerate the exact payload. JWS solves the same chicken-and-egg identically (RFC 7515) — it never signs its own serialized form.
- If the signature goes in a sidecar, it matches arc's existing .arcsig convention exactly (artifact.py, files_write.py:43, arcskill/improver/codepatch.py:31) and eliminates canonicalization risk entirely, since arc's verify_artifact already signs raw content bytes.
- Keep signing frictionless or people disable it — the gitsign pattern (keyless, OIDC, credential cache for headless CI) exists precisely because humans reach for --no-gpg-sign when signing blocks them. D-468's sign-on-write is the same instinct.

**Edge Cases:**
- CORRECTION to D-467/D-468's stated rationale: arcui's operator gate is ROLE-based via a shared bearer token (auth.py:189-239, hmac.compare_digest on operator_token), NOT DID-based. There is no per-user DID on the arcui write path. My claim that key infrastructure was 'already ambient on the arcui write path' was wrong — the operator KEYPAIR lives at ~/.arc/ and is used by the CLI (arc blueprint sign), so server-side signing needs the arcui process to hold the operator private key. OPEN DESIGN QUESTION, not a settled reuse.
- There is no arc skill sign command (arc skill has only list/create/validate/search/evals), so a hand-authored skill cannot be signed today and simply cannot load at enterprise/federal. D-468's arc prompt sign would be the second operator-signing CLI after arc blueprint sign — worth deciding whether skills get the same treatment.
- Canonicalization traps that silently break byte-exact verification: editors inserting a final newline (VS Code default), core.autocrlf rewriting LF to CRLF on checkout, YAML round-tripping by linters or yq reordering keys and changing quote style. All apply to a frontmatter-embedded signature; none apply to a raw-bytes sidecar.
- Sidecar loss is the counterweight: cp, archive extraction, sparse-checkout, and copy-paste routinely drop a companion .sig. Sigstore's .sigstore.json bundle exists specifically to reduce that N-file sprawl.
- identity.md has NO integrity check on the read path — ContextManager reads it with a plain read_text() (context.py:110-114). The only hash touching it is identity_goal_hash() in planning/_runtime.py:169-181, which detects goal drift for stale plans, not file integrity. This confirms the earlier judgment that identity.md is a gap, not a precedent.
- tier = 'personal' is hardcoded in eight sections of the arc agent create scaffold (_common.py:142,237,264,305,316,459,470,495), so D-467's every-tier signing applies to every scaffolded agent from creation. Personal is the de-facto default and this is where the friction lands.

**Performance:**
- _(none)_

**References:**
- https://git-scm.com/docs/signature-format
- https://datatracker.ietf.org/doc/html/rfc7515
- https://docs.sigstore.dev/about/bundle/
- https://docs.github.com/en/get-started/git-basics/configuring-git-to-handle-line-endings
- packages/arcui/src/arcui/auth.py:189-239



#### Research Insights

**From Solutions Archive:**
- security-issues/2026-04-18-tier-must-flow-through-construction.md — the canonical warning for any tier-conditional behavior, including D-471's policy-content approach.

**Best Practices:**
- PromptLayer generalizes deployment labels into dynamic/percentage-split labels, so the overlay mechanism doubles as canary rollout. Out of scope for v1 but shows where D-471's policy-gated model could extend without redesign.

**Edge Cases:**
- Personal is the de-facto default: tier='personal' appears in eight sections of the agent-create scaffold (arccli/commands/agent/_common.py). Only the three shipped blueprints (personal-assistant, enterprise-ops, federal-analyst) set anything else, and they are opt-in presets. Whatever D-467 requires at personal tier IS the default experience.

**Performance:**
- _(none)_

**References:**
- packages/arccli/src/arccli/commands/agent/_common.py:142,305
- https://docs.promptlayer.com/features/prompt-registry/release-labels



#### Research Insights

**From Solutions Archive:**
- _(none)_

**Best Practices:**
- The exact-equality idiom D-469 needs already exists in-repo: arcrun/tests/test_prompts.py:52 asserts result['code_exec_guidance'] == CODE_EXEC_GUIDANCE, and :57 the same for CONTAINED_EXEC_GUIDANCE. Follow that shape rather than inventing one.
- arcui integration-test doctrine matches D-469's spirit — real CandidateStore through arcskill's own write path, real AgentIdentity, real tmp_path dirs, only the audit logger disabled. Explicit header docstring says do not fake the store.
- Add a SHA-256 digest assertion alongside string equality so a diff failure reports a short hash rather than dumping 4.9 KB of prompt into the test output.

**Edge Cases:**
- THE trap D-469 exists to catch, now named concretely: workpad/prompt.py:12 opens with a backslash-continued triple quote to suppress the leading newline, and Path.read_text() returns a .md file's trailing newline verbatim. Byte-identity will fail on exactly this unless the loader's newline handling is decided deliberately.
- CONTEXT_MAINTAINER_SYSTEM_PROMPT (~4.9 KB, the largest prompt in the repo) has ZERO test coverage — no test file references it. Tests must be authored against the constant BEFORE it moves, which inverts the usual order.
- Existing assertions are too loose to prove anything: test_prompts.py:112-120 asserts only isinstance and len(...) > 50 on prompt_guidance, so a migration could silently truncate the text and still pass. context_manager.py:78 asserts 'identity' in prompt.lower() with an or-fallback.
- No snapshot library is installed anywhere (no syrupy, pytest-regressions, approvaltests). This is hand-rolled — the arcskill 'golden' suites are machine-authored eval cases, not byte-comparison fixtures.
- No fixture assembles a full system prompt. arcagent/tests/conftest.py has exactly one fixture (_isolate_arcstore_data_dir). The closest harness is the local chain in test_context_manager.py:17-51, which yields a ContextManager but is not wired to arcrun.get_strategy_prompts().

**Performance:**
- Coverage gates (line >= 80%, branch >= 75%, arcagent fail_under=80 in pyproject) mean a thin arcprompt loader with sparse tests can drag a package below threshold — budget test coverage into the migration, not after it.

**References:**
- packages/arcrun/tests/test_prompts.py:52,57,112-120
- packages/arcagent/tests/unit/core/test_context_manager.py:17-51,78
- packages/arcui/tests/integration/test_skill_versions_routes.py:1-15



#### Research Insights

**From Solutions Archive:**
- _(none)_

**Best Practices:**
- skill-drawer.tsx (158 lines) is the concrete template: Sheet side='right' sm:max-w-xl, sticky action bar, lazy useQuery enabled on selection, useOperatorMode() gating the Edit button, apiPut then queryClient.invalidateQueries, and the prevSkill compare-during-render trick to reset state on selection change.
- Reusable primitives already exist: sheet, badge, tabs, textarea, scroll-area, separator, tooltip, skeleton, data-table.tsx, states.tsx, page-header.tsx, stat-card.tsx, status-badge.tsx, markdown.tsx, frontmatter.tsx (MarkdownFile), code-block.tsx, plus apiGet/apiPut helpers in lib/api.ts:35-65.
- Copy the three-banner convention from skill-drawer.tsx:123-137 — destructive for error, status-warning for signature staleness, status-online for success. A stale-signature banner is directly relevant given D-467.

**Edge Cases:**
- CORRECTION to D-470's stated rationale: there is NO diff renderer and NO version-timeline UI anywhere in web/src. skill_versions.py's backend diff/rollback endpoints (__init__.py:81-97) are entirely unconsumed by the frontend today. The Prompts tab builds the first diff view in the product — the pattern to reuse is the drawer shell, not the diff.
- There is also no useAgentSkillVersions hook to copy; new hooks must be written fresh against queries.ts's useApiQuery/useQuery conventions.
- The generic file editor (file-tree.tsx FileViewer:141) already offers Edit/textarea/Save over identity.md with the same affordance — take care the Prompts tab reads as a distinct, curated surface rather than a second file editor.

**Performance:**
- Server-side unified diff with lru_cache (skill_versions.py:95-105) avoids shipping a diff library to the browser — the established choice, and stronger here since stock content is immutable at runtime.

**References:**
- packages/arcui/web/src/components/skill-drawer.tsx
- packages/arcui/web/src/components/file-tree.tsx:138-174
- packages/arcui/web/src/lib/queries.ts:407-414



#### Research Insights

**From Solutions Archive:**
- security-issues/2026-04-18-tier-must-flow-through-construction.md — THE cautionary precedent for this section. Enforcement was correct while the audit event recorded a hardcoded tier=personal fallback, so federal audit trails lied. D-467's run-start event records prompt source and sha; if the loader resolves posture correctly but the audit context is built with defaults, this reproduces exactly. Pass tier and posture through construction.

**Best Practices:**
- Prompt telemetry rides the existing arctrust.audit.emit single emission point, so no new export target or sampling decision is introduced — but the event payload must be populated from the same constructed posture the loader used, not re-derived.

**Edge Cases:**
- A per-run event listing ~25 prompts with source and sha is materially larger than a typical audit record; confirm sink payload limits and whether the JsonlSink and SignedChainSink handle it without truncation.

**Performance:**
- _(none)_

**References:**
- .claude/solutions/security-issues/2026-04-18-tier-must-flow-through-construction.md



#### Research Insights

**From Solutions Archive:**
- _(none beyond the tier-flow entry cited under Observability)_

**Best Practices:**
- _(none)_

**Edge Cases:**
- D-462 records history as 'from git', but overlays live under team/<agent>/ which is gitignored in this repo — so the overlay half of the system has no history mechanism at all. Either overlays get a store-backed history (as skill candidates do via arcstore) or the audit trail is the only record of what changed. This materially affects the auditor persona.

**Performance:**
- _(none)_

**References:**
- packages/arcstore/src/arcstore/query.py:45-77



#### Research Insights

**From Solutions Archive:**
- _(none)_

**Best Practices:**
- _(none — category confirmed not applicable; arcprompt performs local filesystem reads only)_

**Edge Cases:**
- The not-applicable status is conditional on prompts never travelling. GEPA and SkillOpt both assume optimizer-proposed candidates, and SkillOpt requires retaining rejected candidates — the moment v2 lands, this category and the signing-trigger question reopen together.

**Performance:**
- _(none)_

**References:**
- https://arxiv.org/abs/2605.23904



#### Research Insights

**From Solutions Archive:**
- _(none)_

**Best Practices:**
- Cache stock-vs-overlay diffs with the lru_cache shape already used at skill_versions.py:95-105; stock bodies are immutable at runtime so the hit rate is far better than the skill case.

**Edge Cases:**
- _(none)_

**Performance:**
- Reading ~25 small markdown files once per run is negligible beside a single LLM call, and externalizing reduces NCLOC against the arcrun 5,400 budget.

**References:**
- scripts/check_loc_budgets.py:57-61



#### Research Insights

**From Solutions Archive:**
- _(none)_

**Best Practices:**
- MANDATORY packaging step, learned from the SPEC-047 blueprint migration: every touched pyproject.toml needs artifacts = ['src/<pkg>/**/*.md'] or hatch silently drops the prompt files from the wheel. arcagent/pyproject.toml:136 carries exactly this line with the comment that packaged presets are loaded at runtime and not optional data.
- The blueprint migration (config constants -> packaged TOML) is the direct in-repo precedent for D-469's package-by-package sequencing, including the Path(__file__).parent loader convention at blueprints/loader.py:51.
- Recommended order: (1) author exact-equality + SHA-256 tests against the live constants, especially CONTEXT_MAINTAINER_SYSTEM_PROMPT which has none; (2) move text to .md beside the loader; (3) add the artifacts= line; (4) re-run the pinned hash tests.

**Edge Cases:**
- A new leaf package needs a hand-written tests/architecture/test_no_arcprompt_imports_*.py guard — leaf-ness is NOT generically enforced. All 11 architecture tests are individual AST import scans with no central dependency-DAG table.
- tests/architecture/test_workspace_install.py:47 _CANONICAL_PACKAGES = ['arcgateway','arccli','arcagent','arcllm','arcrun'] must gain arcprompt, and Makefile:52-60 (make install) alongside it.
- Blueprint verification was behavioral (asserting three packaged presets resolve), NOT byte-identical — so D-469 is a deliberate strengthening over the precedent, not a copy of it.
- arcrun mounts identity.md/context.md read-only in the docker backend. Confirm whether a live agent sees overlay writes at all before restart, since D-461 assumes next-run pickup.

**Performance:**
- Moving prompt text out of Python reduces measured NCLOC — the arcrun 5,400 foundation budget gains headroom rather than losing it.

**References:**
- packages/arcagent/pyproject.toml:136
- packages/arcagent/src/arcagent/blueprints/loader.py:51,102,164
- tests/architecture/test_workspace_install.py:47
- scripts/check_loc_budgets.py:49-65

#### Open Questions
- Where do signature bytes live — detached <name>.md.sig sidecar (clean diffs, two files to keep together) or a frontmatter field (one artifact, churning crypto line in every diff)? D-468 settled the flow, not the storage.
- Does arcprompt absorb arcskill.improver's prompt builders (build_reflection_prompt, build_judge_prompt, suitegen._prompt)? They interpolate trace data per call — generated, not authored — so they may not be the same class of artifact at all.
- capability_registry.format_for_prompt() generates an XML capability manifest at runtime. Does the authored preamble around it become a prompt file with a slot, or stay entirely in code?
- arcrun/strategies/code.py:14 _DEFAULT_PREFIX duplicates CodeExecStrategy.prompt_guidance almost verbatim and is unreachable in src/. Confirm it dies during migration rather than becoming a second prompt file.
- identity.md is unsigned while prompt overlays will be signed (D-467). Is that gap worth closing separately, and does it belong to this spec or its own?
- Does `tunable: false` in frontmatter need any enforcement in v1, or is it inert metadata until the v2 optimizer exists?

#### Research Insights

**From Solutions Archive:**
- _(none)_

**Best Practices:**
- RESOLVED — capability_registry.format_for_prompt() (capability_registry.py:360, _render_manifest_locked :371-411) emits pure ElementTree XML with NO authored preamble. The prose wrapper is _SKILL_USAGE_INSTRUCTION (agent_lifecycle.py:38), already a separate constant injected by a separate bus subscriber at priority 91 vs the manifest's 85. No slot mechanism is needed; the boundary is already clean.
- RESOLVED — all four arcskill improver prompts are cleanly externalizable. Runtime data is pre-rendered to flat strings BEFORE each f-string (mutate.py:40-46), so a markdown template taking pre-rendered values is a mechanical swap. evaluator.py:83 has the largest static share (~20 lines) and its module-level DIMENSIONS table is authored prose in Python — a second externalization candidate. Note mutate.py:37 accepts an intent_header parameter that is never interpolated (dead argument).
- RESOLVED — _DEFAULT_PREFIX is dead. system_prompt_prefix has exactly two hits across all packages/*/src, both its own definition (code.py:35-36). Six of its seven guideline bullets duplicate prompt_guidance with only cosmetic differences (hyphen vs em-dash, 'You will receive' vs 'You receive'). Delete during migration.

**Edge Cases:**
- STILL OPEN and now sharper — signature storage. Both options have in-repo/prior-art support: sidecar matches arc's existing .arcsig convention and eliminates canonicalization risk; frontmatter matches git's gpgsig excluded-field pattern and cannot desync from its file. There is NO established convention for signing markdown+frontmatter, so frontmatter means inventing one.
- NEWLY OPEN — who holds the operator private key for arcui-side signing, given the gate is role-based on a shared token with no DID (auth.py:189-239). D-468 assumed this was solved; it is not.
- STILL OPEN — identity.md signing gap, now confirmed as a real absence rather than a design choice (plain read_text at context.py:110-114, no verification anywhere).
- STILL OPEN — whether tunable: false needs v1 enforcement. GEPA and SkillOpt both need far richer per-candidate metadata than frontmatter carries, so v2 likely needs a store regardless; tunable may stay inert metadata.

**Performance:**
- _(none)_

**References:**
- packages/arcagent/src/arcagent/capabilities/capability_registry.py:360,371-411
- packages/arcagent/src/arcagent/core/agent_lifecycle.py:38,353,366-375
- packages/arcskill/src/arcskill/improver/mutate.py:33-72,163-183
- packages/arcrun/src/arcrun/strategies/code.py:14,35-36

#### Related Solutions
_(none)_

#### Research Insights

**From Solutions Archive:**
- security-issues/2026-04-18-tier-must-flow-through-construction.md (SPEC-017) — tier/posture must flow through construction, not per-call; audit events lied while enforcement was correct.
- security-issues/2026-04-18-ast-validator-is-not-enough-defense-in-depth.md — a single gate is not a control; layer placement with verification.

**Best Practices:**
- _(none)_

**Edge Cases:**
- _(none)_

**Performance:**
- _(none)_

**References:**
- _(none)_


---

---
