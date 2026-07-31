# Rapport — The All-Around Agentic CRM

**Status:** Idea / pre-spec brainstorm
**Date:** 2026-07-13
**One-liner:** A harness-neutral agentic CRM — a revenue-specialized memory system of tools + skills + workspace store + entity-centric retriever — that distills customers, deals, commitments, and meeting notes out of the conversations an agent is already having, and layers an automated GTM strategy on top. Ships as `rapport`, a standalone product with per-harness adapter packages; `rapport-arc` is Arc's **first external plugin**, with `rapport-openclaw` and `rapport-hermes` as siblings.

---

## 1. Thesis

A CRM is not a database with a UI. It is a **memory discipline**: who did we talk to, what did they say, what did we promise, what happens next. Agents already sit inside those conversations. Rapport makes the agent the CRM: it *observes* sessions, distills revenue-relevant facts into human-readable workspace files, and gives the agent skills that recombine those files into pre-call briefs, personalized outreach, pipeline reviews, and a self-updating GTM playbook.

**Rapport = arcmemory's anatomy, specialized for revenue:**

| arcmemory | rapport |
|---|---|
| people / places / entities files | **contacts, companies, deals** files |
| daily notes / insights | **meeting notes, interaction log** |
| generic session distillation | **revenue distillation**: commitments, buying signals, objections, stage changes, next steps |
| generic retriever | **entity-centric retriever**: "everything about Acme before this call" as one context pack |
| — | **follow-up extraction** → durable reminders via tasks/scheduler |
| — | **GTM playbook that rewrites itself** from won/lost outcomes |

Three properties fall out of this framing:

1. **No data entry.** The CRM ingests by watching sessions (email threads, meeting transcripts pasted in, chat with the operator), not by asking anyone to fill forms.
2. **The store is inert; the skills are the product.** Storage is plain markdown; every capability is a skill that reads the same files a different way.
3. **Portability is nearly free.** Files + a library + a CLI have no harness dependency. Only three thin seams are per-harness: session-transcript access, tool exposure, and scheduling.

---

## 2. What it does (capability list)

- **Track customers** — contact + company files with structured frontmatter (role, channel, source, ICP fit, tags) and freeform narrative.
- **Track deals** — deal files: stage, value, close date, blocking objection, linked contacts/meetings, stage history.
- **Meeting notes, linked** — one file per meeting, wiki-linked (`[[acme-jane]]`, `[[deal-acme-renewal]]`) to every entity it touches.
- **To-dos & follow-up reminders** — commitments extracted from conversation ("I'll send the security doc Friday") become durable tasks with due dates; overdue ones surface automatically.
- **Personalized email** — drafts composed from *that contact's* history: last conversation, stated pain, their tone, deal stage. Draft-only by default; sending is human-gated (§8).
- **Automated GTM strategy** — a living `playbook/gtm.md` the strategist skill periodically rewrites from observed outcomes: which sources convert, which messaging lands, what the ICP actually looks like from closed-won data.

---

## 3. Architecture

```
rapport (standalone repo + pip package — harness-neutral product; rapport-arc = Arc's first external plugin)
│
├── rapport/core/          # harness-agnostic library (zero harness imports)
│   ├── adapter.py        # HarnessAdapter Protocol — THE seam (see §4.1)
│   ├── store.py          # workspace markdown store + writer (maintains link invariant)
│   ├── models.py         # Pydantic: Contact, Company, Deal, Meeting, Commitment
│   ├── links.py          # bidirectional link graph + traversal
│   ├── views.py          # views over the graph (pipeline, roster, activity)
│   ├── retriever.py      # entity-centric context packs
│   ├── distill.py        # session → CRM facts (needs an LLM loop — via adapter)
│   └── engine.py         # ReactLoop seam type (copies arcmemory react_adapter.py)
│
├── rapport/cli.py         # `rapport` command — the universal tool surface
│
├── rapport-arc/           # harness package: implements HarnessAdapter for Arc
├── rapport-openclaw/      # harness package: implements HarnessAdapter for openclaw
├── rapport-hermes/        # harness package: implements HarnessAdapter for Hermes
│
└── skills/                # skill folders — the universal artifact, identical everywhere
    ├── crm-pre-call-brief/
    │   ├── SKILL.md       # context: procedure, when-to-use, judgment rubrics
    │   └── tool.py        # deterministic script: extraction, skeleton, changeset ops
    ├── crm-follow-up-sweep/
    │   ├── SKILL.md
    │   └── tool.py
    ├── crm-distill/       # the ingest hook target — harness hooks invoke this
    │   ├── SKILL.md
    │   └── tool.py
    └── ...                # one folder per skill in §3.5
```

