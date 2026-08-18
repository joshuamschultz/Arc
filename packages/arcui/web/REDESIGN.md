# Arc UI Redesign — "Console" Design System & Build Plan

> Single source of truth for the arcui visual overhaul. Read this before
> touching `web/` so the look stays continuous across screens and sessions.
> Branch: `feat/ui-redesign`.

---

## 0. Prime directive — re-skin, never rewrite

Every current screen, data point, feature, and button **stays**. We change the
*design system and shell*, not the features. The pages, data hooks, routes, and
state already exist; we restyle their presentation. This is how parity is
guaranteed by construction. If a change would remove or bypass a feature, it is
out of scope for the redesign.

**Additive completeness.** Beyond preserving parity, close feature gaps
*between related views*. If a button, action, or data point exists on a
full-app screen but is missing from the matching detail view — e.g. the task
actions on the full **Tasks** board vs. an agent's **Tasks** tab — add it there
too, scoped to that context. Every place a thing is shown should offer the same
buttons and data as its canonical screen. Running gap list: §11.

Verify after every phase: `npm run build` (tsc + vite) must pass, and the
committed static bundle under `../src/arcui/static/` is rebuilt.

---

## 1. Audience, feel & UX principles

**Who this is for:** business and enterprise operators — a manager watching a
fleet of agents work through the day — **not** developers or hobbyists. The UI
must be legible and confidence-inspiring to a non-technical professional.

**UX principles**
- **Plain language over jargon.** Say what a thing does in business terms. A
  person manages *agents*, *approvals*, *activity* — not `tool_event`, `DID`,
  `trifecta`, `arcllm`. Keep raw technical identifiers to detail/hover/mono,
  never as the primary label. Developer-facing telemetry (ArcLLM/ArcRun
  internals) lives lower in the rail and reads as "Activity" / "Model usage",
  with the technical view one click deeper.
- **Guidance-first.** Every empty state teaches the next action. Every gate
  explains *why* in one plain sentence. No dead ends.
- **Trust cues, calmly.** Signed/verified/approved states are visible but quiet
  (the tan seal), so an operator feels the governance without reading a manual.
- **Progressive disclosure.** Summary first; detail on click in a drawer or
  modal. Never a wall of data.
- **Consequential actions are obvious and reversible.** Clear primary action,
  a why, and an undo. Nielsen heuristics + agentic-UX patterns (§6).
- **Accessible by default.** Visible focus, keyboard paths, reduced-motion,
  legible contrast in both themes.

### Design language

**Feel:** a calm, sharp, professional operations console you look at all day.
Flat surfaces, one confident blue, warm-neutral nothing. Not a consumer app,
not a bought admin theme.

**Rules**
- **No decorative gradients.** Solid fills only. (Functional shimmer on loading
  skeletons is allowed.)
- **Blue is the one accent**, used for interactive things: active nav, primary
  buttons, links, focus rings, key numbers, running state.
- **Tan `--signed`** is reserved *only* for signed/verified/audit marks — the
  seal glyph, "signed" chips, hash chips. It never appears as a general accent.
- **Semantic status** (online/warn/error/info) is separate from the accent and
  used sparingly, only where state must read at a glance.
- **Elevation by lightness, not heavy shadow.** sidebar < background < card <
  popover. Dark uses a hairline top-inset highlight + soft ambient drop.
- **Low glare:** text is dimmed paper (not pure white) on a soft ground (not
  pure black).

---

## 2. Tokens (source of truth: `src/index.css`)

All values are OKLCH. Names are shadcn-standard so every existing component
inherits them. **Never hardcode a hex/oklch in a component — use the token.**

### Accent & signed
| Token | Light | Dark |
|---|---|---|
| `--primary` (blue) | `0.500 0.148 252` | `0.662 0.130 252` |
| `--ring` | = primary | = primary |
| `--signed` (tan) | `0.590 0.078 62` | `0.745 0.070 66` |

### Neutrals (hue 255, tiny chroma — clean, not beige, not graphite)
`--background --card --popover --secondary --muted --accent --border --input`
plus `--sidebar*`. Light background `0.985`, dark background `0.186`, dark card
`0.215`, dark popover `0.248`. Text `--foreground` light `0.235` / dark `0.905`.

### Status / severity (muted)
`--status-online|warning|error|info|idle`, `--severity-critical|high|medium|low`.
Registered as `bg-status-*`, `text-severity-*`, etc.

### Charts
`--chart-1..5` = blue / teal-blue / tan / violet / green (harmonized, muted).

**Radius:** `--radius: 0.625rem` (10px); cards/panels read as `rounded-lg`/`xl`.

