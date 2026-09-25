# ArcUI screen guide

Open a screen from the left navigation. An empty list can mean there is no data yet; a loading or error state means the screen could not retrieve it. Retry only after checking the relevant service, and check Activity or Audit before repeating an operation whose result is uncertain. Field-level help is authored in [`screen-help.json`](../../../packages/arcui/web/src/content/screen-help.json); Settings matches exact serialized paths first, then `*` patterns covering one path segment. The catalog currently has 622 help entries across 18 routes (528 Settings entries); the count does not mean every field is rendered in every installation. Some help IDs are contextual labels rather than config paths; see the [field-help inventory](../../design/arcui-field-help-inventory.md).

## Work

- **Home** (`/home`) — Fleet snapshot, recent activity and attention counts. Start by opening an item that needs you or select Fleet. The counts summarize data ArcUI can currently read; they are not a service-health guarantee. If values are missing, check Fleet and Activity directly.
- **Fleet** (`/agents`) — Registered agents and current presence. Select an agent to inspect its detail tabs. If the list is empty, register an agent with `arc team register`, then reload ArcUI. “Online” describes reported presence, not proof that every capability works.
- **Agent detail** (`/agents/:id`, `/agents/:id/:tab`) — Identity, configuration, recent sessions and performance for one agent. Choose the relevant tab and inspect recent activity. No sessions means none are recorded for that agent. If the agent is absent or stale, return to Fleet and check its registration and status.
- **Chat** (`/messages`) — Direct agent conversations and team channels. Select an agent or room, then send a message when the composer is ready. “Connecting…” means messaging is not ready; retry after connection returns. A successful send is not proof that a reply was delivered; check Activity for run evidence. The operator-mode control only reveals controls; server authorization still applies.
- **Tasks** (`/tasks`) — Task board with status, priority, owner and tag filters and task details. The API returns bounded pages (100 by default, maximum 200) across active and history phases. The UI currently loads the head plus the selected cursor page; its status/priority counts, owner/tag options, blocked/inbox/backlog metrics, and filters are computed locally from those loaded rows. They are page-local and may omit matching tasks on other pages. “Load more” advances the cursor; it does not make those local counts global. Task data uses the configured ArcStore task backend; fleet messaging is not required to display the board, though task-owner messaging requires its messaging path. To add work, switch on Operator controls, select **New task**, enter a title, and optionally add description, priority, owner and review requirement. `task.owner` routes to that agent's to-do; blank owner leaves the fleet backlog. `task.requires_review` sends completion to review; `task.status` changes the lane. Check the task record and Activity before recreating or reassigning work.

## Govern

- **Approvals** (`/approvals`) — Actions waiting for human sign-off. Each request is already expanded: review the requester, requested tool, arguments, gate and reason, then use Approve or Deny. The screen does not collect a separate decision reason. No pending approvals means none are currently listed; it does not mean all actions are permitted. If a decision is refused, check the reason and Audit; do not bypass policy.
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
- **Tools & Skills** (`/tools-skills`) — Capability inventory and per-agent availability. Filter by agent, type or source, and search the tables. The import panel accepts a signed skill ZIP for a selected agent, stages it for review, and allows edits to staged files before authorized promotion/signing or revocation. A review-ready import links to Pending capabilities for trust review. Editing/re-signing an already-installed capability is not available here. Inspect Activity and Audit for execution evidence.
- **Connections** (`/connections`) — Connector bundles, connected accounts, agent grants and connection checks. Choose an available bundle and follow its connection flow, then grant only needed agents. Filled agent chips mean granted; outlined means not granted. Credential fields such as `connection.access_token` and `connection.secret` are masked and not shown after save; `connection.account_name` labels the account, and `connection.endpoint` changes the target service when that connector exposes it. Use **Doctor** for diagnostics and **Probe** for live reachability. **Approve** records the currently served tool contract. Re-authenticate when access expires; Remove deletes the connection and stored credentials. For indexable sources, use **configure & sync** to open Knowledge > Connections. Select resources, save scope, request mapping approval, then approve or deny. Initial sync/Reindex rebuild index state; Retry resumes eligible failed work; Pause/Resume changes scheduling; Revoke withdraws the source grant. If discovery returns no sources/resources, these controls cannot repair discovery; use the reliability escalation. A connector marked non-indexable should show its reason.

## System

