# The Decision Index — Every Seam to Its `D-NNN`

> **Concepts**  ·  Understand  ·  the rationale map under the whole stack
> [Docs home](../README.md)  ·  [The Seam Model](seam-model.md)  ·  [2. Architecture](../walkthrough/02-architecture.md)

---

## In one breath

Arc records **why** it is built the way it is. Every non-trivial design choice
gets a global, monotonic ID — `D-001`, `D-002`, … up to `D-726` today — written
into the project's decision log the moment it is made, and never renumbered. A
superseded decision keeps its ID and gains a note; it is not deleted and not
reused.

This page is the **map from a data flow to the decisions that govern it**. The
big table below (the *data-flow / decision catalog*) is the shared reference:
one row per major flow, with where it lives, what calls what, what passes where,
the security reason, the `D-NNN` set, and a verified code anchor. Every
"How Arc works" flow page repeats those same six fields in its footer, so the
catalog is the index and each page is the expansion. Below the catalog, a
per-seam index lists the individual decisions with a one-line summary and a
code anchor where one is verifiable.

---

## How to read this page

- **`D-NNN`** — a decision ID. The full text, alternatives considered, and
  rationale live in the project's decision log, grouped by concern
  (Architecture, Data Model, API Design, Identity & Trust, Security, Audit &
  Compliance, Observability, Integration, Performance, Extensibility, …). The
  log's appendix is the exhaustive ID lookup; this page is the *curated* subset
  the documentation actually leans on.
- **Code anchor** — a `path:line` into the source tree, verified against the
  current code at authoring time. Line numbers drift; the **symbol name** in the
  cell is the durable anchor. Paths are under the repo root; `packages/*/src/`
  segments are elided in the tables for width and shown in full in each seam
  section.
- **`ADR-NNN`** — a longer-form Architecture Decision Record. ADRs and `D-NNN`
  decisions cross-reference each other; a `D-NNN` is the atomic choice, an ADR
  is the essay.
- Where the decision log or the code does **not** clearly support a claim, the
  cell says **needs confirmation** rather than inventing a rationale.

---

## The data-flow / decision catalog

One row per major flow. This is the source of truth for the per-page footers in
the "How Arc works" set. **Where / when / to** is the load-bearing column: what
data crosses the seam, at what moment, to whom.

