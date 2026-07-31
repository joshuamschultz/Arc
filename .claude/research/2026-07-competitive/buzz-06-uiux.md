# Buzz vs Arc — UI/UX and Workflow Comparison

Every claim is from direct source read. Paths are repo-relative:
Buzz = `.../scratchpad/buzz/`, Arc = `/Users/joshschultz/Projects/arc/`.

**Headline:** Buzz is a ~290k-LOC multi-client collaboration *product* (desktop +
browser + mobile + admin) where agents are first-class colleagues. Arc is a
~15k-LOC single-page *operator control plane* for an agent fleet. They overlap on
exactly three things — agent roster, task board, approval gate — and Buzz is
deeper on two of them. Arc has nothing at all for the other ~80% of Buzz's
surface; Buzz has nothing at all for Arc's governance/cost surface.

## Scale (source LOC, tests excluded)

| Client | Buzz | Arc |
|---|---|---|
| Primary client | 229,634 (`desktop/src`, TS/TSX) | 12,619 (`packages/arcui/web/src`) |
| Browser client | 4,145 (`web/src`) | — (same SPA) |
| Mobile | 55,214 (Dart, `mobile/`) | 0 |
| Admin console | 1,218 (`admin-web/`) | 0 |
| Terminal UI | — | 2,699 (`packages/arctui`) |
| **Total** | **~290k** | **~15.3k** — a **19×** gap |

Accessibility: Buzz desktop carries **854 `aria-*` + 135 `role=`** across 501
`.tsx` files (~1.7 aria/file); Arc carries **29 `aria-*` + 2 `role=`** across 75
(~0.39/file). Buzz's VISION.md targets WCAG 2.1 AA; Arc states no target. Buzz
honors `prefers-reduced-motion` (`TurnLivenessIndicator.tsx:11-74`); Arc does not.

---

## Ground truth: the shipped UI (docs/assets/screenshots/, read directly)

- **`channel-agents.png`** — `#engineering`. Sidebar: Search everything (⌘K),
  Inbox, Projects, Agents, then community sections ("The Hive", "Product",
  "Launch Swarm" with a lock-icon private channel). Messages interleave human
  avatars (Elena Torres, Alex Rivera) and agent avatars (Bumble, Fizz, Honey —
  bee icons) **in one thread**. Agents are @mentioned (`🐝Bumble`) exactly like
  humans, post GitHub-PR and Buzz-PR link-preview cards inline, and receive emoji
  reactions. Header: member count, headphones (huddle), sliders (settings).
- **`channel-thread.png`** — `#flight-path`. Five humans discuss UI polish; agent
  Fizz posts a numbered 3-step plan and gets 👀1 💬1. Pinned above the composer:
  **"🐝 Honey: Working"** — live per-agent activity.
- **`create-channel.png`** — "Add a channel" modal: search-or-create field, tabs
  (All / Joined / Archived), "Create a new channel", then channels with member
  count + one-line description.
- **`media-comments.png`** — video lightbox with scrubber/speed/volume and a
  right-hand **Comments** panel timestamp-anchored to frames (`00:04.2 "Maybe we
  cut it here?"`), a "Comment at current frame" checkbox, and an emoji quick-react
  bar (😂😍😮🙌👍👎).

---

## (a) Screen inventory, side by side

### Buzz — desktop (Tauri 2 + React 19), `desktop/src/app/routes/`

15 route modules, TanStack Router. **All seven VISION surfaces verified in code** —
the "supports all seven surfaces today" claim holds.

