# Arc Documentation Program Plan

> **Design doc — gates a real authoring effort. Not a user-facing doc.**
> Status: PROPOSED · Author: docs-planner agent · 2026-08-29
> Scope: the complete page tree, media assignments, the decision/data-flow
> catalog, the build sequence, and the reverse-engineering gaps — for a docs
> program that lets a new user **fully stand up and tune** an Arc stack, and
> captures **why it is built this way** (decisions + data flows) for architects.

---

## 0. What this plan is answering

Josh wants two things at once:

1. **A new user can FULLY set up their Arc stack and TUNE it** to exactly what
   they want — install → identity → first agent → connect a source → tune
   memory/policy → deploy → every knob.
2. **The architectural decisions and data flows are captured for others** —
   where things live, what calls what, what passes WHERE/WHEN/TO, and the
   security/modularity reasoning behind each choice — indexed to the
   decision log and anchored in code.

These become **two reader tracks**. They are not two doc sites; they overlay the
existing `mkdocs.yml` nav (see §2).

## 1. Survey: what already exists (this is not a blank slate)

The `docs/` tree is **mature** — ~36,500 lines across a 5-part set
(Walkthrough / Building / Reference / Runbooks / Blueprints), 20 per-package
pages, and three rationale-seed concept docs. **Most authoring is REVISE/LINK,
not NEW.** The real gaps are (a) a *cohesive setup+tuning journey* — the
material exists but is reference-shaped and scattered — and (b) a
*decision-indexed rationale layer* — the concepts exist but are not mapped to
`D-NNN` with code anchors, and the README deliberately keeps ADRs out of the
published set (`docs/README.md:161`). Josh now wants that rationale surfaced.

**Rationale seeds already strong (REUSE as the spine of Track 2):**

- `docs/concepts/seam-model.md` — the "everything is a plugin" doctrine, the
  three seam shapes, the five invariants. Excellent; reuse whole.
- `docs/concepts/memory-index-and-scope.md` — dual-layer memory, scope
  isolation, the `vec0→chunks` join, failure modes. Excellent.
- `docs/concepts/fleet-layering.md` — arcteam→arcagent direction, AgentMail
  durability, zero-trust fleet rules. Excellent.
- `docs/walkthrough/data-flows.md` — already carries ~15 mermaid diagrams for
  turn/tool/memory/team/storage/gateway flows. The raw material for Track 2's
  catalog pages; needs decision + anchor annotation, not redrawing.

**Staleness map (from a page-by-page skim, cross-checked against `git log` and
code).** Verdicts drive the REUSE/REVISE tags in §3.

| Page | Verdict |
|---|---|
| `walkthrough/01`–`06`, `10`, `11`, `12` | CURRENT — reuse |
| `walkthrough/07-memory-lifecycle.md` | STALE — omits `ARC_MEMORY_INDEX_BACKEND=postgres` pgvector backend |
| `walkthrough/08-data-and-storage.md` | STALE — omits the optional second memory-Postgres DB (5433); two-DB-per-box topology |
| `walkthrough/09-workflows.md` | THIN — arcteam workflow-node engine (`deliver_to`, cron/scenario nodes) not here |
| `reference/cli.md` | STALE — entire `arc connector` group missing; `arc agent config --sync` missing |
| `reference/config.md` | THIN — a "recently shipped knobs" delta, not a full catalog |
| `reference/troubleshooting.md` | THIN — zero connector/connected-data/sync coverage |
| `building/quickstart.md` | THIN — no connect-a-source, no `arc up`, no external-Postgres step |
| `building/setup.md` | STALE — omits mandatory ArcStore PostgreSQL, `arc up`, connected-data |
| `runbooks/deploy/overview.md` | STALE — lists Postgres as future "M3"; never documents `ARCSTORE_DATABASE_URL` |
| `runbooks/deploy/azure.md` | STALE — no ArcStore Postgres/Supabase provisioning; a fresh box comes up with no operational DB |
| `runbooks/operate/connections.md` | STALE(mostly excellent) — provider matrix omits the shipped Slack connector (SPEC-075) |
| `runbooks/operate/agent-features.md` | STALE — pre-implementation planning memo; features it frames as future have shipped |
| `building/packages/arcstore.md` | STALE — still frames the `last_synced_at` counter fix as future work; `arcui/routes/connected_data.py:110` already reads the field |

**Missing as a cohesive journey (the Track-1 gap list):**

1. End-to-end "connect a source" walkthrough (grant → select → map+approve →
   sync → verify). Worst sub-gap: the **Slack connector is undocumented anywhere.**
2. Every config knob in one place (no catalog spans arcagent/arcllm/arcrun +
   all module `[…]` blocks).
3. Knowledge/memory tuning as one guide (consolidation, `curate_*`,
   `working_set_*`, `doc_chunk_*`, pgvector opt-in).
4. Mandatory external PostgreSQL as a first-class deploy prerequisite; the
   two-DB-per-box topology has no overview.
5. `arc connector` CLI reference.
6. Connector/sync operational health & troubleshooting (`last_synced_at`
   "0/Never", index health, reindex/pause/resume/revoke).
7. A single "stand up + tune a stack" spine linking all of the above.

## 2. How the two tracks map to the existing nav

The tracks are **reader paths**, realized by (a) two new landing pages that
sequence existing + new pages, and (b) filling the gaps. Concretely:

- **Track 1 — "Get set up & tuned"** becomes a new top-nav section
  **`Get Started`** (a guided spine). It reuses `building/*`, `runbooks/deploy/*`,
  `runbooks/operate/*`, `reference/{config,tiers,cli}` — revised and sequenced —
  and adds the 7 missing-journey pages.