| Flow | Where it lives | What calls what | What passes — where / when / to | Security / modularity reason | `D-NNN` / ADR | Code anchor (entry) |
|---|---|---|---|---|---|---|
| **Run / turn** | arcagent core → arcrun | `ArcAgent.run` → `dispatch_stream` → `build_run_context` → `arcrun.run` → `react_loop` → `model` invoke | `run_id` minted once and **pinned** through the whole run; tool-set **frozen** at loop start; per-turn `messages` + tool schemas → model; stream events back to the surface | Frozen tool-set = no mid-run privilege change; one `run_id` = honest trace correlation | D-591, D-587, D-589, D-602, D-618, D-623; ADR-024, ADR-027 | `core/agent.py:738` · `core/agent_dispatch.py:296,42` · `arcrun/loop.py:67,82` · `arcrun/strategies/react.py:237` |
| **Tool call → policy** | arcagent `tool_registry` → arctrust `policy` | `wrapped_execute` → validate args → `sign_call` → `PolicyPipeline.evaluate` (layers) → `pre_tool` → `execute` → `post_tool` → audit | signed `ToolCall`(name, args, `agent_did`, session, classification, `origin`) → each layer **in order**; first `DENY` short-circuits before the handler; result → caller; `tool.executed` → WORM | First-DENY-wins + fail-closed on exception = least privilege, no confused-deputy | D-294, D-662, D-608, D-607, D-563; ADR-017A | `core/tool_registry.py:646,520,537` · `arctrust/policy.py:1187,1221` |
| **Audit emission (WORM)** | arctrust `audit` (+ arcui) | `emit(event, sink)` → `WormSink._append` (flock → hash → sign → write) → `_maybe_rotate`; arcui via `MutationWormWriter` | `AuditEvent` → sink at every op; each record chains `event_hash = H(seq + prev_hash + event)` + signature; rotated on size/count | Tamper-evident, single-writer, **fail-open** (never breaks the audited op) | D-203, D-047, D-437, D-026; ADR-022 | `arctrust/audit.py:518,174,291,316` · `arcui/audit.py:231,235` |
| **Connected source → knowledge** | arcagent `connected_data` → arcmemory `ConnectedDataService` → extension `SourceAdapter` | coordinator → adapter `inspect/list/select/sync/fetch` → `require_approved_mapping` → `ingest` → index → `document_search` | source objects → per-source **doc scope** `<did>:doc:<source_id>`; sync only after the **exact mapping is approved**; provenance stamped; retrieval scoped | Per-source scope = LLM08 cross-tenant isolation; mapping approval = no silent field mapping | D-683, D-686, D-691, D-688, D-684; SPEC-073 | `arcagent/modules/connected_data/service.py:123` · `arcagent/connected_data.py:159,161` · `arcmemory/doc_index.py:153` · `arcagent/extension/source.py:160` |
| **Memory capture → recall** | arcmemory (via arcagent Brain seam) | `FastCapture.capture` → episodic; `Consolidator.run` (distill/dedup, signed) → stores; `Retriever.retrieve` → surface search (vec + bm25 + graph + recency) → fuse → gate | turn text → episodic (every turn); recall query → one bounded fuse; **`vec0` JOIN `chunks` WHERE scope = ?**; boundary-marked DATA → prompt | Markdown = glass-box truth, index disposable; scope join = no cross-agent leak; recalled text is inert DATA (LLM01) | D-726, D-411, D-225, D-226, D-334 | `arcmemory/capture.py:64` · `arcmemory/consolidate.py:216` · `arcmemory/retrieve.py:95` · `arcmemory/index/backend.py` (scope join) |
| **Inter-agent message** | arcteam `mail` → arcstore outbox | `AgentMailService.send` → `_sign_envelope` → atomic inbox+outbox commit → leased delivery worker → NATS / memory backend | signed envelope + `conversation_id` → atomic commit; leased `SKIP LOCKED`; `sent` only after transport ack, else `pending`; per-participant copy | Mail body is untrusted data, not control-plane; durable store authoritative, mail is a wakeup; scoped DIDs | D-538, D-539, D-510, D-647; ADR-007 | `arcteam/mail.py:213,342,188` · `arcstore/mail_outbox.py:199` |
| **An approval** | arcstore `approvals` → arccli → arctrust | `ApprovalStore.create` (pending row) → `arc approve` → `OperatorKey` signs → `resolve` sets grant; scenario: `sign`/`verify_scenario_grant` | `PendingApproval`(call_hash) → durable row; operator signature → grant; scenario grant keyed by `scenario_key` incl. **`origin`** (`workflow:` / `schedule:`; `None` = interactive matches nothing) | Draft/sign split = agent never holds the operator key; `origin` key = a standing grant can't be replayed by an interactive turn | D-523, D-525, D-557, D-111, D-651; ADR-034 | `arcstore/approvals.py:112,134,166` · `arctrust/policy.py:123,569` |
| **LLM routing / load-balance** | arcllm | router selects provider by required features → provider adapter (override URL/auth/model-map) → endpoint pool (weighted round-robin, health circuit) | request → classification / feature check → provider; within provider, spread across N `[[endpoints]]`; per-endpoint vault key; per-call `load_balance=True` | Routing policy stays inside arcllm (concern purity); intra-provider only = no cross-provider data spread | D-190, D-197, D-232, D-233, D-449, D-453, D-458; ADR-035 | `arcllm/registry.py` (router) · `arcllm/modules/load_balancer.py` · `arcllm/config.py` (`[[endpoints]]`) |
| **Prompt assembly** | arcprompt → arcagent context | `PromptResolver.resolve` (stock + operator overlay) → `PromptSnapshot` → `ContextManager.assemble_system_prompt` (+ bus sections) → `ToolRegistry.format_for_prompt` | stock docs + signed overlay → frozen snapshot per run; core files + injected sections + tool catalog → system prompt → model | Prompts are signed protected artifacts (not mutable text); overlay pinned to operator key; snapshot = reproducible | D-459, D-460, D-461, D-462, D-463, D-073, D-074; ADR-006 | `arcprompt/resolver.py:60` · `arcprompt/snapshot.py:28` · `session_internal/context.py:337` · `core/tool_registry.py:202` |
| **Config load + tier resolution** | arcagent `config` + arctrust `paths` | `load_config` → `compose_raw_config` (`sibling_chain` packaged → user → overlay, `deep_merge`, env overrides) → Pydantic validate → `_enforce_tier_crypto_floor` | three TOML layers → one `ArcAgentConfig`; `[security].tier` → crypto floor + active policy set; paths via one named accessor each | Single resolver per home path (one source of truth); tier floor refuses below, cannot raise; secure-by-default | D-489, D-310, D-471, D-579; ADR-003, ADR-029 | `core/config.py:740,564` · `core/config_loading.py:80,69,48` · `arctrust/paths.py:475` |
| **Spool telemetry write** | arcstore `spool` | run / tool / llm code → `record(rec, path)` inside `request_context(run_id)` | `SpoolRecord`(kind, `request_id`) → daily `operational-YYYY-MM-DD.jsonl`, one append; `request_id` via contextvar | Always-on, fail-open, single atomic append; `request_id` = concurrent-run correlation | D-203; ADR-022 | `arcstore/spool.py:83,62,80` · `arcstore/records.py:20` |
| **Gateway inbound → agent** | arcgateway (+ adapter extensions) | adapter normalizes platform update → inbound event → session router → executor → `ArcAgent.run` | platform message + media → gateway session identity; inbound body injected only as **user content**, never control; media stored, trust-posture applied | Gateway owns the session boundary; inbound is untrusted (LLM01); media trust / retention decisions | D-668, D-670, D-677, D-678, D-679, D-325; ADR-020 | `arcgateway/adapters/*` · gateway `session.py` / `executor.py` |
| **Workflow / schedule fire → task** | arcteam / arcagent workflow + scheduler | `[trigger]` cron → owner-scoped schedule → node materialization on the task-DAG substrate → run progression | schedule fire → task **write** (durable) → agent run; `deliver_to` pins a node's notify to a channel (signed) | Handoff is a task write, not a message (D-538); monotonic-progress rule; `deliver_to` model-immutable | D-507, D-508, D-509, D-630, D-119, D-335 | `arcagent/modules/` (scheduler / workflows); SPEC-056 / SPEC-061 |
| **Dynamic tool / sandbox exec** | arcagent tools → arcrun / isolation | agent-authored Python → encoding / AST check → restricted builtins → isolated JSON exec seam → result to LLM; browser via CDP backend | authored code → sandbox (deny-by-default), never `eval`; result → model as DATA | RCE containment (ASI05); isolation only for executable artifacts; allowlist model | D-659, D-667, D-608, D-601, D-145, D-175, D-176; ADR-017C | `arcagent` tools / `arcrun` isolation backends |