---

## 3. Typography

| Role | Face | Usage |
|---|---|---|
| Display | **Bricolage Grotesque** (`font-display`) | page titles, big headings, card stat labels' values |
| Body/UI | **Hanken Grotesk** (`font-sans`, default) | everything |
| Mono | **IBM Plex Mono** (`font-mono`) | hashes, ids, code, tabular data |

- Base `html` font-size **15px**.
- Page titles: `font-display text-[22px] font-extrabold tracking-[-0.02em]`.
- Uppercase micro-labels: `text-[10px] tracking-[0.08em] font-semibold text-muted-foreground`.
- Numbers align with `tabular-nums`.
- Fonts self-hosted via `@fontsource` (air-gap / SCIF — no CDN). Imported in
  `src/main.tsx`.

---

## 4. Shell

- **Slim icon rail**, 64px (`components/shell/sidebar.tsx`). 14 destinations in
  5 groups (`app/nav.ts`: work / watch / govern / build / system) separated by
  hairlines, labels in right-side tooltips. Brand mark top; theme toggle +
  operator avatar bottom. Active = `bg-primary/12 text-primary`.
- **No global top bar.** Each screen renders its own `PageHeader`
  (`components/page-header.tsx`) with the big display title + description +
  right-aligned actions.
- Nav labels use operator vocabulary: Fleet, Chat, Tasks, Workflows / Activity,
  ArcLLM, Knowledge, Audit / Approvals, Gated, Policy / Tools & Skills,
  Connections / Settings. **Routes are unchanged.**

---

## 5. Component standards (`components/ui/*` + composites)

Flatten and sharpen. The tells of the old/bought look to remove: fat colored
left-bars on cards, heavy shadows, soft low-contrast chrome.

| Component | Standard |
|---|---|
| `Card` | flat, `rounded-lg border bg-card`, hairline border, minimal shadow. No accent left-bar. |
| `StatCard` | drop the `bg-primary/70` left spine. Clean tile: uppercase micro-label, big `font-display` tabular value, hint. Optional muted icon. Hover = border-strong, no ring glow. |
| `Button` | `default` = solid blue, flat, `hover:bg-primary/90`. `outline`/`ghost`/`secondary` neutral. Radius `rounded-md`. |
| `FilterPills` | segmented chips; active = `bg-primary/12 text-foreground ring-1 ring-primary/20`. Match Audit "All events / Control plane" row. |
| Tables (`data-table`, `trace-table`, etc.) | uppercase micro-label headers, hairline row separators, `hover:bg-muted/40`, mono for ids/hashes. |
| `SeverityBadge` / status | flat tinted pills `bg-*/15 text-* border-*/30`. |
| Drawers/sheets | `bg-popover`, hairline border, larger radius, section micro-labels. |
| Empty states (`states.tsx`) | keep the dotted frame but tune to new tokens; concise copy. |
| **Signed mark (new)** | small tan seal: `bg-signed text-signed-foreground` rounded circle w/ check; "signed" text `text-signed`; hashes `text-signed font-mono`. |

---

## 6. AI-native components (Beautiful UI vocabulary)

Add these as reusable components (`components/ai/`), driven by our tokens. They
drop into Chat, Activity/trace, and Approvals.

1. **Thinking** — collapsible reasoning trace ("Thought for 3s · N steps");
   tan lead icon, expandable step list. Chat + trace.
2. **Tool chips** — compact `read file.py ✓` chips for tool calls.
3. **Approval card** — trifecta legs (private / external / untrusted), lit leg,
   Approve&sign / Deny; intent preview + "why gated" in the modal.
4. **Confidence signal** — `72% confident` chip, tone by level.
5. **Streaming text** — token stream w/ shimmer + elapsed time.
6. **Autonomy dial** — Observe / Propose / Act&confirm / Autonomous, per agent.
7. **Undo** — 5s escape hatch on signed/approved actions (Nielsen: user control).

Grounded in agentic-UX research: keep the **activity/audit log separate from
chat** (chat clarifies; the signed ledger is its own screen). Show reasoning,
confidence, and an undo path; gate consequential actions.

---

## 7. Motion layer (Framer Motion → `motion`)

**Library:** `motion` (the maintained successor to framer-motion). Tailwind +
React, tree-shakeable, self-hostable. Add `motion` to `web/package.json`.
Complement with hand-built CSS transforms where a dependency isn't warranted.

**Principles**
- Motion serves meaning: state changes, spatial history, focus. Never ambient
  decoration (reads AI-generated).
