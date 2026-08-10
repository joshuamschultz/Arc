# Build Decisions — Skill-Improver Eval Bootstrap

## Deepening Summary

**Deepened on:** 2026-07-10
**Sections enhanced:** 10
**Solutions referenced:** 2
**Skills matched:** python-patterns, testing-unit-test-writer, testing-test-case-generator

### Key Findings
- SECURITY: the current `assert True` placeholder does more than brick improvement — it makes `load_suite` non-empty, silently bypassing the fail-closed no-suite policy at enterprise/federal. Fallback on LLM-unavailable must be NO evals file, never a placeholder.
- Bug-freezing is the design-critical generation risk: LLMs encode actual (possibly buggy) behavior, not intended behavior — oracles must be grounded in the skill's declared Contract/Examples/Validation, and run-inferred expectations labeled change-detectors, never correctness anchors (arXiv 2410.21136).
- Adopt Meta TestGen-LLM's assured filter cascade (build → run → pass reliably N=5 → improves) and mutation-probe gating; expect to discard >40% of generated cases — that is normal, not a bug (arXiv 2402.09171).
- The outcome classifier needs skill-level attribution: `close_turn` stamps every active span with the same turn outcome, so a one-failing-skill turn penalizes innocent skills; the judge must name the responsible skill or abstain, and positive labels need a safety check (users praise jailbreaks — EMNLP 2025).
- Every mechanism has an in-repo precedent to reuse: memory module's threshold/idle sweep predicate, scheduler's single-flight dedup, improver's semaphore + task-set hygiene, arcllm's D-444 audited-disable, arcstore's byte-cursor tailer, and lock.py's atomic write pattern.

### New Risks Discovered
- Marker spoofing: the generation LLM itself can emit or omit `@generated` to game tier counts — the trusted harness must write provenance outside model-controlled content (manifest), attested at enterprise/federal.
- Silent suite shrink: `load_suite` swallows SyntaxError, so an invalid hand-edit quietly removes cases and can un-gate code mutation — the $EDITOR loop must validate-and-reopen before an atomic commit.
- Suite generation racing an in-flight improvement pass on the same skill changes the gate decision under it — per-skill single-flight lock required.
- A skill rewriting its own frontmatter opt-out mid-pass is a self-modification poisoning vector (ASI06) — frontmatter reads bind at mutation-unit start, effective next pass.


> Extends SPEC-044 (skill improver). Approved by Josh 2026-07-10 in design session.
> Goal: a self-improving skill system that "just works behind the scenes" — make the
> golden-task gate real (Half 1) and give the improver a true good/bad signal (Half 2).
> Context: today no skill ships an `evals/` dir (gate inert; personal-tier prose
> mutations auto-accept on judge opinion), `create_skill` scaffolds an always-passing
> placeholder that blocks strict-improvement forever, and `task_outcome` has no producer.

### Suite Generator

- New `SuiteGenerator` in `arcskill.improver`: LLM generates pytest golden cases from
  what the skill already declares — SKILL.md `## Contract`, `## Examples`,
  `## Validation` sections plus its `scripts/`/`src/` files.
- Generation is provider-free: LLM enters through the existing injected `LLMInvoker` seam.
- Every generated case is machine-verified before adoption: `ast.parse` for syntax,
  then a sandbox run against the *current* bundle via the existing `HubEvalRunner`.
- Cases that pass the current bundle are adopted immediately as regression anchors.
- Cases that fail are quarantined as improvement targets, not trusted — an LLM-invented
  failing test can encode a wrong expectation.

### Research Insights

**From Solutions Archive:**
- [AST Validator Is Not Enough — Defense-in-Depth for Dynamic Code Sandboxes](.claude/solutions/security-issues/2026-04-18-ast-validator-is-not-enough-defense-in-depth.md) — ast.parse is one layer, never the boundary; the tier sandbox (HubEvalRunner) remains the security boundary for model-authored test code, exactly as this decision already stages it.

**Best Practices:**
- Adopt Meta TestGen-LLM's 'Assured' filter cascade in exact order — (1) ast.parse/build, (2) runs without error, (3) passes reliably, (4) demonstrates improvement. At Meta only 57% of generated cases passed reliably and 25% improved coverage; treat a >40% discard rate as normal operation, each stage a hard filter, not a warning (arXiv 2402.09171).
- Gate adoption on mutation score, not pass/coverage: studies show 100% line coverage with ~4% mutation score. Before promoting a case to regression anchor, run it against a small set of mutants of the skill's own scripts and keep only mutant-killers — this is what filters tautological assertions (arXiv 2506.02954, 2501.12862 MutGen).
- Defend against bug-freezing — the single most design-critical control here: LLMs shown existing code encode ACTUAL behavior, not intended behavior, and oracle accuracy drops exactly when the code is buggy. Ground expected values in the declared Contract/Examples/Validation sections; label any expectation inferred from a live run as a change-detector, never a correctness anchor (arXiv 2410.21136, 2601.05542).
- Prefer property/invariant oracles derived from the Validation section over example-equality asserts — they have higher discriminatory power and mutation scores, and degrade less often into trivial non-null/type checks (arXiv 2506.18315, 2307.04346).
- Insert an anti-tautology static check between ast.parse and the sandbox run: reject cases with no assert, assertTrue(True)-class no-ops, asserts that only restate an Examples literal, or asserts referencing only the input (Qodo cover-agent pairs this with a coverage-delta filter; EvalGen arXiv 2404.12272).

