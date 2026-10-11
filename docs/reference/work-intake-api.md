# Work intake and events API

> **Reference**  ·  Look up  
> **For** Apps that start Arc work and listen for results  
> [Docs home](../README.md)  ·  [App principals](../concepts/app-principals.md)

An outside app can start an agent run, fire a workflow event, create a task, decide
a gate, and watch for results. This page lists each route, its scope, and how to
verify a callback.

All routes are on the dashboard API (arcui). An app calls them as an
[app principal](../concepts/app-principals.md). A signed-in operator can call every
route too.

## Scopes per route

A call passes only when the app holds the scope **and** the scope is bound to the
agent the route targets. Each handler finds its agent itself: the request body, the
run's agent, the workflow owner, or the `agent` query value.

| Route | Scope |
|---|---|
| `POST /api/runs` | `runs:start` |
| `GET /api/runs/{run_id}` | `runs:read` |
| `GET /api/runs/{run_id}/timeline` | `runs:read` |
| `GET /api/runs/{run_id}/audit` | `runs:read` |
| `GET /api/runs/{run_id}/recalls` | `runs:read` |
| `POST /api/team/tasks` | `tasks:write` |
| `POST /api/workflows/{id}/events/{name}` | `workflows:event` |
| `POST /api/workflow-tasks/{id}/gate` | `gates:resolve` |
| `GET /api/work-events/stream` | `events:subscribe` |
| `GET /api/work-events` | `events:subscribe` |
| `GET /api/work-events/keys` | `events:subscribe` |
| `/api/work-events/callbacks` (all) | none for apps. Operator only |

`GET /api/runs` (the list) is not open to apps. An app can start a run and read
its own, but it cannot list runs.

## Start a run

`POST /api/runs`

Records one run request for one agent. The agent's tasks module must be on with
`dispatch = true`. Its dispatch loop then runs the task under the returned `run_id`.

Headers:

- `Content-Type: application/json`
- `Idempotency-Key`: 1 to 128 characters from `A-Z a-z 0-9 . _ : -`. Required.

Body (64 KiB at most). No other fields are allowed.

| Field | Meaning |
|---|---|
| `agent` | Required. Agent handle or DID |
| `task` | Required. The work to do |
| `metadata` | Optional object. At most 16 KiB and 8 levels deep |
| `correlation_id` | Optional. Same character set as the key. Comes back on events |
| `run_purpose` | `manual` (default) or `message` |
| `callback_url` | Optional. Its origin must be on the callback allowlist |

Reply `202`:

```json
{
  "run_id": "…",
  "status": "queued",
  "links": {
    "run": "/api/runs/…",
    "timeline": "/api/runs/…/timeline",
    "events": {"stream": "/api/work-events/stream?agent=…&run_id=…"}
  }
}
```

- The `run_id` comes from the caller and the key. The same request with the same
  key returns `200` with `"replayed": true`. A different request with the same key
  is `409 idempotency_conflict`. Two callers never share a `run_id`.
- If callbacks are not set up, `links.events.notifications` is `"unavailable"`.

Errors: `400` (`invalid_idempotency_key`, `invalid_body`, `invalid_request`,
`callback_not_allowed`, `invalid_task`), `403`, `404 agent_not_found`,
`409 agent_not_accepting_runs`, `413` (`body_too_large`, `metadata_too_large`),
`503 notifications_unavailable`. Every outcome writes one `run.intake` audit row.

## Read a run

`GET /api/runs/{run_id}`

Returns `run_id`, `agent_did`, `status`, `correlation_id`, `run_purpose`,
`metadata`, `result`, `error`, `created_at`, `started_at`, `completed_at`.

`status` is one of `queued`, `running`, `done`, `failed`, `cancelled`.

An app that lacks `runs:read` on the run's agent gets the same `404` as for a run
that does not exist. It cannot learn which runs exist.

### Scoped reads

These three routes work for any run. An app needs `runs:read` on the run's agent.
For a run started with `POST /api/runs`, that is the intake agent. For another run,
the trace must name exactly one agent. Anything else is the same `404`.

| Route | Returns |
|---|---|
| `GET /api/runs/{run_id}/timeline` | `{"run_id", "timeline": [...]}`: tool, code, LLM and run events in order |
| `GET /api/runs/{run_id}/audit` | `{"run_id", "events": [...]}`: audit rows whose causal chain names the run |
| `GET /api/runs/{run_id}/recalls` | `{"events": [...]}`: memory recall cards for the run |