| Route file | Surface | What it is |
|---|---|---|
| `index.tsx` → `features/home/ui/HomeView.tsx` | 🏠 Home | Two-pane Inbox (resizable), filters, drafts, per-item read state, thread context, inline reactions, reminders count |
| `ChannelRouteScreen.tsx`, `channels.$channelId.tsx` | 💬 Stream | Main chat screen |
| `channels.$channelId.posts.$postId.tsx` | 📋 Forum | Post + flat replies |
| `messages.new.tsx` | ✉️ DMs | New-DM composer with recipient chips |
| `agents.tsx` → `AgentsView.tsx` | 🤖 Agents | Persona grid + Teams + Relay directory |
| `workflows.tsx`, `workflows.$workflowId.tsx`, `WorkflowsRouteScreen.tsx` | ⚡ Workflows | Builder + run traces + approval cards |
| topbar ⌘K → `features/search/ui/TopbarSearch.tsx` | 🔍 Search | Global grouped search |
| `pulse.tsx` → `PulseView.tsx` | — | Social feed; tabs Search / Everyone / People / Liked / **Agents** |
| `projects.tsx`, `projects.$projectId.tsx` | — | Repos, branches, **full PR review UI** (beta-gated) |
| `reminders.tsx` | — | Snooze / remind-me-later |
| `settings.tsx` | — | 15 sections (below) |
| `root.tsx` | — | Shell |

Settings (`features/settings/ui/SettingsPanels.tsx:158-231`), grouped Personal /
Communities / App: Appearance (System/Light/Dark), Profile, Notifications,
Experiments, Agents, Templates, Compute, Shortcuts, Hosted communities, Invites,
Moderation, Custom emoji, Local archive, Mobile pairing, Updates.

Sidebar (`features/sidebar/ui/`): **CommunityRail** — multi-tenant workspace
switcher with "Add community" (`CommunityRail.tsx:381,426`) — drag-and-drop
channel sections (`SidebarDnd.tsx`), relay-connection card, profile card, and a
17-item channel context menu (`ChannelContextMenu.tsx`): Move to section / New
section / Remove from section / Copy channel name / Copy channel ID / Mark as
read / Mark unread / Mute / Unmute / Star / Unstar / Leave / Archive / Delete.

### Buzz — browser client, `web/src/app/routes.ts`

5 routes: landing `index.tsx`, `/invite/$code` (join flow), `/repos`,
`/repos/$repoId`, `/repos/$repoId/blob/$` (README render, file tree, blob viewer,
commits, refs, clone URL). Reads NIP-34 kind:30617 announcements off the relay;
ships a `preview` demo mode backed by `features/repos/mock-repos.ts`.

### Buzz — mobile (Flutter, `mobile/lib/`, 55,214 LOC Dart)

VISION.md marks it 🚧, but it is **far past a stub**. Bottom nav is three
floating tabs (`features/home/home_page.dart:32-57`): Home (= `ChannelsPage`),
Activity, Search. 13 pages total.

| Feature | Files / LOC | Notable |
|---|---|---|
| `channels/` | 77 / **21,075** | `channel_detail_page`, `thread_detail_page`, `media_viewer_page`, `reaction_row`, `emoji_picker`, `message_actions`, `mentions/`, `compose_bar/`, `members_sheet`, `manage_channel_sheet`, `channel_sections/stars/mutes`, `read_state`, `unread_badge`, typing provider, **`agent_activity/`** |
| `pairing/` | 8 / 2,143 | QR scanner + `pairing_crypto` + `pairing_socket` — desktop pairs the phone |
| `pulse/` | 7 / 1,538 | feed + `compose_note_page` |
| `forum/` | 5 / 1,504 | `forum_thread_page` |
| `profile/` | 10 / 1,445 | `set_status_sheet`, presence + user-status caches |
| `search/` | 2 / 656 | |
| `activity/` | 3 / 636 | |
| `settings/` | 2 / 555 | + `theme_picker_page` |
| `invites/` | 2 / 465 | |
| `custom_emoji/` | 3 / 295 | |

The standout: `channels/agent_activity/` ships `agent_activity_sheet.dart`,
`transcript_builder.dart`, `transcript_item_widget.dart`,
`observer_subscription.dart` and `working_bots_provider.dart` — **the agent
receipts transcript and the "which bots are working" signal are on the phone
too.** Paired from desktop via Settings → Mobile
(`features/settings/ui/MobilePairingCard.tsx`). **Arc has no mobile client.**

### Buzz — admin-web (`admin-web/`, 1,218 LOC, React SPA)