**Edge Cases:**
- Flaky generated cases corrupt the strict-improvement gate (a noise fail→pass reads as improvement): run each candidate N=5 in the sandbox, promote only all-pass, quarantine mixed results as untrusted-flaky — the ARC_EVAL per-case harness already gives the needed granularity (arXiv 2402.09171).
- Trivially-passing cases: run a negative-control probe — execute the case against a deliberately mutated script; if it still passes it has zero discriminatory power and is quarantined, not adopted (arXiv 2506.02954).
- Side-effectful cases (network/filesystem/global state): keep every run inside the tier sandbox, and statically flag I/O or global-state touches during the AST scan — quarantine unless the Contract explicitly declares that effect; an impure case cannot be a stable anchor.

**Performance:**
- LLM cost is dominated by retries, not the first draft: K adopted anchors typically cost ~2-4K raw generations. Cap the candidate budget per skill and stop early once min_golden_cases anchors are adopted.
- Order filters cheap-to-expensive: ast.parse + anti-tautology are ~free and should eliminate most candidates before any sandbox run — same staging as the existing guardrails-before-eval ordering in the improver.
- Suite size is a recurring cost multiplier — every adopted case re-runs on every future candidate evaluation. A handful of high-mutation-score anchors beats a large low-signal suite (arXiv 2501.12862).
- Run the N=5 flakiness check and mutation probe ONCE at adoption time and cache the verdict; only plain PASS/FAIL of trusted anchors runs per candidate.

**References:**
- Meta TestGen-LLM (FSE 2024): https://arxiv.org/abs/2402.09171 ; Mutation-Guided LLM Test Generation at Meta: https://arxiv.org/pdf/2501.12862
- Coverage vs mutation-score gap: https://arxiv.org/html/2506.02954v4 ; LLM oracles encode actual-not-expected behavior: https://arxiv.org/html/2410.21136v1
- EvalGen 'Who Validates the Validators?' (UIST 2024): https://arxiv.org/abs/2404.12272 ; property-based tests with LLMs: https://arxiv.org/pdf/2307.04346 ; Qodo cover-agent: https://github.com/qodo-ai/qodo-cover

### Generated-Case Trust Model

- Generated cases live in their own file (machine-authored marker), separate from
  human-authored cases.
- Personal tier: machine-authored cases count toward the gate minimum.
- Enterprise/federal: machine-authored cases supplement but do NOT satisfy the
  `min_golden_cases >= 3` human-authored minimum (OQ-3 preserved).
- A human-edited generated case loses its machine-authored marker and starts counting
  toward the federal minimum.

### Research Insights

**From Solutions Archive:**
- [Tier Must Flow Through Construction](.claude/solutions/security-issues/2026-04-18-tier-must-flow-through-construction.md) — the tier-aware human-authored count must read the constructed tier, never a per-call default, or the gate's audit events will lie about the assurance level.

**Best Practices:**
- Use the industry-standard in-file `@generated` pragma (Phabricator/Meta convention; GitHub Linguist `linguist-generated`) detected in the same single AST pass `load_suite()` already performs — an in-file marker travels with the file, unlike path-based classification (docs.github.com; javax.annotation.Generated).
- NIST SP 800-218A (Generative-AI SSDF profile) treats AI-generated artifacts as requiring independent human review, never self-certifying — which is exactly this decision's tier split: machine cases count at personal, supplement-only at enterprise/federal.
- Make the human minimum an explicit attested count: add a provenance/machine_authored field to EvalCase so the gate computes human_authored >= 3 deterministically. Note: evalgate.py currently has NO provenance concept — the min_golden_cases check counts all cases regardless of author and must be split tier-aware (in-toto/SLSA attestation model).
- Enforce provenance-stripping on human edit with tooling (pre-commit/CI diff detection), not trust in the marker's continued presence — the marker means 'untouched by a human'; any human hunk invalidates it (SSDF PW.7).
- At enterprise/federal, back the marker with a Sigstore-signed in-toto attestation bound to the file digest, written by the trusted generating harness — never let the model self-assert provenance (actions/attest-build-provenance reaches SLSA Build L2).

