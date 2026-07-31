# SDD-018: Hermes-Parity Roadmap — Solution Design

**Spec ID**: SPEC-018 | **Date**: 2026-04-18 | **PRD**: `PRD.md` | **Decisions**: `.claude/decisions-log.md` (Hermes-Parity Roadmap section + Deepening Insights)

## §1 Overview

This SDD covers cross-cutting design for absorbing Hermes-parity capabilities into Arc's package model. **Per-feature implementation detail (file-level diffs, exact function signatures, full test plans) lives in follow-up specs spawned per milestone** (e.g., SPEC-019-arcgateway, SPEC-020-session-search, etc.). This document is the architectural contract those follow-ups inherit.

### Pillar mapping (per principled-coder priority order)

1. **Simplicity** — One new sibling package (arcgateway). Every new module reuses existing module-bus + telemetry + identity primitives. No abstractions added until 3 instances exist (per Arc CLAUDE.md).
2. **Modularity** — Hard boundaries reaffirmed: arcllm = LLM, arcrun = loop/execution, arcagent = tools/skills/extensions/memory. arcgateway = "the daemon that runs ArcAgents." Each new capability owns one Protocol; new providers plug in via the Protocol.
3. **Security** — All new code routes through tier policy layer (federal blocks, enterprise warns, personal informs). Every cross-package call mTLS at federal. No new credentials on disk at federal/enterprise. Capability-based per-turn memory grants over ACL.
4. **Scalability** — gateway daemon is shared-nothing per agent process; sessions are per-(user, agent) so concurrency is naturally horizontal; backends are pluggable.

## §2 Package Layout (D-01)

```
packages/
├── arcllm/                          # No new code in SPEC-018; existing fallback / retry / security used by NL cron LLM fallback
├── arcrun/                          # Extended: ExecutorBackend Protocol + spawn() promoted to first-class
│   └── src/arcrun/
│       ├── executor.py              # MODIFY: refactor behind ExecutorBackend Protocol
│       ├── sandbox.py               # MODIFY: pair with backend selection at tier-policy layer
│       ├── builtins/
│       │   ├── spawn.py             # MODIFY: harden existing stub (root token pool, DID HKDF, audit chain)
│       │   ├── execute.py           # MODIFY: route through ExecutorBackend
│       │   └── contained_execute.py # DELETE (folded into docker backend)
│       └── backends/                # NEW directory
│           ├── __init__.py
│           ├── base.py              # NEW: ExecutorBackend Protocol + capabilities + ExecHandle
│           ├── local.py             # NEW: LocalBackend (formerly executor.py logic)
│           └── docker.py            # NEW: DockerBackend
│
├── arcagent/                        # Extended: new modules; no core changes beyond bus subscriber registration
│   └── src/arcagent/
│       ├── modules/
│       │   ├── session/             # NEW: JSONL primary + FTS5 indexer (extracts from session_manager.py)
│       │   ├── memory_acl/          # NEW: bus priority 10; vetoes memory.read/write/search
│       │   ├── user_profile/        # NEW: per-user markdown w/ YAML frontmatter
│       │   ├── skill_improver/
│       │   │   └── nudge.py         # NEW: submodule; subscribes agent:post_plan @ priority 150
│       │   ├── scheduler/
│       │   │   └── nl_parser.py     # NEW: deterministic-first + LLM fallback
│       │   ├── delegate/            # NEW: agent-facing tool wrapping arcrun.spawn()
│       │   ├── voice/               # NEW: STT/TTS Protocol + provider plugins (Whisper.cpp, Piper, ElevenLabs, OpenAI)
│       │   ├── web/                 # NEW: WebSearchProvider + WebExtractProvider Protocol + plugins (Parallel, Firecrawl, Tavily)
│       │   ├── browser/             # NEW: Playwright wrapper; sandbox-mode aware
│       │   └── ... (existing modules unchanged)
│       └── tools/                   # MODIFY: register session_search tool, delegate tool
│
├── arcgateway/                      # NEW SIBLING PACKAGE
│   └── src/arcgateway/
│       ├── runner.py                # GatewayRunner; supervises adapters
│       ├── session.py               # SessionRouter (key builder + identity graph + ACL hooks)
│       ├── pairing.py               # DM pairing (lifted from Hermes; Postgres backend at federal multi-instance)
│       ├── delivery.py              # DeliveryTarget parser; agent-facing send() API
│       ├── stream_bridge.py         # LLM stream → platform edit transport with flood-strike fallback
│       ├── executor.py              # AsyncioExecutor / SubprocessExecutor / NATSExecutor implementing Executor Protocol
│       ├── adapters/
│       │   ├── base.py              # BasePlatformAdapter Protocol
│       │   ├── telegram.py
│       │   ├── slack.py             # ABSORBS existing arcagent.modules.slack/telegram bot.py logic
│       │   ├── discord.py
│       │   ├── whatsapp.py
│       │   ├── signal.py
│       │   ├── matrix.py
│       │   └── email.py
│       └── cli.py                   # arc gateway start/stop/status/setup
│
├── arcskill/                        # Extended: hub functionality (toggle + CLI install + scanning)
│   └── src/arcskill/
│       ├── hub/                     # NEW: source registry + Sigstore verify + scanner + installer
│       │   ├── sources.py
│       │   ├── verify.py            # cosign + Fulcio + Rekor inclusion proof + CRL
│       │   ├── scanner.py           # semgrep + bandit + Hermes regex bank
│       │   └── installer.py         # quarantine → scan → Firecracker dry-run → activate
│       └── lock.py                  # NEW: HubLockFile schema (content_hash, rekor_uuid, slsa_level, scan_verdict)
│
├── arctui/                          # Completion: Textual-based UI
│   └── src/arctui/
│       ├── app.py                   # Textual App; integrates with arcagent's asyncio loop
│       ├── transcript.py            # Streamed message view
│       ├── activity.py              # Tool activity panel
│       ├── prompts.py               # Approval / clarify / sudo modal renderers
│       └── command_completer.py     # Reads from arccli.commands.registry
│
└── arccli/                          # Centralized command registry
    └── src/arccli/
        └── commands/
            ├── registry.py          # NEW: CommandDef list + resolve_command()
            ├── render.py            # Help / autocomplete / platform-menu generators
            └── handlers/            # Existing per-command handlers; MODIFY to look up via registry
```

