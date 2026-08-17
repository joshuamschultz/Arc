# ADR-031: Ad-hoc model-authored orchestration is a restricted script, not a declared graph

**Status**: Accepted
**Date**: 2026-08-16
**Relates to**: ArcFlow (`arcteam.workflow`, SPEC-061) — permanent, signed, reusable workflows; unaffected
**Supersedes**: the deleted `arcagent/modules/planning/` DAG planner (SPEC-040/043) and the deleted `plan_execute` arcrun strategy

## Context

An ad-hoc task — one invented for a single request, with no reuse value — sometimes has
independent parts: several things that can be investigated at once, a step repeated over many
items, one agent's work checked by another. The ReAct loop handles this by having the model plan
turn by turn, in prose, re-deciding the shape of the remaining work after every tool result. That
is enough for a single linear chain. It is not enough for genuine fan-out: the model has no way to
say "run these four things concurrently, then combine them" except by hoping its own turn-by-turn
tool calls happen to land that way.

Arc already had two answers to "let the model plan more than one step," and neither was live.
`arcagent/modules/planning/` was a complete LLM-driven single-agent DAG planner — the model
decomposed a task into a `Plan` of typed `PlanStep`s, an executor ran the ready frontier
concurrently under a budget, checkpoints were operator-signed. It was never wired: no blueprint or
team config ever set `PlanningConfig.enabled = True`. `arcrun`'s `plan_execute` strategy had no
caller and, if selected, silently returned an empty result. Both represented "the model plans" as
**declared structure** — a `Plan` object, a graph of typed steps — built by one model call and then
handed to an executor that walked it.

ArcFlow (SPEC-061) already owns the *other* kind of multi-agent orchestration: permanent,
operator-signed, deterministic workflows authored ahead of time and reused. It was never a
candidate for this problem — an ad-hoc task has no operator available to sign it and no reuse
value to justify the ceremony. The gap was specifically for orchestration invented for one request
and thrown away.

## Decision

**Ad-hoc, model-authored orchestration is expressed as a script in a restricted, interpreted
subset of Python — the `dynamic` arcrun strategy — not as a declared graph.**

A single model call authors a short script (`if`/`for`/`while`, arithmetic, comparisons,
container literals, f-strings, and nine host functions: `agent`, `parallel`, `phase`, `log`,
`budget`, `scratch_read`, `scratch_write`, `complete`, `pause`). The script is parsed against a
whitelist grammar (`arcrun.dynamic.grammar`) that rejects anything outside it — no `import`, no
attribute access, no `eval`, no clock or randomness — dry-run against a stub host
(`arcrun.dynamic.validate`) so a broken script is caught before it costs a token, and only then
interpreted for real. Its `agent()` and `parallel()` calls become bounded child runs through
`arcrun.dynamic.binding.RunHost`, the one Protocol (`arcrun.dynamic.host.ScriptHost`) naming every
effect the script can have on the world.

`arcagent/modules/planning/` is deleted outright, not deprecated — it was never enabled, and this
strategy does the same job with real control flow. Four pieces of it were good enough to be worth
carrying forward into `dynamic`'s own persistence, audit, grounding, and budget-accounting needs:
the operator-signed checkpoint sidecar, the WORM audit sink construction, the goal-hijack grounding
refusal for protected identity paths, and the reserve-then-settle concurrent budget accounting.
They are preserved verbatim in `packages/arcrun/src/arcrun/dynamic/SALVAGE.md` for whoever picks
each one up. The `plan_execute` strategy is deleted with no replacement need — `dynamic` is that
replacement.

## Rationale

**A declared graph is model-authored structure, and structure needs its own schema, validator,
and gate.** The deleted planner's `Plan`/`PlanStep` objects were a second surface a model output
could take: not text, not a tool call, but a typed graph that then had to be validated, grounded
against goal hijacking, checkpointed, and audited as a first-class artifact in its own right. Every
new field on that structure is a new thing an adversarial or merely-wrong model output can abuse,
and a new thing the runtime must defend.