> **Two anchors are deliberately left open.** (1) The exact site where
> `document_search` is registered as an **LLM-facing tool** (a `ToolSpec`, as
> opposed to the callable at `arcmemory/doc_index.py:153`, the Brain method at
> `arcmemory/brain.py:729`, or the arcui REST route at
> `arcui/routes/knowledge.py:630`) was not located — **needs confirmation** by
> an arcmemory/connected-data trace. (2) Inbound-media storage location,
> untrusted-media posture and retention (D-672 / D-674 / D-675 / D-681 / D-682)
> are decided in the log but not yet anchored in prose — **needs confirmation**.

---

## Decisions by seam

Each seam below expands one catalog row. The one-line summaries are drawn from
the decision log; the anchors are verified `path:line` under the repo root.

### Package layering & dependency direction

The one-way rule the whole stack rests on: `arcllm → arcrun → arcagent`;
`arcteam → {arcagent, arcmemory}`; `arctrust` is the leaf; nothing imports
upward. See [2. Architecture](../walkthrough/02-architecture.md) for the full
package inventory and the AST guards that enforce this.

| `D-NNN` | Decision | Anchor / enforcement |
|---|---|---|
| **D-620** | One-way execution-stack dependency graph | `tests/architecture/` (AST import guards) |
| **D-621** | ArcAgent consumes ArcRun through **one** qualified facade import | `packages/arcagent/src/arcagent/` (`import arcrun` only) |
| **D-626** | Core packages have one clean cross-package import | architecture tests, per package |
| **D-632** | Every module is optional; **none ship in the wheel** | `~/.arc/modules/` signed bundles (ADR-034) |
| **D-635** | One core artifact; the module bundle is the only thing that varies by deployment | deploy pipeline |

