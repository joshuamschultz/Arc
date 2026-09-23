# ArcUI screen guide

This guide describes the screens currently available in ArcUI. Open a screen from the left navigation. An empty list can mean there is no data yet; a loading or error state means the screen could not retrieve it. Retry after checking the relevant service, then use the screen's visible error details or Audit when available. This guide is standalone documentation; contextual help is not yet built into ArcUI.

## Work

- **Home** (`/home`) — Fleet snapshot, recent activity and attention counts. Start by opening an item that needs you or select Fleet. The counts summarize data ArcUI can currently read; they are not a service-health guarantee. If values are missing, check Fleet and Activity directly.
- **Fleet** (`/agents`) — Registered agents and current presence. Select an agent to inspect its detail tabs. If the list is empty, register an agent with `arc team register`, then reload ArcUI. “Online” describes reported presence, not proof that every capability works.
- **Agent detail** (`/agents/:id`, `/agents/:id/:tab`) — Identity, configuration, recent sessions and performance for one agent. Choose the relevant tab and inspect recent activity. No sessions means none are recorded for that agent. If the agent is absent or stale, return to Fleet and check its registration and status.
- **Chat** (`/messages`) — Direct agent conversations and team channels. Select an agent or room, then send a message when the composer is ready. “Connecting…” means messaging is not ready; retry after connection returns. A successful send is not proof that a reply was delivered; check Activity for run evidence. The operator-mode control only reveals controls; server authorization still applies.
- **Tasks** (`/tasks`) — Fleet task board with owner/tag filters and task details. Start by selecting a task or narrowing the list. To add work, switch on Operator controls, select **New task**, enter a title, and optionally add a description, priority, owner and review requirement. Unowned tasks go to the fleet backlog; assigning an owner puts it in that agent’s to-do. The switch only reveals controls: the server still requires an authorized operator role. In-progress, done and failed counts summarize task records.

## Govern

- **Approvals** (`/approvals`) — Actions waiting for human sign-off. Open a request, review its target and reason, then use the displayed decision action. No pending approvals means none are currently listed; it does not mean all actions are permitted. If a decision is refused, check the reason and Audit; do not bypass policy.
- **Pending capabilities** (`/gated`) — Tools or skills held back by signature or policy checks. Review signer and status before using any displayed signing action. Unsigned items are not trusted. If approval is locked, use **Review source** and wait for the displayed source to match the artifact before approving. **Disapprove** withdraws trust. Never disable signature verification.
- **Rules** (`/policy`) — Fleet ACE policy bullets and their status/score. Search or sort to locate a rule and inspect its detail. Active and retired are rule lifecycle labels, not approval outcomes. If a rule has an unexpected effect, inspect related Audit events and have an authorized operator review it.
- **Audit** (`/security`) — Ordered signed control actions and policy denials. Select an event to inspect its actor, target, reason and time. An empty ledger means no events are available to this view. If an expected event is absent or the ledger reports an error, preserve the time and action details and escalate to the system operator; do not alter or discard audit data.

## Observe

- **Activity** (`/arcrun`) — Recorded agent runs and their action traces. Search and select a run to inspect steps. Runs appear as agents work; an empty list is not proof no request was attempted. If a chat has no corresponding run, check Chat delivery and the service logs through the deployment operator.
- **Workflows** (`/workflows`, `/workflows/:id`) — Named workflows and their node/schedule configuration. Open one to inspect it; create or edit only with authorized controls and review its trigger and response target before saving. A schedule shown here is configuration, not evidence of successful execution. If a run does not occur, inspect Activity and the workflow's displayed status; scheduled-run recovery is not guaranteed by this screen.
- **Knowledge** (`/knowledge`) — One agent's memories, entities, insights and graph. Select an agent, then use Overview or a focused tab. In Connections, open a source to inspect status, progress, last sync and error detail; configure selected resources, submit mapping approval, and use Initial sync, Retry, Pause/Resume, Reindex or Revoke. If the source list is empty, first confirm that the account is connected and granted to this agent. If it still has no discoverable source/resources, no UI repair action is available for that discovery failure; see the reliability program.
- **Shared knowledge** (`/shared-knowledge`) — Search fleet knowledge promoted by agents, grouped by owner. Search for a known phrase to confirm visibility. No matches means nothing ranked for that query; it does not prove the source is disconnected. Check the owning agent's Knowledge view and connection state.