**The dependency arrow points one way:** core defines the `HarnessAdapter` Protocol and never imports a harness; harness packages import core and implement the adapter. Harness-specific code *pulls* the adapter contract — core never reaches out. Same inversion as arcagent's Brain port and arcmemory's `react_adapter`.

### 3.1 The store: workspace files, not a database

CRM data lives inside the **agent's workspace** — the same place arcmemory keeps its entity files, and the same convention Hermes and openclaw already use:

```
workspace/crm/
├── contacts/acme-jane-doe.md
├── companies/acme.md
├── deals/acme-renewal.md
├── meetings/2026-07-13-acme-renewal-call.md
├── todos/            # mirror of open commitments (source of truth may be tasks store on Arc)
├── playbook/gtm.md   # the living GTM strategy
└── .index/           # derived: sqlite index + optional embeddings (rebuildable, gitignored)
```

Every file: YAML frontmatter (typed, Pydantic-validated) + markdown body + `[[wiki-links]]`. Consequences: git-versionable, greppable, human-editable, agent-editable, zero server, and *any* harness that gives the agent a filesystem can host it. The `.index/` is derived state — `rapport reindex` rebuilds it from files, so files are always the source of truth (same recovery property as arcmemory's rebuild_index).

### 3.1a Cards: the entity file format

Every entity is a **card**: typed frontmatter (the structured details) + standard sections (the narrative). Cards are complete on their own — a human or agent opening one file sees everything that matters and every path outward.

```markdown
# contacts/acme-jane-doe.md
---
type: contact
name: Jane Doe
company: "[[acme]]"
role: VP Engineering
email: jane@acme.com
phone: +1-555-0100
channel: email            # preferred channel
timezone: America/Chicago
source: warm-intro        # where they came from (feeds GTM analytics)
icp_fit: high
last_touch: 2026-07-13
cadence_days: 14          # follow-up rhythm
tags: [technical-buyer, security-sensitive]
---

## Profile
Direct, technical, allergic to marketing language. Cares about SOC 2 story.

## Timeline
- 2026-07-13 — [[2026-07-13-acme-renewal-call]] — pushed on security review timing
- 2026-06-20 — [[2026-06-20-acme-proposal-review]] — liked usage-based pricing

## Open commitments
- OURS: send security doc by 2026-07-18 → [[todo-2026-0142]]

## Linked
deals: [[acme-renewal]] · company: [[acme]] · meetings: 4
```

```markdown
# companies/acme.md
---
type: company
name: Acme Corp
domain: acme.com
industry: industrial-saas
size: 450
segment: mid-market
health: yellow            # rollup: green/yellow/red from deal + touch signals
tags: [security-sensitive, multi-year]
---

## Overview
...

## Contacts
- [[acme-jane-doe]] — VP Engineering (champion)
- [[acme-raj-patel]] — CISO (blocker)

## Deals
- [[acme-renewal]] — negotiation, $48k, closes 2026-08-15

## Timeline
- 2026-07-13 — [[2026-07-13-acme-renewal-call]]
```

```yaml
# deals/acme-renewal.md (frontmatter)
type: deal
company: "[[acme]]"
contacts: ["[[acme-jane-doe]]", "[[acme-raj-patel]]"]
stage: negotiation        # lead → qualified → demo → proposal → negotiation → closed-won/lost
value: 48000
close_date: 2026-08-15
blocking: "security review sign-off"
stage_history:
  - {stage: proposal, entered: 2026-06-20}
  - {stage: negotiation, entered: 2026-07-08}
```

### 3.1b Bidirectional linking: the invariant that makes it a CRM

Plain wiki-links rot in one direction — a meeting note links to a contact, but the contact doesn't know. Rapport makes reciprocity a **store invariant, not a convention**:

> **Every edge is traversable from both ends, always.** If A references B, then B's card shows A — no exceptions, no manual upkeep.

Mechanics:

- **All writes go through the store writer** (tools/CLI/distiller — nobody hand-edits links in code paths). On any mutation the writer parses outbound `[[links]]` and updates the target card's `## Linked` / relevant section (contact ← meeting, company ← contact, deal ← meeting…). One writer, one invariant enforcement point.
- **The link graph is indexed both directions** in `.index/links.sqlite` (`edge(src, dst, rel, context)`) — traversal never requires opening files.
- **Humans can still hand-edit files** — `crm reindex` (and a cheap consistency pass in the daily sweep) diffs files against the index and repairs missing reciprocals. Files stay the source of truth; the invariant is self-healing.
- **Typed edges**, not blob links: `works_at`, `involved_in`, `attended`, `about`, `owes` / `owed` (commitments), `next_step_for`. Typed edges are what turn traversal into queries.

Traversal surface:

```
crm links acme --in            # everything pointing AT Acme
crm links acme-jane-doe --rel attended     # her meetings
crm path acme-target-corp     # warm-path: shortest link chain to a target account
crm recall acme               # the context pack = a budgeted graph walk from this node
```

### 3.1c Views: deterministic skeleton, agentic finish

Views are living markdown files **the agent keeps and uses** — the workpad-manages-`context.md` pattern. But the agent doesn't grind through cards by hand: **push every mechanical step into tools; the agent adds only judgment.**

The split per view refresh:

1. **Tool layer (deterministic, exact):** `crm_view <name>` extracts cards, filters, sorts, flags, and computes — guaranteed complete and correct. No deal missed, no arithmetic wrong, no contact forgotten. Output is the view *skeleton*.
2. **Agent layer (judgment):** the agent takes the skeleton, verifies it against what it knows, and layers on what a query can't produce — momentum reads, warnings, priority ordering, one-line status calls — then writes the final view file.

| View file | Tool skeleton (deterministic) | Agent judgment layer |
|---|---|---|
| `views/pipeline.md` | all open deals grouped by stage, sorted by age-in-stage, stall flags | which stalls are real vs. seasonal; the move per stuck deal |
| `views/open-deals.md` | every open deal: stage, value, close date, blocker | momentum read ("stalling — Raj hasn't replied since security review came up") |
| `views/customers.md` | full roster with lifecycle tags — `prospect`, `qualified`, `customer`, `champion`, `churned` — last touch, ICP fit | one-line status call per account; tag corrections it proposes |
| `views/today.md` | due follow-ups + overdue commitments + today's meetings with brief links | what it actually intends to do, in what order |
| `views/accounts/<name>.md` | account-360: company + contacts + deals + last 5 touches | relationship health, the single next best action |
| `views/neglected.md` | contacts past `cadence_days`, deals quiet > N days | who's *really* going cold vs. fine, and why |
| `views/forecast.md` | weighted pipeline by stage × close-date (the math) | its honest read where the formula lies |

Refresh cadence: the daily sweep skill + post-distill hook. Views are derived — regenerated from cards; humans read, never hand-edit. As workspace markdown they render in arcui's file viewer and are greppable like everything else.

**The dividing line for the whole system:** determinism does everything determinism *can* do — card schemas, the bidirectional-link invariant, the traversal index, view skeletons, list extraction, math. The **agent does only what requires judgment** — distillation calls, dedup confirmation, view annotation, drafts, strategy. Downward pressure: work lives at the lowest layer that can do it perfectly; the whole system stays agentic because the agent owns, verifies, and uses every artifact.

### 3.2 The distiller: session data in, CRM facts out

The ingest hook. After a session (or on a turn cadence), the distiller reads the **session conversation only** (the proven arcmemory lesson — never raw transcript dumps, deterministic curation first) and extracts:

- new/updated contacts and companies (**LLM-confirmed dedup** against existing files — never auto-merge; another paid-for lesson)
- commitments made, by whom, due when → todos/tasks
- buying signals, objections, competitive mentions → appended to deal/contact narrative
- deal stage evidence → proposed stage change (confirm-gated above a confidence threshold)
- a meeting-note file when the session contained a meeting

**The distiller's output is not prose — it is a typed `Changeset`:** a validated list of operations (`upsert_contact`, `add_commitment`, `append_signal`, `propose_stage_change`, `create_meeting_note`…), each a Pydantic model. The LLM runs *once* to decide what the conversation contained; a **deterministic executor** then applies the changeset mechanically — schema-validated, link-invariant-preserving, idempotent, audited, confirm-gated where thresholds require. The LLM never touches a file.

Distillation needs a bounded LLM loop. That loop is injected through the **engine seam** (§4.2) — the distiller depends on a `Callable`, never on arcrun.

### 3.3 The retriever: entity-centric context packs

Not generic semantic search. Given an entity (or an upcoming calendar event), assemble one bounded pack:

```
crm recall acme
→ company summary + open deal (stage, value, blocker)
  + contacts (role, tone notes, last touch)
  + last 3 meeting summaries
  + open commitments (ours and theirs)
  + relevant playbook plays for this stage
```

Mechanics: graph walk over wiki-links (deterministic, always works) + optional embedding rerank for narrative sections. **If the embedder is missing, degrade LOUD** — banner in output, never a silent no-op (the embedder-silent-degrade lesson). Token-budgeted like arcmemory's `retrieve(budget=...)`.

### 3.4 Tools (the verbs)

Thin wrappers over the core library:

| Tool | Classification | Purpose |
|---|---|---|
| `crm_capture` | state_modifying | run distiller over current session / supplied text |
| `crm_recall` | read_only | entity context pack |
| `crm_upsert_contact` / `crm_upsert_company` | state_modifying | typed field patch — validated against card schema |
| `crm_log_meeting` | state_modifying | create linked meeting note |
| `crm_advance_deal` | state_modifying | stage transition — validated against the stage graph, history entry appended |
| `crm_close` | state_modifying | close any lifecycle item: deal (won/lost + reason), todo (done/cancelled), commitment — timestamped, never deleted |
| `crm_add_commitment` | state_modifying | todo with due date → reminder |
| `crm_apply_changeset` | state_modifying | deterministic executor for LLM-produced changesets (distiller/analyzer output) — dry-run diff first |
| `crm_annotate` | state_modifying | agent prose lands only in designated narrative/analysis sections — never in structured fields |
| `crm_analyze` | state_modifying | run LLM analysis on a meeting/deal/email/contact → typed `analysis` block (tone, readiness, risks, next steps) |
| `crm_links` / `crm_path` | read_only | typed-edge traversal; warm-path between accounts |
| `crm_view` | read_only | deterministic view skeleton: extract cards → filter/sort/flag/compute (agent verifies, annotates, writes the final view) |
| `crm_sweep` | read_only | raw material for the sweep skill: overdue commitments, quiet deals, due follow-ups (the *agent* then finalizes views and queues outreach) |
| `crm_draft_email` | read_only (drafts only) | personalized draft from contact history |
| `crm_pipeline` | read_only | pipeline snapshot: stages, ages, stalls, forecast |

### 3.4a Mutation discipline: a machine you can trust

The design goal, stated plainly: **a machine we can trust — not one where we hope it bubbles insights.** Trust comes from making everything after LLM judgment deterministic:

1. **No freehand card edits — ever.** The agent never opens a card in an editor. Every variable mutation (field update, stage change, todo close, tag change, link) goes through a typed tool that Pydantic-validates the patch, enforces the card schema and the bidirectional-link invariant, appends to the timeline, and emits audit. Prose has exactly one door (`crm_annotate`) and can land only in designated narrative/analysis sections — structured fields are unreachable from free text.
2. **LLMs decide once, at the edges; machinery does the rest.** Distiller and analyzer runs emit **typed changesets**, not actions. The deterministic executor applies them: validated, idempotent, dry-run-diffable, confirm-gated above thresholds (stage changes, entity merges). If the LLM hallucinates a field, the schema rejects it — the store cannot be corrupted by a bad generation.
3. **Lifecycle is a state machine, not an opinion.** Deal stages move only along the allowed graph (`proposal → negotiation`, never `lead → closed-won`); todos go `open → done/cancelled` with timestamps; closes require a reason. Items are closed, never deleted — history is append-only.
4. **Review coverage is guaranteed by code, not memory.** `crm_sweep` and `crm_view` walk **every card via the index** — a deal cannot be skipped because the agent didn't think of it. The machine guarantees exhaustive coverage; the agent only prioritizes and judges what the scan surfaced. Followup review is a scan result, not a recollection.

### 3.4b The analysis layer: LLM judgment as structured data

LLM analysis is a first-class product feature — tone, readiness-to-buy, risk, our next steps — on meetings, deals, emails, and contacts. But it obeys the same discipline: **analysis is typed data on the card, not vibes in a chat log.**

```yaml
# appended to deals/acme-renewal.md by crm_analyze (via changeset)
analysis:
  assessed_at: 2026-07-13
  source: meeting:2026-07-13-acme-renewal-call
  tone: guarded-positive          # enum
  readiness_to_buy: 7             # 1–10, rubric-anchored
  momentum: stalling
  risks: ["security review unowned on their side", "Q3 budget freeze mentioned"]
  next_steps:
    - "send security doc before Friday (owed)"
    - "get Raj (CISO) into the next call"
  confidence: medium
```

Rules that keep it trustworthy:

- **Separated from facts.** Analysis lives in its own block; it can never overwrite observed data (what was said, what was promised). Facts are distilled; analysis is inferred — the card shows which is which.
- **Timestamped and superseded, not mutated.** Each `crm_analyze` run appends a new assessment (prior ones roll into history), so "readiness went 4 → 7 over three meetings" is a queryable trajectory — this is what the forecast view and `gtm-strategist` mine.
- **Rubric-anchored enums and scales,** not free-text adjectives — so views can sort by `readiness_to_buy` and sweeps can trigger on `momentum: stalling`.
- **Same pipeline:** analyzer LLM → typed changeset → deterministic executor. An analysis that doesn't fit the schema doesn't land.

### 3.5 Skills (the creative reuse — the actual product)

Every skill ships as a **skill folder**: `SKILL.md` + `tool.py`. This is the universal artifact — the same folder is picked up by openclaw's skills directory, Hermes's skill convention, and Arc (packaged/signed via arcskill). Downward pressure inside each folder:

- **`SKILL.md`** carries only what needs judgment: the procedure, when-to-use, rubrics (how to read a stall, what makes a good next step). Progressive disclosure — the agent loads it when the skill fires.
- **`tool.py`** carries everything deterministic, at zero AI tokens: card extraction, view skeletons, sweep scans, changeset application — thin entry points over `rapport.core`. Runnable two ways: **in-process** (Arc bridges it as a native signed tool) or **via shell** (`python tool.py sweep --json`) so any harness with shell access gets full fidelity with no integration work.

The skill instructs; the tool executes; the agent judges the output. Because data is plain markdown and every `tool.py` speaks the same core library, **the identical skill pack runs on all three harnesses untouched.**

**Revenue motions**
- `pre-call-brief` — before any calendar event with a known contact, assemble the one-page brief (recall + open commitments + suggested talking points from playbook).
- `personalized-email` — draft from their history and tone; never a `{first_name}` template.
- `follow-up-sweep` — daily: promises made vs kept, deals quiet > N days, contacts past cadence → today's outreach queue as tasks.
- `deal-advance` — for one deal, identify the single blocking objection from meeting notes and propose the next move.
- `inbound-triage` — new inbound (email/chat) → match/create contact, score ICP fit, route per playbook.

**Portfolio intelligence**
- `pipeline-review` — weekly: walk every open deal, flag stalled/rotting, forecast from stage-age patterns.
- `win-loss-autopsy` — diff closed-won vs closed-lost notes for repeating patterns → feeds the playbook.
- `relationship-map` — who knows whom; warm path into a target account.
- `segment-and-campaign` — slice contacts by any frontmatter attribute; per-segment message angle.

**Compounding GTM (the flywheel)**
- `gtm-strategist` — periodically reads the whole corpus and **rewrites `playbook/gtm.md`**: converting sources, landing messages, real ICP from won deals, cadence tuning. Other skills read the playbook, so strategy improvements propagate automatically. Skills act → outcomes land in the same store → strategy updates → skills act better. Compound engineering applied to sales.

---

## 4. Integration seams (grounded in the actual codebase)

**Key constraint discovered:** arcagent's module system is a hardwired internal directory scan (`agent_lifecycle.py` scans `arcagent/modules/` only — no entry-point discovery). An out-of-repo package **cannot be a "module."** So rapport inverts the dependency — our standard pattern:

### 4.1 The HarnessAdapter Protocol — one adapter, harnesses pull it

Core defines a single structural Protocol (primitives-only, like arcagent's `Brain` — implementers never import rapport internals). It names exactly what a harness must supply; everything else in core is harness-blind:

```python
# rapport/core/adapter.py
@runtime_checkable
class HarnessAdapter(Protocol):
    def workspace_root(self) -> Path: ...          # where crm/ lives
    def actor_did(self) -> str: ...                # identity for audit lines
    async def session_text(self, session_id: str | None) -> str: ...
                                                   # curated conversation for the distiller
    def react_loop(self) -> ReactLoop: ...         # bounded LLM loop (engine seam, §4.2)
    def schedule(self, name: str, spec: str, prompt: str) -> None: ...
                                                   # cron/interval → harness scheduler
    def create_task(self, title: str, due: date | None, meta: Mapping[str, str]) -> str: ...
                                                   # durable follow-up → harness task store
```

- **Direction:** `rapport-arc`, `rapport-openclaw`, `rapport-hermes` each import core and implement this Protocol. Core never imports a harness. Adding a harness = one new sibling package, zero core edits.
- **Discovery:** harness packages register via the **`rapport.harness`** entry-point group (the `arcgateway.adapters` precedent — name-regex validation, official-allowlist gating at federal). `rapport` CLI and skills resolve the active adapter from the environment; explicit `--harness` overrides.
- **Degrade, don't crash:** every adapter method has a null/degraded fallback (`NullAdapter`: no scheduler → sweep runs only when invoked; no task store → todos live as workspace files). Missing capability is a LOUD banner, never a silent no-op.

### 4.1a Arc: `rapport-arc` — signed capability drop-in + skill pack (no core changes)

- **Tools & hooks** ship as capability files (`@tool` / `@hook` decorated, per `arcagent/tools/_decorator.py`) installed into `~/.arc/capabilities/` (or agent capability root) by `rapport install --harness arc`. The CapabilityLoader AST-validates, trust-gates, and bridges them into the core ToolRegistry — which then signs every dispatched ToolCall with the agent key and injects `caller_did`. The plugin never touches signing at call time; it only needs its **artifacts** signed (§8).
- **Distillation cadence** rides a `@hook` on session/turn events (mirror the workpad/policy cadence pattern — and **persist the counter**; in-memory "every N turns" gates are defeated by service restarts. Paid-for lesson.)
- **Reminders** reuse existing infrastructure instead of a new daemon:
  - **tasks module (SPEC-056)** — commitments become durable tasks (retry, timeout, dead-letter, owner-gated, arcui board visibility).
  - **scheduler module** — cron/interval entries for `follow-up-sweep` (daily) and `gtm-strategist` (weekly).
- **Skills** install via the arcskill pipeline (packaged, validated, signed).

**Identity ruling: Rapport is like another brain — but it is its own plugin.** It never takes arcagent's single-select Brain slot; arcmemory keeps it, and **the same sessions may distill into both** (arcmemory's general lens and Rapport's revenue lens coexist — no partition convention needed). Rapport's retrieval stays explicit (`crm_recall` + skills), which is also more predictable than automatic injection.

### 4.2 The engine seam inside the adapter (copy `arcmemory/react_adapter.py`)

The one place rapport needs an agentic loop is the distiller (and the strategist). `HarnessAdapter.react_loop()` returns it, using arcmemory's proven isolation pattern verbatim:

- `CrmLoopOutcome` dataclass — engine-neutral result (`content, degraded, reason, turns, tokens_used`).
- `ReactLoop = Callable[..., Awaitable[CrmLoopOutcome]]` — the injectable seam; core depends on this signature only.
- `rapport-arc` wraps `arcrun.run(...)` behind a guarded import; absence → `degraded=True, reason="arcrun-absent"`, never a crash.
- openclaw/Hermes loops are **sibling adapter implementations**, not refactors — exactly the future arcmemory's docstring promises for itself.

### 4.3 The universal callable surface: skill folders + `tool.py` (+ CLI)

The portability contract is the **skill folder** (§3.5): `SKILL.md` + `tool.py`, where every `tool.py` is a thin entry point over `rapport.core` and every §3.4 verb is also `rapport <verb>` on the command line. **Any harness that can load a skill folder and run a shell command has full CRM access** — one implementation, three exposures:

- **arcrun/Arc** — `tool.py` functions bridged as native signed capabilities (`@tool` wrappers → ToolRegistry: typed schemas, signed dispatch, `caller_did`); skills packaged via arcskill.
- **openclaw** — skill folders drop into its skills directory; `SKILL.md` instructs, the agent shells to `tool.py`/CLI.
- **Hermes** — same folders, its skill convention; shell execution.

**Hooking in:** ingest is the `crm-distill` skill folder — each harness's hook surface (Arc session hooks, openclaw/Hermes post-session or cadence hooks) invokes it via the `HarnessAdapter`; the deterministic changeset executor does the rest.

### 4.4 What each harness package implements

The same Protocol, three implementations:

| `HarnessAdapter` method | `rapport-arc` | `rapport-openclaw` / `rapport-hermes` |
|---|---|---|
| `workspace_root()` | agent workspace (from `ModuleContext`) | their workspace/agent-dir convention |
| `session_text()` | session hook events → curated conversation | session/transcript file tail or post-session hook |
| `react_loop()` | arcrun via guarded import | their loop, or degraded pipeline fallback |
| `schedule()` | scheduler module (cron/interval entries) | their cron/heartbeat equivalent |
| `create_task()` | tasks module (SPEC-056: durable, retryable, board-visible) | workspace todo files (NullAdapter fallback) |
| tool exposure (outside the Protocol) | `tool.py` bridged as signed capabilities → ToolRegistry | skill folders + `tool.py`/CLI via shell |

Everything else — store, models, links, views, retriever, distiller, skills — is shared and identical. Registration is one entry point:

```toml
[project.entry-points."rapport.harness"]
arc = "rapport_arc:ADAPTER"
```

---

## 5. What `rapport-arc` means for Arc itself

Rapport is harness-neutral, but its Arc adapter is the pathfinder proving Arc's extension story — the first external package to plug into an arc agent. Arc-side items it surfaces (small, mostly-existing):

1. **Capability install UX** — `rapport install` drops signed `.py` + `.arcsig` into a capability root; may motivate a generic `arc plugin install <pkg>` later. TOFU at personal; pinned signer DID at enterprise/federal — already how the trust gate works.
2. **Scaffold wiring** — `arc agent create` should be able to declare rapport's config block. **Producers-unwired is the #1 recurring failure mode** (SPEC-056 tasks module was dead until `[modules.tasks]` was in each toml): the plugin must fail LOUD at startup if partially wired, and the E2E test must run through the real load path — capability scan → trust gate → registry bridge → tool dispatch — not import the library directly.
3. ~~Brain composition~~ — no longer needed (§10 Q1): Rapport is its own plugin beside arcmemory; no arcagent core changes required at all.

---

## 6. GTM automation loop (end-to-end example)

1. **Monday 08:00** — scheduler fires `follow-up-sweep`: 2 overdue commitments, 1 deal quiet 12 days, 3 contacts past cadence → 6 tasks created (SPEC-056 store: durable, retryable, visible on the arcui board).
2. Dispatch loop picks up each task → `pre-call-brief` / `personalized-email` skills run → **drafts** appear for operator review.
3. Operator approves; sends (or auto-send at personal tier for pre-approved cadence classes, §8).
4. Replies arrive → `inbound-triage` distills: objection captured on deal file, stage-change proposed, thank-you commitment logged.
5. **Friday** — `gtm-strategist` reads the week's outcomes and updates `playbook/gtm.md`: "warm-intro contacts close 3× cold; security objection now appears in 4/5 enterprise deals — move security doc to demo stage." Next week's skills read the new playbook.

Every step: signed tool dispatch, `caller_did`, audited, tier-gated. No step requires a human except where a human genuinely decides.

---

## 7. Why this beats a traditional CRM

- **Zero data entry** — the agent was in the conversation; the CRM distills it.
- **Memory-native personalization** — outreach from actual history, not merge fields.
- **Self-improving strategy** — the playbook is a living artifact updated from outcomes.
- **Owned data** — markdown in your workspace; git is the backup and the audit trail.
- **Harness-agnostic** — one skill pack across Arc, Hermes, openclaw.
- **Compliance-grade** (Arc tier) — signed artifacts, policy pipeline, audited dispatch: a CRM you could actually run in a federal GTM shop.

---

## 8. Security & tiers (four pillars — universal, not federal-only)

- **Identity** — every tool dispatch carries `caller_did` (registry-injected); CLI stamps actor DID into an audit line per mutation.
- **Sign** — capability files + skills ship with `.arcsig` sidecars (Ed25519, arctrust). Personal: TOFU with audit warn. Enterprise/federal: valid signature is the floor AND signer DID must be operator-pinned — nothing unsigned loads.
- **Authorize** — tools declare `read_only` / `state_modifying` (fail-closed default); PolicyPipeline evaluates every call, first-DENY-wins.
- **Audit** — emitted centrally by the ToolRegistry on every dispatch; CRM mutations are all tool calls, so audit coverage is structural.

**The Lethal Trifecta lives here.** Rapport combines private data (customer records) + external comms (email) + untrusted input (inbound email content). Non-negotiables:

- `crm_draft_email` **drafts only**; sending is a separate, human-gated action. Auto-send (if ever) only at personal tier, per-cadence-class allowlisted, never for content derived from untrusted inbound.
- Inbound content is **data, never instructions**: distiller output is structured field updates validated by Pydantic (reuse SPEC-056's free-text sanitization / injection-refusing validators), not free-text agent directives.
- Stage changes and entity merges above a confidence threshold are **confirm-gated** (LLM-confirmed dedup lesson: dupes are cheaper than wrong merges).
- PII lives in the workspace only; classification-aware retrieval at enterprise/federal; no customer data in logs or error messages.

---

## 9. Build phases

| Phase | Scope | Proof |
|---|---|---|
| **1. Core + CLI** | store + writer (bidirectional-link invariant), models/cards, typed link graph, retriever (graph-walk only), CLI verbs; no LLM needed yet | round-trip: create → link → both cards show the edge → recall pack; hand-break a reciprocal link → `reindex` repairs it |
| **2. Arc adapter** | signed capabilities, session-hook distiller (arcrun via engine seam), skills v1 (`pre-call-brief`, `personalized-email`, `follow-up-sweep` incl. agent-kept views: `open-deals`, `customers` w/ lifecycle tags) | E2E through the REAL path: capability scan → trust gate → dispatch; distill a live session on a dev agent; views regenerate correctly after a stage change |
| **3. Reminders + GTM loop** | tasks + scheduler wiring; `pipeline-review`, `gtm-strategist`; embedding rerank (loud-degrade) | Monday sweep → tasks → drafts → approval, end-to-end on the DGX fleet |
| **4. Portability** | `rapport.harness` entry-point group; openclaw adapter; Hermes adapter | same skill pack + workspace files produce a correct recall pack under a second harness |

Phase 2 is the wedge: a single Arc agent that briefs you before calls and drafts follow-ups from real history is already daily-useful.

---

## 10. Open questions

1. ~~**Brain composition**~~ — RESOLVED: Rapport **is like another brain, but it is its own plugin.** It never takes arcagent's single-select Brain slot (arcmemory keeps it); retrieval stays explicit (`crm_recall` + skills). If arcagent ever grows multi-brain injection, Rapport can offer a Brain-conformant facade then — no dependency on it now.
2. ~~**arcmemory overlap**~~ — RESOLVED: **distilling to arcmemory too is fine.** No partition war, no exclusivity convention — the same session may feed both; each keeps its own lens (arcmemory: general entities/insights; Rapport: revenue cards). Dedup discipline lives *within* each store, not between them.
3. ~~**Email transport**~~ — RESOLVED: **Rapport ships no transport.** Gmail (or any channel) is set up through the harness and called/used as needed — Rapport skills draft, then invoke whatever send tool the harness exposes, behind the human gate (§8). No harness email tool → drafts remain workspace files.
4. **Calendar trigger** — `pre-call-brief` wants "upcoming event with known contact." Calendar MCP poll via scheduler, or leave it operator-invoked in Phase 2?
5. **Multi-agent sharing** — one CRM workspace shared by a fleet (SDR agent + AE agent both read/write)? Files + git make this plausible; needs an owner/locking convention (tasks store already has owner-gating to copy).
6. ~~**Naming**~~ — RESOLVED: **Rapport**, a harness-neutral name; Arc is one adapter among siblings (`rapport-arc`, `rapport-openclaw`, `rapport-hermes`). Verify PyPI availability (`rapport` may be taken → fall back to `rapport-crm` as the distribution name, `rapport` as import/CLI name).

---

## 11. Anti-goals

- **No web UI in v1** — arcui's board/files views already render tasks and markdown; the workspace *is* the UI.
- **No custom sync/server** — files + git; no hosted backend.
- **No generic-CRM feature chase** — no bulk import wizards, no 40-field layouts. Anything a skill can do by reading files does not become core code.
- **No daemon of its own** — cadence comes from the host harness's scheduler/hook surfaces. rapport is a library + tools + skills, never a process.
