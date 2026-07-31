# Specification: SPEC-060 — Memory Ingestion & LongMemEval Evaluation

**Feature:** `SPEC-060-longmemeval`
**Created:** 2026-07-30

One ingest pathway that feeds an outside corpus into a real Arc agent's memory through pluggable source adapters, plus the measurement of what comes back out. LongMemEval is the first adapter; email and Slack attach to the same `read()` seam later. Zero framework changes — everything is config, public API, or harness-side code.

- **Requirements:** [PRD.md](./PRD.md) — 43 requirements, `REQ-174`..`REQ-216`
- **Design:** [SDD.md](./SDD.md) — 22 components, `COMP-001`..`COMP-022`
- **Tasks:** [PLAN.md](./PLAN.md) — 43 tasks, `T-783`..`T-825`, 4 phases

## Status

| Doc | Status | Last Update |
|---|---|---|
| PRD | approved | 2026-07-30 |
| SDD | approved | 2026-07-30 |
| PLAN | complete (43/43) | 2026-07-30 |

## Steering References

- Product: [`../../steering/product.md`](../../steering/product.md)
- Tech: [`../../steering/tech.md`](../../steering/tech.md)
- Structure: [`../../steering/structure.md`](../../steering/structure.md)
- Roadmap: [`../../steering/roadmap.md`](../../steering/roadmap.md)

## Decision Log Snippets

Cross-feature decisions referenced from [`../../decisions-log.md`](../../decisions-log.md), section "Memory Ingestion & LongMemEval Evaluation — Build Decisions (2026-07-30)", deepened the same day:

| Decision | Subject | Where it lands |
|---|---|---|
| [D-492](../../decisions-log.md#d-492) | Dataset, workspaces and credentials never committed or written in plaintext | `COMP-014`, `COMP-015`, `COMP-006` |
| [D-493](../../decisions-log.md#d-493) | Benchmark content is untrusted input at the memory boundary; no bypass of `FastCapture` | `COMP-005`, `COMP-007` |
| [D-494](../../decisions-log.md#d-494) | Home is an `evaluations/` folder, not a package — direction of dependency, not distance | repo layout; architecture test in `T-784` |
| [D-495](../../decisions-log.md#d-495) | Session as one unit, chunked on turn boundaries, never mid-turn | `COMP-004` |
| [D-496](../../decisions-log.md#d-496) | A real `ArcAgent` turn per chunk — the agent path is the product | `COMP-007` |
| [D-497](../../decisions-log.md#d-497) | One `read()` contract per source; longmemeval first | `COMP-001`, `COMP-002` |
| [D-498](../../decisions-log.md#d-498) | Source timestamps ride in the chunk text, not `Event.ts` | `COMP-004`, `COMP-016` |
| [D-499](../../decisions-log.md#d-499) | Consolidation fires per session and the harness waits for it | `COMP-008` |
| [D-500](../../decisions-log.md#d-500) | Throwaway workspace per question; phased Oracle → sample-S → full-S; both metrics | `COMP-017`, `COMP-019`, `COMP-012` |

### Operator decisions taken at spec time (2026-07-30)

These four resolved open questions the build phase left unanswered. Each changes the design materially, so each is recorded rather than inferred.

| Choice | Decision | Consequence carried into the spec |
|---|---|---|
| Concurrency | **Fully sequential** — one question, one live agent at a time | Deletes the flocked WORM chain, the shared arcstore, rate-limit cascades and `contextvars` sibling-task hazards from scope. Costs wall-clock time on full S. (`REQ-202`) |
| Judge | **Through arcagent → arcrun → arcllm**, not the reference script | Honors CON-3, but the reference `print_qa_metrics.py` hard-asserts the judge string and cannot consume our output. Comparability therefore rests entirely on prompt fidelity, which is why the byte-diff test is a Must. (`REQ-188`, `REQ-189`) |
| Modules | **`workpad` and `policy` stay ON** — full production agent | Preserves D-496's premise, but the headline number measures arcmemory plus two further system-prompt summarizers. The manifest declares this. (`REQ-200`) |
| Tier | **`personal`** | Lightest `MemoryConfig.for_tier` dynamics and in-process audit signing. Every reported number is tier-specific and labelled so. (`REQ-199`) |

## Phase Notes

_(append `### Phase N: <name>` blocks via `append_phase_note.py` at phase boundaries during `/implement`.)_

### Phase 1: Foundation — COMPLETE (2026-07-30)

All 9 tasks (`T-783`..`T-790`, `T-824`) carry G1–G3 evidence. Evidence at close:
100 harness tests pass, `mypy --strict` clean across 15 files, `ruff check` clean,
34 architecture tests pass (was 32 — two new guards added by `T-784`).

**The repo-root `tests/architecture/` suite had never been committed.** Found because
`T-784` added a guard there and the file did not appear in `git status`. `.gitignore`'s
`**/architecture/` "work files" rule swallowed the whole directory; the existing
negations covered `packages/**/tests/architecture/` and `docs/architecture/` but not
the root suite that `make architecture-tests` actually runs. On a fresh clone that
target collected zero tests. Fixed by adding `!/tests/architecture/` +
`!/tests/architecture/**`, which also made 16 previously-invisible ruff errors in
those files surface (ruff skips gitignored paths) — all fixed in the same session:
7 redundant `print(msg, file=sys.stderr)` lines immediately preceding
`raise AssertionError(msg)` deleted, 8 import/lint autofixes, 1 justified `N815`
noqa on the `ast.NodeVisitor` camelCase alias.

**`packages/arcllm/coverage.json` was tracked despite being gitignored.** A generated
`--cov-report=json` artifact, untracked with `git rm --cached`. The repo now has zero
tracked-but-ignored paths, asserted repo-wide by `test_gitignore_contract.py` — that
sweep is deliberately not scoped to SPEC-060's own patterns.

**The SDD's stated reason for banning a blanket `*.toml` was wrong.** Measured: it
hides 41 tracked files (every `pyproject.toml` plus all five `blueprints/*/blueprint.toml`)
and does *not* touch `config/*.toml.example`, which end in `.example`. The prohibition
is right and more urgent than written. SDD line 63 and the `T-789` acceptance text
were corrected.

**The chunker measures raw character length, not NFKC length.** `sanitize()` normalizes
before it caps, so a turn whose NFKC form crosses `max_event_chars` will not raise
`TurnExceedsCapError` — it will be silently truncated instead. The 300-char margin makes
this unlikely on English chat text, and `SanitizeFidelityGate` (`COMP-005`, `T-794`/`T-795`)
catches the resulting shrinkage and voids the question anyway, so the outcome is the same
with a different reason code. Left as-is deliberately; the gate is where this is handled.

**The harness runs as a module, not a script.** `python evaluations/longmemeval/x.py`
fails with `ModuleNotFoundError: No module named 'evaluations'` because the script's own
directory goes on `sys.path`, not the repo root. Every entry point is invoked as
`uv run python -m evaluations.longmemeval.<module>` from the repo root. The `COMP-022`
CLI must document this.

### Phase 2: Core — COMPLETE (2026-07-30)

All 16 tasks (`T-791`..`T-806`) carry G1–G3 evidence. Evidence at close: 282 harness
tests pass, `mypy --strict` clean across 34 files, `ruff check` clean.

**The reference judge has FIVE prompt variants, not three.** The SDD and PLAN named
only the standard grader, the `_abs` refusal prompt and the preference rubric. The real
`get_anscheck_prompt` in `evaluate_qa.py` also ships a `temporal-reasoning` grader that
explicitly forgives off-by-one day counts, and a `knowledge-update` grader that accepts
a response carrying stale information alongside the updated answer. Scoring those two
types with the standard grader would have made our numbers stricter than the published
ones on 211 of the 500 questions — the largest comparability risk in the spec, and it
was not in the spec. All five are vendored and mapped by question type.

**The prompts were extracted by parsing, not transcription.** Vendored from
`xiaowu0162/LongMemEval` at commit `d6dc8b5`, with the raw URL and the fetched file's
sha256 recorded in the module docstring, and each template's own sha256 pinned by a test.
The reference's oddities are preserved deliberately — including a trailing space before
`\n\nQuestion:` in two of the five templates but not the other three.

**`[modules.memory] <key>` in the SDD is really `[modules.memory.config] <key>`.** The
SDD's shorthand omits the `config` level that arcagent actually reads. Every knob in
`COMP-006` lands one level deeper than written. Verified by loading the emitted TOML back.

**The ingest driver stops the background consolidation loop rather than assuming it is
off.** `SDD COMP-007` only says "never starts" it, but a live agent registers
`memory_consolidate_loop` at startup with `spawn=True`. The driver unregisters it and
raises if it cannot prove the loop is stopped — arcmemory has no lock, so an interleaved
background pass would corrupt the manifest the harness is asserting on.

### Phase 3: Integration and Phase 4: Polish — COMPLETE (2026-07-30)

All 43 tasks done, all 43 requirements traced, zero traceability gaps. Evidence at close:
**522 tests pass, 1 honestly skipped**, `mypy --strict` clean across 56 files, `ruff check`
clean, 34 architecture tests pass, working tree clean.

**One test cannot run here, and says so out loud.** The live end-to-end smoke (`T-822`)
needs the real dataset and a live judge key. It skips with a reason naming exactly what is
missing rather than passing vacuously, and the parts that CAN run offline — the
gold-evidence void through the real `SanitizeFidelityGate`, and the `git status`
cleanliness assertion — were split out and do run. Run the suite with `-rs`; a skip here
is not a pass.

**The resume test uses a real `SIGKILL` against a real child process.** Not an injected
exception (which unwinds and lets `finally` blocks run) and not `SIGTERM` (which the
process could catch). SIGKILL cannot be caught, which is exactly what makes it the right
signal for proving the resume path.

**The sample-S stratification is still open, and the code refuses to hide that.** Even vs
proportional is an explicit parameter with no silent default, recorded beside the manifest.
It stays open until Oracle results show which types are weakest — proportional sampling
puts `single-session-preference` at n=3, where a Wilson interval is not stable.

**Everything is still unspent.** No provider call has been made, no dataset downloaded, no
number produced. What exists is the apparatus and its gates. The next action is not code:
download the dataset, then run `measure_turn_lengths` and `damage_report` — the gold-shrink
rate decides whether any score would have been trustworthy at all.

## Learnings

Feature-specific insights captured here. Global / reusable patterns go to memory via `/memorize`.

**Found at spec time, before any code:**

- **Arc's system prompt carries no date at all.** `assemble_system_prompt` builds from `identity.md`, `context.md` and module-injected sections only (`core/session_internal/context.py:240-252`); no date appears in the prompt path, the arcrun strategies, or the memory module. Without a fix, 133 temporal-reasoning questions are unanswerable on perfect retrieval — a harness gap that would have read as a memory failure. Closed by `REQ-214`/`REQ-215`: the dataset's question date rides in the question turn, symmetric with the session date riding in the chunk text. It must be the dataset's date, never wall-clock, or the anchor lands in the run year instead of the haystack's.
- **`.gitignore` currently protects none of this.** Verified: `evaluations/**/data/*.json`, run workspaces, `traces/*.jsonl`, `.audit/*.worm` and the results JSONL are all trackable today, and no rule mentions `evaluations/`. The patterns must land before the first run, which is why they are a Foundation-phase task rather than a Polish one.
- **The date must repeat on every chunk, not just the first.** A session that splits into five chunks is five separate ingest turns; if only turn one carries `[Session date: ...]`, a retrieval landing on chunk four gets undated text. The 1700-char target against a 2000 cap is what pays for the repeated prefix. (`REQ-176`, tested by `T-824`)
- **The consolidation design rests on a constant that config cannot reach.** The harness drives `consolidate_poll_once()` itself *because* `_CONSOLIDATE_POLL_INTERVAL = 300.0` is a module constant. If that ever drops, the background loop fires alongside the harness's own passes — and arcmemory has no lock, so two passes interleave on the same SQLite connection with nothing raising. The preflight now asserts the constant before running. (`REQ-216`, tested by `T-825`)
- **A task covering six requirements is doing too much.** The PLAN validator flags any task ID appearing more than four times in the traceability table, which surfaces exactly this. The fat tasks were split into genuine units of work rather than worked around — the effective ceiling is about three requirements per task.

## Open Questions

1. **Does the dataset ship a per-question date field, or does the derived fallback apply?** A one-line check against the real file, settled by `T-793`. The preflight asserts a date resolves either way, so this cannot silently regress — but the answer determines whether our temporal numbers are directly comparable to the reference implementation's.
2. **Can arcllm's SPEC-038 budget or circuit breaker trip mid-run and turn distillation into a silent no-op?** Not traced. A long full-S run is exactly the workload that would trip it, and it would degrade the measurement without raising. Trace before the full-S phase, not before Oracle.
3. **Set `consolidate_engine = "pipeline"` in `dynamics`?** It avoids paying for a doomed 20k-token agentic attempt before every pipeline pass, but it changes what is being measured. Needs an explicit recorded call, never a silent default.
4. **Even or proportional stratification for the ~50-question S sample?** Natural distribution is multi-session 133, temporal-reasoning 133, knowledge-update 78, single-session-user 70, single-session-assistant 56, single-session-preference 30 — proportional sampling puts preference at n=3, where a Wilson interval is not stable. Deferred until Oracle results show which types are weakest.
5. **Framework ticket to raise separately, out of scope here:** `select_brain(...)` is called without `audit_sink=` (`modules/memory/_runtime.py:127-136`), so every arcmemory-internal audit event — `memory.dedup_skipped`, `memory.consolidation_degraded`, `memory.fact_updated`, `memory.entity_merged` — is discarded in a live agent. That is a production observability bug, not an eval bug. The harness works around it by watching the `arcmemory.consolidate` logger instead.