### Run / turn

| `D-NNN` | Decision | Anchor |
|---|---|---|
| **D-591** | arcrun owns run-level state | `packages/arcrun/src/arcrun/loop.py:67` |
| **D-587** | Adopt steering (mid-execution interrupt) | `packages/arcrun/src/arcrun/strategies/react.py:237` |
| **D-589** | Adopt streaming response deltas | `packages/arcrun/src/arcrun/strategies/react.py:460` |
| **D-602** | Strategy enforces `max_turns` | `packages/arcrun/src/arcrun/strategies/react.py` |
| **D-618** | ABC base class for Strategy | `packages/arcrun/src/arcrun/strategies/` |
| **D-623** | Parallel dispatch is a public **mechanism**, not a concrete strategy dependency | `packages/arcagent/src/arcagent/orchestration/` |

### Tool call → policy

| `D-NNN` | Decision | Anchor |
|---|---|---|
| **D-662** | Tool dispatch is an ordered typed pipeline | `core/tool_registry.py:646` |
| **D-294** | Tool policy pipeline | `packages/arctrust/src/arctrust/policy.py:1135,1187` |
| **D-607** | `jsonschema` for parameter validation | `core/tool_registry.py` (validate step) |
| **D-608** | Dynamic tools denied by default | `packages/arcrun/src/arcrun/` |
| **D-563** | Tool-contract hashing (rug-pull defense) — a signed tool that changes shape after approval is re-gated | **needs confirmation** (named invariant; hashing site not yet anchored) |

### Audit emission & the WORM chain

| `D-NNN` | Decision | Anchor |
|---|---|---|
| **D-203** | Audit logs tamper-evident (append-only + OTel) | `packages/arctrust/src/arctrust/audit.py:518` |
| **D-047** | Hash chain across rotations | `packages/arctrust/src/arctrust/audit.py:316` |
| **D-437** | Hash chain covers raw bodies + encryption envelope, no new integrity machinery | `packages/arctrust/src/arctrust/audit.py:291` |
| **D-026** | Tamper-evident UI audit log | `packages/arcui/src/arcui/audit.py:231,235` |

> **Correctness note — audit sink names.** The real sinks are
> **`NullSink`** (tests / air-gapped) and **`WormSink`** (durable, signed
> hash-chain), both in `packages/arctrust/src/arctrust/audit.py`. ArcUI's
> operator-mutation capture is **`MutationWormWriter`**
> (`packages/arcui/src/arcui/audit.py:231`); its ephemeral log + OTel span is
> `UIAuditLogger` (`:183`). `JsonlSink`, `SignedChainSink`, and
> `arcui.bridge.UIBridgeSink` are **prose contrasts only** — they name behaviors
> the two real sinks *superseded* (see `audit.py:9-14`), not classes you can
> import. `UIBridgeSink` in particular was a live-push sink that was
> deliberately removed (SPEC-026); do not cite any of the three as real classes.

### Connected source → knowledge

| `D-NNN` | Decision | Anchor |
|---|---|---|
| **D-683** | Ingestion pipeline shape; the router owns source → scope resolution | `packages/arcmemory/src/arcmemory/connected_data.py` |
| **D-684** | Connector → arcmemory ingest seam (deterministic `event_id`) | `packages/arcmemory/src/arcmemory/` (ingest boundary) |
| **D-686** | Index engine + **per-source pools** (N `SurfaceIndex`, one per source) | `packages/arcmemory/src/arcmemory/doc_index.py:153` |
| **D-688** | Re-index strategy + sync by mutability | `packages/arcmemory/src/arcmemory/connected_data.py` |
| **D-691** | Mapping proposal + operator-DID approval (SPEC-035 grant) | `packages/arcagent/src/arcagent/connected_data.py:159` |

