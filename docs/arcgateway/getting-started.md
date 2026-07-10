# Getting Started with arcgateway

arcgateway connects ArcAgent to real-time messaging platforms (Telegram, Slack) with per-(user, agent) session routing and tier-aware execution isolation.

**The gateway runs embedded inside `arc ui start`** — one process serves
the dashboard, web chat, and every enabled remote platform, and it's the
**only** working agent-execution path today, at every tier. The
standalone `arcgateway start` daemon unconditionally refuses to start
rather than silently serve no real agent (or the wrong one):

- Personal/enterprise tier's `AsyncioExecutor` has no agent_factory when
  built standalone — echo stub only.
- Federal tier's `SubprocessExecutor` spawns `arc-agent-worker`, which
  ignores the requested `agent_did` for config selection entirely — it
  always reads the same fixed-location `arcagent.toml` regardless of which
  agent a platform message was addressed to. It cannot correctly serve a
  multi-agent_did gateway (tracked as a federal-blocking bug — see task
  tracker). Federal tier is **not** an exception here, despite
  `SubprocessExecutor` otherwise being the intended isolation model — see
  [security.md](./security.md) for that model's *design*, which this bug
  currently prevents standalone from delivering.

`arcgateway start` still resolves `gateway.toml` via `GatewayConfig.load()`
first (ARC_CONFIG_DIR-aware) purely so its refusal message reflects the
real discovered config — it never actually builds or runs anything.
`arcgateway stop`/`status` are unaffected and remain fully functional
(PID/health management for a daemon started before this change). Use the
embedded path for every real deployment:

```
arc ui start --team-root <dir> --gateway-config ~/.arc/gateway.toml
```

— covered in full by [../deploy/single-node.md](../deploy/single-node.md)'s
validated runbook.

## Install

```bash
pip install arcgateway arccmd
```

Dev workspace (editable, all sibling packages):

```bash
uv pip install -e packages/arcgateway -e packages/arccli -e packages/arcagent -e packages/arcllm -e packages/arcrun
```

Remote platform adapters (Telegram, Slack, Mattermost) are separate
packages discovered via the `arcgateway.adapters` entry-point group.
`arcgateway-telegram` is a root `pyproject.toml` dependency (alongside
`arcmemory`/`arcskill`, pinned for the same reason: the default extension
must load out of the box) — a bare `uv sync` at the workspace root
installs it, no extra step needed. Slack and Mattermost aren't pinned yet;
install those the same way if you need them:

```bash
uv pip install -e packages/arcgateway-slack --no-deps
```

If an enabled platform's adapter package isn't installed, the gateway logs
a warning and skips it rather than failing to start.

## Minimal config

Write `~/.arc/gateway.toml`:

```toml
[gateway]
tier = "personal"                     # personal | enterprise | federal
agent_did = "did:arc:local:executor/..."   # the agent DM traffic routes to

[security]
require_pairing = false               # see "DM pairing" below

[platforms.web]
enabled = true                        # required if you pass --gateway-config explicitly

[platforms.telegram]
enabled = true
token_env = "TELEGRAM_BOT_TOKEN"
allowed_user_ids = [123456789]

[platforms.slack]
enabled = false
bot_token_env = "SLACK_BOT_TOKEN"
app_token_env = "SLACK_APP_TOKEN"
allowed_user_ids = ["U0ABC123"]
```

The `GatewayConfig` Pydantic model (see `arcgateway/config.py`) loads this via `GatewayConfig.from_toml(path)`. Note `token_env` for Telegram — `bot_token_env` is Slack's field name; the two platforms don't share a key name. `arc init`'s TOML generator now writes the correct field for each platform (previously wrote `bot_token_env` for Telegram too, silently dropped since both models use `extra="ignore"` — fixed).

## Telegram

