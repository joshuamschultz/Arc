# SPEC-018: Hermes-Parity Roadmap

**Status**: Complete (M1–M4 implemented + verified 2026-05-24)
**Type**: Multi-milestone roadmap (umbrella spec)
**Created**: 2026-04-18
**Closed out**: 2026-05-24
**Owner**: Josh Schultz
**Confidence**: High (build + deepen both complete; verification + gate-recovery cycle complete)

## What This Is

Single umbrella specification covering Arc's absorption of Hermes-Agent's UX/feature surface in Arc's federal-first, modular, per-package way. Spec-018 is **roadmap-shaped**, not feature-shaped — it covers ONE new sibling package (`arcgateway`) and module additions across `arcagent`, `arcrun`, `arcskill`, `arctui`, `arccli` over 4 milestones.

Per-feature execution detail lives in section-level breakdowns within `PLAN.md`, organized by milestone (M1–M4). Each milestone may be implemented as a separate `/implement` cycle.

## Source Material

| Input | Location |
|---|---|
| Hermes-Arc gap analysis report | Inline conversation 2026-04-18 |
| Build decisions (17 user + 8 auto-applied) | `.claude/decisions-log.md` (Hermes-Parity Roadmap section) |
| Research insights (8 parallel agent reports) | `.claude/decisions-log.md` (Hermes-Parity Roadmap — Deepening Insights section) |
| Build state | `.claude/builds/hermes-parity-roadmap/state.json` |
| Package steering (arcagent) | `packages/arcagent/.claude/steering/{product,tech,structure,roadmap}.md` |
| Package steering (arcrun) | `packages/arcrun/.claude/steering/{product,tech,structure,roadmap,security}.md` |
| Package steering (arcllm) | `packages/arcllm/.claude/roadmap.md` |

## Roadmap Alignment

This spec accelerates and concretizes work already planned in the package roadmaps — it does not introduce conflicting direction:

| SPEC-018 milestone | Aligns with |
|---|---|
| M1 (gateway + recall) | New work; extends arcagent Phase 1b session persistence |
| M2 (compounding) | arcagent Phase 4 "self-improvement loop (policy.md system, agent proposes improvements)" |
| M3 (executor backends + spawn) | arcrun Phase 3 (Recursive — SpawnTool) + arcrun Phase 4 (Container sandbox) |
| M4 (skills hub + voice/web/browser) | arcagent Phase 4 "OpenClaw skill adapter" + "module marketplace infrastructure" |

## Files

| File | Purpose |
|---|---|
| `README.md` | This file — metadata, decisions index, learnings |
| `PRD.md` | Product requirements — 8 capability sets, user stories, acceptance criteria, tier behavior, out-of-scope |
| `SDD.md` | Solution design — package layout, module boundaries, all 17 D-* decisions, research-insight-driven design, cross-cutting concerns |
| `PLAN.md` | Phased implementation — M1→M4, parallelizable tasks, dependency graph |

## Decisions Index (D-* tags from build)

| Tag | Decision | Where in SDD |
|---|---|---|
| D-01 | Package boundaries (1 new sibling: arcgateway) | §2 Package Layout |
| D-02 | arcgateway as separate daemon | §3.1 Gateway Architecture |
| D-03 | In-process asyncio per chat; federal subprocess | §3.1 Gateway Dispatch |
| D-04 | JSONL primary + SQLite FTS5 derived index | §3.2 Session Storage |
| D-05 | Sessions in arcagent; arcgateway depends on arcagent | §2 Package Layout |
| D-06 | Session identity = (user, agent) | §3.3 Session Model |
| D-07 | Per-session FIFO; concurrent across sessions | §3.3 Session Model |
| D-08 | Skills Hub TOML + CLI gated, off by default | §3.8 Skills Hub |
| D-09 | Cross-session reads via per-session ACL | §3.6 Memory ACL |
| D-10 | NL cron deterministic-first + LLM fallback | §3.4 NL Cron |
| D-11 | arcrun.spawn() primitive; agent-side delegate tool | §3.5 Subagent Spawn |
| D-12 | Skill auto-create nudge submodule in skill_improver | §3.7 Skill Nudge |
| D-13 | ExecutorBackend protocol in arcrun | §3.9 Executor Backends |
| D-14 | Vault-required platform creds at federal/enterprise | §3.1 Gateway Credentials |
| D-15 | Textual TUI (Python-only) | §3.10 arctui |
| D-16 | Two-tier memory: agent shared + user_profile per user | §3.6 Memory Architecture |
| D-17 | Centralized command registry in arccli | §3.11 Command Registry |

## Out of Scope (per user decisions during /build)