The source-side contract is the `SourceAdapter` Protocol
(`packages/arcagent/src/arcagent/extension/source.py:160`) — five methods
(`inspect_source`, list, select, sync, fetch). Doc scope is
`<did>:doc:<source_id>`, so "disconnect = delete the pool" is literal.

### Memory capture → recall

| `D-NNN` | Decision | Anchor |
|---|---|---|
| **D-726** | Query-less proactive recall is gated on interactive turns | `arcagent` `turn_context.py` (`interactive`) |
| **D-411** | Session consolidation | `packages/arcmemory/src/arcmemory/consolidate.py:216` |
| **D-225** | Light consolidation | `packages/arcmemory/src/arcmemory/consolidate.py` |
| **D-226** | Deep consolidation | `packages/arcmemory/src/arcmemory/consolidate.py` |
| **D-334** | Memory architecture under per-(user, agent) sessions | `packages/arcmemory/src/arcmemory/` |

The scope join is the isolation invariant: `vec0` carries no scope column, so
`vec_search` joins against `chunks` with `WHERE scope = ?`
(`packages/arcmemory/src/arcmemory/index/backend.py`). See
[Memory, the Index, and Scope](memory-index-and-scope.md).

### Inter-agent message

| `D-NNN` | Decision | Anchor |
|---|---|---|
| **D-538** | Handoff is a task **write**, never a message | `packages/arcagent/src/arcagent/modules/` |
| **D-539** | A run is an office, not a data bus — the shared run workspace | `packages/arcagent/src/arcagent/` |
| **D-510** | Multi-agent scope | `packages/arcteam/src/arcteam/mail.py:188` |
| **D-647** | One coordinator serializes complete turns per session | `packages/arcagent/src/arcagent/` |

The durable seam is `AgentMailService.send` → `_sign_envelope`
(`packages/arcteam/src/arcteam/mail.py:213,342`) → the leased
`PostgresMailOutbox` with `SKIP LOCKED`
(`packages/arcstore/src/arcstore/mail_outbox.py:199`). See
[Fleet layering](fleet-layering.md).

### An approval

| `D-NNN` | Decision | Anchor |
|---|---|---|
| **D-523** | Draft / sign split — the agent drafts, the operator key signs | `packages/arcstore/src/arcstore/approvals.py:134` |
| **D-525** | Activation approval | `packages/arcstore/src/arcstore/approvals.py:166` |
| **D-557** | Trifecta approval policy | `packages/arctrust/src/arctrust/policy.py` |
| **D-111** | Approval Queue | `packages/arcstore/src/arcstore/approvals.py:112` |
| **D-651** | One verb signs and pins; `arc trust approve` produces a real signature | `packages/arccli/src/arccli/commands/approve.py` |

Scenario grants (recurring automation) key on `scenario_key` **including
`origin`** (`packages/arctrust/src/arctrust/policy.py:123,569`): a standing
`workflow:`/`schedule:` grant cannot be replayed by an interactive turn, whose
`origin` is `None` and matches nothing.

### LLM routing & load-balancing

| `D-NNN` | Decision | Anchor |
|---|---|---|
| **D-190** | Routing module stack position | `packages/arcllm/src/arcllm/registry.py` |
| **D-197** | Routing classification source | `packages/arcllm/src/arcllm/` |
| **D-232** | Provider inheritance strategy (override `base_url` / auth / model-map) | `packages/arcllm/src/arcllm/adapters/azure_openai.py` |
| **D-233** | Provider override class name | `packages/arcllm/src/arcllm/adapters/` |
| **D-449** | Scope is **intra-provider**: spread across N endpoints/keys of the *same* provider | `packages/arcllm/src/arcllm/modules/load_balancer.py` |
| **D-453** | Pool topology `[[endpoints]]` in provider TOML; strategy in `[modules.load_balance]` | `packages/arcllm/src/arcllm/config.py` |
| **D-458** | LB sits at the innermost stack position, holding a pool of endpoint adapters | `packages/arcllm/src/arcllm/modules/load_balancer.py` |

### Prompt assembly