Four screens in `admin-web/src/App.tsx`, all against `/api/admin/v1`
(`src/api.ts:1`), all `GET`: **Open reports** ("Review reports across every Buzz
community", `:90-92`), **Report detail** (`:137-139`), **Feedback** (searchable
product feedback, categories Bug / Praise / Needs work, `:204-206,694-696`), and
**Feedback detail** (`:395-397`). Per `docs/admin/README.md` it sits behind
VPN/IP-allowlisted ingress, and "Acted on" is a client-side localStorage
checkbox. So it is a narrow **read-only cross-community moderation triage
console** — no user management, billing, or community admin. Modest, but
**Arc has no separate operator console at all.**

### Arc — `packages/arcui/web/src/pages/` (13 pages, one SPA, `app/router.tsx`)

Note: the real path is `packages/arcui/web/`, not `packages/arcui/src/arcui/web/`.
Stack: React 19 + react-router v7 + TanStack Query/Table + Zustand + Radix
primitives (bespoke shadcn-style, `components/ui/*`) + Recharts + Tailwind v4.

| Page | What it is | Write actions |
|---|---|---|
| `agents.tsx` | Fleet roster cards: DID, model/provider chips, online/idle dot, sessions/schedules/policy counts, 24h token sparkline | **Read-only** — register via `arc team register` CLI (`:153`) |
| `agent-detail.tsx` | 12 tabs: overview/identity/sessions/llm/skills/tools/prompts/tasks/schedules/policy/workspace/files | Edits via drawers (`RubricEditor`, `PromptDrawer`, `ScheduleDrawer`, `ToolDrawer`, `SkillDrawer`), operator-mode gated |
| `tasks.tsx` | Mission Control kanban, stat cards, status/priority/owner/tag filters | New task, edit, delete, cancel, approve/reject (`task-drawer.tsx:166`) |
| `approvals.tsx` | Lethal-Trifecta operator sign-off; shows blocked legs (private_data / external_comms / untrusted_input) | Approve / Deny |
| `gated-capabilities.tsx` | Signing-loader quarantine (unsigned / invalid sig / new sighting / denied) | Approve / Disapprove |
| `messages.tsx` | Agent DMs (`ChatPanel`) + `#channels` (`ChannelPanel`, live `useTeamStream`) + `@mention` composer with colored pills | New channel, manage members, send |
| `tools-skills.tsx` | Fleet capability matrix: tool table w/ classification badge + skill directory | Read-only |
| `policy.tsx` | ACE policy bullets, score distribution, per-agent breakdown | Read-only |
| `security.tsx` | Audit trail (time/event/agent/actor/severity), filter pills, `EventDrawer` payload | Read-only |
| `knowledge.tsx` | Memory inspector: Overview / Insights / Procedures / Entities / Daily Notes / Raw, context-budget + graph stats, workspace `FileTree` | Read-only |
| `arcllm.tsx` | LLM telemetry: token volume, cost by provider + by agent, model perf table, circuit breakers, budgets | Read-only |
| `arcrun.tsx` | Run list (question→response cycles), `RunDetailDrawer`, **`SpawnLineage`**, `?run=` deep link | Read-only |
| `settings.tsx` | TOML editor for arcllm/arcrun/arcagent, System (~/.arc) or per-agent scope | Inline JSON edit + Save |

Theming: `src/index.css` class-based dark mode (`.dark` default), explicit
toggle in `components/shell/topbar.tsx:7,24,27` — **no `prefers-color-scheme`**,
so no System option. Presence: `components/status-badge.tsx` `StatusDot` —
binary Online (pulsing green) / Idle only.

### Arc — `packages/arctui/src/arctui/app.py`

One Textual screen: Header / `Horizontal(TranscriptView | ActivityView)` /
InputComposer / Footer. Transcript streams agent tokens (`agent.run()` →
`TokenEvent` → `append_delta`); ActivityView renders tool-start/end and
turn-start/end off the Module Bus. Keybindings: `ctrl+c` quit, `ctrl+l` clear.
Slash commands via `arccli.commands.registry`. Single conversational screen —
not a multi-surface app, but it runs headless over SSH, which Buzz cannot.

---

## (b) Affordance checklist — Arc vs Buzz