**Edge Cases:**
- Partial human edit of a generated file: file-level demotion-to-human on any human hunk is the defensible gate rule — it can never under-count the human minimum (element-level markers à la javax @Generated are a later refinement).
- Marker spoofing by the LLM itself — emitting `@generated` to inflate personal-tier counts or omitting it to smuggle cases past the enterprise human minimum: the trusted harness writes the marker outside model-controlled content (separate manifest entry), attestation-backed at high tiers.
- Move/rename laundering: load_suite keys nodeids on relative path, so classification must key on the in-file marker or attestation digest, never the directory — moving a file must not change its provenance.

**Performance:**
- Detect provenance in the existing single AST parse per file, bounded to the file header (Phabricator/Linguist scan the first few KB — O(1) per file).
- Attestation verification is network/crypto-bound: verify once per bundle/commit and cache the verified digest set; skip signing entirely at personal tier (text marker only).
- Keep provenance as a precomputed EvalCase attribute so the accept/reject path does set-cardinality only — no I/O inside the gate decision.

**References:**
- NIST SP 800-218A: https://nvlpubs.nist.gov/nistpubs/SpecialPublications/NIST.SP.800-218A.pdf
- GitHub Linguist generated markers: https://docs.github.com/en/repositories/working-with-files/managing-files/customizing-how-changed-files-appear-on-github
- in-toto/SLSA provenance: https://slsa.dev/spec/v1.0/distributing-provenance ; Sigstore attestation action: https://github.com/actions/attest-build-provenance

### Kickoff Triggers and Configuration

- Event-driven, NOT an every-X-turns poller (turn count does not correlate with skill
  usage; existing improver triggers are usage-based or time-based).
- Four triggers: (1) on `create_skill`; (2) lazily when `maybe_improve` fires and
  `load_suite()` is empty or placeholder-only — generate before gating; (3) add-only
  suite extension after an applied mutation (regression anchors never regenerate away);
  (4) backstop pass folded into the existing hourly Curator sweep, busiest skills first.
- Config: new `SuiteConfig` sub-block `[modules.skills.improver.suite]` in
  `arcagent.toml`, forwarded verbatim to arcskill `ImproverConfig` (sibling of
  `LifecycleConfig` / `ChangeBoundConfig`). Fields: `autogen` (bool),
  `min_cases`, `max_cases`, `generate_on_create`, `extend_after_mutation`.
- Per-skill override rides the existing frontmatter `improver:` block.

### Research Insights

**From Solutions Archive:**
_(none)_

**Best Practices:**
- The hybrid event+backstop model is already repo-native: both existing sweep loops (skills lifecycle, memory consolidate) own their asyncio.sleep and re-read live cadence each cycle, so config changes apply next tick without restart — follow that shape, not the decorator interval.
- Make the backstop a threshold/idle predicate, never a wall-clock counter: memory's consolidate_poll_once fires only on 'work pending AND (enough accumulated OR idle long enough)' and short-circuits immediately when nothing is pending — an hourly suite sweep should cost one predicate check per skill when the event path kept things fresh.
- Reuse the improver's fire-and-forget hygiene verbatim: strong-reference task set + done-callback discard + Semaphore(max_concurrent) — the CPython-documented remedy for background tasks being garbage-collected mid-flight (improver.py already does this; cpython#91887).
- Fail-open in the background, fail-closed on consequence: swallow non-cancel exceptions in the loop (re-raise CancelledError), but route anything consequential through the tier approval ladder.
- Model SuiteConfig as its own Pydantic model with extra='forbid', defaulted fields, and Field(default_factory=...) nesting — the LifecycleConfig/ChangeBoundConfig precedent; a misspelled TOML key becomes a load-time error instead of a silent no-op.

**Edge Cases:**
- Thundering herd on first run or bulk import (N suite-less skills discovered in one tick), and 1000s of agents sweeping at the top of the hour: semaphore cap + per-skill jitter + priority ordering (most-used first) so the cap degrades gracefully (standard cache-stampede remedy).
- Suite generation racing an in-flight improvement pass on the same skill: the gate reads load_suite() mid-pass, so a suite appearing under it changes the acceptance decision. Single-flight dedup keyed by skill name — the scheduler's _in_flight set is the in-repo pattern — or a per-skill asyncio.Lock covering both generation and optimization.
- Config reload mid-generation: snapshot the values a task needs at spawn time (the improver already binds tier/config at construction); new limits govern subsequently-spawned work only — never reach into a running semaphore.
- Backstop double-claiming a lazily-triggered skill: the claim-then-clear check must run inside the sweep too, mirroring memory's counter reset after firing, so event and cron paths cannot both claim one skill.
- Graceful shutdown: cancel the loop, then drain in-flight tasks (the aclose() gather pattern) — or a half-written suite is left on disk.