- **MCP client** — no `arcagent.modules.mcp`; reconsider only on explicit ask
- **Migration tooling** — no `arc migrate hermes` / `arc migrate claw`
- **ACP / IDE adapter** — no `arcacp` package; community plugin if demand emerges

## Dependencies & Sequencing

```
M1 (Gateway + Recall foundation)
  arcgateway daemon
  ├── DEPENDS: session API in arcagent (exists; needs FTS5 indexer)
  ├── DEPENDS: command registry in arccli
  └── ENABLES: NL cron platform delivery, session-search tool

M2 (Compounding self-improvement)
  skill_improver.nudge submodule
  memory_acl module (priority 10)
  user_profile per-user memory tier
  ├── DEPENDS: existing skill_improver, bio_memory, module bus
  └── ENABLES: closed learning loop UX promise

M3 (Anywhere execution)
  arcrun ExecutorBackend protocol
  arcrun.spawn() hardening (root-pooled token budget, DID HKDF, audit chain)
  arctui Textual completion
  ├── DEPENDS: existing executor.py, sandbox.py, spawn.py stubs
  └── ENABLES: docker/ssh/modal backends as plugin packages

M4 (Reach further)
  arcskill hub gating + Sigstore signing pipeline
  voice / web / browser arcagent modules
  ├── DEPENDS: arcgateway (for delivery), policy module (existing)
  └── ENABLES: Hermes UX feature parity for non-federal tiers
```

## Success Criteria (Roadmap Level)

- **Operational reach**: Arc agent reachable from Telegram + Slack simultaneously, sharing session memory by user identity. Federal tier passes audit (every cross-platform message routed through DID-bound session, classification-partitioned cache).
- **Compounding**: Agent autonomously creates skills after threshold turns; skills survive across sessions; per-user profile builds from interactions.
- **Execution flexibility**: Same agent code runs against local, docker, and ssh backends without source change. Federal tier signed-backend-manifest enforced.
- **Federal install path**: `arc skills install <name>` runs full Sigstore + Rekor + SLSA L3 + Firecracker dry-run pipeline; CRL-checked; fail-closed when CRL unreachable.
- **No core bloat**: arcagent core stays under 3,500 LOC; arcrun core stays minimal; arcgateway core (runner + base + session + executor) under 1,200 LOC.

## Learnings

Captured during /implement (per-milestone) and the 2026-05-24 verify-and-close cycle.

### Execution (M1–M4, 2026-04-18)

- **Total tests added**: ~1,849 across the four milestones plus the gap-close wave (M1: 511, M2: 223, M3: 240, M4: 548, Gap-close: 327). All passed at the time, none introduced regressions in the existing suite.
- **Spec corrections caught mid-implementation**:
  - **SDD §3.1 session-key formula** showed `build_session_key(user, agent, platform)`; corrected to `(agent_did, user_did)` to match D-06 (identity graph resolves platform → user_did first).
  - **SDD §3.7 nudge priority ordering** said NudgeEmitter at 150 runs *after* trace_collector at 200, but `module_bus` uses "lower number runs first." Agent C kept `SDD_STATED_PRIORITY=150` as a named constant for traceability and added `EFFECTIVE_PRIORITY=210` for runtime correctness. SDD needs a follow-up update.
  - **arccli architecture** was clarified post-PRD: arccli = terminal slash-command REPL (Click lives only in arcui). All new arccli files are argparse-or-registry; the legacy Click files are allowlisted in `tests/architecture/test_no_click_in_arccli.py` as a shrinking ratchet.
- **Data-loss incident (recovered)** — Mid-M1 execution, some upstream operation deleted `modules/scheduler/` and `modules/pulse/` from the working tree. Agent B's NL cron work and Agent K's cron runner work were uncommitted, so restoring scheduler from git rolled back to pre-Agent-B state. Recovery agents re-implemented; final state has all 106 affected tests passing. **Lesson**: commit at the agent boundary, not at the milestone boundary.
- **LOC-budget pressure surfaced architectural intent** — Gap-close wave 1.D had to refactor for budget compliance. The script encodes the rule "core stays small, complexity lives in modules"; the budget failure correctly told us where complexity had accreted in the wrong place.

### Verification + close-out cycle (2026-05-24)

When the README's "Draft" status was revisited five weeks later, gate machinery had drifted while functional code held strong. **5,193 tests still passed across the affected packages, but 5 separate gate failures had crept in.** Each one is a discrete lesson:

| # | Gate failure | Cause | Resolution |
|---|---|---|---|
| 1 | `test_no_click_in_arccli` | `arccli/commands/spec017.py` (from SPEC-017) used Click groups but was never registered into the actual arccli command tree — dead-coded except for direct test invocation | Replaced the Click decorations with plain Python functions; test now calls functions directly. 5 helpers, 11 tests, ~100 LOC simpler. |
| 2 | `test_no_unsigned_backends_at_federal` (2 tests) | `9312bc0 refactor(arcrun): §8.13 — split backends/loader.py` (Phase C) **strengthened** the gate from "federal only" to "all tiers," and moved the helpers to `_verifier.py` with re-exports at `loader.py:232`. The test still looked for the old `if tier == "federal":` branch shape | Rewrote test against the post-§8.13 invariant: gate must run before any import for **any non-builtin** load; helper must be reachable via FunctionDef, ImportFrom, or alias assignment in loader.py scope |
| 3 | `test_arcgateway_readme_documents_canonical_install` | README install section only showed the meta-package `pip install arcmas`; missing the dev `uv pip install -e packages/*` snippet | Added an "Install" section with both the user install and the canonical multi-package editable install (matches `make install`) |
| 4 | LOC budgets (G1.5 + G1.6) | Five weeks of subsequent specs (`§8.1`, `§8.3`, `ba1b1c2`, `PRD Fix 6 Phase C`, etc.) added `capability_loader.py` (405), `capability_registry.py` (419), `skill_validator.py` (210), `agent_dispatch.py` (247) to arcagent core — these are orchestration concerns, not nucleus concerns. Same drift inside arcgateway: `adapters/base.py` had bundled the `BasePlatformAdapter` Protocol with `FailedAdapter`/`reconnect_watcher` implementation helpers. | Moved `capability_*` and `skill_validator` from `arcagent.core.*` to new sibling subpackage `arcagent.capabilities.*` (1034 LOC out of core; 78 import sites updated by sed). Extracted `FailedAdapter` + `reconnect_watcher` from `arcgateway.adapters.base` to new `arcgateway.adapters._reconnect`. **Result**: arcagent core 4204 → 3170 (-1034, 330-line margin); arcgateway core 1267 → 1168 (-99, 32-line margin). |
| 5 | arctui pyproject still "coming soon" / `Development Status :: 1 - Planning` / version 0.0.2 / zero deps | M3 T3.7 finished the Textual code (303 tests) but never updated the package metadata. arctui's `transcript.py` imports `from textual.app import ComposeResult` while pyproject declared no dependency, so a fresh checkout could install arctui without textual and the suite would silently collect 0 items. | Added `textual>=0.80,<2` and `arccmd>=0.4` deps; bumped to `0.1.0`; classifier → `Development Status :: 3 - Alpha`. After install, 67 tests collect and pass. |

#### Meta-learnings from gate recovery

- **Architecture tests rot when the architecture they encode evolves.** §8.13 (Phase C) made the federal gate stricter — but the test was still asserting the *weaker* old shape. Tests like this need to evolve with the design; treating them as immutable led to a green-looking suite after the refactor because the test was wrong, not the code.
- **LOC budgets are an early-warning system for "wrong-home" code.** Both budget overages flagged real architectural mistakes: capability/skill loading shouldn't live in core, and Protocol contracts shouldn't be bundled with their implementation helpers. The fix was to move things to their correct home, not to raise the budget.
- **Status fields lie if you let them.** The execution-status tables (lines 7–197 of PLAN.md) were accurate on 2026-04-18 but the README's frontmatter still said "Draft." Five weeks of work happened on adjacent specs (SPEC-019 through SPEC-025) before anyone re-checked SPEC-018's actual state. **Lesson**: when implementation finishes, the status field moves the same commit. Otherwise verification drift is inevitable.
- **Test-isolation flakes hide behind successful unit runs.** Three `test_spawn_e2e.py` failures and one `test_nudge_false_positive_rate.py` failure only manifest when run as part of the larger suite — env leakage from earlier tests changes their skip conditions. They pass standalone and stay invisible until the full suite is run. Not introduced by SPEC-018 but surfaced by it.

## Next Steps

1. `/implement SPEC-018 --milestone M1` — start with arcgateway + session FTS5 + NL cron delivery
2. After M1 deployed and stable: `/implement SPEC-018 --milestone M2`
3. M3 and M4 can run partially in parallel (executor backends and skills hub don't share files)

## Related Specs

- SPEC-011 (slack-messaging) — existing platform adapter pattern; M1 absorbs this into arcgateway
- SPEC-012 (skill-improver) — existing module; M2 adds nudge submodule
- SPEC-014 (arcllm-call-queue) — wrapping module pattern (informs how arcgateway wraps platform adapters)
- SPEC-016 (multi-agent-ui) — process model + WebSocket pattern (informs gateway dispatch)
- SPEC-017 (arc-core-hardening) — security baselines this spec inherits