- **Track 2 — "How Arc works & why"** is the existing **`Walkthrough` +
  `Concepts`**, elevated: each data-flow page gains a **decision + anchor**
  footer, and a new **`Decision Index`** page (Track 2's keystone) maps every
  major seam to its `D-NNN` set. The `Reference/Security` and per-package pages
  absorb the currently-undocumented rationale items (§6).

Do **not** fork the mkdocs site. Add two landing pages and re-sequence nav.

---

## 3. Section A — Full information architecture (both tracks)

Legend for the **Do** column: **NEW** = author from scratch · **REVISE** =
substantial edit of an existing page · **REUSE** = keep, add cross-links/footer
only. Size in estimated final words. "Prereq" = pages a reader should have read
first. "Source" = existing doc and/or code anchor (`path:line`).

### Track 1 — Get set up & tuned  (17 pages)

| # | Page (title) | Purpose (one line) | Prereq | Do | Source material | Size | Media |
|---|---|---|---|---|---|---|---|
| T1.0 | **Stand up your stack** (landing) | The whole setup journey on one map; where each step lives | — | NEW | this plan §1 gap list; `docs/README.md` | 700 | mermaid (journey map) + nanobanana hero |
| T1.1 | Install & the two homes | `pip install arcmas` vs single-layer; `~/.arc` install vs `~/arc` operator | T1.0 | REVISE | `building/setup.md`; `arctrust/paths.py:107,154,163,179,193`; `install.py:201` | 1400 | mermaid (home split) + canvas (build-your-stack picker) |
| T1.2 | Identity & keys | Operator key, agent DID, non-exportable custody | T1.1 | REVISE | `reference/security.md`; `arctrust/operator.py:90,120`; `identity` cmd `arccli/commands/identity.py` | 1100 | mermaid (key custody) |
| T1.3 | Provision the operational store | External PostgreSQL is required; two-DB topology (arcstore 5432 + optional arcmemory pgvector 5433) | T1.1 | NEW | `runbooks/deploy/arcstore-postgres.md`; `ARCSTORE_DATABASE_URL`; `walkthrough/08` | 1500 | mermaid (two-DB topology) |
| T1.4 | Configure providers | `arcllm.toml`, provider override, endpoint pools/load-balancing | T1.1 | REVISE | `walkthrough/04`; `config_loading.py:80,87`; D-232–240, D-449–458 | 1600 | mermaid (router+pool) |
| T1.5 | Your first agent | `arc agent build` → `arc agent chat`; what a workspace is | T1.2 | REVISE | `building/quickstart.md`; `commands/agent/build.py:25`; `commands/agent/chat.py:265` | 1200 | mermaid (agent workspace tree) |
| T1.6 | The config surface (catalog) | Every knob, all three files + module blocks, in one place | T1.5 | NEW | `reference/config.md` (delta today); `core/config.py:688,114,259,421`; `config_loading.py:69` | 2600 | canvas (three-file merge explorer) |
| T1.7 | Connect a data source (end-to-end) | grant → select → map+approve → sync → verify retrieval, one provider all the way | T1.5, T1.3 | NEW | `runbooks/operate/connections.md`; `connected_data/coordinator.py:38,54`; `connected_data.py:212,284,358,881`; `extension/source.py:160` | 2400 | mermaid (connection lifecycle) + canvas (knowledge-scope demo) |
| T1.8 | Source cookbook (per-provider) | The specific grant/auth for postgres, dropbox, s3, gmail, slack, confluence, m365 | T1.7 | NEW | `extensions/*/arc_ext_*/__init__.py` (e.g. postgresql `:47,163,210`); `connector authorize` `connector.py:248` | 2200 | table + small per-source mermaid |
| T1.9 | Tune knowledge & memory | Consolidation cadence, `curate_*`, `working_set_*`, `doc_chunk_*`, pgvector opt-in, D-726 gate | T1.7 | NEW | `concepts/memory-index-and-scope.md`; `arcmemory/consolidate.py:216`; `retrieve.py:95`; `turn_context.py:96` | 2000 | mermaid (dual-speed cadence) |
| T1.10 | Policy & tiers | personal→federal dial; approvals; scenario grants for automation | T1.5 | REVISE | `reference/tiers-and-presets.md`; `runbooks/operate/policy-and-proactive.md`; `arctrust/policy.py:124,569`; `tiers.py` | 1800 | canvas (tier dial) + mermaid (policy layers) |
| T1.11 | The ArcUI dashboard tour | observe / interact / manage planes; what's live vs on-demand | T1.5 | REVISE | `walkthrough/data-flows.md` (dashboard flow); `arcui`; `commands/ui.py:229` | 1300 | nanobanana (annotated dashboard hero) |
| T1.12 | Build a fleet | `arc team register`; agent mail; shared knowledge | T1.5 | REVISE | `runbooks/operate/teams.md`; `concepts/fleet-layering.md`; `commands/team.py:389` | 1600 | mermaid (fleet topology) |
| T1.13 | Workflows & schedules | ArcFlow DAGs, cron triggers, `deliver_to`, nightly ingest pattern | T1.5 | REVISE | `walkthrough/09`; `runbooks/operate/tasks.md`; SPEC-056/061; D-507–509 | 1700 | mermaid (workflow DAG) |
| T1.14 | Gateways (chat) | Connect Telegram/Slack; 1 bot per agent; token custody | T1.5 | REVISE | `building/packages/arcgateway*.md`; `runbooks`; D-668–682 | 1400 | mermaid (gateway inbound) |
| T1.15 | Deploy | `arc up` local; `deploy-vm.sh dgx\|azure`; docker; runtime activate/rollback | T1.3 | REVISE | `runbooks/deploy/{up,azure,docker,overview}.md`; `scripts/deploy-vm.sh`; `deploy-node.sh:193`; `runtime.py:82` | 1900 | mermaid (deploy + runtime flip) |
| T1.16 | Operate & troubleshoot | Connector health (`last_synced_at`), index health, reindex/pause/resume/revoke, keyring hangs | T1.7 | NEW | `reference/troubleshooting.md` (silent on this); `connected_data.py:821,850,570`; memory: connector-counters, keyring-hang | 1800 | table (symptom → cause → command) |

**Track 1 total: 17 pages, ~30,600 words. New: 6 · Revise: 10 · Reuse-as-is: 1 (landing links).**

### Track 2 — How Arc works & why  (18 pages)

Each **flow page** carries a standard footer: **Where it lives · What calls what
· What passes (where/when/to) · Security reason · D-NNN · Code anchor** — the
same six fields as the catalog (§5), so the catalog is the index and each page
is the expansion.

| # | Page (title) | Purpose (one line) | Prereq | Do | Source material | Size | Media |
|---|---|---|---|---|---|---|---|
| T2.0 | **How Arc works** (landing) | The mental model in one breath; map of the flows | — | REVISE | `walkthrough/01`, `02`; `README.md` | 900 | mermaid (layer stack) + nanobanana hero |
| T2.1 | The seam model | Everything is a plugin; three shapes; five invariants | T2.0 | REUSE | `concepts/seam-model.md` | (exists) | mermaid (seam boundary — exists) + canvas (seam/dependency explorer) |
| T2.2 | Package layering & dependency direction | One-way graph; why `arcagent` never imports `arcllm` | T2.1 | REVISE | `walkthrough/02`; `data-flows.md` layer diagram; D-620,621,626,632,635 | 1500 | mermaid (dependency graph) |
| T2.3 | The Four Pillars | Identity/Sign/Authorize/Audit ride every seam at every tier | T2.1 | REVISE | `walkthrough/10`; `reference/security.md`; ADR-019; verify-before-load D-474,636 | 2000 | mermaid (pillars-per-seam) + nanobanana hero |
| T2.4 | **Decision Index** (keystone) | Every seam → its governing `D-NNN` + ADRs; the rationale map | T2.1 | NEW | this plan §5–§6; `.claude/decisions-log.md`; `.claude/architecture/decisions/` | 2200 | table (linked index) |
| T2.5 | Anatomy of a run/turn | One request end-to-end; run_id pinning; tool-set freeze | T2.2 | REVISE | `walkthrough/03`; `agent.py:739`; `agent_dispatch.py:296,42`; `arcrun/loop.py:67,82`; `strategies/react.py:237` | 2000 | mermaid (run sequence) |
| T2.6 | A tool call through policy | The ordered guard chain; first-DENY-wins; fail-closed | T2.5, T2.3 | REVISE | `data-flows.md` tool pipeline; `tool_registry.py:646,512,537`; `policy.py:1186,1221` | 1900 | mermaid (7-layer pipeline) + canvas (policy simulator) |
| T2.7 | Audit emission & the WORM chain | `emit` → sinks; hash-chain fields; single-writer; fail-open | T2.3 | NEW | `data-flows.md` WORM §; `arctrust/audit.py:518,174,291,316`; `arcui/audit.py:235` | 1700 | mermaid (hash-chain) |
| T2.8 | Connected-source sync → knowledge | The full lifecycle; the `SourceAdapter` contract; doc-scope isolation | T2.5 | NEW | `connected_data.py:212,284,358`; `doc_index.py:36,153`; `extension/source.py:160`; D-683–691 | 2300 | mermaid (lifecycle) |
| T2.9 | Memory capture → recall | Dual layer; four-way fuse; the `vec0→chunks` scope join | T2.5 | REUSE | `concepts/memory-index-and-scope.md`; `capture.py:64`; `retrieve.py:95`; `index/backend.py:266` | (exists) | mermaid (two-layer + join — exists) |
| T2.10 | Inter-agent message | AgentMail envelope → sign → durable outbox → inbox copy | T2.5 | REVISE | `concepts/fleet-layering.md`; `arcteam/mail.py:213,342,251`; `arcstore/mail_outbox.py:200` | 1700 | mermaid (mail + outbox lease) |
| T2.11 | An approval | Draft/sign split; pending row → `arc approve`; scenario-grant `origin` | T2.3 | NEW | `arcstore/approvals.py:33,134,166`; `commands/approve.py:162`; `policy.py:124,569,624`; D-523,525,651 | 1600 | mermaid (draft→sign→grant) |
| T2.12 | LLM routing & load-balancing | Router placement; provider override; endpoint pools (undocumented today) | T2.2 | NEW | `arcllm`; D-190,197,232–240,449–458; ADR-035 | 1900 | mermaid (router + endpoint pool) |
| T2.13 | Prompt assembly | Stock + operator overlay → snapshot → system prompt + tool catalog | T2.5 | REVISE | `walkthrough/06`; `arcprompt/resolver.py:60`; `snapshot.py:28`; `session_internal/context.py:337` | 1400 | mermaid (assembly) |
| T2.14 | Config load & tier resolution + arc-home | Three-file merge; tier floor; the single path resolver | T2.2 | REVISE | `walkthrough/08,12`; `config_loading.py:80`; `config.py:563`; `paths.py:154`; D-489,471,579 | 1700 | mermaid (merge + home lifecycle) |
| T2.15 | Gateway inbound & media trust | Envelope → LLM content; who owns the session; untrusted media | T2.6 | REVISE | `walkthrough/11`; D-668–682 (media trust D-672/674/675 undocumented) | 1500 | mermaid (inbound path) |
| T2.16 | Workflows/tasks/scheduler substrate | Task DAG substrate; run progression; monotonic-progress rule | T2.5 | REVISE | `walkthrough/09`; SPEC-056/061; D-507–509,630,119–129 | 1700 | mermaid (task state machine) |
| T2.17 | Sandbox, dynamic tools & RCE | Isolated JSON exec seam; browser backend; deny-by-default | T2.6 | REVISE | `data-flows.md` dynamic-tool §; D-659,667,608,145–149,175–176; ADR-017C | 1600 | mermaid (exec seam) |

**Track 2 total: 18 pages, ~29,900 words (2 reuse-as-is bring their own).
New: 5 · Revise: 11 · Reuse: 2.**

**Program total: 35 pages, ~60,500 words. NEW: 11 · REVISE: 21 · REUSE: 3.**

---

## 4. Section B — Media assignment (don't over-assign)

Rule of thumb applied above: **mermaid** is the default for any structural,
sequence, or state relationship (it is already the house style and renders
natively via `pymdownx.superfences`). **HTML canvas** is reserved for the few
places where *manipulating* the model teaches something a static picture can't.
**nanobanana** (Gemini 2.5 Flash Image, `GEMINI_API_KEY` in env) is reserved for
**hero/section openers** where a diagram would be dry — at most one per landing
page and a couple of concept openers. Most pages get mermaid only.

### Top 10 mermaid diagrams (priority order — these carry the program)

1. **Layer stack + one-way dependency graph** (T2.2, T2.0) — REVISE from
   `data-flows.md:11`. The single most-reused picture.
2. **Seam boundary** — core knows ports, not parts (T2.1) — REUSE
   `concepts/seam-model.md:31`.
3. **Connection lifecycle** — grant → select → map+approve → sync → reindex →
   retrieve (T1.7, T2.8) — NEW. The biggest missing diagram; anchors
   `connected_data.py:284,358,821`.
4. **Run/turn sequence** — entry → dispatch → `run_stream` → `react_loop`,
   run_id pinned, registry frozen (T2.5) — REVISE `data-flows.md:278`.
5. **Tool-call policy pipeline** — schema → 7 policy layers (first-DENY-wins) →
   pre_tool → execute → post_tool → audit (T2.6) — REVISE `data-flows.md:342`;
   layers `policy.py:695–1085`.
6. **Memory dual-speed + `vec0→chunks` scope join** (T2.9, T1.9) — REUSE
   `concepts/memory-index-and-scope.md:31,112`.
7. **Fleet topology + AgentMail durable outbox** (T2.10, T1.12) — REVISE
   `data-flows.md:184`; add the outbox lease/DLQ (`mail_outbox.py:200`).
8. **WORM audit hash-chain** — `seq / prev_hash / event_hash / signature`,
   rotation, flock (T2.7) — NEW; `audit.py:291,316`.
9. **LLM router + endpoint-pool load-balancing** (T2.12, T1.4) — NEW
   (undocumented today); D-449–458.
10. **arc-home lifecycle split** — install `~/.arc` vs operator `~/arc`, runtime
    flip (T2.14, T1.1, T1.15) — REUSE `data-flows.md:636`.

(Runners-up already drawn and reusable: approval draft→sign→grant, dual-speed
cadence timeline, gateway inbound, task state machine, capability-loading gates.)

### HTML canvas candidates (interactive teaching — 6, build only if T1/T2 core ships on time)

1. **Seam / dependency explorer** (T2.1) — click a package; legal imports light
   up, illegal ones (upward/past-neighbour) show red; a "delete this part"
   toggle greys the capability and proves the rest still runs. Teaches the
   one-way rule and "survive its own absence" better than any static arrow.
2. **Build-your-stack picker** (T1.1) — toggle `arcllm` / `arcrun` / `arcmas` +
   modules; output the resulting capability set, the `pip` line, and the config
   files created. Turns the install decision into a manipulable choice.
3. **Policy-pipeline simulator** (T2.6) — set tool, tier, classification,
   `origin`; step the 7 layers; watch first-DENY-wins short-circuit. Makes
   fail-closed and scenario-grant matching tangible.
4. **Three-file config-merge explorer** (T1.6) — edit packaged / user-wide /
   per-agent overlay; see the deep-merged effective value per knob. Directly
   teaches `config_loading.py:69` precedence.
5. **Knowledge-scope demo** (T1.7) — one `index.db`, several scopes; run a
   scoped search; visualize the `vec0→chunks` join filtering out cross-agent
   chunks. The LLM08 isolation story, made clickable.
6. **Tier dial** (T1.10) — slide personal→enterprise→federal; the changed knobs,
   crypto floor, and active policy layers update live. Teaches "tier is a dial,
   not a rewrite."

Priority if trimming: **1, 3, 5** (they teach the three hardest invariants:
one-way seams, fail-closed policy, scope isolation). 2/4/6 are nice-to-have.

### nanobanana hero/illustration candidates (5 — one-line prompts)

Keep to openers; a diagram does the teaching, the hero sets the frame.

1. **Track-1 hero (T1.0)** — "A single laptop with clean glowing conduits
   branching into a small fleet of identical agent servers, security-blue and
   graphite, isometric, calm, no text."
2. **Track-2 / seam hero (T2.0 or T2.1)** — "A small solid core cube with many
   labeled ports around its faces, interchangeable modules hovering just
   detached, showing plug-and-unplug, blueprint aesthetic, blue/white."
3. **Four Pillars hero (T2.3)** — "Four engraved stone pillars — Identity, Sign,
   Authorize, Audit — each carrying the same thin conduit that runs through
   every arch, unified, federal-serious, monochrome-with-blue-accent."
4. **The two homes (T1.1)** — "Two labeled vaults side by side: a disposable
   'install' crate being swapped out, and a permanent 'operator' safe that stays
   bolted down; clear contrast, warm vs cool, no text."
5. **ArcUI dashboard hero (T1.11)** — "A clean control-plane dashboard glowing on
   a dark desk, three faint panes labeled observe / interact / manage; graphite
   and emerald palette matching Arc UI." (Pair with a real annotated screenshot
   in-body.)

