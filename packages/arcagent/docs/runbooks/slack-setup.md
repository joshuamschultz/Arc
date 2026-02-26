# Slack Module Setup Runbook

Connect your ArcAgent to Slack via Socket Mode for bidirectional DM-based interaction.

## Prerequisites

- A Slack workspace where you have admin access (or permission to create apps)
- ArcAgent installed with Slack extras: `pip install 'arcagent[slack]'`
- Python 3.11+

## Step 1: Create a Slack App

1. Go to https://api.slack.com/apps
2. Click **Create New App** > **From scratch**
3. Name it (e.g., `ArcAgent-Dev`) and select your workspace
4. Click **Create App**

## Step 2: Enable Socket Mode

Socket Mode uses a WebSocket connection — no public URL needed. Works behind firewalls, NAT, and Azure GCC.

1. In the left sidebar, click **Socket Mode**
2. Toggle **Enable Socket Mode** to ON
3. When prompted, give the app-level token a name (e.g., `arcagent-socket`)
4. Copy the generated token — it starts with `xapp-`
5. Save this as your **app-level token**

## Step 3: Configure Bot Token Scopes

1. In the left sidebar, click **OAuth & Permissions**
2. Scroll to **Bot Token Scopes** and add these four scopes:

| Scope | Purpose |
|-------|---------|
| `im:history` | Read DM messages |
| `im:read` | View DM info |
| `im:write` | Open DMs with users |
| `chat:write` | Send messages |

3. Scroll to the top and click **Install to Workspace**
4. Authorize the app
5. Copy the **Bot User OAuth Token** — it starts with `xoxb-`

## Step 4: Enable Events

1. In the left sidebar, click **Event Subscriptions**
2. Toggle **Enable Events** to ON
3. Under **Subscribe to bot events**, add:
   - `message.im` (messages in DMs with the bot)
4. Click **Save Changes**

## Step 5: Get Your Slack User ID

1. In Slack, click your profile picture (top-right)
2. Click **Profile**
3. Click the **...** (more) menu
4. Click **Copy member ID**
5. It looks like `U0123ABC456`

## Step 6: Configure ArcAgent

Set environment variables:

```bash
export ARCAGENT_SLACK_BOT_TOKEN=xoxb-your-bot-token-here
export ARCAGENT_SLACK_APP_TOKEN=xapp-your-app-level-token-here
```

Add to your `arcagent.toml`:

```toml
[modules.slack]
enabled = true
priority = 100

[modules.slack.config]
allowed_user_ids = ["U0123ABC456"]  # Your Slack user ID(s)
max_message_length = 4000
```

## Step 7: Start ArcAgent

```bash
arcagent start
```

You should see in the logs:

```
INFO     arcagent.slack.bot: Slack bot connected via Socket Mode
INFO     arcagent.slack: Slack module started
```

If tokens are missing or invalid, you'll see:

```
WARNING  arcagent.slack.bot: Bot token not found in env var 'ARCAGENT_SLACK_BOT_TOKEN'; slack module dormant
```

## Step 8: Test the Connection

1. Open Slack
2. Find your bot in the DM list (search for the app name)
3. Send `start` — the bot should reply with a session ID
4. Send any message — the bot will route it through `agent.chat()` and reply
5. Send `status` to check the current session
6. Send `new` to start a fresh session

## Text Commands

| Command | What It Does |
|---------|-------------|
| `start` | Register as active user, create new session |
| `new` | Rotate to a fresh session (new session_id) |
| `status` | Show current session ID, user, connection status |

## Troubleshooting

### Bot doesn't respond

1. Check that both env vars are set and have correct prefixes:
   - Bot token: `xoxb-...`
   - App token: `xapp-...`
2. Verify your Slack user ID is in `allowed_user_ids`
3. Check ArcAgent logs for `slack:auth_rejected` events

### "slack-bolt not installed"

```bash
pip install 'arcagent[slack]'
```

### Bot stays dormant

The module stays dormant (no connection attempt) when:
- Either token environment variable is not set
- Token has wrong prefix (bot token must be `xoxb-`, app token must be `xapp-`)
- `slack-bolt` package is not installed

### Messages not received

1. Verify **Socket Mode** is enabled in app settings
2. Verify `message.im` event is subscribed
3. Check that bot token has `im:history` scope
4. Reinstall the app to workspace if scopes changed

### Rate limiting

Slack limits `chat.postMessage` to ~1 message/second/channel. The asyncio.Lock in the bot naturally rate-limits since it processes messages sequentially.

## Security Notes

- Tokens are read from environment variables only — never stored on disk or in config files
- State file (`{workspace}/slack/state.json`) is written with `0o600` permissions (owner-only)
- Empty `allowed_user_ids` = deny all messages (fail-closed)
- Bot messages (with `bot_id` field) are automatically skipped to prevent infinite loops
- No tokens appear in telemetry events or logs

## Network Requirements

Socket Mode only needs **outbound port 443** (WSS over TLS) to `wss-*.slack.com`. No inbound ports or public URLs required.

If behind a proxy: the proxy must support WebSocket upgrade. Exempt `wss-*.slack.com` from SSL inspection.

## Multi-Agent Setup

For multiple agents, each needs its own Slack app:

1. Create one Slack app per agent (e.g., `ArcAgent-Research`, `ArcAgent-Ops`)
2. Each app gets its own bot token + app token pair
3. Each agent config maps to specific `allowed_user_ids`
4. Use custom env var names if needed:

```toml
[modules.slack.config]
bot_token_env_var = "AGENT_RESEARCH_SLACK_BOT_TOKEN"
app_token_env_var = "AGENT_RESEARCH_SLACK_APP_TOKEN"
```

## OAuth Scopes Reference

### Bot Token Scopes (minimum)

| Scope | Required For |
|-------|-------------|
| `im:history` | Reading DM messages |
| `im:read` | Viewing DM channel info |
| `im:write` | Opening DMs with users |
| `chat:write` | Sending messages |

### App-Level Token Scopes

| Scope | Required For |
|-------|-------------|
| `connections:write` | Socket Mode WebSocket |

### Not Needed

- `commands` — we use text commands, not slash commands
- `reactions:write` — no emoji reactions for processing indicator