- **Settings** (`/settings`) — System (`~/.arc`) or selected-agent configuration, provider key presence and configuration files. Choose a scope, then a tab. System values are fleet defaults; agent values layer over them. Provider credentials are shared fleet-wide and are never shown back—only whether a provider credential is set. Agent identity keys are separate and are not shared through this panel. Edit only if authorized, save, then inspect the relevant screen. If a provider is unavailable, confirm key presence and configuration; never paste credentials into chat or logs. The operator-mode switch only reveals controls and does not grant server privileges.

## Current limits and safe recovery

Customer queue inspection/control, anchored skill revision editing/re-signing, and HTML report viewing are unavailable. Connected-source controls cover resource selection, mapping approval, sync, retry, pause/resume, reindex and revoke once a source has been discovered. Empty source/resource discovery has no UI repair action. These gaps are tracked in the [business reliability program](../../design/business-reliability-program.md). For configuration, `settings.scope` selects System defaults or an agent override; editing a scalar under `settings.<file>.<path>` changes the serialized config value at that path, and an agent override can supersede the fleet default. `settings.provider_key` identifies the masked provider-key control; saved values are not shown. After saving a config change, revisit the affected screen and inspect Activity/Audit if behavior changes unexpectedly. If a screen reports an error, note screen, time and non-sensitive status; use a displayed retry once, then check Activity/Audit and contact the deployment operator for service-level recovery.

## Field effects and safe recovery

Help describes what a field changes; use the following recovery checks before editing or repeating work:

| Existing control | Effect | Recovery check |
|---|---|---|
| Tasks: `task.owner`, `task.status`, `task.requires_review` | Owner routes work to an agent's to-do (blank leaves fleet backlog); status moves it between board lanes; review requirement sends completion to operator review. | Board filters, facets and metrics use only the head and currently selected cursor page. Use the task record or another authorized data view to confirm a task beyond those rows; continuation alone does not make page-local counts global. Reload and check Activity before recreating work. |
| Workflows: `workflow.trigger.type`, `workflow.trigger.cron`, `workflow.trigger.interval`, `workflow.trigger.not_before`, `workflow.trigger.not_after`, `workflow.trigger.timezone`, `workflow.response_target` | Trigger fields set start mode and schedule window; response target selects where a workflow reply goes. | Configuration is not execution evidence. Check workflow status and Activity; avoid saving a duplicate schedule as a retry. |
| Knowledge: `knowledge.agent`, `knowledge.source`, mapping/resource controls | Agent/source selection scopes inspection and sync; selected resources and approved mappings determine indexed content. | Confirm grant and source discovery first. Retry failed sync only after inspecting its status; Reindex rebuilds from the selected scope; Revoke withdraws access. |
| Connections: `connection.agent_grant`, `connection.authorization_code`, `connection.access_token`, `connection.secret`, `connection.endpoint` | Grants agent access; code/token/secret establish connector authorization; endpoint selects its service target. | Run Doctor/Probe. Confirm the account and agent grant, and use the connector's re-auth flow when authorization is invalid. |
| Connections (Google): `connection.google_account`, `connection.google_client`, `connection.google_access`, `connection.google_sign_in`, `connection.sign_in_address` | The account a connection reads, the operator's own OAuth client name, read-only versus draft-and-send access, the Open Google sign-in step, and the pasted address from the browser bar. | Status reads Working, Reconnect needed, Not signed in or Not installed. Reconnect: Open Google sign-in, sign in as the shown account, paste the address the browser lands on. A blank client warns that sign-ins expire in about 7 days. See [Google accounts](google-accounts.md). |
| Settings: `settings.arcrun.max_turns`, `settings.arcrun.tool_timeout` | Raises/lower run turn ceiling and per-tool wait ceiling, affecting run duration and resource occupancy. | Restore a known-valid value if runs stall or consume excess resources; inspect Activity before retrying timed-out work. |
| Settings: `settings.arcagent.telemetry.capture_tool_io`, `settings.arcagent.telemetry.exporter_endpoint` | Controls whether raw tool I/O is persisted and where traces are exported. | Treat captured arguments/results as sensitive. If export fails, check endpoint/service readiness and follow the configured exporter schema for credential or secret references. |

Availability note: hosted queue controls, anchored skill revisions, and report viewing are pending; see the [execution ledger](../../design/business-reliability-execution.md) for current status.

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
