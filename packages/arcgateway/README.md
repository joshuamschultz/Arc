<div align="center">

# 📡 arcgateway

### **Make Your Agents Reachable from Telegram, Slack, Discord — Safely**
*Long-running daemon. Multi-platform adapters. Operator-approved pairing. TaskGroup isolation per platform.*

[![License: Apache 2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](https://opensource.org/licenses/Apache-2.0)
[![Tests](https://img.shields.io/badge/tests-494-success.svg)](#status)
[![Coverage](https://img.shields.io/badge/coverage-94%25-brightgreen.svg)](#status)
[![Strict mypy](https://img.shields.io/badge/mypy-strict-2563EB.svg)](#status)
[![Pairing Required](https://img.shields.io/badge/pairing-operator_approved-DC2626.svg)](#-operator-approved-pairing)

</div>

---

## ✨ What is arcgateway?

`arcgateway` is the long-running daemon that lets users talk to your agents through chat platforms — Telegram, Slack, Discord — **without** giving anyone implicit access to anything.

Every DM gets a per-(user, agent) session. Every session must be **explicitly paired** by an operator before the agent will respond. One platform crashing never takes down the others. Every action emits an audit event.

> 🛡️ **No pairing → no response. Operator-approved allowlist. TaskGroup isolation. Replay-protected.**

---

## 🏗️ Where It Fits

```mermaid
flowchart LR
    classDef gw fill:#FB923C,stroke:#9A3412,color:#431407
    classDef ag fill:#A78BFA,stroke:#5B21B6,color:#2E1065
    classDef ext fill:#E5E7EB,stroke:#6B7280,color:#111827

    Telegram[Telegram]:::ext --> arcgateway
    Slack[Slack]:::ext --> arcgateway
    Discord[Discord]:::ext --> arcgateway
    arcgateway[arcgateway<br/>session router · pairing<br/>TaskGroup isolation]:::gw --> arcagent[arcagent]:::ag
```

Depends on `arcagent`, `arcrun`, `arcllm`, `arctrust`. **No other Arc package depends on arcgateway** — it's a terminal node.

---

## 🚀 Install

For end-users, install the meta package:

```bash
pip install arcmas              # arcgateway is included in the meta package
```

For Arc monorepo development, install all sibling packages in editable mode together — installing only `arcgateway` leaves `arcagent`, `arcllm`, `arcrun`, `arccli` un-linked and import errors surface as cryptic "module not found" failures during test runs:

```bash
uv pip install -e packages/arcgateway \
               -e packages/arcgateway-telegram \
               -e packages/arcgateway-slack \
               -e packages/arcgateway-mattermost \
               -e packages/arccli \
               -e packages/arcagent \
               -e packages/arcllm \
               -e packages/arcrun
```

`make install` runs this canonical command from the repo root.

---

## 🧪 Quick Example

```python
from arcgateway import GatewayRunner, AsyncioExecutor

# In-process executor (Personal / Enterprise tier)
executor = AsyncioExecutor(agent_config_path="my-agent/arcagent.toml")
runner = GatewayRunner(executor=executor)

await runner.run()              # blocks; SIGINT/SIGTERM handled gracefully
```

Configure platform adapters in `gateway.toml` (enable a block **and** install
its extension package):

```toml
[gateway]
agent_did = "did:arc:agent:default"

[security]
require_pairing = true

# pip install arcgateway-telegram
[platforms.telegram]
enabled = true
token_env = "TELEGRAM_BOT_TOKEN"
allowed_user_ids = [123456789]

# pip install arcgateway-slack
[platforms.slack]
enabled = false
bot_token_env = "SLACK_BOT_TOKEN"
app_token_env = "SLACK_APP_TOKEN"
```

---

## 🤝 Operator-Approved Pairing

Anyone can DM the bot. **Nothing happens** until an operator approves the pairing.

### The pairing flow

```mermaid
sequenceDiagram
    participant U as User
    participant G as arcgateway
    participant O as Operator (CLI)
    participant A as arcagent

    U->>G: DM "/pair"
    G-->>U: 8-character code (TTL: 15 min)
    U->>O: shares code (out of band)
    O->>G: arc gateway pair approve ABCD1234
    G->>G: append user hash to allowlist
    G-->>U: "Paired. You can talk to the agent now."
    U->>G: any message
    G->>A: routed via SessionRouter
    A->>G: response
    G->>U: delivered
```

### CLI commands

```bash
arc gateway pair list                    # show pending (unexpired, unconsumed) codes
arc gateway pair approve ABCD1234        # approve a code; adds user hash to allowlist
arc gateway pair revoke ABCD1234         # revoke a pending code
```

**Codes are exactly 8 characters, uppercase, with TTL.** They auto-expire. `pair list` shows the remaining minutes.

User identifiers are stored as **hashes**, not raw IDs. The allowlist contains nothing personally identifying.

Every approve / revoke / pair-attempt emits an arctrust audit event with the operator's identity, the code, and the outcome.

---

## 🧱 Public API

```python
from arcgateway import (
    GatewayRunner,           # supervises all platform adapters
    SessionRouter,           # per-(user, agent) session routing
    build_session_key,       # canonical (user_hash, agent_did) tuple

    InboundEvent,            # normalized event from any platform
    Delta,                   # streamed response chunk

    Executor,                # protocol
    AsyncioExecutor,         # in-process implementation

    DeliveryTarget,          # parsed "platform:chat_id[:thread_id]" address
)

# SPEC-022 — agent data plane (read-only)
from arcgateway import (
    fs_reader,               # read_file() / list_tree() with audit + size cap
    fs_watcher,              # WatcherManager (lazy, ref-counted, watchfiles+poll)
    policy_parser,           # parse_bullets() — pure ACE bullet parser
    team_roster,             # list_team() — discover agents from team/<id>_agent/
    agent_config,            # load_ui_section() — optional [ui] in arcagent.toml
    file_events,              # FileChangeEvent + FileEventBus async pub/sub
)
```

### Agent data plane (SPEC-022)

The `fs_reader`, `fs_watcher`, `policy_parser`, `team_roster`, `agent_config`, and `file_events` modules together form the **single read API for `team/<agent>/...`**. arcui consumes them in-process; nothing else may. ADR-020 explains why this lives in gateway.

| Module | Responsibility |
|--------|----------------|
| `fs_reader` | All read access. `read_file(scope, agent_id, agent_workspace, rel_path, caller_did)` and `list_tree(...)`. Path traversal blocked, size capped at 1 MB, depth-limited tree. Read-only by structure (no write methods exist). `scope: agent\|team\|shared` arg from day one — only `agent` is wired today; `team` and `shared` raise `NotImplementedError` for forward-compat. |
| `fs_watcher` | Per-agent watcher lifecycle. `WatcherManager.subscribe(agent_id, workspace_root)` lazy-starts a watcher, ref-counted; `unsubscribe()` decrements and tears down at zero. Uses `watchfiles` when available, polls stdlib mtime otherwise (D-007). |
| `policy_parser` | Pure parser for ACE policy bullets `- [P##] <text> {score:N, ...}`. Text in / dataclasses out. No I/O coupling. Same parser used in arcui detail Policy tab and fleet Policy Engine page. |
| `team_roster` | `list_team(team_root, online_ids) -> list[RosterEntry]`. Walks `team/*_agent/arcagent.toml`, applies `[ui]` overrides, overlays online/offline status from caller-supplied set. |
| `agent_config` | `load_ui_section(toml_dict) -> UISection`. Optional `[ui]` block: `display_name`, `color`, `role_label`, `hidden`. ADR-021. |
| `file_events` | `FileChangeEvent` dataclass + `FileEventBus` in-process async pub/sub. Bus is fanout — every subscriber sees every event. Audit emission is direct via `arcgateway.audit.emit_event` (D-022-B). |

Audit events emitted on every fs op: `gateway.fs.read`, `gateway.fs.tree`, `gateway.fs.changed`. Each row carries `caller_did`, `agent_id`, `path`, `scope` for NIST AU-2.

### How the runner stays resilient

`GatewayRunner` supervises N platform adapters inside an `asyncio.TaskGroup`. **A crash in one adapter never kills its siblings.** Telegram disconnects → Slack and Discord keep serving. The crashed adapter is logged, audited, and restarted with backoff.

### How the session router prevents races

`SessionRouter` uses a **synchronous pre-await guard** to guarantee exactly **one agent task per session key**, regardless of how many concurrent inbound messages arrive at the same instant. This closes a race condition where two messages arriving on the same TCP connection could both spawn a fresh agent task.

---

## 🔌 Platform Adapters — a plugin system

The gateway core contains **zero** platform-specific code. The only built-in
adapter is `web` (the in-process browser chat surface arcui hosts). Every remote
platform ships as a **separate extension package** that registers an
`AdapterPlugin` under the `arcgateway.adapters` entry-point group:

| Platform | Package | Bot Token Source |
|---|---|---|
| **Telegram** | [`arcgateway-telegram`](../arcgateway-telegram) | `TELEGRAM_BOT_TOKEN` env or vault |
| **Slack** | [`arcgateway-slack`](../arcgateway-slack) | `SLACK_BOT_TOKEN` / `SLACK_APP_TOKEN` |
| **Mattermost** | [`arcgateway-mattermost`](../arcgateway-mattermost) | `MM_BOT_TOKEN` env or vault |

Install a platform from the CLI (pip/uv under the hood, official names only):

```bash
arc gateway adapter list                # show official adapters + install status
arc gateway adapter install telegram    # installs arcgateway-telegram
# standalone daemon equivalent:
arcgateway adapter install telegram
```

…or install the package directly:

```bash
pip install arcgateway arcgateway-telegram     # install only the platforms you need
```

At startup `arcgateway.adapters.registry`:
1. **Discovers** every installed plugin via `importlib.metadata.entry_points`.
2. **Authorizes** it — validates the name and applies a tier-aware allowlist
   (official plugins always allowed; unofficial ones load with an audit warning
   at personal/enterprise and are **blocked** at federal).
3. **Builds** an adapter for each enabled `[platforms.<name>]` block, gating on
   credential presence (a missing token skips at personal, fails closed at federal).
4. **Audits** every load / skip / block (`gateway.adapter.*`).

**Writing a new platform** = one package: an adapter class implementing
`BasePlatformAdapter` (`connect` / `disconnect` / `send`), a Pydantic config
model, and a `PLUGIN = AdapterPlugin(name, build)` registered via the entry
point. No changes to the gateway core. See `arcgateway-telegram` as the
reference implementation.

---

## 🛡️ Security Architecture

### Pairing-Gate

| Layer | Defense |
|---|---|
| **Allowlist** | Stored as user hashes, not raw IDs. Operator-approved. Persisted to JSONL |
| **Code TTL** | Codes expire (default 15 min). `pair list` shows time remaining |
| **Code throttling** | `pairing_throttle.py` rate-limits pairing-code generation per user |
| **Pairing signature** | Every pairing record is signed (Ed25519 via arctrust) — tampering with the allowlist file is detectable |
| **Replay protection** | Codes are single-use. `approve` consumes the code immediately |

### Per-Platform Isolation

| Property | How |
|---|---|
| **Crash containment** | TaskGroup isolation — one platform's `RuntimeError` never kills siblings |
| **Backoff on restart** | Exponential backoff with jitter on adapter restart |
| **Per-platform queues** | Inbound events queue per-platform — slow Slack doesn't backpressure Telegram |

### Audit on Everything

Every pair attempt, every approve, every revoke, every inbound event, every outbound delta emits an arctrust audit event. The dashboard (`arcui`) surfaces these in real time.

---

## 📋 Compliance Mapping

| NIST 800-53 | What `arcgateway` Provides |
|---|---|
| AC-3 | Allowlist-gated session routing — no pair → no response |
| AC-6 | Per-session DID-bound agent task; no shared session state |
| AU-2, AU-12 | Every pair, approve, revoke, inbound, outbound is audited |
| IA-3 | Each session keyed on (user_hash, agent_DID) |
| SC-13 | Pairing records signed with Ed25519 |

| OWASP Agentic | Mitigation |
|---|---|
| ASI03 (Identity Abuse) | Per-(user, agent) session keys; user IDs hashed; no shared credentials |
| ASI07 (Insecure Inter-Agent Comms) | Pairing signed; allowlist tamper-evident |
| ASI08 (Cascading Failures) | TaskGroup isolation; backoff restart; per-platform queue |
| ASI09 (Trust Exploitation) | Operator approval required; nothing happens implicitly |

---

## 🧪 Status

```bash
uv run --no-sync pytest packages/arcgateway/tests
```

- **Tests:** 494
- **Coverage:** 94%
- **Type check:** `mypy --strict` clean
- **Lint:** `ruff check` clean

---

## 📄 License

Apache 2.0 · Copyright © 2025-2026 BlackArc Systems.