| Affordance | Buzz | Arc |
|---|---|---|
| Emoji reactions | ✅ pill badges + reactor popover + **personalized top-4** ranked by frequency+recency (`useQuickReactionEmojis.ts`) + burst particles | ❌ |
| Custom emoji | ✅ NIP-30 per-member sets | ❌ |
| Threads | ✅ nested tree, depth rails, follow/unfollow, per-reply unread, **split-pane ↔ drawer toggle** (`ThreadViewModeToggle.tsx`) | ❌ flat feed |
| @mention | ✅ Tiptap autocomplete, humans + agents identically | ✅ `mention-composer.tsx` colored pills |
| Rich composer | ✅ Bold/Italic/Strike/Code/Codeblock/Link/Lists/Quote/**Spoiler**, ⌘B ⌘I ⌘⇧X ⌘E ⌘K (`FormattingToolbar.tsx:213-279`) | ❌ plain textarea |
| Message edit / delete / mark-unread / copy-link / remind-later / report | ✅ `MessageActionBar` ⋮ menu | ❌ |
| Create channel | ✅ search-first, inline "Create «query»", stream/forum/dm, **Ongoing vs Temporary w/ 9 TTL presets** 30min–30d (`ChannelTypePicker.tsx:14-91`) | ✅ `CreateChannelSheet` (name only) |
| Channel canvas (shared doc) | ✅ per-channel markdown, human + MCP writable (`ChannelCanvas.tsx`) | ❌ |
| Channel templates | ✅ seed canvas **and bulk-create agents/teams** (`useApplyTemplate.ts:57-154`) | ❌ |
| Invite / guest scoping | ✅ invite codes, `/invite/$code` web join, scoped guest tokens | ❌ |
| Add agent to channel | ✅ **three** surfaces (see workflow b) | ✅ member add sheet |
| Per-agent channel controls | ✅ Start/Stop/Respawn, respond-to scope (owner/allowlist/anyone), role, timeout 1h/24h/7d, ban, bulk spawn-all/stop-all | ❌ |
| Agent presence | ✅ **Working** (pulsing, per-channel, "Working in #x · Ns"), Starting… (15s grace), active, error w/ severity | ⚠️ binary Online/Idle |
| Human presence | ✅ online/away/offline, OS-idle-driven, 30s heartbeat | ❌ n/a |
| Custom user status | ✅ emoji + text, 5 presets, Clear | ❌ |
| Huddles / voice | ✅ push-to-talk ⌃Space, device picker, level meter, agent-TTS mute, **"Add agent" to huddle**, captions, emoji burst | ❌ |
| Comment on video frame | ✅ auto `[mm:ss]` timecodes, nested frame.io-style replies, "React {emoji} at {timecode}" (`VideoPlayer.tsx:1490-1545,1845-1900`) | ❌ |
| Inline diff in chat | ✅ `DiffMessage` + fullscreen **Unified/Split toggle** | ❌ |
| PR review | ✅ Approve / Request changes / Merge (real git merge + conflict recovery) / draft-reopen-close, **line-anchored inline comments**, agents as reviewers | ❌ |
| Approval gate | ✅ `WorkflowApprovalCard` inline in run trace | ✅ **3 gates**: trifecta, capability quarantine, task review |
| Workflow builder | ✅ Form↔YAML, 5 trigger types | ❌ no authoring UI |
| Run trace | ✅ step cards, status badges, duration, collapsible JSON, 1s poll | ✅ `arcrun.tsx` + drawer + **SpawnLineage** |
| Agent memory browser | ✅ owner-gated engram accordion, BFS over `[[slug]]`, orphans + dangling refs | ✅ **richer** — 6 tabs + budget/graph stats |
| Agent creation UI | ✅ scratch / catalog / import `.agent.json` / drag-drop | ❌ CLI only |
| Agent share/export | ✅ DM / copy link / export file + **memory-inclusion picker** + plaintext warning (`PersonaShareDialog.tsx:119-178,717-757`) | ❌ |
| Teams of agents | ✅ persona bundles, Deploy to channel, export `.team.json/.png` | ⚠️ `arc team` CLI, no UI |
| Global search | ✅ ⌘K grouped + in-channel find bar (Prev/Next/Close) | ❌ per-page filters only |
| Multi-tenant communities | ✅ CommunityRail + hosted-communities settings | ❌ |
| Moderation | ✅ timeout/kick/ban/NIP-56 report, queue, countdown banner | ❌ |
| Notifications | ✅ per-pubkey desktop/badge/sound, mention + follow rules | ❌ |
| Reminders / snooze | ✅ panel + `RemindMeLaterDialog` + `SnoozeMenu` | ❌ |
| Theming | ✅ System / Light / Dark | ⚠️ Light/Dark, no System |
| Mobile client | ✅ 55k LOC Flutter | ❌ |
| Separate admin console | ✅ (narrow, read-only) | ❌ |
| **LLM cost/token dashboard** | ❌ | ✅ `arcllm.tsx` |
| **Signed-capability quarantine** | ❌ | ✅ `gated-capabilities.tsx` |
| **Policy inspector** | ❌ | ✅ `policy.tsx` |
| **Audit trail viewer** | ❌ (hash chain server-side, no UI) | ✅ `security.tsx` |
| **Config editor** | ⚠️ settings panels | ✅ per-agent TOML editor |
| **Terminal/headless UI** | ❌ | ✅ `arctui` |

---

## (c) The six workflows, step by step (Buzz)

**(a) Onboarding / joining a community.** Buzz ships **two distinct flows** —
`CommunityOnboardingFlow.tsx` (join a workspace) and `MachineOnboardingFlow.tsx`
(set this machine up to run agents) — across 30+ files in
`features/onboarding/ui/`.

*Community path:* invite link → `web/src/app/routes/invite.$code.tsx` renders
`InvitePage` + `InviteJoinPolicyNotice`, keypair-signed accept; or in-app
`InviteRedeemForm.tsx` behind `PendingInviteGate.tsx`. `OnboardingFlow.tsx`
sequences `ProfileStep` → `AvatarStep` → `key-import` (`NostrKeyImportForm.tsx`
for an existing nsec), with `BackupStep` forcing a key backup shown through
`NsecMaskedDisplay.tsx`. Membership is checked live and gets its own rendered
states — `membership-denied` (`MembershipDenied.tsx`), `unreachable`, `error`
(`OnboardingFlow.tsx:52-79,243-265`) — plus `KeyringLockedScreen`,
`RecoveryScreen`, `RelaunchRequiredScreen`, `ResetFailedScreen`. Then
`WelcomeKickoffStage.tsx` drops you into a seeded room; the starter team is
literally the three bee agents from the screenshots
(`desktop/public/onboarding/starter-team/{bumble,fizz,honey}.png`), so a new user
lands **with agents already in the room**, not an empty workspace.

*Machine path:* `SetupStep.tsx` installs and signs into an agent **runtime
harness** — Claude, ChatGPT, or Goose
(`features/onboarding/assets/harness-logos/`) — with per-runtime Install / Retry
installing / Sign in / Check again / View install instructions affordances
(`:152,206,243,259,273`), live status ("Not installed yet.", "Installation
failed", "Configuration invalid", "Status unavailable") and
`DefaultConfigStep.tsx`.

Identity is a secp256k1 keypair; the profile is re-posted per community, so
joining a second community leaks nothing from the first.

**(b) Adding an agent to a channel.** Three paths, all one-screen:
(1) `MembersSidebar.tsx:256-372` — "Add people and agents", one search box mixing
humans and agents; picking a local agent also boots its runtime (`:556-574`).
(2) `AddChannelBotDialog.tsx` — multi-select personas/teams that *creates* N new
agent instances directly into the channel. (3) `QuickBotBar.tsx` — hover row of
three one-click bot avatars. Role is chosen at add time (bot/member/guest/admin,
`AddAgentToChannelDialog.tsx:167-181`); respond-to scope (owner / allowlist /
anyone) is editable per member afterwards (`MembersSidebarMemberCard.tsx`).

**(c) Asking an agent a question and getting receipts.** @mention the agent
in-channel exactly as you would a human. Its badge pulses "Working in #channel ·
12s" in the roster (`ManagedAgentRow.tsx:316-343`) and a per-agent line pins above
the composer ("🐝 Honey: Working"). Receipts land in
`ManagedAgentSessionPanel.tsx`: a live transcript whose rows are typed render
classes — `ThoughtActivity` (markdown reasoning), `PlanActivity` (plan + "Updated"
diff), `ToolActivity` (shell blocks, todo summaries, file-edit diffs, image
previews), `MessageActivity`, `LifecycleActivity`, `SuppressedActivity` — plus a
toggleable **raw event rail** for ground truth, and a connection badge
(Live/Connecting/Unavailable/Closed/Idle, `:339-366`). This is VISION_ACTIVITY.md's
"verb → object → outcome, polished by default, raw on demand," actually built.

**(d) Branch-as-room patch review.** *Aspiration vs reality — the gap matters.*
VISION_PROJECTS.md describes branch → auto-created channel → CI agent posts
results inline → signed approval (kind:46011) → merge → channel archives as the
permanent record of why the code exists. **Branch-as-room is not built.** Only a
whole *project* binds to one channel (`Project.projectChannelId`,
`features/projects/hooks.ts:79,213`), surfaced as a single "Open Discussion"
button (`ProjectDetailScreen.tsx:862-876`); branches are plain NIP-34 refs
(`branchMutations.ts`) with no room, and `ChannelRouteScreen.tsx` has zero project
awareness. **But** a genuine GitHub-equivalent PR review UI *is* shipped in-app:
Approve (`PullRequestReviewCard.tsx:191-201`, dialog `:289-322`), Request changes
(`ProjectPullRequestsPanel.tsx:882-889`), Merge with real git merge + conflict
recovery ("Resolve in Terminal" / "Copy commands",
`MergePullRequestButton.tsx:190-286`), Ready-for-review / Reopen / Convert to
draft / Close (`:210-278`), **line-anchored inline diff comments** with hover-"+"
and "Outdated" badges (`ProjectPullRequestFilesChangedPanel.tsx:549-563`), and an
Add-Reviewer picker **listing agents alongside humans**
(`PullRequestReviewersRow.tsx:177-253`). Preset agent asks ("PR review", "Release
check") live at `ProjectsAgentPromptPage.tsx:101-125`. Beta-gated behind
`usePreviewFeatureWarning`.

**(e) Approving a workflow step.** `WorkflowFormBuilder.tsx` (Form↔YAML toggle)
defines a `request_approval` action (from / message / timeout). At runtime the
approval renders **inline under its step** in the run trace
(`WorkflowRunTrace.tsx:74-76,115-122`), not in a separate inbox:
`WorkflowApprovalCard.tsx` shows "Approver: {spec}" `:30`, "Expires: {at}"
`:32-34`, an optional note textarea `:36-42`, green **Approve** `:44-59` and
destructive **Deny** `:60-75` → Tauri `grant_approval`/`deny_approval` keyed by
token (`hooks.ts:148-169`). It renders `null` once decided — **no decision
history**. Runs poll every 1s while `pending|running|waiting_approval`, approvals
every 10s: the client is built for durable resumable server state. Also fires a
notification (kind 46010, "X requested approval",
`features/notifications/lib/feed.ts:51-71`). **The honest caveat, printed in
Buzz's own UI:** `WorkflowStepCard.tsx:31-37` renders *"Backend note: approval
gates still stop runs with WF-08; approval records are not persisted yet."* So
Buzz's approval UI is complete and its executor is not — the exact mirror image of
Arc, whose approval *backends* are wired but which has no workflow layer to gate.

**(f) Searching across everything.** ⌘K `TopbarSearch.tsx` returns results grouped
by section against one permission-aware Postgres FTS index spanning stream, forum,
DMs, canvases and workflow traces — "one event log, one search index, three
lenses." `resolveSearchHitDestination.ts` + `searchHitEventCache.ts` route a hit
back to its native surface. In-channel, `ChannelFindBar.tsx:74-116` gives
Previous/Next/Close match navigation. Pulse adds its own Search tab alongside
Everyone / People / Liked / Agents.

---

## Design systems

Both are Radix + Tailwind + lucide, so the *primitives* are peers. The gap is
depth and polish.

| | Buzz desktop | Arc arcui |
|---|---|---|
| Shared UI files | **79** (`desktop/src/shared/ui/`) | ~12 (`web/src/components/ui/`) |
| Radix packages | 14 (incl. `focus-scope`, context-menu, switch, toggle) | `radix-ui` meta-package |
| Editor | Tiptap + `tiptap-markdown` | plain textarea |
| Router / data | TanStack Router + Query + **Virtual** (virtualized lists) | react-router v7 + Query + Table |
| Charts | — | Recharts |
| Theme | `ThemeProvider` + `adaptive-theme.ts` + `theme-loader.ts` + **`useSystemColorScheme.ts`** + `ThemePreviewFrame` + `useThemePreviewVars` (live preview) | `index.css` `.dark` class + a toggle |
| System theme | ✅ | ❌ |
| Reduced motion | ✅ `TurnLivenessIndicator.tsx:11-74` | ❌ |
| aria / role density | 854 / 135 across 501 tsx | 29 / 2 across 75 tsx |
| Delight layer | `EmojiBurstProvider`, `PoofBurstProvider`, `SpoilerParticles`, `Shimmer`, `AnimatedCount`, `smoothCorners`, `card-texture.css`, `modalMotion` | — |

Buzz also ships a sound system (`desktop/public/sounds/`, `SoundPicker.tsx`) and
a `DoctorSettingsPanel.tsx` self-diagnostic. Arc has neither.

---

## (d) What Arc's UI is missing to compete — ranked

1. **No conversational surface where humans and agents are peers.** Arc's
   `messages.tsx` is a fleet inspector with a chat pane bolted on. Buzz's channel
   is a room where a bee-avatar agent posts a PR card, gets a ❤️, and hands off to
   another agent in the same thread. This is the entire product thesis; Arc lacks
   it.
2. **No reactions / threads / rich composer.** Buzz has personalized top-4 quick
   reactions, nested threads with follow state, and a 9-mark formatting toolbar.
   Arc has a textarea. Cheapest, highest-visibility gap to close.
3. **No receipts UI for a delegated ask.** Arc has `arcrun.tsx` traces, but they
   are a separate page keyed by run id — you cannot see "what is this agent doing
   right now, in this conversation." Buzz's per-agent live transcript with typed
   render classes + raw rail is the reference implementation, and it is *specified*
   in VISION_ACTIVITY.md.
4. **No agent lifecycle UI.** Arc requires the `arc team register` CLI to add an
   agent (`agents.tsx:153`). Buzz creates from scratch / catalog / imported
   `.agent.json` snapshot / drag-drop, with Start/Stop/Respawn/Deploy-to-channel
   and a share dialog carrying a memory-inclusion picker.
5. **No global search.** Arc has per-page filters only; Buzz has one
   permission-aware index behind ⌘K.
6. **No mobile, no separate admin console.** 55k LOC of Dart and a moderation
   dashboard vs zero.
7. **No huddles, media-frame comments, PR review, custom emoji, moderation,
   notifications, reminders, invites, communities, channel templates, canvases.**
   Each is a shipped Buzz surface with no Arc counterpart.
8. **Accessibility debt.** 0.39 aria/file vs 1.7, effectively no `role=`, no
   reduced-motion, no System theme. Buzz targets WCAG 2.1 AA explicitly.

## (e) What Arc's UI does better

1. **Trust and governance are visible surfaces, not backend facts.**
   `approvals.tsx` (trifecta legs shown per blocked call), `gated-capabilities.tsx`
   (unsigned / invalid-signature / new-sighting quarantine), `policy.tsx` (ACE
   bullets + score distribution), `security.tsx` (audit trail, severity filters,
   payload drawer). Buzz has a hash-chain audit log server-side and **no UI for it
   at all**. For a federal buyer this is the whole evaluation.
2. **Cost is a first-class screen.** `arcllm.tsx` — token volume, cost by provider
   *and* by agent, model performance, circuit breakers, budgets. Buzz has no cost
   surface anywhere.
3. **Three approval gates vs one.** Arc gates trifecta-blocked tool calls,
   unsigned capabilities, *and* task review — all with live backends. Buzz gates
   one thing and its executor doesn't persist it (WF-08).
4. **Richer memory inspector.** `knowledge.tsx`: 6 tabs (Overview / Insights /
   Procedures / Entities / Daily Notes / Raw) + context-budget and graph stats.
   Buzz's is a read-only link-graph accordion.
5. **Config as a real screen.** `settings.tsx` edits arcllm/arcrun/arcagent TOML
   per-agent and system-wide from the browser.
6. **Spawn lineage.** `arcrun.tsx`'s `SpawnLineage` visualizes agent→sub-agent
   trees; Buzz has no equivalent.
7. **Operator-mode gating on every write** — a cleaner default-safe posture than
   Buzz's beta-warning modal.
8. **A TUI.** `arctui` runs in a SCIF over SSH with no browser. Buzz requires a GUI.

## (f) Concrete UX target spec for arcui

Ordered by strategic value ÷ build cost. Arc should **not** rebuild Slack. It
should make the fleet legible and governable, then add exactly the collaboration
primitives that make delegation feel like teamwork.

**Tier 1 — close the credibility gap (weeks)**
1. **Live agent activity panel** on `agents.tsx` / `agent-detail.tsx`: typed
   render rows (message / tool / thought / plan / lifecycle / error) + raw-event
   toggle, mutate-in-place, never-go-dark idle states. Adopt VISION_ACTIVITY.md's
   twelve render classes wholesale — it is a better spec than anything Arc has
   written. Reference: `ManagedAgentSessionPanel.tsx`, `activityRenderClasses/`.
2. **Presence with a "Working" state**, per-channel and per-task, replacing the
   binary dot: pulsing badge + "Working on {task} · 12s", 15s Starting… grace,
   error-severity coloring. Reference: `AgentStatusBadge.tsx:9-56`.
3. **Global ⌘K search** over runs, tasks, audit events, memory, tools, agents.
   Arc already stores all of it; only the index and the palette are missing.
4. **Emoji reactions + threads on `messages.tsx`.** Reactions are ~200 LOC and are
   the single strongest "colleagues work here" signal.
5. **Accessibility pass**: `aria-label` on every icon-only control, `role` on live
   regions, `prefers-reduced-motion` on pulsing dots, System theme. Target ≥1.5
   aria/file.

**Tier 2 — agent lifecycle in the UI (weeks)**
6. **Create / import / share an agent from the browser** — kill the
   `arc team register` dependency. Port Buzz's memory-inclusion picker and
   plaintext-exposure warning; that pattern maps directly onto a classification
   prompt for federal.
7. **Agent teams as a UI object** (bundle personas, deploy to a channel or task) —
   `arc team` already exists on the CLI.
8. **Channel/room templates that seed agents**, mirroring
   `useApplyTemplate.ts:57-154`. "Spin up an incident room with a triage agent and
   a comms agent" is a one-click federal demo.

**Tier 3 — the differentiated bet (months)**
9. **Unified Governance inbox** merging `approvals.tsx` + `gated-capabilities.tsx`
   + task review, with SLA/expiry, delegation, and decision **history** — Buzz's
   card renders `null` once decided; Arc must keep the record (an AU-family
   control).
10. **Audit-native everything**: make `security.tsx` the join point where every
    approval, policy denial, and tool call links back to its run and its signed
    chain entry. This is the surface Buzz structurally cannot match, and it is
    what a DOE evaluator opens first.
11. **Workflow authoring UI** (Form↔YAML) so approvals have something to gate —
    but wire the executor **first**. Buzz shipped the card before the executor and
    had to print an apology in its own UI (`WorkflowStepCard.tsx:31-37`); Arc's own
    producers-unwired history says do the opposite.
12. **Cost and policy as guardrails, not dashboards**: budget ceilings that block,
    circuit breakers that explain themselves, spend-limit hard stops in the run
    view.

**Explicitly do not build:** huddles/voice, media-frame comments, custom emoji,
forum, Pulse-style social feed, multi-tenant communities, mobile. That is Buzz's
consumer-collaboration surface area and none of it advances Arc's federal-harness
thesis.