| `D-NNN` | Decision | Anchor |
|---|---|---|
| **D-459** | Prompt load model | `packages/arcprompt/src/arcprompt/resolver.py:60` |
| **D-460** | Overlay scope | `packages/arcprompt/src/arcprompt/` |
| **D-461** | Reload semantics | `packages/arcprompt/src/arcprompt/` |
| **D-462** | Frontmatter schema | `packages/arcprompt/src/arcprompt/` |
| **D-463** | Resolution failure mode | `packages/arcprompt/src/arcprompt/resolver.py` |
| **D-474** | Loaded artifacts verified **before** use | `packages/arcprompt/src/arcprompt/` |
| **D-073 / D-074** | Tool-catalog formatter location / prompt format | `core/tool_registry.py:202` |

### Config load + tier resolution + arc-home

| `D-NNN` | Decision | Anchor |
|---|---|---|
| **D-489** | Home vs working dir | `packages/arctrust/src/arctrust/paths.py` |
| **D-310** | Config architecture (three-file sibling chain) | `core/config_loading.py:69,80` |
| **D-471** | Tier variance via policy **content**, not code | `packages/arcprompt/src/arcprompt/` |
| **D-579** | A manifest's tier floor refuses below, and cannot raise | `core/config.py:564` (`_enforce_tier_crypto_floor`) |
| **D-636** | Bundle signature is verified **before** materialize, never after | `packages/arcagent/src/arcagent/` (bundle load) |

Runtime activation lands at `arctrust/paths.py:475` (`activate_runtime`) — the
atomic `current` flip. The single-resolver rule (`~/.arc` install vs `~/arc`
operator) is guarded by `tests/architecture/test_arc_home_single_resolver.py`.

### Gateway inbound & media trust

| `D-NNN` | Decision | Anchor |
|---|---|---|
| **D-668** | Inbound envelope shape | `packages/arcgateway/src/arcgateway/adapters/` |
| **D-670** | Adapter location and discovery | `packages/arcgateway/src/arcgateway/adapters/` |
| **D-677** | The adapter author's contract | `packages/arcgateway/src/arcgateway/adapters/base.py` |
| **D-678** | Who owns session identity | `packages/arcgateway/src/arcgateway/session.py` |
| **D-679** | What an inbound message may be injected into (user content only) | `packages/arcgateway/src/arcgateway/executor.py` |
| **D-325** | arcgateway process model | `packages/arcgateway/src/arcgateway/` |
| **D-672 / D-674 / D-675** | Where inbound media bytes live / trust posture / filename+size+audit | **needs confirmation** (decided in log; not yet anchored) |

### Workflows, tasks & the scheduler substrate

| `D-NNN` | Decision | Anchor |
|---|---|---|
| **D-507** | Execution substrate (task DAG) | `arcagent/modules/` (tasks); SPEC-056 |
| **D-508** | Run progression | SPEC-056 |
| **D-509** | Vocabulary and node kinds | SPEC-061 (ArcFlow) |
| **D-630** | A nonterminal plan iteration must make **monotonic progress** | `arcagent/modules/` |
| **D-119** | Where does the scheduler live? | `arcagent/modules/scheduler/` |
| **D-335** | Natural-language cron parser | `arcagent/modules/scheduler/` |

### Dynamic tools & sandbox (RCE containment)

| `D-NNN` | Decision | Anchor |
|---|---|---|
| **D-659** | Authored Python crosses an isolated JSON execution seam | `packages/arcrun/src/arcrun/` (isolation) |
| **D-667** | Isolation backends are acquired **only** for executable artifacts | `packages/arcrun/src/arcrun/backends/` |
| **D-601** | Allowlist security model | `packages/arcrun/src/arcrun/` |
| **D-145 / D-175 / D-176** | CDP client library / what the container sandbox wraps / how it integrates | `arcagent` browser module / `arcrun/backends/` |

---

## Where the full record lives

- Every decision, in full, with alternatives and rationale, is kept in the
  project's decision log (whose appendix is the exhaustive `D-NNN` lookup).
- The longer-form essays are the Architecture Decision Records (`ADR-NNN`).
- The doctrine these decisions serve: [The Seam Model](seam-model.md) ·
  [2. Architecture](../walkthrough/02-architecture.md) ·
  [Memory, the Index, and Scope](memory-index-and-scope.md) ·
  [Fleet layering](fleet-layering.md).
