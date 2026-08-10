# arcllm-gateway-hardening — build & deepen notes

The `/build` and `/deepen` output for this feature: research insights, architecture diagrams,
component lists, risk registers, open questions and handoff notes. Verbatim, in original order.

**Decisions from this build:** D-419–D-458 (40 total) — see [`.claude/decisions-log.md`](../../decisions-log.md).

---

## ArcLLM Gateway Hardening — SPEC-015/016/017 (2026-07-05)

Three opt-in arcllm transport modules (informed by the gavio gateway comparison), built federal-first (NIST 800-53 + OWASP LLM/ASI). Full rationale in each spec's `packages/arcllm/.claude/specs/<id>/README.md`. Shipped to `main`: ruff 0, mypy --strict 0, 1289 tests, 100% coverage on new modules.

#### SPEC-015 Content Guardrails (D-419–D-434)

| ID | Decision | Rationale |
|----|----------|-----------|

#### SPEC-016 Full Trace Capture & Replay (D-435–D-448)

| ID | Decision | Rationale |
|----|----------|-----------|

#### SPEC-017 Load Balancing (D-449–D-458)

| ID | Decision | Rationale |
|----|----------|-----------|

Post-review fix pass (20 findings, 2 HIGH / 5 MED / 13 LOW): spool bodies sealed once when encryption on (no plaintext CUI leak); full-text banned-content scan (closed length-cap bypass); added sk-ant-/sk-/AIza/xox secret patterns; trace-store hash+write offloaded to `asyncio.to_thread` (event-loop unblock at scale); tool_call args redacted; shared `resolve_enforcement` helper; honest `verify_chain` docs. OPEN: head-truncation/rollback tamper-evidence requires the arctrust `SignedChainSink` external anchor (cross-package, not arcllm's to implement).

---

---

---
### Deepening Summary

**Deepened on:** 2026-07-21
**Sections enhanced:** 16
**Solutions referenced:** 2
**Skills matched:** coding-workflow:langchain-patterns, coding-workflow:agent-delegation, coding-workflow:compound-docs, coding-workflow:auto-documentation, architecture-adr-generator, coding-workflow:python-patterns

#### Key Findings
- CORRECTION to D-467/D-468 rationale: arcui's operator gate is ROLE-based on a shared bearer token (auth.py:189-239), not DID-based. There is no per-user DID on the arcui write path. 'arcui signs with the operator key' requires the server process to hold the operator private key — an unresolved design question, not a free reuse.
- CORRECTION to D-470 rationale: the skill list-to-drawer-to-diff-to-rollback pattern exists only on the BACKEND. skill_versions.py's diff/rollback endpoints are entirely unconsumed by the frontend, and no diff renderer exists anywhere in web/src. The Prompts tab builds the first one.
- Packaging trap: .md files will be silently dropped from the wheel unless each touched pyproject.toml declares artifacts = ["src/<pkg>/**/*.md"] — the exact lesson from the SPEC-047 blueprint TOML migration (arcagent/pyproject.toml:136).
- Byte-identity trap named: prompt.py:12 uses a backslash-continued opening quote to suppress a leading newline, and read_text() returns a .md file's trailing newline verbatim. This is precisely the failure D-469 exists to catch.
- CONTEXT_MAINTAINER_SYSTEM_PROMPT (the largest prompt at ~4.9 KB) has ZERO test coverage today. Tests must be written against the constant BEFORE it moves.
- Three open questions resolved: capability_registry.format_for_prompt() needs no slot mechanism (it emits pure XML with no authored preamble; the prose wrapper _SKILL_USAGE_INSTRUCTION is already a separate constant on a separate bus subscriber); all four arcskill improver prompts are cleanly externalizable (runtime data is pre-rendered to flat strings before the f-string); _DEFAULT_PREFIX is confirmed dead with only two hits in all of src/, both its own definition.
- Every commercial prompt-management system surveyed (Langfuse, LangSmith, Humanloop, Agenta) auto-derives version identity rather than trusting a hand-typed field — independent convergence on D-462's reasoning.
- tier = "personal" is hardcoded in EIGHT places in the arc agent create scaffold, so D-467's sign-at-every-tier lands signing friction on every scaffolded agent by default.

#### New Risks Discovered
- ~~Overlay staleness inverted: D-465 avoids staleness for un-overridden prompts, but an OVERRIDDEN prompt silently misses every upstream improvement with no 3-way merge available. This is the documented dpkg conffiles limitation (dpkg stores only a stock checksum, which is why ucf exists as a separate layer).~~ **ACCEPTED — see D-480.** An override is intended to be permanent; upstream prompt changes deliberately do not reach it. No stock-hash tracking, no merge machinery, no staleness UI.
- arcrun mounts identity.md/context.md read-only in the docker backend — writes may be invisible to a live agent until restart. Must be verified against D-461's pin-at-run-start assumption.
- No agent/session fixture exists that assembles a full system prompt end-to-end. arcagent/tests/conftest.py has exactly one fixture. The assembled-prompt harness must be built, not reused.
- Coverage gates (line >= 80%, branch >= 75%, fail_under=80) mean a thin new arcprompt loader with few tests can drag a package under threshold.
- A new leaf package needs its own hand-written tests/architecture/test_no_arcprompt_imports_*.py guard — leaf-ness is not generically enforced; all 11 architecture tests are hand-written AST scans with no central DAG table.
- decomposer.py:38 _PROTECTED_NAMES omits context.md where _validation.py:23 includes it — a latent inconsistency independent of this work, worth fixing under the repo's leave-it-correct standard.

---
