---
topic: "editable system prompts"
date: 2026-07-21
status: complete
---

# editable system prompts

## Inspiration

Immediate trigger: a new deployment needs different memory extraction types and different consolidation behavior, and there is no seam to vary that without forking Python. Standing frustration: tuning the general agentic system prompt — telling an agent about its tools better, adjusting tone or strategy guidance — means grepping source, editing a triple-quoted string, redeploying, and hoping. Prompts are the highest-leverage behavior surface in the harness and the only one that is invisible, unversioned, and untunable at runtime. Everything else in Arc is inspectable: traces, policy, config, skills, memory. Prompts are the blind spot. Longer arc: if prompts become first-class artifacts with eval suites attached, they stop being static text and become an optimization surface — GEPA-style reflective evolution over real execution traces, SkillOpt-style scoring, promptfoo-style regression gating, built into Arc rather than bolted on.

## Projects

1. arcprompt becomes the real prompt-management package — today it is a 3-line stub with zero importers and its own README says the logic 'currently lives in arcrun.prompts'. Clean slate, no teardown cost.
2. Per-package context/ folders holding every prompt as versioned markdown with frontmatter metadata.
3. arcui prompt viewer/editor with version history and diff-vs-stock, alongside the existing skills editor.
4. Later: eval suites per prompt, trace-fed variant generation, Pareto selection, operator-signed promotion.

## Audience

Four distinct readers, all real:
- **Josh, tuning the fleet.** Wants raw markdown and a fast edit → observe → keep loop, from arcui or straight on the DGX box. Zero friction.
- **Deployment owner (non-Arc-dev).** Stands up Arc for their org, needs domain-fit prompts without touching Python. Needs stock defaults, safe rails, and reset-to-stock.
- **Federal reviewer / auditor.** Never edits. Must view, diff across versions, and see who changed what, when, and under whose signature. Prompts are policy.
- **The agent itself.** Proposes prompt variants from traces and evals. Never promotes — see Guiding Principles.

## Use Cases

- Josh retunes arcmemory's consolidation prompt for a new deployment's extraction types, from arcui, without a redeploy or a fork.
- Josh adds tool-awareness guidance to the general agentic prompt and watches the next few runs in the trace dashboard to judge it.
- A deployment owner overlays domain-specific prompts on stock, and can always see the diff against what shipped.
- An auditor opens a prompt, reads its full version history, and confirms every change carries an operator signature.
- Later: the improver reads execution traces, proposes a reflective mutation to a prompt, an eval suite scores it against the incumbent, and the winner is presented to an operator to sign.

## Desired Outcomes

Harness behavior becomes tunable in minutes rather than a code-edit-redeploy cycle. Behavior that today can only be explained by reading Python becomes explainable by reading markdown. A new deployment varies its prompts without forking Arc. Prompts join traces, policy, config, and skills as inspectable, version-controlled, auditable artifacts — closing the last blind spot in the observability story. And a foundation exists for prompts to improve themselves over time from real fleet traces instead of staying frozen at whatever was typed once.

## Guiding Principles

**The optimizer proposes, a human signs.** Every tier, no exceptions. The agent may generate and score prompt variants; promotion to active always requires an operator signature. This keeps the trust story identical from personal to federal and keeps eval machinery honest.
**Prompts are policy.** Anything entering a model's context is a governed artifact — versioned, diffable, attributable. Consistent with skill markdown already being signature-verified precisely because it enters the prompt.
**Stock ships, overlays vary.** A deployment must never have to fork Arc to change wording, and must always be able to see its drift from stock.
**Inspectable by default.** If behavior is driven by text, that text is readable in arcui without shell access.
**Every package, one pattern.** Not a special case for arcmemory — the same seam across arcrun, arcagent, arcmemory, arcskill and whatever comes next.

## Constraints

Existing reality that shapes this:
- ~25 prompt sites across arcrun, arcagent, arcmemory, arcskill. Largest: arcagent/modules/workpad/prompt.py:12 (~4.9 KB), arcmemory/agent_consolidate.py:25 (~4.1 KB), arcagent/orchestration/prompts.py:10.
- arcrun's prompts are entirely static authored text — every strategy prompt_guidance and description is a plain literal with zero interpolation, and get_strategy_prompts() is pure selection-plus-join (pick sections by allowed_strategies and tool_names, join them). strategy_selection's one apparently-dynamic piece is a static preamble joined with each strategy's static description. Each fragment is independently editable; compilation stays a few lines of join in code.
- arcrun/strategies/code.py:14 _DEFAULT_PREFIX duplicates CodeExecStrategy.prompt_guidance almost verbatim and is only reachable via a ctor arg nothing in src/ passes. Externalizing surfaces the duplication; the no-legacy standard says it dies in the same edit.
- A hot-reload precedent already exists: ContextManager.assemble_system_prompt re-reads identity.md and context.md from the workspace on every call and emits agent:assemble_prompt on the module bus for module injection.
- arcui already has the editing surface: operator-gated file write with path confinement and secret scanning, plus skill versioning with diff and rollback.
- The eval golden gate is currently inert — no eval suites exist yet. Anything gated on evals is blocked until they do.
- No TOML prompt overrides exist today; the only override hook (CodeExecStrategy system_prompt_prefix) is unused in production.
- mypy --strict, ruff clean, core LOC budget, no backward-compat shims: old hardcoded strings get deleted in the same edit that externalizes them.

## Scope

**In:** v1 is externalize + view/edit. Every prompt across the arc packages moves out of Python into versioned markdown under a per-package context/ folder with frontmatter metadata (name, version, and whatever else proves necessary), loaded at runtime through arcprompt, and viewable and editable in arcui with version history and diff-against-stock. Success test: Josh retunes arcmemory extraction behavior for the new deployment from the UI, and an auditor can read every prompt the fleet is running.

**Out:** Explicitly v2 and later, on top of the v1 spine: eval suites per prompt; regression gating on edits; trace-fed reflective mutation (GEPA); Pareto-frontier variant selection; SkillOpt-style scoring; automatic promotion of any kind. Also out: prompt A/B traffic splitting across live agents; a prompt marketplace or hub; migrating skill markdown into this system (skills already have their own versioned, signed pipeline).

## Open Questions

- Frontmatter schema: what beyond name and version actually earns its place (owner, tier, tunable-or-not, eval-suite ref)?
- The dividing line is authored text vs runtime data, not static vs dynamic. Authored text (all of arcrun's strategy fragments, workpad, consolidation, spawn guidance) externalizes to markdown with selection-and-join left in code. Runtime data (capability_registry.format_for_prompt()'s generated XML manifest, arcskill improver f-strings interpolating trace excerpts) is generated, not authored. Open: does v1 need a slot or placeholder mechanism at all, or is selection-plus-join sufficient — and are the improver's per-call builders even the same kind of artifact as harness prompts?
- Where do overlays live and how does diff-vs-stock get computed — package-relative, workspace, or store-backed?
- Does the prompt loader reuse the existing TOFU signature verification that skill markdown already gets?
- Reload semantics: per-call re-read like identity.md, or explicit reload — and what happens to an in-flight run mid-edit?
- Does arcprompt take ownership of arcskill.improver's existing prompt-building helpers, or leave them where they are?

## Related Solutions

_(solutions archive searched for `prompt` and `context` — no matches; archive is empty for this domain)_
