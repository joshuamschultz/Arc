# arcgateway-slack - Slack Integration

> **Layer:** Surface  
> **Dependencies:** arcgateway  
> **Install:** `pip install arcgateway-slack`
> **See also:** [arcgateway.md](arcgateway.md), [DATA_FLOW.md](../DATA_FLOW.md)

---

## Overview

`arcgateway-slack` provides **Slack app integration** for Arc agents.

```mermaid
flowchart LR
    classDef slack fill:#0073FE,stroke:#0055BC,color:#FFFFFF
    classDef gateway fill:#5A9CFF,stroke:#0073FE,color:#002550

    Slack[Slack<br/>Events API]:::slack -->|events| Adapter[Slack<br/>Adapter]:::gateway
    Adapter -->|messages| arcagent[arcagent]
```

---

## Setup

### Create Slack App

1. Go to [api.slack.com/apps](https://api.slack.com/apps)
2. Click "Create New App" → "From scratch"
3. Configure:
   - **Bot Token Scopes**: `chat:write`, `channels:read`, `groups:read`
   - **Event Subscriptions**: `message.channels`, `message.im`
   - **Install App** to workspace

### Configure Environment

```bash
# Add to .env
SLACK_BOT_TOKEN=xoxb-1234567890-1234567890123-abcdefghijklmnopqrstuvwx
SLACK_SIGNING_SECRET=1234567890abcdef1234567890abcdef
SLACK_APP_TOKEN=xapp-1234567890123-abcdefghijklmnopqrstuvwx-123456789012345678901234

# Or in arcagent.toml
[gateway.slack]
bot_token_env = "SLACK_BOT_TOKEN"
signing_secret_env = "SLACK_SIGNING_SECRET"
```

### Start Gateway

```bash
arc gateway start --platform slack
```

---

## Features

### Supported Events

| Event | Handled |
|-------|---------|
| `message.channels` | ✅ |
| `message.im` | ✅ |
| `message.groups` | ✅ |
| `app_mention` | ✅ |
| `message.app_home` | ✅ |

### Message Types

```python
# Regular message
{"type": "message", "text": "Hello agent", "channel": "C12345"}

# DM
{"type": "message", "text": "Private message", "channel": "D12345"}

# App mention
{"type": "app_mention", "text": "<@BOTID> help", "channel": "C12345"}
```

---

## Slash Commands

### Built-in Commands

| Command | Purpose |
|---------|---------|
| `/arc help` | Show help |
| `/arc status` | Agent status |
| `/arc pair` | Get pairing code |

### Custom Commands

```python
# Define in Slack app
# Command: /analyze
# Request URL: https://your-domain.com/slack/commands

# Handler
@tool(name="slack_command", classification="network")
async def handle_slash_command(command: str, text: str, user: str) -> str:
    """Handle Slack slash command."""
    if command == "/analyze":
        return await analyze_data(text)
```

---

## Configuration

```toml
[gateway.slack]
enabled = true
bot_token_env = "SLACK_BOT_TOKEN"
signing_secret_env = "SLACK_SIGNING_SECRET"
allowed_channels = [
    "C1234567890",
    "D1234567890"
]
```

### Finding Channel IDs

```bash
# In Slack, open channel and use:
# https://api.slack.com/methods/conversations.info?channel=CHANNEL_ID
# Or use Slack's "Copy Link" and extract ID from URL
```

---

## API Reference

### Classes

```python
class SlackAdapter:
    def __init__(
        self,
        bot_token: str,
        signing_secret: str,
        allowed_channels: list[str] = None
    ): ...
    
    async def handle_event(self, event: dict) -> None:
        """Handle Slack event."""

def verify_slack_signature(
    body: bytes,
    timestamp: str,
    signature: str,
    signing_secret: str
) -> bool:
    """Verify Slack request signature."""
```

---

## Security

### Signature Verification

```python
def verify_slack_signature(body: bytes, timestamp: str, sig: str, secret: str) -> bool:
    """
    Verify Slack request signature.
    Uses HMAC-SHA256 with Slack signing secret.
    """
    # Slack signs with v0:timestamp:body
    ...
```

### Channel Allowlist

```python
def is_allowed_channel(channel: str, allowed: list[str]) -> bool:
    """Check if channel is in allowlist."""
    return channel in allowed
```

---

## Next Steps

- [arcgateway](arcgateway.md) - Base gateway documentation
- [Package Index](PACKAGE_INDEX.md) - All Arc packages