- Honor `prefers-reduced-motion`: disable transforms, keep opacity.
- 150–250ms for UI micro-interactions; 400–550ms `cubic-bezier(.2,.75,.25,1)`
  for spatial transitions.

**Signature spatial views** (a "History" viewer, side-to-side):
- **Cover Flow — Runs.** Flip through recent runs like album art; centered run
  in focus (agent, task, status, signed count, sparkline). Drag / arrows / click.
  Mounts on **Activity (arcrun)** as a view toggle beside the table.
- **Time Machine — Checkpoints/Sessions.** Depth (or side-to-side) stack of
  session restore points; scrub through and open one.
- **Prompt fan** — recent prompts fanned like pages; spin to revisit / re-run.

**Micro-interactions:** card hover lift, list stagger-in, drawer slide, tab
underline, number roll on stat cards, gate "seal" stamp on approve.

---

## 8. Per-screen plan (all 14 tabs + detail routes)

Status: ☑ done · ◐ in progress · ☐ todo

| Screen (route) | Redesign | Motion | Status |
|---|---|---|---|
| Shell + nav | slim grouped rail, display type | rail tooltips | ☑ |
| Tokens/theme | flat blue + tan `--signed`, light+dark | — | ☑ |
| Fleet (`agents`) | clean agent cards, status, sparkline, signed-today | card hover lift, stagger | ☐ |
| Agent detail (`agents/:id/:tab`) | tab bar restyle, flat panels | tab underline | ☐ |
| Chat (`messages`) | room list + thread + info sidebar (no audit); Thinking, tool chips, inline approval | typing/stream, message rise | ☐ |
| Tasks (`tasks`) | board columns + task drawer | card drag/hover | ☐ |
| Workflows (`workflows` + detail) | list + DAG editor polish | node/edge transitions | ☐ |
| Activity (`arcrun`) | run table + **Cover Flow** view toggle; honest status labels | Cover Flow | ☐ |
| ArcLLM (`arcllm`) | stat tiles, charts, traces in new palette | number roll | ☐ |
| Knowledge (`knowledge`) | memory/entity/insight cards | stagger | ☐ |
| Audit (`security`) | dedicated signed ledger: tan hash chain, filter pills, verdict chips | row stagger | ☐ |
| Approvals (`approvals`) | queue cards + approval modal (trifecta, why-gated, undo) | seal stamp | ☐ |
| Gated (`gated`) | capability cards, load decision | — | ☐ |
| Policy (`policy`) | bullets + per-agent stats | — | ☐ |
| Tools & Skills (`tools-skills`) | inventory tables + drawers | — | ☐ |
| Connections (`connections`) | connector cards, grants, auth panels | — | ☐ |
| Settings (`settings`) | config editors, flat forms | — | ☐ |
| Shared components | §5 primitives + §6 AI-native | §7 micro | ◐ |

---

## 9. Build & verify workflow

1. Edit under `web/src/`.
2. `npm run build` — must pass tsc + vite.
3. Commit `web/src` changes + rebuilt `../src/arcui/static/` together (air-gap:
   static is committed). Stage explicitly; never `git add` broad paths (avoid
   sweeping in unrelated in-progress files).
4. One commit per coherent phase; message names the phase.
5. Dev preview: `npm run dev` → `http://localhost:5173/#auth=<anything>` (token
   box accepts any string; backend optional — theme renders without data).

---

## 10. Progress log

- ☑ Palette re-skin (Console theme) — `966e2c2d`
- ☑ Shell identity: slim grouped rail + Bricolage/Hanken type — `83fe5a8b`
- ◐ Component polish (§5) + AI-native (§6) + Motion (§7) — in progress
  - ☑ HITL set: ApprovalRequest, TrifectaGate, SignedSeal, ContextNote
    (`components/hitl.tsx`) — used on Approvals, agent Trust tab, and **inline
    in agent chat** when blocked on a gate.
  - ☑ Tool chips in chat; inline HITL approval in chat.
  - ☑ All 10 list/detail screens redesigned (parallel wave) + agent-detail.
  - ☐ Motion spatial views (Cover Flow runs / Time Machine checkpoints).
  - ☐ Streaming + Thinking-trace wiring in chat (needs reasoning in the data).

---

## 11. Feature parity & cross-view completeness (audit)

Two jobs: (A) confirm the redesign dropped nothing, and (B) surface features
that exist on a canonical screen but are missing from a related detail view.