## Advanced

- **Model usage** (`/arcllm`) — Request, token, latency and cost summaries by provider/agent. Choose the displayed time window or inspect the charts. No activity may mean no recorded calls in the window or unavailable telemetry. Compare with Activity before treating zero as proof that no call ran.
- **Tools & Skills** (`/tools-skills`) — Capability inventory and per-agent availability. Filter by agent, type or source, and search the tables. The **Import agent capabilities** panel accepts a signed skill ZIP for a selected agent, stages it inactive, shows review files, allows edits to staged files, and lets an authorized operator promote/sign or revoke the import. A review-ready import links to Pending capabilities for trust review. These controls do not provide an edit/re-sign flow for a capability that is already installed; use the staged import journey for a reviewed replacement. Inspect Activity and Audit for execution evidence.
- **Connections** (`/connections`) — Connector bundles, connected accounts, agent grants and connection checks. To begin, choose an available bundle and follow its connection flow; then grant only the agents that need it. Filled agent chips mean granted; outlined means not granted. Provider credentials are not displayed. Use **Doctor** for diagnostics, **Probe** for a live reachability check, and **Approve** to record the currently served tool contract. Depending on the connector, **Sign in**, **Connect**, **App key/secret**, or **Re-auth** renews access; Remove deletes the connection and stored credentials. For indexable sources, use each agent’s **configure & sync** action to open Knowledge > Connections. Select resources or categories, save the scope, request mapping approval, then approve or deny the mapping. A degraded Knowledge module can be enabled from the connection card by an authorized operator. A connector marked non-indexable shows its reason.

## System

- **Settings** (`/settings`) — System (`~/.arc`) or selected-agent configuration, provider key presence and configuration files. Choose a scope, then a tab. System values are fleet defaults; agent values layer over them. Provider credentials are shared fleet-wide and are never shown back—only whether a provider credential is set. Agent identity keys are separate and are not shared through this panel. Edit only if authorized, save, then inspect the relevant screen. If a provider is unavailable, confirm key presence and configuration; never paste credentials into chat or logs. The operator-mode switch only reveals controls and does not grant server privileges.

## Current limits and safe recovery

ArcUI does not yet provide customer queue inspection/control or a safe HTML report viewer. The capability import panel stages, reviews, edits and signs imports; editing an already-installed skill revision and re-signing/reloading it is not exposed as a separate workflow. Connected-source controls support resource selection, mapping approval, sync, retry, pause/resume, reindex and revoke. Recovery when source/resource discovery itself is empty is not exposed in the UI. These gaps are tracked in the [business reliability program](../../design/business-reliability-program.md). Do not use filesystem edits or unsigned artifacts as a workaround. If a screen reports an error, note the screen, time and non-sensitive status text; use the displayed retry/action if available, then check related Activity or Audit and contact the deployment operator for service-level recovery. Never disable authorization or signature checks.

## Route map

Routes are defined in `packages/arcui/web/src/app/router.tsx`; visible names and groups are in `packages/arcui/web/src/app/nav.ts`.

| Screen | Route |
|---|---|
| Home | `/home` |
| Fleet | `/agents` |
| Agent detail | `/agents/:id` and `/agents/:id/:tab` |
| Chat | `/messages` |
| Tasks | `/tasks` |
| Approvals | `/approvals` |
| Pending capabilities | `/gated` |
| Rules | `/policy` |
| Audit | `/security` |
| Activity | `/arcrun` |
| Workflows | `/workflows` and `/workflows/:id` |
| Knowledge | `/knowledge` |
| Shared knowledge | `/shared-knowledge` |
| Model usage | `/arcllm` |
| Tools & Skills | `/tools-skills` |
| Connections | `/connections` |
| Settings | `/settings` |
