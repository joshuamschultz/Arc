# SPEC-064 — the web contract

Frozen before either side is written, so the routes and the page are built against
the same shapes rather than against each other.

Every path below is under arcui's existing `AuthMiddleware`: a valid token is
required, `request.state.role` is `viewer` or `operator`. **Every mutation is
operator-only** (403 otherwise), matching `agent_detail/config_files.patch_config_file`.

Bodies are capped at 64 KB (`_MAX_BODY_BYTES`), matching the config editor.

---

## Keys — `~/.arc/.env`, fleet-wide

### `GET /api/keys`

```json
{
  "keys": [
    { "provider": "anthropic", "env_var": "ANTHROPIC_API_KEY", "required": true, "present": true },
    { "provider": "openai",    "env_var": "OPENAI_API_KEY",    "required": true, "present": false }
  ]
}
```

`present` is the whole answer. There is no field carrying a value, a prefix, a
length, or a hash — a surface that cannot display a key cannot leak one (D-583).
Readable by `viewer`.

### `PUT /api/keys/{env_var}`

Body: `{ "value": "sk-..." }` — operator only.

- 400 when `env_var` is not declared by any provider arcllm knows.
- 400 when `value` is empty or contains a newline.
- 200 → `{ "env_var": "...", "present": true }`. The value is never echoed.

The value must not appear in a log line, a trace, or an error message.

### `DELETE /api/keys/{env_var}`

Operator only. 200 → `{ "env_var": "...", "present": false, "removed": true|false }`.

---

## Connectors

### `GET /api/connectors/catalog`

What this deployment could connect, read from the extension search path.

```json
{
  "available": [
    {
      "name": "jira",
      "version": "1.0.0",
      "description": "Jira Cloud issues, projects, and comments.",
      "attachment": "native",
      "tier_floor": "personal",
      "approval_default": "outbound",
      "secrets": [{ "name": "api_token", "prompt": "Jira Cloud API token ..." }],
      "host_requires": [{ "name": "gog", "instruction": "brew install ..." }],
      "tools": [
        { "name": "jira_search_issues", "description": "...", "classification": "read_only", "capability_tags": [] }
      ],
      "root": "/Users/x/.arc/extensions"
    }
  ],
  "unreadable": [{ "name": "brokenbundle", "reason": "manifest did not parse" }]
}
```

A bundle whose manifest will not parse appears in `unreadable` — it must not
disappear silently, and it must not 500 the whole listing.

Readable by `viewer`.

### `GET /api/agents/{id}/connectors`

```json
{
  "instances": [
    { "instance": "work", "extension": "jira", "approval": "outbound" }
  ],
  "extensions_root": "/Users/x/arc/team/coder_agent/extensions"
}
```

Readable by `viewer`.

### `POST /api/agents/{id}/connectors`

Operator only. Body:

```json
{ "extension": "jira", "instance": "work", "secrets": { "api_token": "...", "email": "...", "base_url": "..." } }
```

Drives `arcagent.modules.connectors.install.plan_connector` then `install_connector` —
the same functions `arc connector add` calls. Nothing about the ordering, the
verification, or the rollback is re-derived here.

- 400 with `{ "error": ..., "unsatisfied_host": [{ "name", "instruction" }] }` when the
  host lacks a prerequisite. The operator installs it; arcui never does.
- 403 for viewer.
- 409 when `instance` already exists.
- 422 when a declared secret is missing from the body.
- 200 → `{ "instance", "extension", "tools": ["..."], "detail": "..." }`

Secret values are consumed and dropped. They are not returned, not logged, and not
included in any error the route raises.

### `PUT /api/agents/{id}/connectors/{instance}/auth`

Operator only. Body `{ "secrets": { ... } }` — re-supply credentials (rotation).
200 → `{ "instance": "...", "updated": ["api_token"] }` (field names only).

### `POST /api/agents/{id}/connectors/{instance}/probe`

Operator only (it opens a live connection). 200 → `{ "reachable": true, "detail": "...",
"tools": [{ "name", "description", "classification", "capability_tags" }] }`.

### `GET /api/agents/{id}/connectors/{instance}/doctor`

Readable by `viewer`. 200 → `{ "checks": [{ "check": "...", "status": "...", "detail": "..." }] }`
— the same rows `arc connector doctor` prints.

### `POST /api/agents/{id}/connectors/{instance}/approve`

Operator only. Records the tool contract served right now (rug-pull defense, REQ-291).
200 → `{ "instance": "...", "approved": ["jira_search_issues", "..."] }`.

### `DELETE /api/agents/{id}/connectors/{instance}`

Operator only. 200 → `{ "instance", "removed_secrets": ["..."], "removed_config": true,
"removed_state": true }`.

---

## Errors

Every failure uses the existing `ErrorResponse` shape: `{ "error": "message" }`, plus
the extra keys named above where a surface needs them to act. An `ExtensionError`
becomes a 400 carrying its `message` — those messages are written for operators and
never carry credential material.
