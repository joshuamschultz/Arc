# Arc business reliability operations

Use this guide to distinguish an ArcUI display problem from a service capability outage and to collect safe evidence for the deployment operator. It describes current behavior; deployment topology and service-manager commands are environment-specific.

## Check readiness

From an authenticated operator browser session, open `/api/health` and `/api/ready` on the ArcUI origin. `/api/health` means the UI process answers and reports its bundle identifier. It does not check data stores, fleet messaging, or workflows. `/api/ready` reports `store`, and when configured as required, `fleet` and `workflow`. HTTP 200 with `status: ready` means the required probes passed at that moment; HTTP 503 with `status: degraded` identifies a component reported unavailable. The response intentionally omits endpoints, exception text, and credentials. It is a bounded point-in-time check, not proof of end-to-end user actions, provider availability, or future reliability.

Readiness checks ArcStore with a safe cursor read. When fleet is required it checks the broker's JetStream account readiness and the messaging service/backend availability. When workflows are required it checks for an available runner and control plane. A standalone configuration that does not require fleet or workflow omits those components. Liveness remains separate: a process can be alive while readiness is degraded.

## Respond to a degraded component

1. Record the UTC time, HTTP status, component labels, ArcUI screen, and non-sensitive error/status text. Do not copy credentials, tokens, message bodies, or private arguments into a ticket or log excerpt.
2. Retry the relevant screen's documented refresh/retry action once the component reports ready. Use Activity and Audit to determine whether an operation was recorded before attempting a potentially consequential operation again.
3. For fleet messaging, an owned broker or configured ArcTeam NATS client retries connection. ArcAgent's inbox subscription retries and readiness falls when its active connection/subscription is unavailable. A recovery can restore new delivery without restarting the agent. An accepted asynchronous turn with uncertain delivery is not automatically replayed; verify Activity and the thread before retrying to avoid duplicate work.
4. For workflows, ArcUI supervises the runner and retries startup/replacement after failure. Check workflow status and Activity after readiness returns; a configured schedule is not evidence that a run occurred.
5. If readiness stays degraded, preserve the component label and timestamps and ask the deployment operator to inspect service logs and host health. Use that environment's service manager and documented deployment procedure. Do not broadly restart services, delete state, edit signed files, or bypass authorization/signature checks.

Readiness and screen-level settings answer different questions: `/api/ready` checks required service probes, while saved UI values describe configuration. For example, `settings.arcrun.max_turns` and `settings.arcrun.tool_timeout` change run/tool limits but cannot repair a degraded store or broker. `settings.arcagent.telemetry.exporter_endpoint` selects a trace destination; it does not prove that destination is reachable. Check readiness and the relevant screen after changing a setting, then use Activity/Audit to confirm the effect.

## Fleet and task limits

The Tasks API retrieves bounded active and history pages (100 by default; at most 200 per request) with a continuation cursor. The screen currently combines the head page with only the selected cursor page; its owner/tag filters, status/priority facets, and task/inbox/blocked/backlog metrics are computed locally from those loaded rows. They may omit tasks on other pages, and continuation does not turn them into global counts. Task display depends on the configured ArcStore task backend; fleet transport is not required for the board, but is needed for messaging the owner. If the board fails, inspect ArcStore readiness and the screen error. A status change can move a row between phases; reload and query the relevant page before concluding it is missing.

The current fleet reconnect behavior restores transport and subscription availability, but it is not a durable outbox for accepted work or replies. A transport failure after acceptance may leave an uncertain outcome; inspect the run and conversation before retrying. Do not infer that an empty inbox sweep means no older unanswered messages exist.

On Tasks, changing `task.owner` routes the item to an agent's to-do, leaving it empty retains the fleet backlog, and `task.requires_review` sends completion into review. A status change can move the row between active and history pages; confirm the record and inspect Activity before recreating it. On Workflows, `workflow.trigger.type`, `workflow.trigger.cron`, `workflow.trigger.interval`, `workflow.trigger.not_before`, `workflow.trigger.not_after`, and `workflow.trigger.timezone` set trigger mode and schedule window; `workflow.response_target` changes the reply destination. Saving a trigger is not evidence it ran: check workflow status and Activity before attempting a manual duplicate. In Knowledge, the selected agent/source, resource scope, and approved mapping define what sync can index; Retry addresses failed sync work, Reindex rebuilds from current scope, and Revoke removes the source grant. None of these operations recovers an undiscovered source.

The [ArcUI screen guide](arcui-screen-guide.md) maps current routes to their controls, effects, recovery checks, and the availability note for known UI gaps.

## Escalation evidence

Include deployment/version identifier, UTC onset and recovery times, readiness component labels, affected screen/action, whether the action appeared in Activity or Audit, and whether the issue recovered without a restart. Redact all secrets and private message or document content. Never place operator tokens on a command line or include process arguments in a support bundle; use the authenticated operator workflow for credential rotation.