---

## 5. Section C — The data-flow / decision catalog (the spine of Track 2)

One row per major flow. **This table is the source of truth for T2.4 (Decision
Index) and the footer of every Track-2 flow page.** Anchors are absolute under
`/Users/joshschultz/Projects/arc`. "Passes (where/when/to)" is the load-bearing
column Josh asked for.

| Flow | Where it lives | What calls what | What passes — where / when / to | Security / modularity reason | D-NNN / ADR | Code anchor (entry) |
|---|---|---|---|---|---|---|
| **Run / turn** | arcagent core → arcrun | `ArcAgent.run` → `dispatch_stream` → `build_run_context` → `arcrun.run_stream` → `run` → `react_loop` → `model.invoke(_stream)` | `run_id` minted once and **pinned** through the whole run; tool-set **frozen** at loop start; per-turn `messages`+tool schemas → model; `StreamEvent`s (Token/TurnEnd) back to the surface | Frozen tool-set = no mid-run privilege change (ADR-027); one run_id = honest trace correlation | D-591,587,589,602,618,623; ADR-024,027 | `agent.py:739`; `agent_dispatch.py:296`; `arcrun/loop.py:67,82`; `strategies/react.py:237` |
| **Tool call → policy** | arcagent `tool_registry` → arctrust `policy` | `wrapped_execute` → `_validate_tool_args` → build+`sign_call` `ToolCall` → `PolicyPipeline.evaluate` (7 layers) → `agent:pre_tool` → `tool.execute` → `agent:post_tool` → audit | signed `ToolCall`(name,args,agent_did,session,classification,origin) → each layer **in order**; first `DENY` short-circuits before the handler; `tool_result` → caller; `tool.executed` → WORM | First-DENY-wins + fail-closed on exception = least privilege, no confused-deputy | D-294,662,608,607; ADR-017A | `tool_registry.py:646,512,537`; `policy.py:1186,1221,695–1085` |
| **Audit emission (WORM)** | arctrust `audit` (+ arcui) | `emit(event, sink)` → `WormSink._append` (flock → hash → sign → write) → `_maybe_rotate`; arcui via `MutationWormWriter` | `AuditEvent` → sink at every op; each record chains `event_hash = H(seq+prev_hash+event)` + Ed25519/ECDSA sig; rotated at 100k/50MB | Tamper-evident, single-writer, **fail-open** (never breaks the audited op) | D-203,047,437; ADR-022 | `audit.py:518,174,291,316`; `arcui/audit.py:235` |
| **Connected-source sync → knowledge** | arcagent `connected_data` module → arcmemory `ConnectedDataService` → extension `SourceAdapter` | `ConnectedDataCoordinator.run` → adapter `inspect/list/select/sync/fetch` → `require_approved_mapping` → `ingest` → `_write_and_index_document` → `DocIndex.document_search` | source objects → per-source **doc scope** `<did>:doc:<source_id>`; sync only after **exact mapping approved**; provenance stamped; retrieval scoped | Per-source scope = LLM08 cross-tenant isolation; mapping approval = no silent field mapping | D-683,686,691,688,684; SPEC-073 | `connected_data/coordinator.py:38,351`; `connected_data.py:212,284,358,881`; `doc_index.py:36,153`; `extension/source.py:160` |
| **Memory capture → recall** | arcmemory (via arcagent Brain seam) | `FastCapture.capture` → episodic; `Consolidator.run` (distill/dedup, signed) → stores; `Retriever.retrieve` → `SurfaceIndex.search` (vec+bm25+graph+recency) → `rrf_fuse` → gate | turn text → episodic (every turn); recall query → one bounded fuse; **`vec0` JOIN `chunks` WHERE scope=?**; boundary-marked DATA → prompt | Markdown = glass-box truth, index disposable; scope join = no cross-agent leak; recalled text is inert DATA (LLM01) | D-726,411,225/226,334; concepts doc | `capture.py:64`; `consolidate.py:216`; `retrieve.py:95`; `index/backend.py:266`; `types.py:80` |
| **Inter-agent message** | arcteam `mail` → arcstore outbox | `AgentMailService.send` → `_sign_envelope` → `record_event_with_outbox` → `MailDeliveryWorker.deliver_once` → NATS/Memory backend | signed envelope + `conversation_id` → atomic inbox+outbox commit; leased `SKIP LOCKED`; `sent` only after transport ack, else `pending`; per-participant copy | Mail body is untrusted data, not control-plane; durable store authoritative, mail is a wakeup; scoped DIDs | D-538,539,510,647; ADR-007; fleet-layering | `mail.py:213,342,251,114`; `mail_outbox.py:200`; `messenger.py:464` |
| **An approval** | arcstore `approvals` → arccli → arctrust | `ApprovalStore.create` (pending row) → `arc approve` `_resolve` → `OperatorKey` signs → `resolve` sets `grant`; scenario: `sign_scenario_grant`/`verify_scenario_grant` | `PendingApproval`(call_hash) → durable row; operator signature → `grant`; scenario grant keyed by `scenario_key` incl. **`origin`** (`workflow:`/`schedule:`; `None`=interactive matches nothing) | Draft/sign split = agent never holds operator key; origin key = a standing grant can't be replayed by an interactive turn | D-523,525,557,111,651; ADR-034 | `approvals.py:33,134,166`; `approve.py:162`; `policy.py:124,569,624` |
| **LLM routing / load-balance** | arcllm | router selects provider by required features → provider adapter (override URL/auth/model-map) → endpoint pool (weighted RR, health circuit) | request → classification/feature check → provider; within provider, spread across N `[[endpoints]]`; per-endpoint vault key; per-call `load_balance=True` | Routing policy stays inside arcllm (ADR-035); intra-provider only = no cross-provider data spread | D-190,197,232–240,449–458; ADR-035,025 | arcllm router/pool modules (see D-458, D-453) |
| **Prompt assembly** | arcprompt → arcagent context | `PromptResolver.resolve` (stock + operator overlay) → `PromptSnapshot` → `ContextManager.assemble_system_prompt` (+ `agent:assemble_prompt` bus, tiered `extra_sections`) → `ToolRegistry.format_for_prompt` | stock docs + signed overlay → frozen snapshot per run; core files + injected sections + tool catalog → system prompt → model | Prompts are signed protected artifacts (not mutable text); overlay pinned to operator key; snapshot = reproducible | D-459–463,073–080; ADR-006 | `resolver.py:60`; `snapshot.py:28`; `context.py:337`; `tool_registry.py:202` |
| **Config load + tier resolution** | arcagent `config` + arctrust `paths` | `load_config` → `compose_raw_config` (`sibling_chain` packaged→user→overlay, `deep_merge`, env overrides) → Pydantic validate → `_enforce_tier_crypto_floor` | three TOML layers → one `ArcAgentConfig`; `[security].tier` → crypto floor + active policy set; paths via one named accessor each | Single resolver per home path (one source of truth); tier floor refuses below, cannot raise; secure-by-default | D-489,310,471,579; ADR-003,017D,029 | `config.py:740,563`; `config_loading.py:80,69`; `paths.py:154,475` |
| **Spool telemetry write** | arcstore `spool` | run/tool/llm code → `record(rec, path)` with `request_context(run_id)` | `SpoolRecord`(kind, `request_id`) → daily `operational-YYYY-MM-DD.jsonl`, one `os.write`; request_id via contextvar | Always-on, fail-open, single atomic append; request_id = concurrent-run correlation | D-203 (telemetry); ADR-022 | `spool.py:83,46,62`; `records.py:17,20`; `loop.py:385` |
| **Gateway inbound → agent** | arcgateway (+ adapter extensions) | adapter normalizes platform update → `InboundEvent` → `SessionRouter.handle` → `Executor.run` → `ArcAgent.run` | platform message + media → gateway session identity; inbound body injected only as user content (never control) | Gateway owns session boundary; inbound is untrusted (LLM01); media trust/retention decisions | D-668,670,677,678,679,325; ADR-020 | adapters `arcgateway/adapters/<name>/`; `data-flows.md:335` |
| **Workflow/schedule fire → task** | arcteam/arcagent workflow + scheduler | `[trigger]` cron → owner-scoped schedule → node materialization on task-DAG substrate → run progression | schedule fire → task write (durable) → agent run; `deliver_to` pins a node's notify to a channel (signed) | Handoff is a task write not a message (D-538); monotonic-progress rule; deliver_to model-immutable | D-507–509,630,119–129,335 | SPEC-056/061; `walkthrough/09` |
| **Dynamic tool / sandbox exec** | arcagent tools → arcrun/isolation | agent-authored Python → encoding/AST check → restricted builtins → isolated JSON exec seam → result to LLM; browser via CDP backend | authored code → sandbox (deny-by-default), never `eval`; result → model as DATA | RCE containment (ASI05); isolation only for executable artifacts; allowlist model | D-659,667,608,601,145–149,175–176; ADR-017C | `data-flows.md:472`; browser module (D-145) |