**Performance:**
- Size the semaphore to the expensive resource (LLM + sandbox) with a conservative default — the OWASP LLM10 unbounded-consumption posture in CLAUDE.md argues for a small cap plus a config knob.
- O(1) dedup membership check before spawn naturally coalesces a burst of triggers for one skill into a single pass.
- Lazy on-first-need beats eager generation-for-all-at-boot: protects the <500ms cold-start / <50MB per-agent budgets and never pays for skills that don't run.
- Add jitter to sweep scheduling to desynchronize thousands of concurrent agents.

**References:**
- In-repo: modules/skills/capabilities.py (sweep loop), modules/memory/capabilities.py (threshold/idle predicate), improver.py (_spawn/_guarded/semaphore/aclose), scheduler.py (_in_flight single-flight), improver/config.py (sub-block precedent)
- asyncio task hygiene: https://docs.python.org/3/library/asyncio-task.html ; https://github.com/python/cpython/issues/91887
- Thundering herd/jitter: https://redis.io/blog/how-to-tame-the-thundering-herd-problem/ ; Dependabot event+cron hybrid: https://docs.github.com/en/code-security/reference/supply-chain-security/dependabot-options-reference

### create_skill Scaffold Replacement

- Replace the `test_placeholder` (assert True) scaffold with a generated suite at
  creation time. The placeholder actively bricks improvement: one always-passing case
  means the strict-improvement rule (>=1 previously-failing case must flip) can never fire.

### Research Insights

**From Solutions Archive:**
_(none)_

**Best Practices:**
- Use a generate-validate-repair loop with a bounded repair budget (2-3 rounds) before persisting — parse the toolchain error and feed it back for self-correction; one-shot emission frequently yields uncollectable tests (ChatUniTest/TestGen-LLM pattern, arXiv 2511.21382, 2402.09171).
- Gate the result through pytest --collect-only discoverability plus an assertion-quality reject (AST-reject bodies whose only statement is `assert True`/`pass`, flake8-assertive/Ruff PT-class checks) so a placeholder can never count as an EvalCase again.
- Model the replacement as a fail-closed post-generation hook, the way cookiecutter's post_gen_project halts and cleans the output tree on non-zero exit: refuse to finish create_skill with only trivial cases present.

**Edge Cases:**
- Empty body sections (create_skill accepts body=''): generation has nothing to assert against and will emit superficial or hallucinated tests. Fallback: emit NO cases and let no_suite_policy govern — safer than a green placeholder.
- Generation LLM unavailable at create time: never fall back to `assert True`. CONFIRMED CRITICAL: the placeholder makes load_suite non-empty, so EvalGate never reaches no_suite_policy — the enterprise/federal fail-closed 'prose blocked with no suite' rule is silently bypassed by a fake case. Write no evals file so the fail-closed policy holds until real cases exist.
- Created-then-immediately-used race: load_suite tolerates OSError/SyntaxError per file, so a mid-generation improver run sees fewer cases than intended — write the suite atomically (write-then-rename) and gate reload on completion.

**Performance:**
- Synchronous generation puts an LLM call + repair loop on the create_skill critical path — cap repair iterations to keep worst-case latency predictable.
- Preferred: decouple entirely — create_skill writes no golden cases synchronously; generation runs async (or on first improver need) while fail-closed no-suite policy holds the line. This removes LLM latency from the create path completely.
- pytest --collect-only is execution-free; defer multi-run stability checks to the async improver, never the synchronous create path.

**References:**
- cookiecutter hooks (fail-closed post-gen): https://cookiecutter.readthedocs.io/en/stable/advanced/hooks.html ; cargo-generate hooks: https://cargo-generate.github.io/cargo-generate/templates/scripting.hook-types.html
- flake8-assertive: https://github.com/jparise/flake8-assertive ; Ruff PT009: https://docs.astral.sh/ruff/rules/pytest-unittest-assertion/
- LLM test-gen survey: https://arxiv.org/pdf/2511.21382 ; 'When Generated Tests Pass but Don't Protect': https://dev.to/jamesdev4123/when-generated-tests-pass-but-dont-protect-llms-creating-superficial-unit-tests-24c0

### CLI and UI Eval Surface

- Extend the existing `arc skill` command group: `arc skill evals <name>` (list cases +
  latest gate pass/fail), `arc skill evals regen <name>`, `arc skill evals edit`
  (open in `$EDITOR` — cases are plain pytest files in `evals/`).
- arcui gets a per-skill evals view (read surface) so generated cases can be reviewed
  when they are "off a bit".

### Research Insights

**From Solutions Archive:**
_(none)_

