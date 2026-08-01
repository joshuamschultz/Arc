# SPEC-061 — ArcFlow: Named, Signed, Conversationally-Authored Workflows

## Deepening Summary

**Deepened on:** 2026-08-01
**Sections enhanced:** 18
**Solutions referenced:** 4
**Skills matched:** langgraph-workflows, python-patterns, react-patterns, langsmith-observability, agentic-framework

### Key Findings
- The design's own example graph deadlocks: qa needs both provision and manual_review but the router guarantees only one runs. Fix: needs satisfied by done-OR-skipped, plus join=all|any, statically validated (Argo's lesson).
- Sign a canonical JSON projection (RFC 8785) plus a manifest of every referenced file, never raw TOML bytes — formatters legitimately rewrite TOML and would break signatures.
- The runner's host must be named: the only ready host today is arcui's lifespan (which already holds TaskStore + arcteam MessagingService + background-task precedent), but that makes the dashboard load-bearing for execution — needs an explicit decision, a singleton guard, and a runner actor DID.
- Typed handoff (ARC-3) necessarily lands in arcagent's tasks module, not arcteam: _format_task_prompt, complete_task, and set_task_output are where prompts are built and outputs written.
- The proactive module is a dead skeleton despite claiming to supersede the scheduler — extend ScheduleEntry with the typed action; default overlap policy skip; no catch-up replay on restart.
- Repair loops: 2 rounds capture 76-95% of achievable gain and regress beyond ~3; validation errors must carry field path + observed value + admissible alternatives (+42pts vs raw validator text).
- arcui already has DM, live group channels, and agent presence — the net-new UI is the gate-answer card in the channel stream and the React Flow graph views.

### New Risks Discovered
- Two arcui instances would run two runners racing the same frontier — singleton guard required wherever the runner lands.
- arcagent declares neither arcstore nor arcteam in pyproject; a scaffold-enabled workflows module promotes arcteam from optional to required — declare or keep lazy-import with graceful degrade, decided explicitly.
- TaskStore.create_batch does not exist — it is new arcstore work, not existing machinery.
- arcteam has no tier source: the runner must receive tier at construction or URL-bearing node text is rejected at write time by the arcstore sanitizer.
- steering/structure.md's Layer Model omits arcstore entirely — must be updated in the same change or it is wrong on merge.
- Per-run capability-leg accumulation is an unbounded collection (SPEC-009 hardening lesson) — bound and monitor it.

## 1. What this is

A **workflow** is a named, semi-permanent, signed graph of nodes — LLM/agent steps, tool calls, scripts, routers, and human gates — that:

- an agent **authors from conversation**, a human **authors in an IDE**, or an operator **authors in arcui** — same artifact, same validation,
- **runs repeatedly** — on demand, on a schedule, or when triggered,
- is **edited by talking about it** (or by hand, or in the UI),
- and is **viewable and auditable** as a graph: per-version, per-run, per-node.

This is the graph-engineering layer: the topology of the work is a versioned, governable artifact, not an emergent side-effect of one agent's reasoning loop. It complements the ad-hoc side (dynamic fan-out stays in arcrun strategies + `orchestration.spawn`); ArcFlow is for processes that should be **the same every time** — onboarding, sales pipelines, report generation, intake-and-route.

This is ARC-2 from the app-store vision plus the layers those docs left open: live conversational authoring, multiple named workflows per agent, scheduling as a workflow property, routers/loops, and a UI editor.

## 2. Positioning: not a third DAG engine

### Research Insights

**From Solutions Archive:**
_(none)_

**Best Practices:**
- arcteam is a true leaf today (pyproject deps: arctrust, nats-py, pydantic, python-frontmatter, rank-bm25; zero sibling imports beyond arctrust) — 'never imports arcagent' is currently true and enforceable.
- arcteam→arcstore introduces no cycle: arcstore depends only on arctrust+pydantic, and tests/architecture/test_no_arcstore_arcteam_upward_imports.py deliberately omits arcstore from its upward-layer list, so the new edge passes the guard unmodified.

**Edge Cases:**
- The 'new package would touch _CANONICAL_PACKAGES' claim is overstated: test_workspace_install.py only asserts the listed six exist — arcstore/arcteam/arcui/arcskill are already absent and green. The real argument against a new package is install/deploy surface, not that guard.

**Performance:**
_(none)_

**References:**
- packages/arcteam/pyproject.toml
- packages/arcstore/pyproject.toml
- tests/architecture/test_no_arcstore_arcteam_upward_imports.py:27
- tests/architecture/test_workspace_install.py:44-51


Arc already contains two working DAG executors and one fan-out primitive (all verified green: planning 85 tests, tasks 75, arcstore tasks 73):

| Existing system | What it owns | ArcFlow's relationship |
|---|---|---|
| `arcagent.modules.tasks` + `arcstore.tasks` (SPEC-056) | Durable fleet-shared task DAG: `blocked_by`/`deps_met`/`deps_would_cycle`, atomic claim, retry/backoff/dead-letter, review gates, cross-agent handoff via signed DMs, dispatch + reliability loops | **The execution substrate.** A workflow run instantiates into this DAG. |
| `arcagent.modules.planning` (SPEC-040/043) | Ephemeral goal→DAG→execute→checkpoint→replan for one agent | **Unchanged.** For ad-hoc goals the model decomposes itself. ArcFlow ports up its three superior properties: signed definition, run-scoped reserve-then-settle budget, typed step outcomes. |
| `arcagent.orchestration.spawn` | Bounded, budget-capped sub-agent fan-out | **A node implementation detail.** A node's agent may spawn children that message each other; ArcFlow never calls spawn directly. |

Genuinely new (confirmed missing by code inspection): the WorkflowDefinition artifact; atomic multi-owner instantiation; the Run aggregate (ARC-1 — `run_id` today is a pure correlation string, no Run row exists); typed handoff (ARC-3 — `_format_task_prompt` at `modules/tasks/capabilities.py:470-490` injects nothing from upstream outputs); routers/loops; a structured trigger seam (the scheduler can only fire free-text prompts today); builder tools + builder skill; the graph UI.

## 3. Vocabulary

**workflow** (named definition) · **node** (a unit of work in the graph) · **needs** (dependency edges) · **kind** (node type: `agent | tool | script | router | gate`) · **run** (one execution instance) · **output schema** (typed handoff contract per node) · **handoff** (the task-row write that assigns the next node to its owner — never a message) · **route** (a declared outgoing branch of a router) · **loop** (a declared, bounded back-edge).

Internal shorthand from research: **Infer** nodes (LLM judgment: `agent`, `router mode="llm"`) vs **Derive** nodes (deterministic: `tool`, `script`, `router mode="rules"`). Never name a class `*Pipeline` (claimed by `arctrust.PolicyPipeline`).

## 4. The artifact: `workflow.toml`