1. Create a bot via [@BotFather](https://t.me/BotFather) → `/newbot` → save token
2. Put the token in `~/.arc/arc.env` as `TELEGRAM_BOT_TOKEN=...` (never inline it in `gateway.toml`)
3. Add your Telegram user id to `allowed_user_ids` — this is the authorization gate that's live today (see "DM pairing" below). Send `/start` to your bot once; the rejected DM is audited with your `user_id`, or use `@userinfobot`.
4. `arc ui start --team-root team --gateway-config ~/.arc/gateway.toml` (or the systemd unit — see [single-node.md](../deploy/single-node.md))

## Slack

1. Create app at [api.slack.com/apps](https://api.slack.com/apps) → enable Socket Mode
2. Generate app-level token with `connections:write` scope (`xapp-...`)
3. Add bot scopes: `chat:write`, `im:history`, `im:read`; install; copy `xoxb-...` bot token
4. Set both env vars + add your Slack user id to `allowed_user_ids`
5. Same `arc ui start` invocation as Telegram

## DM pairing

DM pairing landed: `PairingStore`/`PairingInterceptor` are wired into
`GatewayRunner`, and `arc gateway pair approve` mutates a live store the
running gateway actually reads. When `[security] require_pairing = true`,
an unrecognized user gets a pairing code and must be approved.

**Prerequisite — run once per operator, before the first approval**:
`arc gateway pair approve` signs the approval with the operator's key, and
`PairingStore.verify_and_consume()` rejects any approval without a
verifiable Ed25519 signature at every tier (not just federal). If you've
never run it, do:

```bash
arc identity init
```

This generates a signing authority and registers it as a trusted pairing
operator (`arctrust.register_operator`). Skip this and every approval
attempt fails with a signature error, regardless of whether the code
itself is valid.

```bash
arc gateway pair approve X7K2MQJP
```

Codes expire in 1h. Max 3 pending per platform. 5 failed approvals → 1h platform lockout. Alphabet is 32 unambiguous chars (no `0/O/1/I`). Implementation: `arcgateway/pairing.py::PairingStore`.

**`allowed_user_ids` is the gate that matters day-to-day.** Whether or not
pairing is enabled, `allowed_user_ids` is checked first and is fail-closed
(empty list = deny all) — it's the mechanism every validated deployment so
far has actually used for personal-tier access control. Pairing adds a
second, revocable layer on top for cases where a static allowlist isn't
enough (e.g. onboarding flow, temporary access).

## Common pitfalls

- **One bot token per gateway instance.** `TelegramAdapter` uses long polling. Two instances on the same token silently lose updates to the race. See [multi-instance.md](./multi-instance.md).
- **Tokens out of source.** Use `token_env`/`bot_token_env` (never a literal token in `gateway.toml`). Federal tier requires vault-backed credentials; see [security.md](./security.md).
- **`allowed_user_ids` empty = deny all.** Fail-closed default (auth check in `adapters/telegram.py::TelegramAdapter`).
- **Wrong bot answering, or none at all.** If you've rotated `TELEGRAM_BOT_TOKEN`, confirm which bot it belongs to before debugging config: `curl -s "https://api.telegram.org/bot$TELEGRAM_BOT_TOKEN/getMe"` returns the bot's `username`/`id`. Restart after changing the token — it's read once at adapter construction, not live-reloaded.
- **Adapter log lines look silent even when connected.** `TelegramAdapter`'s connect/poll-started messages are `.info()` calls; if the effective root log level is `WARNING` they won't appear even though the adapter is genuinely up. Check for an established outbound connection to the platform's API range (`ss -tnp`) instead of trusting log absence.

## CLI reference

```bash
arc ui start --team-root team --gateway-config ~/.arc/gateway.toml   # embedded path — every tier, the only one that works
arcgateway start                          # ALWAYS refuses — prints why + the embedded invocation, exits 1
arcgateway start --config <path>          # same refusal; --config only shapes which gateway.toml the message reflects
arcgateway stop                           # SIGTERM via PID file (ARC_CONFIG_DIR-aware default runtime_dir) — still works
arcgateway status                         # health check (ARC_CONFIG_DIR-aware default runtime_dir) — still works
arc gateway pair approve <CODE>           # approve pending pairing (via arccli)
arc gateway pair list                     # list pending
arc gateway pair revoke <CODE>            # revoke
```

`arcgateway start/stop/status/setup` run through the `arcgateway` console script directly.
`start` no longer takes `--team-root`/`--runtime-dir` (removed — dead once standalone doesn't
serve); `stop`/`status` still do. All three resolve `gateway.toml`/`runtime_dir` via
`GatewayConfig.load()` when not given explicit flags, honoring `ARC_CONFIG_DIR`.
The operator pairing commands (`arc gateway pair *`) dispatch through `arccli.commands.registry`
(centralized `CommandDef` list — one source of truth for CLI + gateway + platform-menu
generators) so they're available from `arc` without a separate install.

## Next

- [../deploy/single-node.md](../deploy/single-node.md) — the full validated single-node runbook (systemd unit, secrets, troubleshooting)
- [security.md](./security.md) — tier matrix, vault, subprocess isolation, audit events, NIST controls
- [multi-instance.md](./multi-instance.md) — horizontal scaling plan