---

## 6. Section E — Gaps / risks: rationale & flows that are UNDOCUMENTED

These have ~0 hits in `docs/` and are load-bearing. Each needs a **subject-matter
deep-read of code** before it can be written — flagged so authoring doesn't stall
on them. Ordered by size of gap.

**The 5 biggest (must reverse-engineer from code before writing):**

1. **LLM load-balancing / endpoint pools (D-449–458, `[[endpoints]]` D-453).**
   `round-robin` = 0 doc hits. Weighted-RR default, health-aware circuit, sticky
   routing, innermost-stack placement, per-endpoint vault key — all undocumented.
   → Owns T2.12 + feeds T1.4. Needs an arcllm SME read.
2. **Provider override / inheritance model (D-232–240).** `provider override` =
   0 hits. How a provider overrides base_url/auth-header/model-name mapping (the
   Azure/OpenAI-compat seam) has no conceptual page. → Feeds T1.4, T2.12.
3. **Verify-before-load ordering invariant (D-474, D-636 "verified before
   materialize, never after").** Signing is described, but the *ordering* — the
   actual heart of the Sign pillar — is never stated. → Belongs in T2.3.
4. **Tool-contract hashing / rug-pull defense (D-563).** `rug-pull` = 0,
   `contract hash` = 0. A named security invariant with no footprint. → Belongs
   in T2.6 + `reference/security.md`.
5. **Connected-source `document_search` *tool-registration* site + gateway
   inbound-media trust/retention (D-672/674/675/681/682).** Two open anchors:
   (a) the code-anchor agent could not locate where `document_search` is exposed
   to the LLM as a tool (the callable exists at `doc_index.py:153` /
   `connected_data.py:881`, but the ToolSpec registration was not found); (b)
   media-bytes storage location, untrusted-media posture, and retention are
   undocumented. → Blocks part of T2.8 and T2.15; needs an SME to trace the tool
   wiring first.

**Also undocumented (smaller, can be written by REVISE-ing an existing page):**

6. Entity dedup / wiki-link resolution in memory (D-081/082, D-100, D-105) —
   `concepts/memory-index-and-scope.md` covers scope, not canonicalization.
7. Multi-spawn scheduling reservation (D-656–658) — fairness/reservation of
   parallel child dispatch (distinct from the documented D-623 mechanism).
8. arcllm queue backpressure / concurrency contract (D-280–286) — no conceptual
   doc; only `building/performance.md` touches load generically.

**Correctness risks to fix while authoring (from the survey):**

- Do **not** cite `JsonlSink`, `SignedChainSink`, or `UIBridgeSink` as real
  classes — they appear only as prose contrasts in `audit.py:11`. The real sinks
  are `NullSink` and `WormSink`; arcui durable capture is `MutationWormWriter`
  (`arcui/audit.py:235`), ephemeral logging is `UIAuditLogger` (`:187`).
  `docs/concepts` and the project CLAUDE.md still name `UIBridgeSink` — the
  Track-2 audit page should quietly correct this, not propagate it.
- `building/packages/arcstore.md:412` frames the `last_synced_at` fix as future
  work though `arcui/routes/connected_data.py:110` already reads it — update the
  package doc while writing T1.16.
- `walkthrough/02` still labels ADR-022 storage-split "Proposed"; it has shipped.

---

## 7. Section D — Build sequence & parallelization

The dependency backbone is: **rationale (Track 2 keystones) before the flow
pages that footer-reference them; the setup spine (Track 1) can proceed in
parallel** because it mostly REVISEs existing pages. Media comes after prose is
frozen, because diagrams encode the final anchors.

### Wave 0 — Foundations (serial-ish, 2 keystones gate the rest)

- **T2.4 Decision Index** (NEW) and **T2.2 layering** (REVISE) first — they set
  the `D-NNN` vocabulary every other Track-2 footer cites.
- Confirm the §6 correctness fixes (audit sink names, arcstore counter) so no
  page inherits a wrong claim.
- **Deliverable that unblocks everyone:** the §5 catalog table, published as
  T2.4, is the shared reference.

### Wave 1 — Parallel authoring (independent; fan out)

Two independent lanes, no cross-dependencies within a lane:

- **Lane A — Track 1 setup spine (REVISE-heavy, quick writes):** T1.1, T1.2,
  T1.4, T1.5, T1.10, T1.11, T1.12, T1.13, T1.14, T1.15. These are edits of
  existing pages; a general/technical-writer agent can take several at once.
- **Lane B — Track 2 flow pages (REVISE + footer):** T2.5, T2.6, T2.7, T2.10,
  T2.13, T2.14, T2.15, T2.16, T2.17. Each is an existing walkthrough/data-flows
  page + the standard six-field footer from §5.
- **Reuse-as-is (no authoring, add cross-links only):** T2.1, T2.9, T2.3-base.

### Wave 2 — The NEW deep pages (need a subject-matter deep-read first)

Flag: **do not quick-write these.** Assign an SME agent to trace code first.

- **T1.3** (two-DB Postgres provisioning), **T1.6** (full config catalog),
  **T1.7 + T1.8** (connect-a-source + per-provider cookbook), **T1.9** (memory
  tuning), **T1.16** (connector troubleshooting) — Track-1 gaps.
- **T2.8** (connected-source), **T2.11** (approvals), **T2.12** (LLM
  routing/LB — §6 items 1–2). T2.12 and the T2.8 tool-registration open anchor
  are the two that may need a code fix/trace before they can be finished.

### Wave 3 — Media pass (after prose freeze)

Order: (1) the **top-10 mermaid** — items 1–2, 6, 10 are REUSE and land
immediately; 3, 5, 8, 9 are NEW and follow their prose pages. (2) **nanobanana
heroes** for the 5 landing/opener pages. (3) **canvas** builds — priority 1, 3,
5; then 2, 4, 6 if schedule holds. Canvas pieces are self-contained HTML embeds;
they can be built by a frontend agent in parallel with the mermaid pass.

### Wave 4 — Integration

- Re-sequence `mkdocs.yml` nav for the two tracks (§2); add the two landing
  pages to nav.
- `uv run mkdocs build --strict` (the link/anchor gate) must pass — it is the
  merge gate for every doc PR.
- Cross-link Track 1 ⇄ Track 2 at each seam (a setup page links to its "why"
  page and back).

### Effort shape

- **Quick writes (REVISE + footer):** ~19 pages — parallelizable, low risk.
- **Deep writes (NEW, SME read first):** ~11 pages — the schedule's critical
  path; T2.12, T1.7/T2.8 are the tallest poles.
- **Reuse-as-is:** 3 pages.
- **Media:** ~10 mermaid (4 reuse), 6 canvas, 5 heroes.

---

## 8. Appendix — verified code-anchor index (for authors)

Handed to page authors so diagrams and footers cite real symbols. All under
`/Users/joshschultz/Projects/arc`.

**Run / turn:** `agent.py:739` (`ArcAgent.run`) · `agent_dispatch.py:296`
(`dispatch_stream`), `:42` (`build_run_context`) · `arcrun/streams.py:175,274`
(`run_stream`, run_id) · `arcrun/loop.py:67` (mint), `:82` (`registry.freeze`),
`:385` (`request_context`) · `strategies/react.py:237` (`react_loop`), `:460`
(`_stream_model_call`) · `streams.py:57,92` (Token/TurnEnd events).

**Tool → policy:** `tool_registry.py:646` (`wrapped_execute`), `:467`
(validate), `:512/520` (`ToolCall`/`sign_call`), `:537` (evaluate call), `:574`
(pre_tool), `:583` (execute), `:608` (post_tool) · `policy.py:1186` (`evaluate`),
`:1221` (short-circuit), layers `:695,749,823,890,981,1010,1085` ·
`arcrun/executor.py:45` (`execute_tool_call`).

**Audit:** `audit.py:518` (`emit`), `:174` (`WormSink`), `:249` (flock), `:291`
(`_append`), `:316` (`_maybe_rotate`), `:333` (`_restore_tip`), `:436`
(`verify_chain`) · `arcui/audit.py:235` (`MutationWormWriter`), `:187`
(`UIAuditLogger`). **No `JsonlSink`/`SignedChainSink`/`UIBridgeSink` classes.**

**Prompt:** `arcprompt/catalog.py:85` · `resolver.py:60` · `snapshot.py:28,64` ·
`prompt_context.py:66` · `session_internal/context.py:337,393` ·
`tool_registry.py:202` (`format_for_prompt`) · `arcrun/registry.py:70`.

**Spool:** `arcstore/records.py:17,20` · `spool.py:46,62,80,83`.

**Memory:** `capture.py:40,64` · `consolidate.py:125,216,359` ·
`agent_consolidate.py:49` · `distill.py:182` · `index/backend.py:46,163,266`
(scope join) · `index/rebuild.py:138,161` · `index/surface.py:98,126,238` ·
`retrieve.py:41,95,200,266` · `types.py:80,94` · brain: `AA/brain/protocol.py:24`
(`Brain`), `:144` (`NullBrain`), `select.py:57` · `AM/brain.py:124` ·
`turn_context.py:96` (`interactive`, D-726).

**Connected sources:** `connected_data.py:212` (`ConnectedDataService`), `:284`
(`propose_mapping`), `:297` (`require_approved_mapping`), `:358` (`ingest`),
`:821,850` (reindex), `:570` (purge), `:881` (`document_search`) ·
`doc_index.py:36` (`doc_scope`), `:66,153` (`DocIndex`) · `extension/source.py:160`
(`SourceAdapter`, 5 methods) · `connected_data/coordinator.py:38,54,351` ·
`extension/grants.py:95,154` (`ConnectionRegistry.granted_to`) · reference impl
`extensions/postgresql/arc_ext_postgresql/__init__.py:47,163,210`. **Open
anchor:** the `document_search` *tool*-registration site was not located.

**Inter-agent mail:** `arcteam/mail.py:188,213,342,251,114` · `storage.py:38,164`
· `backends/nats.py:128` · `arcstore/mail_outbox.py:30,200` (`PostgresMailOutbox`,
`SKIP LOCKED`) · `messenger.py:326,464` · `composition.py:30`.

**Approvals:** `arcstore/approvals.py:33,112,134,166` · `commands/approve.py:162`
· `arctrust/operator.py:90,109,120` · `policy.py:124` (`ScenarioGrant`), `:569`
(`scenario_key`), `:594` (`sign`), `:624` (`verify`).

**Config / paths / tiers:** `core/config.py:688,740,563,717` ·
`config_loading.py:30,48,69,80,87,93` · `tiers.py` (`resolve_tier_floor`) ·
`paths.py:107,154,163,179,193,475` (`activate_runtime`).

**CLI (setup-critical):** `up.py:810,715,433` · `install.py:201,138,106` ·
`runtime.py:105,82` · `ui.py:720,229` · `approve.py:162` · `agent/build.py:25` ·
`agent/chat.py:265` · `team.py:389` (`register`) · `connector.py:248`
(`authorize`), `:734` dispatcher. Full `arc <group>` list: `registry.py:511`.

**Deploy:** `scripts/deploy-vm.sh` (dgx|azure lane) → `scripts/deploy-node.sh:107,193`
(install runtime + atomic `current` flip) · `scripts/deploy-azure.sh` (separate
Docker/ACR lane).

---

## 9. Report summary (for the team lead)

- **Plan path:** `/Users/joshschultz/Projects/arc/docs/design/DOCS-PROGRAM-PLAN.md`
- **Page counts:** Track 1 = **17** (6 NEW · 10 REVISE · 1 link) · Track 2 =
  **18** (5 NEW · 11 REVISE · 2 REUSE). Program = **35 pages, ~60,500 words**.
- **Top 10 mermaid:** layer/dependency stack · seam boundary · connection
  lifecycle · run/turn sequence · tool-call policy pipeline · memory dual-speed +
  scope join · fleet + AgentMail outbox · WORM hash-chain · LLM router +
  endpoint pool · arc-home lifecycle split. (§4)
- **Canvas candidates (6):** seam/dependency explorer · build-your-stack picker ·
  policy-pipeline simulator · three-file config-merge explorer · knowledge-scope
  demo · tier dial. Priority 1/3/5.
- **5 biggest undocumented rationale/data-flow items:** (1) LLM load-balancing /
  endpoint pools (D-449–458) · (2) provider override/inheritance (D-232–240) ·
  (3) verify-before-load ordering invariant (D-474/636) · (4) tool-contract
  hashing / rug-pull (D-563) · (5) the `document_search` tool-registration site +
  gateway inbound-media trust/retention (D-672/674/675). All need an SME code
  read before writing.