### Research Insights

**From Solutions Archive:**
_(none)_

**Best Practices:**
- Version the language separately from the workflow: add schema_version to [workflow] and refuse an unknown major at load (ASL carries a language Version; n8n's most common import failure is newer-instance definitions landing on older instances). Cheap now, impossible to retrofit onto signed artifacts later.
- Freeze one predicate grammar at v1, workflow-wide — no per-node dialect switching (ASL's JSONPath/JSONata QueryLanguage split is a permanent audit tax).
- Pin [[node]] as the only node syntax and keep prompts file-referenced: TOML multiline strings make editor whitespace cleanup change bytes, change the hash, and drop a signed workflow to draft for no semantic reason.

**Edge Cases:**
- Node ids are immutable once signed: a rename is delete + add, encoded that way, so in-flight runs (pinned by hash) and historical audit rows are unaffected; LangGraph has no published story for renames against existing checkpoints, which supports the rule.
- A $nodes.X.output.* reference from a node that cannot co-occur with X (only reachable via a route the referencer does not share) is statically unsatisfiable — reject at sign time; Argo defers this to runtime and it is a recurring bug source.

**Performance:**
- Cap the definition at 256 KB and ~200 nodes (Step Functions' hard quota is 1 MB), and per-node output at 256 KiB — node outputs thread into downstream prompts, so an uncapped output is both a token blowout and an injection surface.
- Parse + SCC + SHA-256 over a few hundred KB is low-millisecond; the real cost is re-reading referenced files per node dispatch — cache the verified manifest per (id, version, content_hash) for the life of a run.

**References:**
- https://states-language.net/spec
- https://docs.aws.amazon.com/step-functions/latest/dg/service-quotas.html
- https://toml.io/en/v1.0.0
- https://community.n8n.io/t/how-do-i-update-node-typeversion/29617


One canonical serialized form — simultaneously the executable definition, the render source for the UI graph, the IDE-editable file, and the diffable audit record. Never a separate "pretty" representation that can drift (the strongest cross-system lesson: Step Functions, n8n, Dify).

```toml
[workflow]
id = "customer-onboarding"          # stable name; the semi-permanent identity
version = 4                         # monotonic; every edit increments
description = "New customer intake through provisioning"
owner = "@sales"                    # owning agent: authors it, default node agent
channel = "channel://onboarding"    # public channel where handoffs are narrated
budget = { tokens = 400_000, wall_clock_s = 1800 }

[trigger]                           # optional; manual if absent
type = "cron"                       # cron | interval | manual
expression = "0 9 * * MON"
active_hours = { start = "08:00", end = "18:00", timezone = "America/Chicago" }

[input]                             # typed run input — the only door for run-time variability
schema = "schemas/onboarding_input.json"

# ---- Infer node: an LLM step run by a specific agent ----
[[node]]
id = "collect"
kind = "agent"
agent = "@sales"                    # which agent runs it (multi-agent v1)
skill = "customer-intake"           # optional: skill body activated deterministically
strategy = ["react"]                # forced arcrun allowed_strategies for this node
prompt = "prompts/collect.md"       # node instructions (file-referenced, signed with the bundle)
output_schema = "schemas/customer_record.json"
artifacts = ["customer_record.json"]  # files that must exist on disk for completion to count
timeout_s = 300
max_attempts = 3

# ---- Derive node: one declared tool call (API calls are tools) ----
[[node]]
id = "verify"
kind = "tool"
tool = "crm_lookup"
agent = "@sales"                    # whose identity/grant executes the call
needs = ["collect"]
args = { domain = "$nodes.collect.output.company_domain" }
output_schema = "schemas/verification.json"

# ---- Router: declared branch selection ----
[[node]]
id = "risk_router"
kind = "router"
mode = "rules"                      # rules (deterministic predicates) | llm (schema-constrained choice)
needs = ["verify"]
routes = [
  { to = "provision", when = "$nodes.verify.output.risk == 'low'" },
  { to = "manual_review", default = true },
]

# ---- Human gate ----
[[node]]
id = "manual_review"
kind = "gate"
gate = "human:approve_high_risk"
needs = ["risk_router"]

# ---- Script node: sandboxed deterministic code ----
[[node]]
id = "provision"
kind = "script"
script = "scripts/provision.py"     # runs via the existing execute/sandbox machinery
agent = "@ops"
needs = ["risk_router"]
output_schema = "schemas/provisioned.json"

# ---- Bounded loop: maker → checker → revise ----
[[node]]
id = "qa"
kind = "agent"
agent = "@reviewer"
needs = ["provision", "manual_review"]
join = "any"                        # router above makes these mutually exclusive; any-join required
output_schema = "schemas/qa_verdict.json"

[[node]]
id = "revise"
kind = "agent"
agent = "@ops"
needs = ["qa"]
when = "$nodes.qa.output.verdict == 'revise'"
loop_back_to = "provision"          # declared back-edge
max_iterations = 3                  # hard bound; exhaustion = node failure, audited
```

### Design rules baked into the schema

- **Models choose only among declared options (D-004, adapted).** A `router mode="llm"` node is an Infer step whose output schema is an enum of the declared `routes[].to` ids — the model picks among pre-declared branches, the choice is recorded and audited, and deterministic runner code follows it. A model never invents a next node.
- **Loops are declared and bounded.** `loop_back_to` + `max_iterations` gives the maker→checker→revise shape production graphs need (the LangGraph lesson) without unbounded wandering. Every iteration is a fresh audited node attempt; exhaustion fails the node. Cycle validation changes from "reject all cycles" to "reject any cycle not declared as a loop, and every loop must carry `max_iterations`."
- **Non-determinism enters only through `[input]` and node outputs.** No clock/randomness/environment reads in wiring expressions (Claude Code Workflows' enforced-determinism rule).
- **Prompt files carry prompts only**; model config lives with the agent, not the workflow. A node may force `strategy` (arcrun `allowed_strategies`) but never model/temperature.
- **`when` predicates stay small**: equality/comparison/boolean over `$nodes.*.output.*` and `$input.*` only.
- **Join semantics (from the deepen research):** a node's `needs` are satisfied when each upstream is done OR skipped; `join = "all" | "any"` (default `all`) declares fan-in behavior, and the validator statically rejects a node whose needs span mutually exclusive routes of one router unless it declares `join = "any"`. Exactly one non-run terminal state (`skipped`), propagating transitively.

### Research Insights

**From Solutions Archive:**
_(none)_

**Best Practices:**
- Use a ~150 LOC hand-written recursive-descent predicate parser (whitelisted AST: Path, Literal, Compare, BoolOp) behind one evaluate(expr, scope) -> bool seam — not CEL (the only pure-Python implementation, cel-python, is still beta) and not JSONPath; CEL can swap in behind the same seam if ever needed.
- Resolve args by VALUE, never textual interpolation: every GitHub Actions expression-injection class comes from substituting expressions into command strings — upstream node output must bind as a typed value into the tool call, never concatenate into a prompt or command (LLM01, enforced in the resolver not by review).

**Edge Cases:**
- DEFECT IN THE EXAMPLE (fixed in body post-review): qa needs [provision, manual_review] but risk_router guarantees only one branch runs — under all-needs-must-succeed, qa deadlocks forever. Fix: needs satisfied by (done OR skipped), plus join = all|any per node (default all); statically reject a node whose needs span mutually exclusive routes of one router unless join=any.
- Keep exactly ONE non-run terminal state (skipped) that propagates transitively — Argo's Skipped-vs-Omitted split has caused years of join bugs where (Succeeded||Skipped||Omitted) spellings still fail to fire.
- Bind loop counters to the back-edge keyed (run_id, loop_back_to target), not the node carrying it, or a router inside the body resets the counter; compute SCCs at validation — every SCC of size >1 must be entered by exactly one declared loop_back_to, all members share its counter, nested/overlapping loops rejected in v1.

**Performance:**
_(none)_

**References:**
- https://cel.dev/overview/cel-overview
- https://github.com/cloud-custodian/cel-python
- https://docs.github.com/en/actions/concepts/security/script-injections
- https://argo-workflows.readthedocs.io/en/latest/enhanced-depends-logic/
- https://github.com/argoproj/argo-workflows/issues/8654

### Storage, signing, versioning

- The definition lives in the **owning agent's workspace**: `workspace/workflows/<id>/workflow.toml` + `schemas/` + `prompts/` + `scripts/`, with detached `.arcsig` sidecars. Workspace is the source of truth; arcstore **indexes** (metadata, current version, content hash) but never owns. Written by direct filesystem I/O (ADR-029), never through the LLM file tools.
- **Signing rides the existing rail wholesale**: `arctrust.artifact.sign_artifact`/`verify_artifact`, `.arcsig` sidecars, operator-key pinning (pinned, not TOFU — inherit the SPEC-047 LLM03 fix from `arccli/blueprints.py:307-309`), tier-graded trust. No new signing scheme.
- **Agents and UIs author drafts; only the operator signs.** The sharp edge (LLM06/ASI04): if the authoring process could also sign, prompt injection could author an exfiltration pipeline and bless it. `workflow_create`/`workflow_edit` (and the arcui editor, and a hand-edit in the IDE) all produce an **unsigned draft**; signing is out-of-band via `arc workflow sign` (arccli — the operator key never enters the agent process; same shape as `arc approve` and `arc blueprint sign`). Personal tier may run drafts with an audit warning; enterprise/federal refuse unsigned or agent-signed definitions, fail-closed.
- **Every edit is a validated transaction** (ARC-10 pattern): validate → atomic write (temp, fsync, rename) → bump `version` → audit with actor DID and reason → re-sign out-of-band. Edits carry `expected_version` (n8n-style optimistic concurrency); stale edits are refused. An edit changes the content hash, so an edited-but-unsigned definition visibly drops to draft.
- **IDE hand-edits** are picked up by the loader on next resolve: schema-validated, version-bump enforced (a content change without a version bump is refused with a clear error), audited as `workflow.edited` with `actor=filesystem` provenance. Same validator, same rules as every other surface.
- **In-flight runs pin** to the definition version + content hash they started from (Temporal's discipline). Edits never touch a running instantiation; the next run picks up the new version. Prior versions retained as `versions/<n>.toml` (+ sidecar) so version diff/history renders in the UI without git on-box.

### Research Insights

**From Solutions Archive:**
_(none)_

**Best Practices:**
- Canonicalize before hashing: TOML has no canonical form (inline tables vs [[array-of-tables]] are the same data; formatters legitimately rewrite), so hash an RFC 8785-style canonical JSON projection of the parsed document (sorted keys, defined number formatting, NFC strings), never raw bytes.
- The signature must cover a sorted manifest of {relative_path: sha256} for every referenced schema/prompt/script file, not just workflow.toml — verified at run start AND at each node dispatch (a long run can outlive an editor save); mismatch fails the run closed as workflow.integrity_failed rather than executing a hybrid of two versions.

**Edge Cases:**
- A perfectly valid draft must still yield status=draft — there must be no path where validation success flips a signed flag; make this an explicit E2E test at enterprise/federal tier.

**Performance:**
_(none)_

**References:**
- https://datatracker.ietf.org/doc/html/rfc8785

## 5. Execution: the runner, the substrate, the channel

### Research Insights

**From Solutions Archive:**
- security-issues/2026-04-18-tier-must-flow-through-construction.md — the runner receives tier at construction (like ToolRegistry), never resolves it per-node, so audit events reflect true posture.
- security-issues/2026-02-21-arcrun-phase4-hardening-review-learnings.md — individual try/except per cleanup step (apply to cancel fan-out); staleness/reclaim lease pattern; unbounded-collection debt (applies to per-run leg accumulation).

**Best Practices:**
- The task row IS the journal: status=done + output is the memoization record that wiring reads — a completed node is never re-executed (Restate/Inngest model), and arcstore's status-conditional update_if is already the exactly-once primitive; reuse it verbatim for Run-row transitions and materialization instead of adding any lock.
- Idempotency keys are (run_id, node_id, attempt) — stable across retries (Temporal guidance: task tokens are NOT stable across attempts; use ids).
- Materialization must be idempotent and replay-safe: check-before-create keyed on (run_id, node_id), re-deriving 'already materialized' from existing rows on restart — nothing guards double-creation today.
- Run-level cancel flips the Run row to cancelled BEFORE fanning out task-level cancels, else a node completing mid-sweep re-extends the frontier behind the sweep.
- Cross-agent gate advancement needs its own signed-DM notification: _reconcile_parents is explicitly same-owner only (capabilities.py:678-702), so a gate resolved by an operator must actively wake the downstream node's owner.
- Query scoped, not list-then-filter: _reconcile_parents' full owned-task scan per 5s tick must not be copied — the runner queries by flow_run_id/metadata.workflow via mutable_query.

**Edge Cases:**
- Every engine's honest guarantee is exactly-once orchestration but at-least-once side effects (Temporal/Restate/Inngest/DBOS all converge here) — the idempotency-key decision (D-527) is what closes the gap, not the journal.
- A singleton runner protected by a lease alone is unsafe under pauses/partitions without fencing tokens (Kleppmann): fine as a stated v1 ceiling with one host process, but multi-process runners require a fenced lease or Postgres-advisory-style session lock — do not drift into multi-runner without it.

**Performance:**
- Ceilings from comparable engines: Temporal caps 51,200 events/50 MB history and recommends <=500 concurrent pending items per workflow — ArcFlow's substrate ceiling is 'thousands of concurrent modest runs, not one 10k-node run'; do not let the format permit graphs the per-agent serial dispatcher cannot serve.
- The reserve-then-settle budget lock is per-run and could serialize parallel node dispatch under heavy fan-out (map/subagents, Phase 4) — a contention ceiling to watch, not a v1 blocker.
- The runner adds another poller against the same shared SQLite mutable plane in the same process as per-agent dispatch/reliability loops — keep tick queries indexed and scoped.

**References:**
- https://temporal.io/blog/idempotency-and-durable-execution
- https://docs.restate.dev/foundations/key-concepts
- https://docs.temporal.io/workflow-execution/limits
- https://martin.kleppmann.com/2016/02/08/how-to-do-distributed-locking.html
- packages/arcstore/src/arcstore/tasks.py:435-447
- packages/arcagent/src/arcagent/modules/tasks/capabilities.py:678-702


### Lineage: BlastForge's `pipeline.py`, generalized

The working ancestor is `~/blackarc/demos/blastforge/backend/src/blastforge/pipeline.py` on inference-1 (1,610 lines): five roles hardcoded in Python, sequenced by an orchestrator that syncs settings, loads the agent, runs the stage, forces the completion tool call with bounded re-prompts, verifies **artifacts on disk rather than the agent's report**, awaits human gates, applies fixes deterministically, and writes `context.md` between stages. ArcFlow's runner (`modules/workflows/runner.py`) is that file made generic and durable:

| pipeline.py (hardcoded, in-process) | ArcFlow runner (TOML-driven, durable) |
|---|---|
| `ROLES` tuple in Python | `[[node]]` list in `workflow.toml` |
| `COMPLETION_TOOL` + `COMPLETION_REQUIRED` per role | `output_schema` per node, validated on completion |
| `STAGE_ARTIFACTS` disk checks ("trust the filesystem, not the report") | `artifacts` per node — completion counts only if the declared files exist |
| `COMPLETION_RETRIES` re-prompt in the same session | `max_attempts` + in-session completion forcing |
| `context.md` prose handoff | typed wiring `$nodes.x.output.y` + `assemble_prompt` injection |
| in-process `await` gates | gate nodes as `review` tasks, control-plane resolved |
| `MAX_ITERATIONS` owned by orchestrator | declared loops with `max_iterations` |
| evaluator verdict beats agent report, mismatch audited | Derive verify nodes downstream of Infer nodes; schema + artifact checks |
| Python sequencing, dies with the process | deterministic runner over durable task rows; restart-safe |

Two pipeline.py lessons adopted as first-class node features:
1. **Artifact verification** (`artifacts = [...]`): a completion tool call proves the agent produced a structured answer, not that it did the work — in testing, models filled in plausible job ids without ever running the solver. Missing artifacts fail the attempt with a message naming the specific tool that produces each file.
2. **Deterministic verdicts win**: when a workflow needs a judgment call validated, the pattern is an Infer node followed by a Derive node (tool/script) that recomputes the verdict; a disagreement is an audited signal, never papered over.

(`pipeline.py`'s "backstop" — the orchestrator running a stage's own tools itself when the agent won't — is deliberately deferred: it is a demo-floor device, and in Arc the equivalent is a retry policy plus dead-letter plus coordinator notification.)

**No orchestrator agent.** Progression is **deterministic runner code** in the workflows module (the "workflow runner" — an extension of the existing 5s reliability tick pattern, not an LLM, not an agent). This is the app-store D-022/D-024 answer and the research consensus: control flow is replayed deterministic code; LLM calls live inside journaled nodes. An optional `coordinator = "@handle"` field can name an agent to be *notified* on failures/stalls/gate-timeouts for judgment calls — but the coordinator never sequences.

`workflow_run(id, input)` does:

1. Verify signature (per tier) and validate input against `[input].schema`.
2. Create a **Run** row (ARC-1) in a new `arcstore.runs` mutable collection: `run_id`, workflow id+version+hash, status (`pending|running|waiting_gate|done|failed|cancelled`), initiator DID, budget, per-node rollup. (Own collection — `SpoolKind` is a closed Literal; the spool has no `run_id`.)
3. **Materialize lazily** (D-514): the runner creates a task row only when a node becomes reachable — the ready frontier (`metadata.workflow`, `node_id`, `flow_run_id`, `blocked_by` from `needs` among materialized nodes, gates as `requires_review=True`, owner per node's `agent`; frontier batches via `TaskStore.create_batch` — NEW arcstore work, does not exist yet). Untaken branches never become tasks; loop iterations mint fresh iteration-stamped rows. Materialization is idempotent: check-before-create keyed on `(run_id, node_id)`, re-derived from existing rows after a crash. The Run row records the **path taken** — the authoritative ordered trace of materialized nodes, router choices, and loop iterations.
4. **Existing machinery executes it.** Each node-task dispatches through its owning agent's `tasks_dispatch_loop`; the task row itself is the cross-agent handoff (a signed `task_assigned` DM follows only as a wake signal); retry/backoff/dead-letter/stuck-reclaim come free from the reliability watcher.

**Handoff is carried by tasks. Never by messaging.** This is a hard invariant, not a preference (Josh, 2026-08-01):

| Property | A task row gives it | A message does not |
|---|---|---|
| **Forces action** | must be claimed and driven to a terminal state; an unclaimed ready task is visible, alarming, and reclaimable | can be read and ignored with no trace and no consequence |
| **Retry** | attempts, backoff, timeout, dead-letter already exist per row | delivery is best-effort and fire-and-forget |
| **History** | a durable, queryable, audited row is the record of what was handed off and what came back | a chat line is a story, not a state machine |
| **Directed ownership** | `owner_did` names exactly one agent; the atomic claim guarantees exactly one executor | anyone in the channel may answer, or several may, or none |

So: the runner writes the next node's task row with its owner set from the definition, and **that write is the handoff**. A signed direct message may follow it purely as a **wake signal** — it says "you have work," it never *contains* the work, and a lost DM costs latency only, because the owning agent's dispatch loop finds the row on its next tick regardless. Nothing about a run's progress depends on a message being delivered, read, or understood.

**Narration is separate and one-way.** The runner posts every transition to the workflow's group channel — node started, node completed with output summary, handed to @agent, gate waiting, gate resolved by \<person\>, run done or failed. Narration-class messages never wake anyone and never carry work; they are the human-readable story of a run whose truth lives in task rows. That gives public visibility without letting the channel become the dispatch mechanism.

Restating the app-store rule exactly as it should be read here: **tasks carry the work, DMs carry the signals, the channel carries the story.**

Per node kind:

- **`agent` nodes** run as bounded fixed-process engine calls (the arcmemory `run_react_loop` precedent): the node's `strategy` field threads into arcrun `allowed_strategies` — **omitted = pinned `react`** (deterministic default, no meta-selection call, the workflow stays predictable); **a single entry = forced**; **a list = arcrun's existing `select_strategy` picks its best among them** (the machinery already exists: multi-entry `allowed_strategies` triggers an LLM choice, audited as `strategy.selected`); the node prompt plus upstream outputs are injected via the `agent:assemble_prompt` sections dict (the compaction-safe path planning uses at `planning/capabilities.py:251-268`); the skill body (if declared) is activated deterministically via the capability registry, not via the model choosing `use_skill`; the tool set is the role's grant, frozen per run (ADR-027); caps from node/workflow budget; `run_id` pinned at claim. Output validated against `output_schema` — a schema failure is a retryable node failure, never a silent pass-forward (ARC-3). Inside its run the agent can use everything it already has: spawn children that message each other, use skills, call tools. Multi-agent *within* a node is just an agent using its existing powers; multi-agent *across* nodes is the graph.
- **`tool` nodes** execute one declared tool call with wired args under the executing agent's identity and policy pipeline — deterministic, no LLM.
- **`script` nodes** run through the existing sandboxed execute machinery — deterministic, audited, no LLM.
- **`router` nodes**: `mode="rules"` is pure predicate evaluation by the runner; `mode="llm"` is a minimal Infer step whose output enum is the declared routes. Un-taken branches' tasks are marked `skipped`.
- **`gate` nodes** are `review`-status tasks resolved **only** by the control plane (arcui/arccli with the human's identity). Agents can read pending gates; no agent tool can resolve one (D-025). Gate events are narrated to the channel and answerable in arcui.

**Wiring** (`$nodes.x.output.y`) resolves by deterministic code from schema-validated outputs on the task rows — journaled once, reused on resume; a completed node is never re-executed (Restate/Inngest memoization, not Temporal replay).

**Run budget** lifts `RootTokenBudget` to the Run and reuses planning's reserve-then-settle grants (`planning/executor.py:212-238`) verbatim for concurrent nodes. The runner also enforces run-level stall/livelock detection and cancel fan-out (ARC-5).

### Research Insights

**From Solutions Archive:**
_(none)_

**Best Practices:**
_(none)_

**Edge Cases:**
_(none)_

**Performance:**
_(none)_

**References:**
_(none)_

### New arcrun strategies (companion work, separate deliverable)

Nodes force strategies via `strategy = [...]`; any arcrun caller benefits. Respecting the boundary (arcrun strategies never see the graph — `plan_execute.py:1-13`), the candidates are **loop-shaping** strategies:

1. **`reflect`** — maker→checker inside one run: draft, self-critique against criteria, revise, bounded rounds. (The in-node miniature of the graph-level QA loop.)
2. **`map`** — schema-driven fan-out over a list input through `ParallelDispatcher`, results collected into one typed output. (Generalizes `plan_execute.run_ready` into a first-class strategy.)
3. **`subagents`** — the strategy face of `orchestration.spawn`: decompose, spawn bounded children (who can message each other via existing arcteam seams when run by an arcagent-hosted caller), synthesize. Needs careful layering: the strategy shape lives in arcrun; the spawn/messaging execution binds in from arcagent (same pattern as `build_arcrun_run_fn`).

Each is its own small spec; ArcFlow depends on none of them for v1 (react covers v1 nodes) but the node `strategy` field is wired from day one.

### Research Insights

**From Solutions Archive:**
_(none)_

**Best Practices:**
- subagents reuses spawn_many verbatim (spawn.py:810-918): budget-pooled via RootTokenBudget.try_debit, semaphore-capped, ordered results, fail_fast cancellation — exactly the decompose/spawn/synthesize shape; do not reimplement fan-out.
- The binding seam mirrors build_arcrun_run_fn (planning/executor.py:249-304): the Strategy shape and loop-hook live in arcrun taking an injected opaque callable (structurally like ItemRunner); arcagent constructs the closure bound to spawn_many/messaging and passes it in. Any arcrun/strategies/*.py importing arcagent.orchestration is the layering violation to flag in review.
- Share one gather path: ParallelDispatcher is already the single concurrency primitive for react tool batches and plan_execute.run_ready — subagents and map should reuse it rather than introduce a third.

**Edge Cases:**
- spawn/spawn_many assume a live RunState (depth/max_depth/event_bus, spawn.py:150-161) — a subagents strategy invoked from a workflow node needs a real RunState threaded per node, not a bare prompt string.

**Performance:**
_(none)_

**References:**
- packages/arcagent/src/arcagent/orchestration/spawn.py:810-918
- packages/arcagent/src/arcagent/modules/planning/executor.py:249-304
- packages/arcrun/src/arcrun/strategies/plan_execute.py:1-13

### Security invariants (load-bearing, from the deep-dives)

1. **Trifecta legs accumulate per RUN, not per node-session.** Fresh per-node sessions would silently reset lethal-trifecta accumulation, letting a workflow complete a forbidden composition across nodes that no single session could. The runner threads the run's accumulated capability legs into every node's `PolicyContext.session_capabilities`. Hard requirement, v1.
2. **Activation is a gated consequential action.** At definition/edit time ArcFlow computes the union of capability legs the graph can touch; a definition spanning private-data + external-comms requires an operator-signed approval (existing `HumanGate`/`ApprovalGrant`/`arc approve` machinery) at first activation and on any widening edit — not on every run.
3. **Idempotency keys on side-effecting nodes** (ARC-6): node attempt id carried into tool dispatch so a retried node can't double-send the email or double-write the ERP.
4. Definition text fields pass the same sanitizer discipline as `Task` free text; node prompts are signed files, not inline chat strings.
5. Audit: definition lifecycle (`workflow.created/edited/signed/activated`) → WORM sink with actor DID; per-node execution reuses the hash-chained run events + `tool.executed` audit; every loop iteration and router choice is a recorded event. Who authored v4, who signed it, what node 3 did on run 17, which branch the router took and why, who resolved the gate — all reconstructible from durable signed records.
6. **Gate identity prerequisite:** `arcui/routes/tasks.py:30` hardcodes `_CREATOR = "did:arc:ui:operator"` — gate resolutions carry a role, not a person. SPEC-057's real-DID-on-control-plane-writes is required for the full "who approved this" story.

### Research Insights

**From Solutions Archive:**
- security-issues/2026-02-21-arcrun-phase4-hardening-review-learnings.md — per-run capability-leg accumulation is an unbounded collection; bound it and monitor growth.

**Best Practices:**
- The runner needs its own actor DID, distinct from both agents and the arcui operator hardcode — mint/resolve it the way arcui messaging resolves the on-disk operator key (arcui/messaging.py:131-140, generate_if_absent=False); Run rows and node tasks it creates must carry it.
- ASI09 for gates-in-chat: an approval decision is validated against the authenticated identity of the human actor (Slack's HMAC-signed interaction payload is the reference pattern), never against the text of a chat message; for irreversible actions, step-up confirmation OUTSIDE the chat surface is the OWASP-recommended mitigation.
- Anthropic telemetry: ~93% of per-call approvals get rubber-stamped — gate sparingly (activation + genuinely consequential nodes), not per-call, or operators stop reading.

**Edge Cases:**
_(none)_

**Performance:**
_(none)_

**References:**
- https://docs.slack.dev/authentication/verifying-requests-from-slack/
- https://www.anthropic.com/engineering/how-we-contain-claude
- https://genai.owasp.org/download/52117
- packages/arcui/src/arcui/messaging.py:131-140

## 6. Authoring: three surfaces, one validator

### Research Insights

**From Solutions Archive:**
- security-issues/2026-02-16-async-scheduler-hardening-6agent-review.md — field allowlists on every mutating tool (BLOCKING finding #2), NFKC unicode normalization before any injection regex on inline free text, pydantic validation-context for tier-aware validators, quota checks before validation work.

**Best Practices:**
- Builder tools follow the repo's proven order (decompose_task, tasks/capabilities.py:376-421): validate the whole graph in memory -> cycle-check -> then atomic write; never validate-node-by-node-while-writing.
- Every mutating workflow_* tool uses an explicit field allowlist (schedule_update's candidates-dict pattern, scheduler/capabilities.py:196-217) and owner-gating (tasks' _require_owner) — only the owning agent or operator edits.
- Validation errors are a typed list of {node_id, field, error, expected, admissible_alternatives} — research shows admissible alternatives drive the largest repair gain (+42 pts; invalid-action episodes 35->2); prose-vs-JSON delivery barely matters, alternatives do.
- Cap repair loops at 2-3 attempts then fail to 'ask the human to clarify X': rounds 1-2 capture 76-95% of achievable improvement and models regress/oscillate beyond ~3.
- Schema-forced tool calls eliminate malformed shapes but NOT semantics (100% schema-validity benchmarks still show ~80% semantic success) — the deep validator (dangling needs, unreachable nodes, resolving refs) must still run on every accepted call.
- Add quotas: max_workflows and max_nodes_per_workflow checked before validation work (LLM10) — currently absent from the design.
- The workflow-builder skill follows the 7-section builtins format (create-skill/SKILL.md): Contract = what to elicit before workflow_create; Knowledge = node-kind decision guide + the named coordination patterns; Anti-Patterns = the design's own list plus 'don't build a workflow for something that runs once'.

**Edge Cases:**
- n8n's builder (the closest production analog) uses granular validated tools with iteration caps (Discovery <=50, Builder <=100) and versionId+checksum concurrency — and its designed-against failure mode is exactly the 25-node one-shot generation error.

**Performance:**
_(none)_

**References:**
- https://arxiv.org/html/2607.14167v1
- https://arxiv.org/html/2604.10508
- https://arxiv.org/html/2607.18261v1
- packages/arcagent/src/arcagent/modules/tasks/capabilities.py:376-421
- packages/arcagent/src/arcagent/builtins/capabilities/skills/create-skill/SKILL.md


**The reliable creation mechanism is validated builder tools + a builder skill** (the n8n lesson: constrain the authoring model to a small set of validated tools emitting into the canonical schema — never "LLM writes TOML and we parse it"):

- **The tools** are the mechanism: each call validates the whole graph and returns structured errors the model repairs from (bounded attempts).
- **A shipped `workflow-builder` skill** is the knowledge: how to elicit a process from conversation, which node kinds fit which work, the patterns (intake→specialist, fan-out→synthesize, maker→checker, scheduled watcher), and the anti-patterns to refuse (one agent with 20 tools, a "manager" node that does the work itself).

| Surface | Path | Notes |
|---|---|---|
| **Conversation** | agent calls `workflow_*` tools, guided by the builder skill | describe → build → agent renders the graph back in plain language → "change X" → targeted edit → new draft version |
| **IDE** | hand-edit `workflow.toml` | loader validates on resolve; version-bump enforced; audited with filesystem provenance |
| **arcui** | graph/form editor | calls the same validation via new routes (`POST/PATCH /api/workflows`…); operator-gated; renders the DAG from the TOML |

All three converge on the same artifact, the same validator, the same draft→sign lifecycle.

Module tool surface (`arcagent/modules/workflows/capabilities.py`, mirrors the tasks module template — `config.py` with `WorkflowsConfig(ModuleConfig)`, `models.py`, `store.py`, `_runtime.py`):

| Tool | Classification | Purpose |
|---|---|---|
| `workflow_create(id, description, nodes=[…])` | state_modifying | Create v1 as an **unsigned draft**. Full validation: dangling `needs`, undeclared cycles (Kahn + loop declarations), unknown agents/tools/skills/scripts, schema refs resolve, `when`/routes parse, loop bounds present, budget sanity. |
| `workflow_add_node / edit_node / remove_node / set_trigger / set_channel` | state_modifying | Targeted edits; `expected_version` optimistic concurrency; re-validate whole graph; bump version; audit; drop to draft. At-rest only. |
| `workflow_run(id, input={})` | state_modifying | Start a run (§5). |
| `workflow_list / inspect(id, version) / runs(id) / run_status(run_id)` | read_only | The agent talks about its workflows: render graph as text, explain version diffs, report run history/status/cost. |
| `workflow_cancel_run(run_id)` | state_modifying | Owner/operator cancel; fans out via existing cancellation machinery. |

## 7. Triggers: a structured seam, not a prompt

### Research Insights

**From Solutions Archive:**
_(none)_

**Best Practices:**
- Extend ScheduleEntry (scheduler/models.py:120-197) with action: Literal['prompt','workflow_run'] + workflow_id/workflow_input, validated in _validate_type_fields like the type-specific fields; branch on action in SchedulerEngine.execute (scheduler.py:130-168).
- Reuse the existing circuit breaker (consecutive_failures + threshold auto-disable) and active_hours gating unchanged — both are entry-level and action-agnostic.
- Default overlap policy: skip (matches Temporal's ScheduleOverlapPolicy default and the existing _in_flight dedup); 'queue' would require a per-schedule pending-queue concept the store doesn't have — defer it.

**Edge Cases:**
- No catch-up replay exists: a fire missed while the process was down executes once on restart if overdue, never once-per-missed-window (like K8s startingDeadlineSeconds-skipped occurrences, unlike Temporal's catchupWindow) — document this as the trigger's stated semantics.
- The proactive module is a dead skeleton despite its 'replaces the legacy pulse + scheduler modules' docstring: no store, no CRUD tools, cron-only, a no-op handler, nothing constructs it. Extend the real scheduler; defer the proactive migration question entirely.

**Performance:**
_(none)_

**References:**
- packages/arcagent/src/arcagent/modules/scheduler/models.py:120-197
- packages/arcagent/src/arcagent/modules/scheduler/scheduler.py:122-168
- packages/arcagent/src/arcagent/modules/proactive/capabilities.py:147-158
- https://docs.temporal.io/schedule


Today the scheduler fires free text into `agent_run_fn` — English-and-hope, unacceptable as a workflow trigger. Add a typed `action` to `ScheduleEntry` (`prompt` | `workflow_run`): `[trigger]` materializes as a schedule whose action calls the workflows module's run entry directly — deterministic dispatch, no model in the loop for the decision to start. Manual triggers: chat tool, `arc workflow run`, arcui Run button. Event triggers (message/webhook/task) are out of scope for v1 but the `action.kind` seam is designed for them.

## 8. Viewing and auditing (arcui)

### Research Insights

**From Solutions Archive:**
_(none)_

**Best Practices:**
- Less new chat work than assumed: DM panel (ChatPanel), live group channels (ChannelPanel + useTeamStream websocket frames), mention autocomplete, presence dots, and operator-gated channel-membership routes all EXIST (web/src/pages/messages.tsx, routes/team_chat.py).
- The genuinely new chat piece is the gate-answer card rendered inside the channel stream — relocate the ApprovalCard pattern (pages/approvals.tsx: approve/deny + busy/error + query invalidation) keyed to review-status task rows on the workflow's channel; resolution still posts through the control-plane route.
- React Flow v12 is MIT, ~58 KB gzipped — route-level code-split so only the Workflows pages load it; memoize nodeTypes/custom nodes (React.memo) and use v12's updateNodeData/useNodesData for per-node status ticks without remapping the array.
- Live status: reuse the existing useTeamStream live-frame pattern rather than adding a polling loop; throttle status frames into the store per animation frame.

**Edge Cases:**
- Layout engine disagreement between sources: dagre is small (~25 KB gz) and handles back-edges by feedback-arc reversal but is unmaintained; elkjs has first-class cycle-breaking strategies but is ~433 KB gz. Recommendation: dagre first (cache the computed layout per immutable workflow version, re-layout only on structural edit), switch to elkjs only if loop rendering proves poor.
- Un-taken branches and loop iterations need distinct visual states (skipped, looping n of m) sourced from the Run's path-taken record — with lazy materialization there are no task rows to render for unreached nodes; the definition supplies the geometry.
- Whether person-DIDs and agent-DIDs resolve uniformly in a channel roster needs a follow-up check in arcteam/registry.py — the membership routes resolve refs to DIDs but person-vs-agent distinction isn't visible at that layer.
- Shipping React Flow requires the full rebuild-AND-restart cycle: index.html is read once into memory at server start (server.py:242-267, ARC_BUILD_ID cache-busting) — vite build alone changes nothing.

**Performance:**
- No hard node-count ceiling: React Flow's viewport rendering carries modest workflow-sized graphs fine; elkjs first-layout is async/slow on large graphs — another reason to cache layout per version.

**References:**
- packages/arcui/web/src/pages/messages.tsx
- packages/arcui/src/arcui/routes/team_chat.py
- packages/arcui/web/src/pages/approvals.tsx
- https://reactflow.dev/learn/advanced-use/performance
- https://bundlephobia.com/package/@xyflow/react@12.8.1
- https://eclipse.dev/elk/reference/groups/org-eclipse-elk-layered-cycleBreaking.html


- **Workflows page**: list (name, version, draft/signed status, trigger, last run), definition detail rendering the DAG from the TOML, version history with signer + reason per version, **editor** (create/edit via the same validator, draft lifecycle surfaced).
- **Run detail**: the genuinely new UI — the graph with live per-node status (pending/running/waiting-gate/done/failed/skipped/looping n of m), each node linking to the existing per-`run_id` timeline (tool/LLM/cost events already joinable), router choices and loop iterations visible, gates answerable in place (operator role, existing approve/reject routes + real-DID work).
- The workflow's public channel already shows the narrated story in chat; the run view is the structural view of the same events.

## 9. Layering (who owns what)

### Research Insights

**From Solutions Archive:**
_(none)_

**Best Practices:**
- Confirmed-viable host wiring: arc.service runs arc ui start -> arcui.create_app lifespan (server.py:273) which already starts the arcstore TaskStore backend (:282), builds the arcteam MessagingService (:311-330), and launches a long-lived background asyncio task (:352-358) — the runner's exact shape, in a package legally allowed to import both.
- arcteam already runs background loops of its own (audit.py:128, messenger.py:604-615) — a runner loop object in arcteam with the host calling create_task matches house style.
- arcagent->arcteam is already established via lazy in-function typed-Any imports (modules/messaging/_runtime.py:119-122) — the thin workflows tool module imports arcteam models the same way.

**Edge Cases:**
- R2 (most consequential): name the runner's host module explicitly. arcui's lifespan is the only ready host but §9 calls arcui 'viewpoint only' — hosting the fleet-singleton runner there makes the dashboard load-bearing for execution and two arcui instances race the frontier. Alternative (arcgateway.bootstrap) needs arcstore+arcteam added to its deps. Decide, add a singleton guard, and give the runner its own actor DID (R8).
- R5: the arcagent share is NOT thin — upstream-output prompt injection (via _format_task_prompt's assemble path), output_schema validation (complete_task:204-227, set_task_output:429-439) all land in arcagent/modules/tasks. This is also the design's own named producers-unwired candidate.
- R1: arcagent declares neither arcstore nor arcteam (both arrive transitively/lazily; messaging treats arcteam as optional). A scaffold-enabled workflows module makes arcteam required — declare it, or keep lazy-import with graceful degrade; decide explicitly.
- R3: steering/structure.md's Layer Model omits arcstore (and arcprompt) and shows no arcteam->arcstore edge — update it in the same change or it is wrong on merge.
- R4: TaskStore.create_batch does not exist anywhere in packages/ — new arcstore work, not existing machinery.
- R6: tier flows from the caller, not arcstore: the Task sanitizer fails closed on URLs without context, and arcteam has no tier source — hand the runner its tier at construction (same fix as the 2026-04-18 tier solution).

**Performance:**
_(none)_

**References:**
- packages/arcui/src/arcui/server.py:273-358
- packages/arcagent/src/arcagent/modules/tasks/capabilities.py:470-490
- packages/arcagent/src/arcagent/pyproject.toml
- deploy/systemd/arc.service:9
- .claude/steering/structure.md


| Package | Gets | Why |
|---|---|---|
| **`arcteam`** | **The engine**: definition models + validator, **runner** (lazy materialization, routers, loops, wiring, leg-threading, narration, path-taken record), run coordination, workflow group-channel binding; (Phase 4) `worker` entity kind | Workflows are multi-agent coordination; arcteam owns that layer (D-506). Needs only arcstore + messaging — never imports arcagent |
| `arcstore` | `runs` collection + Run model (with path taken); `TaskStore.create_batch` (frontier batches); workflow index | Durable fleet-shared truth, same mutable plane as tasks |
| `arcagent/modules/workflows/` | Thin tool surface (`workflow_*` tools calling into arcteam), builder skill, workspace draft storage for the owning agent | Agents author and kick off; removable; scaffold declares `enabled=true` (D-535) |
| `arcrun` | Nothing for v1; then `subagents` first, `reflect`, `map` (separate specs, D-533) | Loop stays pure; strategies never see the graph |
| `arcllm` / `arctrust` | Nothing new | Reused as-is |
| `arcagent` scheduler | Typed `action` on `ScheduleEntry` (`prompt` \| `workflow_run`) | Smallest change that creates the structured seam |
| `arcui` | Workflows pages, React Flow editor + live-run graph, agents as first-class chat citizens (group + DM), routes | Viewpoint only; operator-gated mutations, `emit_mutation_audit` |
| `arccli` | `arc workflow list/show/run/sign/verify/approve` | Operator surface; signing key lives here, never in the agent process |
| `arcgateway` | Hosts the arcteam runner task in its bootstrap (agent side of the fleet service); gains arcstore + arcteam deps | Fleet singleton without a new deployment unit (D-508); arcui stays a pure watcher — execution never requires the dashboard (Josh, deepen resolution) |

### Repo invariants the implementation must respect

- No new top-level package (would touch `_CANONICAL_PACKAGES` in `tests/architecture/test_workspace_install.py:44-51`); revisit only if a second harness needs the engine.
- Zero arcflow code in `arcagent/core/` (already 4772 LOC vs 3500 ceiling, pre-existing).
- The graph type never enters arcrun (`plan_execute.py:1-13` boundary).
- Declare the arcstore dependency in arcagent's pyproject (currently transitive across 9 import sites).
- Per-run tool-set freeze (ADR-027): each node is its own bounded run with its own frozen registry.
- Workflow state via direct workspace I/O (ADR-029), never LLM file tools; files the workflow *produces* use the tools — that's the point.
- Upstream-output injection via `agent:assemble_prompt` sections only (ADR-026/028 compaction single-path).
- **Producers-unwired guard** (the repo's recurring failure): highest-risk dead-wiring candidates are the schedule→workflow trigger, `output_schema` validation actually firing on node completion, the signed-definition gate actually refusing at enterprise/federal, and cross-run leg-threading actually reaching `PolicyContext`. Each demands an E2E test through the real path; full package matrix before merge.
- Skills are context, not callables: a node "using a skill" = a bounded agent run with that skill's body activated deterministically.
- MCP transport is declared-and-dead (enum + dep, zero dispatch). If nodes must reach MCP tools, that transport is built first as separate work.
- Spec-status drift: SPEC-040/043 PLAN files read PENDING but both are MERGED (ROADMAP-PROGRAM.md, PR #14/#15). Scope against code, not PLAN checkboxes.

### Research Insights

**From Solutions Archive:**
_(none)_

**Best Practices:**
- Add to the E2E list: a fully valid builder-produced draft still cannot execute above personal tier (no validation-success path flips signed status); and the runner receives tier at construction with an audit event asserting it.

**Edge Cases:**
_(none)_

**Performance:**
_(none)_

**References:**
_(none)_

## 10. Remaining open questions

### Research Insights

**From Solutions Archive:**
_(none)_

**Best Practices:**
- Channel binding: the membership routes and live stream already exist; one channel per WORKFLOW with a thread per run is the likely answer, pending a check that the channel model supports threads — the gate-answer card lands on whichever granularity is chosen.

**Edge Cases:**
_(none)_

**Performance:**
_(none)_

**References:**
- packages/arcui/src/arcui/routes/team_chat.py


(The v2 open decisions were resolved in the /build walk — see D-514 (lazy materialization supersedes the skipped-status question), D-526 (gate reject: reviewer chooses), D-517 (loop scope: ancestor-in-branch, statically validated), D-533 (subagents strategy first).)

1. **Runner host — RESOLVED (Josh, 2026-08-01):** the runner is arcteam code hosted on the **agent side of the fleet service** — `arcgateway.bootstrap` (the same startup that hosts the agents), never arcui. Rule: **arcui is a watcher and initiator only; execution must never require the dashboard.** A headless gateway (no UI) still progresses workflows. arcgateway gains arcstore + arcteam dependencies; the runner gets a singleton guard and its own actor DID (resolved from the on-disk operator key path, `generate_if_absent=False`, mirroring arcui/messaging.py:131-140).
2. **Channel binding granularity**: one group channel per workflow with a thread per run (likely), or a channel per run?
3. **arcui editor scope for Phase 3**: full graph editing, or node-property editing over a rendered graph first?
4. **Event triggers**: which lands first after v1 — message, webhook, or task-created?
5. **arcagent dependency posture**: declare arcstore + arcteam in arcagent's pyproject (workflows module makes them real), or keep lazy-import + graceful degrade?

## 11. Phasing

### Research Insights

**From Solutions Archive:**
_(none)_

**Best Practices:**
- Phase 1 gains three named prerequisites from research: TaskStore.create_batch (new arcstore work), the runner host + singleton guard + runner actor DID decision, and join semantics (done-or-skipped + join=all|any) in the validator.

**Edge Cases:**
_(none)_

**Performance:**
_(none)_

**References:**
_(none)_


- **Phase 1 — artifact + run.** Definition models/validator/versioning (draft lifecycle), `runs` collection, atomic cross-owner instantiation, runner with `agent`/`tool` nodes + conditional edges + typed wiring, `workflow_create/run/list/inspect`, `arc workflow sign/verify/run`, channel narration. Exit: describe a 3-node multi-agent workflow in chat, sign it, run it twice, watch the handoffs in the channel, see both runs' node-level history.
- **Phase 2 — gates, routers, loops, triggers, editing.** Human gates end-to-end, router nodes (both modes), bounded loops, scheduler `workflow_run` action, edit tools + IDE hand-edit path + optimistic concurrency, activation approval for trifecta-spanning definitions, run budget/stall/cancel hardening.
- **Phase 3 — the surfaces.** arcui workflows page + editor + run graph view + version diff; gates answerable in place; builder skill polished.
- **Phase 4 — depth.** `script` nodes, registry-level worker entities (arcteam), event triggers, new arcrun strategies land (`reflect`, `map`, `subagents`), app-store shell convergence.

## 12. Anti-goals

- No LLM-invented next node; routers choose only among declared routes. No undeclared or unbounded cycles.
- No orchestrator agent sequencing the graph; the runner is deterministic module code.
- No second DAG executor — if instantiation-onto-tasks can't express something, fix the task layer.
- No unsigned definition executing above personal tier; no gate resolvable by any agent tool at any tier; no signing key in the agent process.
- No workflow state through LLM file tools; workspace direct I/O only.