A malformed `run_id` is `400`.

## Create a task

`POST /api/team/tasks`

Scope `tasks:write`, bound to the `owner_did`. A task with no owner binds no agent,
so an app is refused.

Body: `title` (required), and these optional fields:

| Field | Meaning |
|---|---|
| `description`, `priority`, `owner_did`, `tags`, `requires_review` | As on the task board |
| `metadata` | A JSON object. Stored under `metadata.app`. The caller cannot write any other metadata key |
| `parent_id` | An existing task. Checked on create |
| `blocked_by` | A list of existing tasks. Checked on create |
| `due_at` | ISO 8601 time with a time zone. A time without a zone is refused |
| `classification` | A label. Refused above the deployment clearance. Strict names at federal |

A refused value is `400` with the reason. A good create is `201` with the task and
an `owner_notification` field. `parent_id` and `blocked_by` are set at create only.

## Fire a workflow event

`POST /api/workflows/{id}/events/{name}`

Scope `workflows:event`, bound to the workflow's owner agent. The workflow must
declare the event in its signed document. See
[Workflows](../get-started/workflows.md#events-start-a-run-from-outside).

- Header `Idempotency-Key` is required (`400 idempotency_key_required`).
- Body is a JSON object, at most 64 KiB (`413 payload_too_large`). It is checked
  against the event's schema.
- Limit: 60 events a minute per principal and workflow (`429 rate_limited`).
- Reply `201` for a new run, `200` for a repeat of the same key and payload. The key
  is scoped to the caller. The run id comes from the workflow, event, caller and key.
- `404` unknown workflow or event. `409` same key, other payload. `422` names every
  failing payload field. An unsigned workflow is refused at every tier.

## Decide a gate or human input

`POST /api/workflow-tasks/{id}/gate`

Scope `gates:resolve`, bound to the gate's owner agent. An app on an unknown gate
gets `404`. Tier rules for named deciders apply. See
[App principals](../concepts/app-principals.md#acting-for-a-person).

Body (128 KiB at most in all):

```json
{"decision": "approve", "notes": "ok", "payload": {"amount": 40}}
```

- `decision`: required. For a gate: `approve`, `fail_run`, `return_for_revision`.
  For `human_input`: `submit` or `fail_run`.
- `notes`: optional string.
- `payload`: optional object, at most 64 KiB (`413`). It is checked against the
  node's `output_schema`. A bad payload is `422` with one issue per field, and the
  step keeps waiting.

The result is the output envelope. A later node reads it as
`$nodes.<id>.output.payload.<field>`. See
[the envelope](../get-started/workflows.md#the-output-envelope). The control plane
also checks the decider against the node's `approvers`. A refusal there is `403`.

## Work events

A work event says that something happened. It holds **pointers only**. It never
holds a result or a payload. Fetch those through the scoped reads above.

| Kind | When |
|---|---|
| `run.finished` | A run finished |
| `run.failed` | A run failed |
| `gate.waiting` | A gate is waiting for a person |
| `human_input.waiting` | A human input step is waiting |
| `gate.escalated` | A deadline passed and the step moved to `escalate_to` |

Envelope fields: `event_id`, `kind`, `occurred_at`, `agent_did`, `workflow_id`,
`run_id`, `node_id`, `task_id`, `correlation_id`, `status`, `classification`, and
`links`. The stream and the page add `seq`. `links` holds relative paths: `run`,
`timeline` (for runs started through `POST /api/runs`), and `gate` (on a waiting or
escalated gate).

Only events at or below the deployment clearance are sent. An unknown label is never
sent.

The three read routes below take the same query values:

| Query | Meaning |
|---|---|
| `agent` | Required for an app, and must be bound to its scope. A signed-in session may omit it to see every agent |
| `run_id`, `correlation_id` | Filters |
| `kinds` | Comma list of kinds |

### Stream

`GET /api/work-events/stream`

Server-Sent Events. Each frame:

```
id: 42
event: run.finished
data: {…envelope…}
```

- **Resume** with the `Last-Event-ID` header. You get events after that `seq`.
  A bad value is `400 invalid_last_event_id`.
- **Heartbeat:** a `: keepalive` comment at least every 15 seconds.
- **Limit:** 4 open streams per principal. The next one is `429 too_many_streams`.
- **Lifetime:** a stream ends after one hour. Reconnect with `Last-Event-ID`; the
  reconnect is authorized again, so an app that lost its scope gets no more events.
- Opening and closing a stream are each audited.
- `503 work_events_unavailable` when the event log is absent.

### Page

`GET /api/work-events?after=<seq>&limit=<n>`

For clients that cannot hold a stream. Returns
`{"events": [...], "next_after": <seq>}`. The default `limit` is 100. The most it
returns is 1000. Pass `next_after` as the next `after`.

### Verify key

`GET /api/work-events/keys`

Returns `{"key_id", "algorithm", "public_key"}` with the key in base64. It is a
dedicated work-events key, never the operator key. `503
work_events_key_unavailable` if none exists.

`arc init` mints this key. A deployment set up before it has none, and the deploy
scripts skip `arc init` on a box that already has config. Mint it once with
`arc callback key` (safe to re-run; it never replaces a key), then `arc restart`.

## Callbacks

Give `callback_url` on `POST /api/runs` and Arc sends a signed `POST` to it for that
run's events.

### The allowlist

A callback origin (`scheme://host[:port]`) must be on the allowlist first. Only a
signed-in operator manages it. App principals and static tokens are refused.

| Route | Use |
|---|---|
| `GET /api/work-events/callbacks` | List entries |
| `POST /api/work-events/callbacks` | Body: `origin`, `allow_private` (default false), `clearance` (default `UNCLASSIFIED`) |
| `POST /api/work-events/callbacks/{id}/remove` | Remove. Callbacks to it are refused from the next check |

CLI:

```bash
arc callback add https://hooks.example.com [--allow-private] [--clearance LEVEL]
arc callback list
arc callback remove <id>
arc callback key        # mint the work-events key if absent; prints its key id
```

By tier:

- Personal: active at once.
- Enterprise: `pending` until an operator runs `arc approve <id>`.
- Federal: the CLI refuses. Add the origin in the dashboard as a signed-in
  operator. A second operator approves it.

An event above the origin's `clearance` is never sent there.

### Delivery

Arc re-checks the allowlist on every attempt. It resolves the host once and refuses
private addresses unless `allow_private` is set. It follows no redirects and waits
10 seconds.

A non-2xx answer, a timeout, or a refusal is a failed attempt. Retries start at
30 seconds and double each time, up to 1 hour: 30, 60, 120, … 3600 seconds. After
10 attempts the delivery is dead. Arc audits it and tells the operator.

### Verify a callback

Headers on each call:

| Header | Value |
|---|---|
| `Arc-Event-Id` | The event id |
| `Arc-Timestamp` | Unix seconds, as text |
| `Arc-Signature` | `<algorithm>=<base64 signature>` |
| `Arc-Key-Id` | Which key signed |

The signed bytes are four parts joined by a newline:

```
b"arc.work-event.v1\n" + timestamp + b"\n" + event_id + b"\n" + sha256(body).hexdigest()
```

`timestamp` is the `Arc-Timestamp` value as sent. `event_id` is `Arc-Event-Id`.
`body` is the raw request bytes. The digest is lowercase hex text.

Steps for a receiver:

1. Read the raw body. Do not re-serialize it.
2. Get the public key from `GET /api/work-events/keys`. Match `Arc-Key-Id` to its
   `key_id`. Cache it.
3. Build the signed bytes. Check the signature.
4. Reject an old `Arc-Timestamp`. Pick a window that fits your clock.
5. **Dedupe on `Arc-Event-Id`.** A retry sends the same id. Answer `2xx` for one you
   already handled.
6. The body holds pointers only. Fetch results with the scoped reads.

The `algorithm` is `ed25519` by default and `ecdsa-p256` on a FIPS deployment. An
Ed25519 key is the raw public key. An ECDSA key is DER SPKI.

Python sketch for Ed25519:

```python
import base64
import hashlib

from nacl.exceptions import BadSignatureError
from nacl.signing import VerifyKey


def verify(body: bytes, headers: dict[str, str], public_key_b64: str) -> bool:
    _algorithm, _, signature = headers["Arc-Signature"].partition("=")
    message = b"\n".join(
        (
            b"arc.work-event.v1",
            headers["Arc-Timestamp"].encode("ascii"),
            headers["Arc-Event-Id"].encode("ascii"),
            hashlib.sha256(body).hexdigest().encode(),
        )
    )
    try:
        VerifyKey(base64.b64decode(public_key_b64)).verify(
            message, base64.b64decode(signature)
        )
    except BadSignatureError:
        return False
    return True
```