**A script constrained to a narrow host surface is bounded by that surface, not by a schema
chasing every field.** The grammar has no production for escaping: an unsupported construct is a
*parse* error, before any evaluation. The only way a script affects anything outside itself is
through the nine names on `ScriptHost` — auditing "what can a generated script do" is reading one
Protocol, not reasoning about a language or validating an evolving object schema. The security
question moves from "is this graph well-formed and non-hijacking" to "is this token in the
whitelist," which is a much smaller and much more stable question.

**Determinism was a design goal, not an accident.** The interpreter reads no clock, no randomness,
no environment. That is what makes journal-based replay sound (`arcrun.dynamic.journal`): a resumed
run re-executes the identical script and gets the identical sequence of host calls back, so
already-performed effects (a spawned child, a scratch write) never happen twice. A declared graph
gets this for free by construction; a script had to earn it by removing everything that could make
two runs of the same source diverge.

### Alternatives considered

- **Real Python in the sandbox.** Run the model's script as actual Python inside the existing
  Docker/Firecracker sandbox. Rejected: a sandbox bounds *side effects on the machine* (filesystem,
  network, process), not *what the script can express*. Determinism, a fixed host-call surface, and
  parse-time rejection of unknown constructs all disappear — the language itself becomes the attack
  surface (ASI05) instead of nine named functions.
- **A bespoke DSL (YAML/JSON control flow, a custom mini-language).** Rejected: a model is
  measurably better at producing valid, idiomatic Python-shaped text than an invented syntax with
  no training-data precedent, and a bespoke grammar still needs its own parser, its own escape
  analysis, and its own documentation — all the cost of the restricted-Python approach with worse
  model output quality and no reuse of Python's own `ast` module for the whitelist walk.
- **Keep the declared-graph planner, just wire it up.** Rejected: it was unwired for months with no
  caller asking for it, its checkpoint/audit/grounding machinery existed to defend a general typed
  `Plan` schema that would keep growing, and it solved a strictly narrower problem (single-agent
  decomposition) than `dynamic` does (agent + parallel fan-out, in one pass).

## Consequences

**Positive**

- One Protocol (`ScriptHost`) is the entire audit surface for "what can a generated orchestration
  do," instead of a typed graph schema that grows a new validation rule per field.
- Determinism is structural, not policed — the grammar has no production that could read a clock or
  randomness, so replay-safety cannot regress through an added feature the way a schema's optional
  field can.
- A rejected or malformed script degrades to the ordinary ReAct loop after one correction attempt;
  there is no dead-end failure mode, only a more expensive one.
- Journal-based resume works the same way at every scale: replaying recorded host calls, not
  restoring a serialized interpreter frame that would have to track every language feature added
  later.

**Negative**

- The script language will eventually need a feature the current whitelist refuses (a new builtin,
  a new method), and each addition is a security review, not a routine change — see
  `packages/arcrun/CLAUDE.md`.
- Journal-based resume adds a durable per-run artifact under the agent workspace
  (`<workspace>/runs/dynamic/<run_id>/journal.jsonl`), which is new state to retain, size, and
  eventually prune. It is written with direct filesystem I/O and never through the LLM-facing file
  tools (ADR-029). `work_dir` remains optional at the `arcrun` layer, so a caller that supplies none
  silently loses resume rather than failing — a deliberate trade, since arcrun must never invent a
  path it has no business knowing.
- Two strategies now cover "the model plans more than one linear step" (`dynamic` for ad-hoc,
  ArcFlow for reusable-and-signed) and a reader has to know which one applies; this ADR and the
  `arcrun`/`arcteam` package docs exist specifically to keep that boundary explicit.

**Neutral**

- ArcFlow is untouched. It was never a candidate for the ad-hoc case this ADR addresses, and remains
  the only path for a workflow that must be reusable, operator-signed, and deterministic across
  runs.

## Reconsider when

- A real workload needs a script-language feature the whitelist cannot safely admit (e.g. genuine
  recursion depth, a data structure beyond list/dict) — that is a signal to extend the grammar
  deliberately, not to loosen it ad hoc.
- Journal volume under `<workspace>/runs/` becomes a retention problem, or a second consumer needs
  `work_dir` for something other than the dynamic strategy — at that point the path deserves its own
  named accessor rather than a bare `RunState` field.