**Best Practices:**
- Validate-on-save loop, git/kubectl style: copy to temp file, launch $VISUAL then $EDITOR (block until exit; non-zero exit or unchanged buffer = abort, as git does), re-parse on return, and on failure print the error and RE-OPEN the same buffer preserving edits (kubectl edit's reject-and-reopen). This is load-bearing: load_suite silently drops SyntaxError files, so the CLI edit path is the only place a syntax error gets caught.
- Commit only via atomic temp-file + os.replace — the exact pattern arcskill's lock.py already documents ('a crash during write never leaves a partial record'); a half-written test_*.py silently corrupts the golden suite.
- Validate against the gate's real contract, not just syntax: warn when a save yields zero test functions or drops a code-kind skill below min_golden_cases=3, since that silently disables all future code self-improvement (fail-closed).
- Keep the arcui surface read-only and discovery-free, mirroring the knowledge.py seam: a GET route consuming the discovery facade, distinguishing empty-but-OK (200) from unreadable (503) — no globbing or AST walking inside arcui.
- Match the existing `arc skill` conventions: subparser per verb, _SUBCOMMAND_MAP dispatch, _write/_print_table formatting, and a 'Next steps: arc skill validate …' hint like _create emits.

**Edge Cases:**
- Invalid save silently shrinks the suite (load_suite swallows the SyntaxError and the case vanishes) — a code mutation could then pass the gate against fewer cases; an unparseable save must never reach evals/.
- Concurrent edit while the improver is mid-pass changes the before/after pass-sets non-atomically and can invalidate the gate decision — take the lock/snapshot and warn the user an improvement pass is running before opening $EDITOR.
- Editing or deleting the only passing anchor: if a previously-passing case now fails, every future candidate reads as a regression and improvement is permanently blocked — warn before an edit reduces the passing-anchor count.
- Regen clobbering hand-edited cases: show a diff and confirm before overwrite (kubectl edit / promptfoo convention) so human-curated anchors are never lost to automation.

**Performance:**
- List/view is a pure static AST walk — never import or execute test modules to enumerate them.
- Never run pytest in the edit/list/save path; execution belongs to the tier sandbox and is the slow, security-gated step. Validate-on-save does ast.parse only.
- Cache the discovered case list per skill, invalidate on directory mtime, and paginate with limit/offset as knowledge.py does.
- Preserve load_suite's sorted-rglob ordering so CLI and web output are stable and diffable.

**References:**
- In-repo: evalgate.py (load_suite silent-skip, min_golden_cases, strict-improvement), sandbox_runner.py (ARC_EVAL harness), lock.py (atomic write), arccli commands/skill.py + _shared.py, arcui routes/knowledge.py + agent_detail/skills.py
- External conventions: git commit/rebase -i ($VISUAL/$EDITOR, abort semantics), kubectl edit (validate + reject-and-reopen, diff before apply), promptfoo / openai-evals CLI UX

### Turn-End Outcome Classifier

- A cheap turn-end classifier reads the turn transcript for implicit feedback — user
  corrected the agent, immediately re-asked, expressed dissatisfaction — and produces
  `task_outcome`, filling the existing producer-less `outcome_source="evaluator"` slot.
- Lives arcagent-side (it already has the eval LLM and the transcript at
  `agent:pre_respond`); forwards only the resulting label through the existing unused
  `on_turn_end(outcome=...)` parameter. Zero arcskill API change; arcskill stays
  provider-free.
- Josh's framing: this is the policy-engine shape applied to skills — passive
  evaluation over artifacts, gate, audit every decision.

### Research Insights

**From Solutions Archive:**
_(none)_

**Best Practices:**
- Fuse multiple weak signals rather than trusting one: combine the existing per-skill error-count heuristic WITH transcript-derived signals (correction/re-ask/sentiment), scored at task level — neither alone decides the label (Microsoft satisfaction-prediction line; arXiv 2006.07113 hybrid framework).
- Give the judge an explicit dissatisfaction taxonomy, not free-form 'is the user happy': termination/abandonment, interruption, error-correcting language, negative-sentiment/frustration, and request reformulation — each mapped to success/failure/partial (arXiv 2010.12251, 2411.17437).
- Treat the label as a NOISY training signal, never ground truth: EMNLP 2025 shows in-dialogue feedback is a good UX lens but noisy for learning — require aggregation over many traces before a failure label triggers a skill rewrite (aclanthology 2025.emnlp-main.133).
- Reserve bias mitigation for the escalated subset: fixed presentation order, 2-3x self-consistency voting on ambiguous turns only, and monitor the success/failure/partial base rate for drift (CalibraEval, position-bias studies).
- Cheap-first cascade: a small classifier handles common unambiguous turns, escalating only low-confidence ones to a stronger judge — and watch the escalation rate (beyond ~40% the overhead outweighs savings).

**Edge Cases:**
- Attribution error — the sharpest one: frustration is conveyed cross-turn through context and repetition, and close_turn currently stamps EVERY active skill span with the same turn outcome, so in a multi-skill turn one failing skill penalizes the innocent ones. Require the judge to name the responsible target; bind a failure to a skill only when attribution is explicit, else emit partial/abstain, and credit-split using the per-skill error counts.
- Sarcasm and ambiguous phrasing invert lexical polarity ('great, exactly what I didn't ask for') — instruct the judge to weigh behavioral signals (re-ask, correct, abandon) over tone.
- Silence is ambiguous — a user quietly moving on is often success, silent abandonment is not: default to success or abstain, never failure, to avoid punishing skills that quietly worked.
- Adversarial praise: LMSYS analysis found the highest-praised utterances were often jailbreak encouragement — gate positive labels behind a quality/safety check or the self-improving loop can be steered toward harmful behavior (ASI01/ASI09 relevance).

**Performance:**
- Never block the respond path: fire-and-forget after close_turn persists the span — the label is consumed later by improver aggregation, so judge latency is off the user's critical path.
- Cheap heuristic pre-filters (regex/embedding detection of correction/rephrase/negative sentiment) trigger the LLM judgment only when a candidate signal is present — most turns cost near-zero; reserve self-consistency voting for the escalated subset.
- Bound the transcript window: current turn plus a small number of prior turns balances cross-turn attribution against token cost and position bias.
- Compute the label once at write time and read it in bulk during improvement — never re-judge historical traces per pass.

**References:**
- User Feedback in Human-LLM Dialogues (EMNLP 2025): https://aclanthology.org/2025.emnlp-main.133 ; implicit-feedback taxonomy: https://arxiv.org/pdf/2010.12251
- User frustration detection: https://arxiv.org/html/2411.17437v2 ; hybrid satisfaction prediction: https://arxiv.org/pdf/2006.07113
- LLM-judge calibration: CalibraEval https://arxiv.org/pdf/2410.15393 ; position bias: https://aclanthology.org/2025.ijcnlp-long.18.pdf

### Trace Arg Capture and Promotion

- Config-gated arg capture for tool calls attributed to a skill span (today
  `observe()` forwards no args; `args_hash=""`). Hash-only remains the federal default.
- Trace promoter: verified-good traces (evaluator-labeled success) distilled into
  deterministic replay cases for the golden set; observed failures become repro cases —
  the legitimate kind of failing golden case, encoding real breakage rather than LLM
  speculation.

### Research Insights

**From Solutions Archive:**
_(none)_

**Best Practices:**
- Scrub BEFORE persistence, never after: VCR.py's filter_headers/before_record model replaces credentials and PII with placeholders before the cassette lands on disk. Route captured args through the existing sanitize path (sanitize_text is already applied to task_summary) at the observe() boundary, so hash-only remains the untouched federal default.
- Make replay strictly hermetic and offline: pytest-recording defaults record_mode='none' and blocks network so the fixture — not the live world — drives the run; promoted replay cases need the equivalent 'no live tool execution' gate.
- Never auto-bless: recorded baselines are committed artifacts; re-blessing a replay case's expected output after a legitimate contract change is a deliberate, human-reviewed action, or trust erodes and failures get ignored (snapshot-testing literature).
- Determinism is a precondition of promotion, not an afterthought: a trace is promotable only if its tool-call ordering and inputs are captured completely enough to reproduce the span deterministically (CACM deterministic record-replay).
- Hash-only-at-federal directly implements NIST SI-12(2) minimize-PII-in-testing; promoted cases must also carry a retention/disposal path per SI-12(3).

**Edge Cases:**
- Non-deterministic tool outputs (timestamps, generated IDs, network responses) break exact-equality replay — canonicalize volatile fields or match dynamic regions with regex/normalizers instead of literals.
- Secrets/CUI in args: scrub pre-write, and note an unsalted SHA-256 of a low-entropy secret is brute-forceable — args_hash is a shape fingerprint, not a confidentiality control, which is exactly why federal stays hash-only.
- Obsolete repro after a legitimate contract change: pin promoted cases to skill_version (already in the SkillTrace schema) plus an expiry/quarantine path so stale repros retire visibly rather than becoming ignored false failures.

**Performance:**
- Arg capture multiplies per-line JSONL size — keep it opt-in per config/skill and lean on the existing monthly rotation for bounding and SI-12(3) age-based disposal.
- load_traces() linearly scans all monthly files — promote the small verified set into a dedicated compact case store rather than re-scanning the trace corpus at replay time.
- Replay runs at fixture-read speed precisely because it avoids live work; keep fixtures small and focused (exclude dynamic regions) for cheap diffs and fast runs.

**References:**
- vcrpy filtering: https://vcrpy.readthedocs.io/en/latest/advanced.html ; pytest-recording: https://github.com/kiwicom/pytest-recording
- Deterministic record-and-replay (CACM): https://cacm.acm.org/practice/deterministic-record-and-replay/
- NIST SP 800-53 SI-12 / SI-12(2) / SI-12(3): https://csf.tools/reference/nist-sp-800-53/r5/si/si-12/

### Skill Version History and Diffs

- `CandidateStore` already persists seed snapshot, every candidate file, manifest with
  generation/scores/active-flag, and the append-only audit log — versions and diffs are
  a read surface over existing data.
- arcui: per-skill panel with version timeline (seed → gen N, judge scores + gate
  results per candidate), side-by-side diff, rollback button wired to the existing
  `improver.rollback()`.
- Ingestion into arcstore; arcui pulls (SPEC-026 rule — no parallel push wires).
- CLI: `arc skill history <name>`, `arc skill diff <name> <candidate-id>`.

### Research Insights

**From Solutions Archive:**
_(none)_

**Best Practices:**
- Pure read projection, never a second writer: arcstore is a pure file-tailer and arcui's query layer never touches a backend driver — ingest seed.md/candidates/*.md/manifest.json into store tables and render only from those, matching LangSmith/Humanloop/PromptLayer immutable-version UX.
- Build the timeline from the manifest's lineage graph (generation, parent_id, active_candidate_id), not filesystem mtime — it is a DAG (a generation can have multiple frontier candidates); badge active_candidate_id as 'live' (the W&B artifacts / MLflow registry model).
- Offer unified AND side-by-side diff with word-level intra-line highlighting (difflib.SequenceMatcher / jsdiff / diff-match-patch), defaulting to seed-vs-active; diff the raw markdown that actually ships, not rendered HTML.
- Gate rollback behind an explicit confirm naming the exact target and emit one ui.mutation audit event (AU-3) recording from→to candidate ids — candidate_store.rollback() is deliberately non-destructive (flips active_candidate_id), so the confirmation IS the safety layer.
- Treat candidates as immutable and content-addressed: key diff caches and permalinks on (hash_a, hash_b) content hashes, not generation numbers, so deep links survive frontier pruning.

**Edge Cases:**
- Candidate file pruned while the UI references it: load() returns None — render a 'content no longer on disk' tombstone (manifest metadata survives), disable rollback to that id, and keep serving the last-ingested body from the store snapshot.
- Non-adjacent or sibling diffs: the DAG makes 'previous version' ambiguous — let the user pin any two candidate ids as A/B, optionally annotating intermediate hops via the parent_id chain and audit MutationEvents.
- Rollback target not re-validated: its stored scores are as-of its own generation — warn that they are historical, offer a re-gate before promotion, and never conflate rollback with revive (retired lifecycle state is a distinct operation).
- Ingest lag/torn reads: manifest writes are atomic but the tailer polls (~2s) — treat manifest active_candidate_id as live-badge truth and show an 'ingesting…' state for manifest-present/store-absent candidates.

**Performance:**
- Ingest incrementally via the arcstore per-file byte cursor; diff the manifest against last-ingested and upsert only changed ids (content-hash keyed, INSERT OR IGNORE) — O(1) work per save.
- Use the after_seq cursor-incremental read pattern so timeline polls are O(new rows), not O(history).
- Compute diffs lazily server-side, memoized on the content-hash pair; fall back to line-level/hunk-collapsed view for very large documents.
- Ship metadata only (generation, parent, scores, gate outcome, size/hash) in timeline responses; fetch full markdown only when a candidate is opened.
- Verify the WORM chain at ingest and stamp the row; read the stored verified flag on the hot path — never re-run verify_chain per request.

**References:**
- In-repo: candidate_store.py (manifest lineage, non-destructive rollback, append_audit), arcstore ingest.py/query.py (byte cursors, content-derived ids, after_seq), arcui audit.py (ui.mutation taxonomy, AU-3)
- Product UX: LangSmith Prompt Hub compare-versions, Humanloop deploy/rollback, PromptLayer registry diff, W&B Artifacts / MLflow Model Registry lineage
- Myers, 'An O(ND) Difference Algorithm' (1986); Python difflib; jsdiff; Google diff-match-patch

### Toggles and Audit

- Three toggle levels, all existing patterns: `adapter = "none"` master off (current
  default); `outcome_classifier` bool under the improver block kills the trace-judging
  half; per-skill frontmatter + `exempt_tags` (security-critical/compliance/auth) for
  surgical opt-outs.
- Every toggle flip emits an audit event — disabling self-improvement on a federal
  deployment is a compliance-relevant act.

### Research Insights

**From Solutions Archive:**
- [Tier Must Flow Through Construction](.claude/solutions/security-issues/2026-04-18-tier-must-flow-through-construction.md) — resolve the layered toggle stack once at construction, not per-call; the archived failure mode was audit events lying (tier=personal in a federal deployment) while enforcement was correct.

**Best Practices:**
- Every flip emits an audited config_change with from→to, actor/resolver identity, and timestamp, fired BEFORE the downgrade takes effect and at-most-once — this is the D-444 pattern already implemented in arcllm telemetry (_maybe_audit_disable: 'an operator turning capture off can't blind the forensic trail without leaving a trace'); maps to AU-2 and CM-5(1).
- Classify toggles by Fowler's taxonomy and govern accordingly: the master adapter switch and any hard stop are ops/kill-switch toggles (dynamic, security-relevant, mandatory audit); per-skill frontmatter opt-outs and exempt tags are permission toggles with different retention/approval needs.
- Deny-by-default deterministic precedence with recorded REASON codes (OpenFeature evaluation-details model): global master-off dominates per-skill-on, and the audit trail distinguishes 'suppressed by master' from 'never requested' so an auditor reconstructs the decision, not just the outcome.
- Treat disabling self-improvement as a kill-switch control: actuation restricted and human-reachable (CM-5, CM-3(5); EU AI Act Art. 14 human-oversight analog), and the disable event must emit through a path that survives the disable itself.

**Edge Cases:**
- Toggle flipped mid-improvement-pass: bind the decision at the pass boundary — in-flight work completes under the snapshot it started with, the flip governs the next pass, and the config_change timestamp marks the boundary. A true emergency kill is the exception: it aborts in-flight work with a distinct abort event.
- Frontmatter opt-out on a skill that is mid-mutation: read the opt-out at mutation-unit start and treat it as immutable for that unit — a self-improving system that rewrites its own frontmatter must NOT have that change honored live within the same pass (ASI06 self-modification poisoning vector); it takes effect next evaluation, audited.
- Global-off vs per-skill-on conflict: master wins, and the per-skill 'on' is audited as OVERRIDDEN rather than silently dropped; exempt tags must never be able to re-enable under a global off, or the master switch is not a true kill switch.
- The flip that blinds auditing itself: disabling the classifier or adapter fires its own audit record before the downgrade, guarded to fire exactly once (pending-flag) — preventing both silent blinding and audit-log flooding.

**Performance:**
- Resolve the stack once per construction/pass and cache the snapshot — no per-call frontmatter re-parse; exempt-tag checks are O(1) membership against a precomputed set.
- Emit audit events off the hot path (callbacks fired outside locks — the arcllm config_controller pattern).
- Idempotent disable-audit (fire-once flag) matters at 1000s-of-agents scale — reading a disabled toggle thousands of times must not generate thousands of records.
- Defer behavior-changing flips to pass boundaries to avoid mid-flight re-evaluation and audit/enforcement skew — a truthful-by-construction trail is worth the small operational delay.

**References:**
- Fowler, Feature Toggles: https://martinfowler.com/articles/feature-toggles.html ; OpenFeature flag-evaluation spec: https://openfeature.dev/specification/sections/flag-evaluation/
- NIST SP 800-53 AU-2: https://csf.tools/reference/nist-sp-800-53/r5/au/au-2/ ; CM-3/CM-5: https://csf.tools/reference/nist-sp-800-53/r5/cm/cm-5/
- In-repo: arcllm telemetry.py (_maybe_audit_disable, D-444), config_controller.py (actor + changes diff), tier-through-construction solution doc

### Open Questions

- Should generated failing cases (quarantined improvement targets) ever auto-promote,
  or always require human review to enter the gate?
- What transcript window does the outcome classifier see (full turn vs last N messages),
  and what is its cost budget per turn?
- Where does the machine-authored marker live — file naming convention
  (`test_golden_generated.py`) vs in-file pragma vs manifest entry?
- Does suite extension after mutation run synchronously in the improvement pass or as
  a follow-up background task?

### Research Insights

**From Solutions Archive:**
_(none)_

**Best Practices:**
- OQ-1 (auto-promote quarantined failing cases?): research says never — a failing LLM-generated case can encode a wrong expectation (the inverse of bug-freezing), and snapshot practice forbids auto-blessing. Trace-derived repro cases are the trusted failing kind; LLM-invented failing cases require human review to enter the gate (arXiv 2410.21136; snapshot-testing literature).
- OQ-2 (transcript window + cost budget): a sliding window of the current turn plus a few prior turns — frustration attribution is cross-turn but full-history inflates tokens and position bias; pair with heuristic pre-filters so the LLM judge fires only on candidate signals, keeping most turns near-zero cost with escalation under ~40%.
- OQ-3 (marker location): in-file `@generated` pragma detected in load_suite's existing AST pass, plus a manifest entry written by the trusted harness (attestation-backed at enterprise/federal). Path/file-naming conventions alone launder provenance on move/rename.
- OQ-4 (sync vs async suite extension after mutation): decouple — run the add-only extension as a follow-up background task through the same single-flight per-skill dedup, keeping mutation-pass latency bounded and avoiding a gate-read race within the pass.

**Edge Cases:**
_(none)_

**Performance:**
_(none)_

**References:**
- Each recommendation cross-references the research under its owning section above (Suite Generator, Turn-End Outcome Classifier, Generated-Case Trust Model, Kickoff Triggers and Configuration).