**Net additions:** 1 new package (arcgateway), ~10 new arcagent modules, 1 new arcrun subdir (backends/), arcskill `hub/` subpkg, arctui completion, arccli centralized commands.

**Federal-tier extras packages** (SPEC-018 doesn't ship; documented for downstream):
- `arcrun-backend-ssh`, `arcrun-backend-modal`, `arcrun-backend-daytona`, `arcrun-backend-singularity` — separate publishable packages (signed wheels at federal).

## §3 Cross-Cutting Design Per Capability

### §3.1 Gateway Architecture (D-02, D-03, D-14)

#### Process Model
arcgateway is a **single long-running asyncio daemon**, separate from any single ArcAgent process. It supervises N platform adapters and routes incoming messages to per-(user, agent) sessions. **D-02.**

```
                ┌──────────────────────────────┐
                │        arcgateway daemon       │
                │                                │
   Telegram ───┤  Adapter   ┐                    │
   Slack ──────┤  Adapter   ├──> SessionRouter ──┤──> Executor.run(event)
   Discord ────┤  Adapter   │      │             │       │
   WhatsApp ───┤  Adapter   ┘      │             │       ├─ AsyncioExecutor (personal/enterprise)
                │                    │             │       └─ SubprocessExecutor (federal)
                │             SessionStore         │            │
                │             (arcagent.session)   │            ▼
                │             via fcntl-locked    │       AsyncIterator[Delta]
                │             JSONL append +     │            │
                │             SQLite FTS5 index  │            ▼
                │                                  │       StreamBridge
                │                                  │            │
                │                                  │            ▼
                │                                  │       Adapter.send()
                └──────────────────────────────────┘
```

#### Adapter Lifecycle (BasePlatformAdapter Protocol)
- `async connect() / disconnect() / send(target, message)` — minimum surface.
- Each adapter owns its own background poll/socket task inside an `asyncio.TaskGroup` (Python 3.11+) so a crash in one adapter doesn't kill siblings.
- Single reconnect watcher walks `_failed_adapters: dict[str, FailedAdapter]` with backoff `min(30 * 2**(n-1), 300)` capped at 5 min, 20 attempts (Hermes pattern).
- Tier-policy layer chooses dispatch executor:

```python
class Executor(Protocol):
    async def run(self, event: InboundEvent) -> AsyncIterator[Delta]: ...

class AsyncioExecutor:    # personal, enterprise
    async def run(self, event):
        async with asyncio.TaskGroup() as tg:
            tg.create_task(self._spawn_agent_inproc(event))

class SubprocessExecutor:  # federal
    async def run(self, event):
        proc = await asyncio.create_subprocess_exec(
            "arc-agent-worker", "--did", event.session.agent_did, ...,
            stdin=PIPE, stdout=PIPE,
            preexec_fn=lambda: resource.setrlimit(...)
        )
        # JSON-lines IPC; child has its own httpx pool, own ToolRegistry, own audit chain
        ...
```

#### Race-Condition Guard (CRITICAL)
**Hermes PR #4926** (set-active-before-await) is the #1 gateway bug. Implementation contract:

```python
# In SessionRouter.handle_message:
# CORRECTED: session key is (agent_did, user_did) per D-06 — same user across
# platforms = same session. Identity graph resolves platform→user_did BEFORE
# this point (see §3.3). DO NOT add platform here.
session_key = build_session_key(event.agent_did, event.user_did)

# SYNCHRONOUS guard — no await before assignment
if session_key in self._active_sessions:
    self._queue_for_session(session_key, event)
    return

self._active_sessions[session_key] = asyncio.Event()  # PLACE FIRST
asyncio.create_task(self._process_event(session_key, event))  # THEN spawn
```

**Test contract:** integration test fires N=20 concurrent messages to the same session and asserts exactly one agent task per session, no duplicates.

#### Session Key (validates D-06)
`f"{agent_id}:{platform}:{chat_type}:{user_id}"` — Hermes pattern; matches Arc's per-(user, agent) D-06.

**Identity graph** resolves cross-platform identity BEFORE session-key construction:
- Stored in `arcagent.modules.session.identity_graph` (SQLite table `user_identity_links`).
- Key: `user_identity_id` (DID-anchored). Value: `[telegram:123, slack:U456, signal:+1...]`.
- Linking event itself audited at federal tier (it's an information disclosure).

#### Platform Credentials (D-14)

| Tier | Source | Failure mode |
|---|---|---|
| Federal | Vault Protocol (Azure KV / HashiCorp Vault / AWS Secrets Manager) — required | Hard error at startup; no platforms connect |
| Enterprise | Vault preferred; env fallback | Warn-log + audit event; gateway proceeds with env |
| Personal | `~/.arc/gateway.toml` 0600 (or env) | None |

Same code path; tier-policy layer chooses the resolver. Builds on existing `arcagent.modules.vault_azure`.

#### DM Pairing (Epic A5)
Lifted from Hermes `gateway/pairing.py`:
- 8-char codes from 32-char unambiguous alphabet (`ABCDEFGHJKLMNPQRSTUVWXYZ23456789`) via `secrets.choice()`.
- 1h TTL; max 3 pending codes per platform; 1 code request per user per 10 min; 5 failed approvals → 1h platform lockout.
- Atomic temp-file + `os.replace()` + `chmod 0600` on every write.
- **Federal addition:** approver must sign pairing acceptance with their DID; multi-instance gateway moves storage from filesystem to Postgres with pessimistic lock.

#### Stream Flood-Control (Hermes pattern)
`stream_bridge.py` consumes LLM streaming output and edits platform messages incrementally. **3-strikes rule:** after 3 consecutive edit failures (Telegram flood limit, Discord rate limit, Slack tier-3), permanently disable progressive edits for the rest of that turn and fall back to final-send-only. Without this, one rate-limited channel stalls all concurrent sessions.

### §3.2 Session Storage: JSONL Primary + SQLite FTS5 Derived (D-04)

**Conflict acknowledged:** Hermes explicitly rejected this pattern (consolidated to SQLite primary). Their reason was contention/performance. **Arc's federal audit-trail requirement (NIST AU-9 tamper-evident, append-only) makes JSONL primary non-negotiable.** The polling-with-checkpoint indexer addresses Hermes' contention concern.

#### Architecture
```
session writer (existing arcagent.core.session_manager)
    ↓ append + fsync
sessions/*.jsonl  ← primary store (audit-truth)
    ↑ poll every 30s (configurable)
SessionIndex (NEW)
    ↓ INSERT INTO messages + trigger → messages_fts (FTS5 external-content)
sessions/index.db  ← derived store
    ↑ session_search tool reads here
```

#### Why polling and not file watcher / in-process queue?
- File watchers (inotify/FSEvents/watchdog) miss events during indexer downtime; race on rotate; macOS FSEvents silently drops.
- In-process queue loses entries on crash.
- **Polling-with-byte-offset-checkpoint is the only crash-safe pattern.** On startup, replay from last checkpoint. Idempotent.

#### Schema (External-content FTS5)
```sql
CREATE TABLE IF NOT EXISTS messages(
    id INTEGER PRIMARY KEY,
    session_id TEXT NOT NULL,
    user_did TEXT,                   -- for ACL filter
    agent_did TEXT,
    classification TEXT,             -- 'unclassified' | 'cui' | 'secret'
    role TEXT, ts REAL, content TEXT,
    jsonl_path TEXT NOT NULL, jsonl_offset INTEGER NOT NULL
);
CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
    content, content=messages, content_rowid=id,
    tokenize='porter unicode61 remove_diacritics 2', columnsize=0
);
CREATE TRIGGER messages_ai AFTER INSERT ON messages BEGIN
    INSERT INTO messages_fts(rowid, content) VALUES (new.id, new.content);
END;
CREATE TABLE sync_state(
    jsonl_path TEXT PRIMARY KEY, offset INTEGER, last_hash TEXT
);
```

WAL mode (`PRAGMA journal_mode=WAL`) so readers don't block writers.

#### Rebuild Costs (per research)
| JSONL size | Full rebuild |
|---|---|
| 100 MB | 1-2 min |
| 1 GB | 10-20 min |
| 10 GB | 2-3 hr (use `PRAGMA synchronous=OFF` during) |

#### `session_search` Tool
- Inputs: `query: str`, `limit: int = 20`, `since: datetime | None = None`, `classification_max: str | None = None`.
- Outputs: list of `{session_id, role, ts, snippet, jsonl_path, jsonl_offset}`.
- ACL filter applied at query: agent only sees sessions whose `user_did` ∈ ACL or `classification` ≤ caller's clearance.
- Top results auxiliary-LLM-summarized via arcllm before injection (token efficiency).

#### Federal At-Rest Encryption
SQLite SEE ($2k commercial) or SQLCipher (BSD) wraps `index.db`. Polling indexer is oblivious. JSONL on encrypted FS or age-encrypted at rotation.

### §3.3 Session Identity Model (D-06, D-07)

#### Session Key
`session_id = sha256(f"{agent_did}:{user_did}".encode())[:16]`

- Same `(agent, user)` pair → same session, regardless of platform.
- Different user → new session, but agent memory is shared per ACL.
- Concurrency: per-session FIFO (one in-flight turn per session). Cross-session = parallel.

#### Cross-Session Reads (D-09)
Each session has an ACL field:
```yaml
acl:
  cross_session_visibility: "private" | "shared-with-agent" | "shared-with-others-via-agent"
```

Tier defaults:
- Federal: `private`. Cross-session reads blocked by `memory_acl` module unless user marked `shared`.
- Enterprise: `shared-with-agent` within team; warn on cross-org.
- Personal: `shared-with-agent`.

### §3.4 Natural-Language Cron + Platform Delivery (D-10, Epic C)

#### Parser Pipeline
```python
def parse(text: str, user_tz: str, federal: bool) -> Schedule:
    for fn in (parse_interval, parse_duration_oneshot, parse_cron_cronsim, parse_iso_dateparser):
        try:
            return validate(fn(text, user_tz))
        except ParseError:
            continue
    if federal:
        raise ParseError("deterministic-only mode: unrecognized schedule")
    raw = llm.tool_call("normalize_schedule", text, user_tz)  # JSON-schema constrained
    return validate(reify(raw, user_tz))
```

#### Library: `cronsim` + `dateparser`
- `cronsim` (Healthchecks.io) — production-grade, DST-correct, fail-loud on `L/W/#` it can't evaluate. Replaces croniter (DST bugs).
- `dateparser` (Scrapinghub) — handles "in 30m / tomorrow 9am / next Tuesday 3pm" + TZ.

#### LLM Fallback Schema (Anthropic tool-use; equivalent in OpenAI strict json_schema; air-gap via llguidance/outlines)
```json
{
  "name": "normalize_schedule",
  "input_schema": {
    "type": "object",
    "required": ["kind", "cron_or_iso", "tz"],
    "properties": {
      "kind": {"enum": ["cron", "interval", "once"]},
      "cron_or_iso": {"type": "string"},
      "interval_minutes": {"type": "integer", "minimum": 1, "maximum": 525600},
      "tz": {"type": "string", "pattern": "^[A-Za-z_]+/[A-Za-z_]+$"},
      "rationale": {"type": "string", "maxLength": 200}
    }
  }
}
```

#### Sanity Validation (post-parse)
Compute next 5 fire times. Reject if min-interval < 60s OR max gap > 366 days OR any iteration raises.

#### Self-Scheduling Prevention (CRITICAL — Hermes pattern)
Cron-triggered agent sessions REMOVE the cronjob tool from the registry for that session's duration:
```python
# In scheduler.spawn_cron_agent:
agent_kwargs = {
    "disabled_toolsets": ["cronjob", "messaging", "clarify"],
    "quiet_mode": True,
    "skip_context_files": True,
    "skip_memory": True,
}
```
**Tool-registry-layer enforcement, NOT a policy flag.** The model cannot emit `cronjob.create` because the tool doesn't exist in that session's manifest. Survives prompt injection.

#### Platform Delivery
Cron job spec:
```toml
[[cron.jobs]]
name = "morning-digest"
schedule = "0 9 * * 1-5"
prompt = "summarize overnight Slack DMs"
deliver_to = "telegram:joshs-channel"
silent_on_success = true   # delivers only on failure if true
```
Delivery flow: scheduler tick → spawn agent → agent produces output → `arcgateway.delivery.send(target, message)`.

### §3.5 Subagent Spawn Hardening (D-11, Epic F2)

**Existing:** `packages/arcrun/src/arcrun/builtins/spawn.py` already has the right defaults (`_DEFAULT_SPAWN_TIMEOUT_SECONDS=300`, `_DEFAULT_MAX_CONCURRENT_SPAWNS=5`, `_DEFAULT_MAX_CHILD_TURNS=25`, event bubbling at `child.<run_id>.<event_type>`). The work is **harden + integrate**, not design + implement.

#### API
```python
# arcrun/builtins/spawn.py (modify)
async def spawn(
    *,
    parent_state: RunState,                # for depth, trace ctx, chain tip
    task: str,                              # the only instruction content
    context: str | None = None,             # structured, non-instructional
    tools: list[Tool],                      # MUST be subset of parent's tools
    system_prompt: str,                     # built by arcagent
    identity: ChildIdentity,                # DID + ephemeral keypair (from arcagent)
    sandbox: SandboxConfig | None = None,
    max_turns: int = 25,
    token_budget: int | None = None,        # drawn from parent's root pool
    wallclock_timeout_s: int = 300,
    model: LLMClient | None = None,         # inherits parent if None
    on_event: Callable[[Event], None] | None = None,
) -> SpawnResult: ...

async def spawn_many(
    specs: list[SpawnSpec],
    *,
    max_concurrent: int = 3,
    fail_fast: bool = False,
) -> list[SpawnResult]: ...
```

```python
class SpawnResult(BaseModel):
    child_run_id: str
    child_did: str
    status: Literal["completed", "max_iterations", "timeout", "interrupted", "error", "budget_exhausted"]
    summary: str
    tokens: TokenUsage
    tool_trace: list[str]
    audit_chain_tip: str        # for parent to merge back
    duration_s: float
    error: str | None = None
```

#### Hermes Bugs to Avoid (per research)
1. **No implicit token pooling** — root-level `token_budget_remaining` debited atomically by every descendant. New spawns refuse when exhausted; in-flight children get `budget_exhausted`.
2. **No daemon threads for heartbeats** — `asyncio.TaskGroup` for structured concurrency. Cancellation deterministic.
3. **No tool-name globals** — per-`RunState` `ToolRegistry` is current; do not regress.

#### Federal Additions
- Per-child DID via `HKDF(parent_sk, nonce=spawn_id, info="arc-delegate-v1")`. TTL = `wallclock_timeout_s`.
- OTel `trace_id` inherits via Context propagation; child span = `child_of` parent span w/ attribute `arc.delegation.depth`.
- Hash-chained audit: child's first audit entry contains `parent_chain_tip`. Chain merges back deterministically on child completion.
- `spawn.start` event: parent DID + child DID + task hash. `spawn.complete`: exit reason + tokens.

#### Agent-Facing Tool
`arcagent.modules.delegate.delegate` — thin wrapper. Calls `arcrun.spawn(...)`. Schema mirrors `SpawnResult`. `DELEGATE_BLOCKED_TOOLS = frozenset({"delegate", "memory", "send_message", "execute_code", "clarify"})` stripped from any child's tool list.

### §3.6 Memory Architecture (D-09, D-16, Epic D)

#### Two-Tier
```
workspace/
├── identity.md                 # agent identity (admin-controlled)
├── context.md                  # agent working memory (existing)
├── policy.md                   # learned behaviors (existing)
├── bio_memory/                 # existing — agent self/episodic
│   ├── identity/
│   ├── working/
│   └── episodic/
└── user_profile/               # NEW
    ├── {user_did}.md           # per-user profile w/ YAML frontmatter
    └── {user_did}.agents/
        └── {agent_did}.md      # per-agent annotations on a user (federation)
```

#### `user_profile/{user_did}.md` Schema
```yaml
---
user_did: did:arc:...
created: 2026-04-18T...
classification: unclassified | cui | secret
acl:
  owner: <user_did>
  agent_read: true
  cross_user_shareable: false   # federal default
schema_version: 1
---
## Identity
display name, role, org — user-confirmed only

## Preferences
communication style, tone, formats

## Durable Facts
- (each entry: source_session_id, ts) append-only

## Derived (dialectic)
- (regeneratable from sessions after tombstone)
```

**2 KB hard cap on body.** Overflow spills to episodic store (NOT silent truncation).

#### `memory_acl` Module (D-09)
- Subscribes to `memory.read`, `memory.write`, `memory.search` at module-bus priority **10** (highest — runs before existing memory module at 85/90).
- Vetoes via `ctx.veto(reason)` with audit-event side-effect.
- Defense in depth: memory provider re-checks at read.
- **Caller DID bound at TRANSPORT layer** — strip/rewrite `user_id` arguments from LLM-supplied tool call inputs. Model cannot override identity via injection.

#### Capabilities Over ACLs (per research insight)
For each turn, the orchestrator issues a short-lived signed capability: "this module may read user:X:profile for turn:Y." Capability scoped to the turn; expires when turn ends. Memory module refuses any read without a valid capability. ACL on stored object is for admin operations (delete, classify-up).

#### GDPR Tombstone Pattern (per research)
- Signed `user.forgotten` event appended to session JSONL.
- Readers (FTS5 indexer, dialectic derivers) treat as erasure barrier.
- `user_profile/{user_did}.md` deleted outright.
- Session JSONLs redacted FIELD-wise (preserves tool-call audit structure).
- FTS5 rebuilt excluding tombstoned entries.
- Tombstone metadata retained (user_did hash + timestamp) for compliance proof.

### §3.7 Skill Auto-Create Nudge (D-12, Epic E)

#### Wiring
`NudgeEmitter` subscribes to `agent:post_plan`. **Priority correction (2026-04-18):** module_bus uses "lower number runs first." To run AFTER `trace_collector` (priority 200), `NudgeEmitter` uses `EFFECTIVE_PRIORITY=210`. `SDD_STATED_PRIORITY=150` is preserved as a named constant for spec traceability.

#### Trigger Conjunction (all true, AND)
```python
should_nudge = (
    turn.tool_calls_ok >= 5
    and turn.task_outcome == "success"
    and (
        turn.error_count >= 1
        or turn.user_correction_detected
        or turn.max_existing_skill_coverage < 0.3
    )
    and not in_cooldown(session_id)
    and not any(skill.tags & exempt_tags)
)
```

Reuses existing `trace_collector.py` signals (lines 86-205), `guardrails.py` cooldown + exempt_tags (lines 99-121), `candidate_store.py` audit (lines 136-141), `config.py` thresholds (cooloff_turns=200, exempt_tags=[security-critical, compliance, auth]).

#### Dedup (pre-commit)
1. Name collision via `candidate_store._validate_skill_name`.
2. Fingerprint match against `Candidate.fingerprint`.
3. Semantic similarity ≥ 0.85 (mirrors `config.trace_similarity_threshold`).

#### Cooldown
- Max 1 nudge per 50 turns (aligns w/ `trace_buffer_turns=50`).
- Per-skill-shape signature: 200-turn suppression (`cooloff_turns=200`).
- Hard ceiling: 3 nudges per session.

#### Nudge Format (system-message injection)
> "The last turn used N tools successfully, recovered from an error, and doesn't match any existing skill (top coverage X%). If this workflow is likely to recur, consider calling `skill_manage(action='create', ...)`. Skip if one-off. Confirm with the user before committing."

Preserves ASI-09 (agent asks, doesn't act) + LLM06 (no excessive agency).

#### Audit
- `TelemetryEvent("skill_improver.nudge_emitted", {turn_id, session_id, signal_vector, tool_sequence_hash, outcome_source})`.
- Auto-created skill: `MutationEvent(stop_reason="auto_nudge", trace_ids=[turn.trace_id])` via existing `candidate_store.append_audit`.

### §3.8 Skills Hub (D-08, Epic H)

#### Toggle + CLI Gate (default OFF)
`config.toml`:
```toml
[skills.hub]
enabled = false
[skills.hub.tier]
level = "federal"
[skills.hub.policy]
require_signature = true
require_slsa_level = 3
require_scan_pass = true
install_path = "cli_only"           # blocks agent-driven install
max_findings_allowed = { critical = 0, high = 0, medium = 2 }
[[skills.hub.sources]]
name = "arc-official"
type = "github"
repo = "arc-foundation/skills"
trust = "builtin"
signer_identity = "https://github.com/arc-foundation/skills/.github/workflows/publish.yml@refs/heads/main"
signer_issuer = "https://token.actions.githubusercontent.com"
[[skills.hub.sources]]
name = "arc-trusted-partners"
type = "registry"
url = "https://skills.arcagent.dev/v1/index.json"
trust = "trusted"
allowed_publishers = ["anthropics", "openai", "ctg-federal", "doe-*"]
fulcio_root_ca = "/etc/arc/fulcio.pem"
[skills.hub.revocation]
crl_url = "https://skills.arcagent.dev/v1/crl.json"
crl_refresh_interval_seconds = 3600
fail_closed_if_unreachable = true   # federal
```

#### Federal Install Pipeline (Epic H1)
1. Operator: `arc skill hub install <name>` (CLI only — no agent path).
2. Check `enabled = true` AND source on allowlist. Else abort.
3. Download bundle → `~/.arc/skills/.hub/quarantine/<name>` (writable-nothing dir).
4. **Signature verify**: `cosign verify --certificate-identity ... --certificate-oidc-issuer ...` Fulcio cert chain + Rekor inclusion proof.
5. **CRL check**: fetch `crl.json`; fail-closed if unreachable at federal.
6. **Scan**: Hermes regex bank (8 categories) + semgrep `p/security-audit` + `p/python-security` + GuardDog (Datadog) + bandit + custom AST visitor for dynamic-import.
7. Federal: only `safe + signature-valid + SLSA-L3` installs.
8. **Sandbox dry-run**: Firecracker microVM (already in arcrun stack), import skill module, run declared test fixture, kill after 10s. **DO NOT use RestrictedPython** (CVE-2023-41039, CVE-2024-49755).
9. Move quarantine → `skills/<name>`. Write `HubLockFile` entry: `{content_hash, rekor_uuid, slsa_level, scan_verdict, install_path, files, installed_at, updated_at}`.
10. OTel + audit log entry: `INSTALL <name> <source>:<trust> <verdict> <hash> <rekor_uuid>`.

#### Update Model
Lock file pins `content_hash`. `arc skill hub update` re-runs full pipeline. Hash change → re-scan + re-sign-verify. No drift-install.

#### Revocation
- Weekly CRL refresh (configurable).
- On hit: `arc skill hub quarantine <name>` moves to `revoked/` dir; module bus unloads on next agent boot.
- Federal: phone-home at install AND every agent start.

#### Top 3 Attack Patterns Defended Against
1. **Mass typosquat + ClickFix** (ClawHavoc 2026, 1,184 skills) — auto-block on `curl_pipe_shell` + `remote_fetch`. Federal blocks any remote fetch at install OR runtime.
2. **Covert agent-config persistence (ASI06)** — auto-block writes to `CLAUDE.md`/`AGENTS.md`/`identity.md`/`policy/*.toml` from any skill. Telemetry integrity hashes on those paths.
3. **Description-injection** — scan ALL user-visible text fields with injection-pattern bank. Federal: auto-block (no human review).

### §3.9 Executor Backends (D-13, Epic F1)

#### Protocol
```python
@runtime_checkable
class ExecutorBackend(Protocol):
    name: str
    capabilities: BackendCapabilities

    async def run(self, command: str, *, cwd: str | None = None,
                  env: dict[str, str] | None = None,
                  timeout: float = 120.0,
                  stdin: str | None = None) -> ExecHandle: ...

    async def stream(self, handle: ExecHandle) -> AsyncIterator[bytes]: ...
    async def cancel(self, handle: ExecHandle, *, grace: float = 5.0) -> None: ...
    async def close(self) -> None: ...

class BackendCapabilities(BaseModel):
    supports_file_copy: bool
    supports_persistent_workspace: bool
    supports_port_forward: bool
    supports_bind_mount: bool
    cold_start_budget_ms: int   # local≈10, docker≈800, ssh≈300, modal≈400-6000
    max_stdout_bytes: int
    isolation: Literal["none", "container", "vm", "remote"]
```

Optional methods guarded by capabilities: `copy_to`, `copy_from`, `workspace_id`, `port_forward`. Agent consults `backend.capabilities` before calling.

#### Discovery (3-tier, federal-aware)
1. **Built-ins** — `local`, `docker` ship in `arcrun.backends`, imported directly. Always trusted.
2. **Explicit config (primary)** — `arcrun.toml` names backend by dotted import path. Loader imports + verifies `isinstance(obj, ExecutorBackend)`.
3. **Entry points (opt-in, dev-tier ONLY)** — setuptools group `arcrun.executor_backends`. **DISABLED in federal tier.** Federal: backend must be in signed `allowed_backends` manifest; Ed25519 signature verify against Arc signing cert before import.

#### Hermes Pattern: `_ThreadedProcessHandle`
For SDK-only backends (Modal, Daytona) with no real subprocess, wrap `(exec_fn, cancel_fn)` behind `os.pipe`. Worker thread writes to pipe so unified poll/drain loop works. **Critical for plugging wildly different backends behind one Protocol.**

#### Streaming + Cancellation
- Async iterator of bytes (NOT lines — ANSI/binary safe). Backpressure via `asyncio.Queue(maxsize=N)`.
- Hard truncation at `capabilities.max_stdout_bytes` w/ marker frame.
- ANSI stripped at display layer (Hermes `tools/ansi_strip.py`), NOT at capture — audit logs keep raw bytes.
- Cancel: SIGTERM, wait `grace`, SIGKILL. Local backend MUST `os.setsid` + `killpg` (Hermes hit orphaned-pgroup bug in production).

### §3.10 arctui Completion (D-15, Epic G)

#### Architecture
Single Python process. Textual `App` lives alongside arcagent's asyncio loop in the same event loop. No subprocess split (Hermes-style Ink/Node bridge avoided — that pattern is justified only by React ecosystem dep, which Arc doesn't need).

#### Components
| Component | File | Source |
|---|---|---|
| Main app | `arctui/app.py` | NEW |
| Transcript view | `arctui/transcript.py` | streams from arcagent.core.context_manager |
| Activity panel | `arctui/activity.py` | subscribes to arcrun event bus |
| Approval prompts | `arctui/prompts.py` | hooks existing approval workflow |
| Slash command completer | `arctui/command_completer.py` | reads `arccli.commands.registry` |

### §3.11 Centralized Slash Command Registry (D-17, Epic J)

> **arccli = terminal slash-command interface** (not Click). The legacy Click-based `arccli/main.py` is migrated to a slash-command REPL. Only `arcui` retains Click (it's the web admin shell). The centralized registry is the single source of truth for arccli, arctui, arcgateway, and platform adapters.

#### `CommandDef` Dataclass
```python
@dataclass(frozen=True)
class CommandDef:
    name: str                                 # canonical, no leading slash
    description: str
    category: Literal["Session", "Configuration", "Tools & Skills", "Info", "Exit"]
    aliases: tuple[str, ...] = ()
    args_hint: str = ""
    cli_only: bool = False
    gateway_only: bool = False
    gateway_config_gate: str | None = None    # config dotpath; truthy → command available
    handler: Callable | None = None           # resolved at registration; not stored in CommandDef
```

#### Six Consumers
1. **arccli dispatch** — `process_command()` resolves alias via `resolve_command()`; dispatches on canonical name.
2. **arcgateway dispatch** — same `resolve_command()`; respects `gateway_only` and `cli_only` filters.
3. **arccli help** — `COMMANDS_BY_CATEGORY` dict generates `show_help()`.
4. **arcgateway help** — `gateway_help_lines()`.
5. **Telegram BotCommand menu** — `telegram_bot_commands()`.
6. **Slack subcommand routing** — `slack_subcommand_map()`.
7. **Autocomplete** — `COMMANDS` flat dict feeds `SlashCommandCompleter`.

#### Adding a Command
1. Add a `CommandDef` to `COMMAND_REGISTRY`.
2. Add handler in `arccli.commands.handlers.<name>.handle(...)`.
3. If gateway-relevant: add handler in `arcgateway.commands.<name>.handle(...)`.

That's it. Six surfaces auto-update.

#### Federal Tier Filter
Command catalog filtered at render by tier-permission. `GATEWAY_KNOWN_COMMANDS` always includes config-gated commands (gateway can dispatch); help/menus only show when gate is open. Tier permission check at render layer, not filter layer.

## §4 Cross-Cutting Concerns

### §4.1 Tier Propagation
All new code reads tier from `config.security.tier ∈ {"federal", "enterprise", "personal"}`. Tier flows from arcagent core → all modules → arcgateway. Single policy enforcement point per package; no `if tier == "federal"` scattered through business logic.

### §4.2 Audit Surface Additions
New audit event types (all flow through existing `arcagent.core.telemetry`):
- `gateway.adapter.{connect,disconnect,fail,reconnect}`
- `gateway.session.{create,resume,close,executor_choice}`
- `gateway.message.{received,acked,sent,deduped}`
- `gateway.pairing.{requested,approved,denied,expired,locked_out}`
- `session.search.queried`, `session.acl.veto`, `session.acl.cross_session_read`
- `cron.parsed`, `cron.session.disabled_tools`, `cron.delivered`, `cron.skipped_silent`
- `skill_improver.nudge_emitted`, `skill.auto_created`
- `memory.user_profile.{read,write,gc,tombstone}`
- `delegate.{spawn_start,spawn_complete,budget_exhausted}`
- `executor.backend.{loaded,signature_verified,denied}`
- `skills_hub.{enabled,install_started,install_completed,scan_failed,sig_failed,crl_hit,quarantined}`
- `voice.transcribed`, `web.search`, `web.extract`, `browser.navigate`

### §4.3 OTel Span Conventions
All new spans use GenAI semantic conventions where applicable. Cross-package spans use parent-child propagation via OTel Context API. New attributes:
- `arc.tier`: "federal" | "enterprise" | "personal"
- `arc.session.id`, `arc.user.did`, `arc.agent.did`
- `arc.delegation.depth`
- `arc.classification`
- `gateway.platform`, `gateway.adapter.healthy`

### §4.4 Threat Surface Coverage
| OWASP | Mitigation in SPEC-018 |
|---|---|
| LLM01 (Prompt Injection) | §3.6 caller DID bound at transport; §3.8 description-injection scan |
| LLM02 (Sensitive Info) | §3.6 ACL filter at retrieval; voice transcripts PII-redacted |
| LLM03 (Supply Chain) | §3.8 Sigstore + SLSA L3; §3.9 federal-signed-backend manifest |
| LLM05 (Output Handling) | §3.4 sanity-validation on cron outputs; §3.5 SpawnResult structured failure |
| LLM06 (Excessive Agency) | §3.5 child tool-allowlist intersection; §3.7 nudge is advisory not command |
| LLM07 (Prompt Leakage) | §3.6 system prompt has no secrets (existing); §3.10 TUI never displays system prompt by default |
| LLM10 (Unbounded Consumption) | §3.5 root-pooled token budget; §3.4 cron sanity check (no every-second runs) |
| ASI01 (Goal Hijack) | identity.md immutable; §3.8 covert-config-persistence auto-block |
| ASI02 (Tool Misuse) | §3.5 DELEGATE_BLOCKED_TOOLS; §3.4 self-scheduling tool-registry-removal |
| ASI03 (Identity Abuse) | §3.5 per-child DID via HKDF; §3.6 caller DID at transport |
| ASI04 (Supply Chain) | §3.8 Sigstore/Rekor/CRL; §3.9 backend signature verify |
| ASI05 (RCE) | §3.8 Firecracker dry-run (NOT RestrictedPython); §3.9 docker/ssh isolated |
| ASI06 (Memory Poisoning) | §3.6 memory_acl bus veto; §3.8 covert-config writes auto-block |
| ASI07 (Inter-agent Comms) | mTLS (existing); §3.5 child audit chain |
| ASI08 (Cascading Failures) | §3.1 per-adapter TaskGroup; §3.5 spawn timeout + budget |
| ASI09 (Trust Exploitation) | §3.7 nudge requires user confirmation; §3.1 DM pairing |
| ASI10 (Rogue Agents) | All audit events feed existing telemetry; identity revocation existing |

## §5 Module Boundary Rules (per Arc CLAUDE.md "don't mix concerns")

| Boundary | Rule |
|---|---|
| `arcllm` ←→ `arcrun` | arcrun calls `model.invoke(messages, tools)`. Never `load_model()`. |
| `arcrun` ←→ `arcagent` | arcrun executes loops; arcagent owns tools/skills/memory. Spawn primitive in arcrun, agent-facing tool in arcagent. |
| `arcgateway` ←→ `arcagent` | arcgateway depends on arcagent (it's "the daemon that runs ArcAgents"). arcagent has zero knowledge of arcgateway. |
| `arctui` ←→ `arcagent` | arctui consumes arcagent events; never modifies agent state directly. Slash commands resolved through registry. |
| `arccli` ←→ all | arccli is the user-facing entry point. It composes other packages. Other packages do not import arccli except for command registry consumption (which lives in `arccli.commands.registry` — minimal API surface). |
| `arcskill` ←→ `arcagent` | arcskill discovers + verifies + installs skills. arcagent loads them at agent startup. |

**Concrete tests** (cross-package import discipline):
- `tests/architecture/test_no_arcrun_calls_load_model.py` — AST scan; fail if found
- `tests/architecture/test_no_arcagent_imports_arcgateway.py` — same pattern
- `tests/architecture/test_arccli_command_registry_minimal_surface.py` — only `CommandDef`, `COMMAND_REGISTRY`, `resolve_command`, `commands_by_category` exported

## §6 Open Design Questions (deferred to per-feature SDDs)

| Question | Where to decide |
|---|---|
| arcgateway NATS-vs-in-process queue for >1 instance | SDD-019-arcgateway |
| FTS5 indexer crash recovery rebuild strategy detail | SDD-020-session-search |
| Skill hub `crl.json` exact schema + push protocol | SDD-027-skills-hub |
| Voice provider plugin manifest format | SDD-029-voice-module |
| Browser sandbox mode-strict implementation (forced remote) | SDD-031-browser-module |
| arctui keybindings scheme | SDD-026-arctui |

## §7 References

- **Build decisions**: `.claude/decisions-log.md` (Hermes-Parity Roadmap section, D-01 through D-17)
- **Research insights**: `.claude/decisions-log.md` (Hermes-Parity Roadmap — Deepening Insights section, 8 agent reports)
- **Hermes Agent reference architecture**: NousResearch/hermes-agent — `gateway/run.py`, `gateway/platforms/base.py`, `gateway/session.py`, `gateway/pairing.py`, `cron/scheduler.py`, `tools/delegate_tool.py`, `tools/skills_hub.py`, `tools/skills_guard.py`, `tools/environments/base.py`, `tools/skill_manager_tool.py`, `tools/memory_tool.py`, `hermes_state.py`, `hermes_cli/commands.py`
- **Existing Arc components extended**: `arcagent.modules.{bio_memory,memory,policy,skill_improver,scheduler,vault_azure,slack,telegram}` — preserved interfaces; additive changes only
- **Standards**: NIST 800-53 (AU-2/9/10/11, AC-3/4, IA-3/5, SC-8/12/28, SI-12), FedRAMP Rev5, FIPS 140-3, EO 14028, SLSA v1.1, in-toto, Sigstore (cosign + Fulcio + Rekor)
- **Papers**: ACE: Agentic Context Engineering (arXiv:2510.04618), Voyager (arXiv:2305.16291), KV-Cache Sharing in Multi-Tenant LLM Serving (NDSS 2025)