**Known target (Josh's example):** the full **Tasks** board (`pages/tasks.tsx`)
exposes actions/data that the agent-detail **Tasks** tab does not — add them to
the agent view, scoped to that agent.

**Sibling pairs to reconcile** (full screen ⇄ agent-detail tab):
tasks, schedules, tools, skills, sessions/runs (traces), knowledge, policy,
prompts, connections/capabilities. Also chat actions, approvals, and per-row
row-actions in every table.

**Regression check (redesign so far): CLEAN.** The removed topbar held only the
theme toggle (moved to the rail) + a text wordmark (cosmetic). PageHeader and
StatCard kept every prop. All 14 routes intact.

Gap list (from the audit — check off as closed):

- ☑ **GAP-5 (Josh's example): agent Tasks tab** now has the full board chrome —
  4 stat cards, status/priority pills, tag filter, count strip, New-task that
  pre-owns to this agent. `7b29e007`.
- ☑ **GAP-3: per-agent Approvals** — new agent Trust tab; Approve/Deny inline
  under operator mode. `27521d68`.
- ☑ **GAP-4: per-agent Pending capabilities** — Trust tab lists the agent's
  quarantined caps with a Manage link to act. `27521d68`.
- ☑ **GAP-1: Knowledge tab in agent-detail** — reuses the knowledge browsers at
  the detail agent's scope. `2e06e892`.
- ☑ **GAP-2: Runs tab at agent scope** — this agent's runs (filtered by DID) +
  StatusChip + RunDetailDrawer + scoped SpawnLineage. `2e06e892`.
- ◐ **GAP-6: Policy divergence.** Fleet page gained `TopPerformers` (`2c503805`).
  Still: `PolicyConfigCards`/`SystemPolicyRules` need a config (fleet page has
  none — likely skip); add the bullet search/sort/hide-retired bar to the agent
  policy tab.
- ☐ **GAP-7: Fleet Tools & Skills page inert.** Deferred — a fleet tool row
  spans many agents, so which agent to open the drawer for is ambiguous; needs
  a picker, not a naive first-agent guess.
- ☐ **GAP-8 (product, needs backend): schedules have no create/delete/run-now**
  anywhere — server is GET+PATCH only. Equal-parity gap; flag, don't fake.
- ☑ **GAP-9:** connections grant/revoke is intentionally fleet-only — no change.
- ☑ **Consistency: global OperatorModeToggle** moved into the rail — now on all
  screens. `7b29e007`.

---

## 12. Information architecture & UX rethink

Re-skinning isn't enough. For a business operator we rethink *where info lives,
what leads, and how it's shown* — while keeping every feature/data point (§11).

### 12.1 A "Home / Today" landing (new)
Today the app opens on the raw **Agents** roster. Replace the landing with a
**Home** that answers "what do I need to know right now?":
- **Needs you** — pending approvals, failed runs, review gates (act inline).
- **Fleet at a glance** — agents, who's working / idle / blocked, live now.
- **Recent activity** — last runs with honest status; **Upcoming** — schedules.
Every tile deep-links to its full screen. This is the operator's daily driver;
the detailed screens are the drill-down.

### 12.2 Altitude & progressive disclosure
Each screen leads with a **plain-language summary + the 3–5 numbers that matter**
(clean stat tiles), then filters, then the table/board, then a **drawer that
holds the full detail and *every* row action**. Business users see the answer
first and open detail only when they want it. The drawer is also where
cross-view parity is satisfied — a task/agent/run drawer offers the same actions
as its canonical screen.

### 12.3 Navigation for business users (not developers)
Regroup the rail by what an operator does, and push developer telemetry down:
- **Primary:** Home, Fleet, Chat, Tasks, Approvals, Activity.
- **Governance:** Approvals, Gated, Policy, Audit.
- **Advanced / developer:** ArcLLM internals, raw traces, Tools & Skills wiring,
  Connections — present, but lower and clearly labeled as advanced.
Rename to plain terms; keep the technical id in hover/detail only:
`ArcLLM → Model usage`, `Gated → Pending capabilities`, `Policy → Rules`,
`Security → Audit`, `ArcRun → Activity`. Routes stay; labels change.

### 12.4 How we show things
- **Status in plain words + one glance of color.** "Waiting on you", "Failed at
  step 7", "Signed" — not raw enum values.
- **Signed/verified is felt, not read** — the quiet tan seal on trace/audit.
- **Charts and sparklines** get the same care as type (area fill, faint grid,
  emphasized endpoint).
- **Motion** guides attention (§7); the spatial history views live under
  Activity as an operator-friendly way to flip through runs/checkpoints.

**Decision gates — CONFIRMED (Josh):** (a) add the Home/Today landing; (b)
regroup + rename the rail per 12.3 (business labels: Model usage, Rules, Audit,
Pending capabilities; developer telemetry under an Advanced group). Routes stay;
`DEFAULT_PATH` → `home`.

