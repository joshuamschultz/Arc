# ArcAgent Design Document v3
## Memory, Skills, Tools, Self-Learning, Scheduling, and Workspace Architecture
### Implementation Language: Python

---

## 1. Memory System Comparison

### How Each System Works

#### OpenClaw — File-Based, Human-Readable
- **MEMORY.md**: Long-lived curated facts, injected into system prompt every turn. Agent writes/edits directly via file tools.
- **memory/YYYY-MM-DD.md**: Daily append-only notes. Today + yesterday auto-loaded; older files accessed via `memory_search`.
- **Hybrid Search**: BM25 keyword + vector similarity (70/30 weighted). Chunks ~400 tokens.
- **Compaction Flush**: Before context compaction, agent dumps important context to MEMORY.md so nothing is lost.
- **Entity Handling**: None. No extraction, no knowledge graph. Everything is flat markdown.
- **Strengths**: Dead simple. Human-readable. Git-diffable. Auditable. Zero infrastructure.
- **Weaknesses**: No entity resolution. No temporal reasoning. No contradiction detection. Memory quality depends entirely on LLM's discipline. MEMORY.md can bloat.

#### MemGPT/Letta — OS-Inspired Tiered Memory
- **Core Memory** (always in-context): Split into `persona` (agent identity, self-editable) and `human` (user info, self-editable). Fixed token budget.
- **Recall Memory** (out-of-context): Searchable database of conversation history. Vector-indexed.
- **Archival Memory** (out-of-context): Long-term vector store (Chroma, pgvector). Agent explicitly moves data in/out.
- **Self-Editing**: Agent manages memory via tool calls (`memory_insert`, `memory_replace`, `conversation_search`, `archival_memory_search`).
- **Strategic Forgetting**: Context full → conversation compacted into recursive summary. Old messages still searchable.
- **Strengths**: Elegant OS metaphor. Agent has explicit control. Unbounded context illusion. Well-researched.
- **Weaknesses**: Tool-driven = latency + token cost. No entity extraction. No temporal reasoning. Complex infra (Docker, PostgreSQL).

#### Supermemory — Graph-Enhanced Memory-as-a-Service
- **Documents → Memories**: Input ingested and broken into interconnected "memories" with relationships (supersedes, extends, inferred).
- **Knowledge Graph**: Typed relationships. Automatic entity linking and inference.
- **Smart Forgetting**: Decay, recency bias, context rewriting.
- **Temporal Reasoning**: Tracks `isLatest`, document dates, relationship evolution.
- **Infrastructure**: Managed on Cloudflare (Durable Objects, KV, PostgreSQL). Sub-300ms recall.
- **Strengths**: Best recall benchmarks (LongMemEval). Graph relationships enable multi-hop reasoning. Fast.
- **Weaknesses**: External SaaS dependency. Not self-hostable for air-gapped federal. Opaque. Cost at 10K agents.

#### Claude Memory — Transparent File-Based Summaries
- **claude.ai**: Curated memory summary from chat history. User can view/edit. Project-scoped.
- **Claude Code**: `CLAUDE.md` (user instructions) + auto-memory `MEMORY.md` (Claude's notes). First 200 lines loaded.
- **Search**: `conversation_search` (keyword) + `recent_chats` (time-based). No vector search.
- **Strengths**: Maximum transparency. Simple. No infrastructure.
- **Weaknesses**: Context window is the bottleneck. No entities/graph/temporal. "Fading memory" as files grow.

### Comparison Matrix

| Feature | OpenClaw | MemGPT/Letta | Supermemory | Claude |
|---------|----------|-------------|-------------|--------|
| **Storage** | Markdown files | PostgreSQL + Vector DB | Managed cloud graph | Markdown files |
| **In-Context** | MEMORY.md (full) | Core blocks (persona+human) | Retrieved chunks | Summary (200 lines) |
| **Entity Extraction** | ❌ | ❌ | ✅ Auto | ❌ |
| **Knowledge Graph** | ❌ | ❌ | ✅ | ❌ |
| **Temporal Reasoning** | ❌ | ❌ | ✅ | ❌ |
| **Hybrid Search** | ✅ BM25+vector | ✅ Vector | ✅ Vector+graph | ❌ Keyword only |
| **Self-Editing** | ✅ File writes | ✅ Tool calls | ❌ (external) | ✅ File writes |
| **Auditable** | ✅ Git-diffable | Partial | ❌ Opaque | ✅ Visible |
| **Self-Hostable** | ✅ | ✅ | ❌ | ❌ |
| **Federal Ready** | Partial | No | No | No |
| **Infra Required** | None | Docker+PostgreSQL | API key | None |

### Also Worth Noting

- **Zep/Graphiti**: Temporal knowledge graph. Neo4j backend. Hybrid retrieval (semantic + BM25 + graph traversal). No LLM calls at query time. Heavy infra.
- **Mem0**: Hierarchical memory (user/session/agent levels). Optional graph layer. Simple `add()`/`search()` API. More "smart store" than full engine.
- **Memvid**: Everything in one portable `.mv2` file. BM25 + vector. Built-in entity extraction. Zero infra. Interesting for portable/auditable use case.
- **Martian agent-memory**: Three-layer bash-based system (knowledge graph + daily notes + tacit knowledge). File-based entities with JSONL facts, contradiction detection, recency scoring. Zero dependencies beyond `jq`.

---

## 2. ArcAgent Memory Architecture

### Default: "Structured Markdown + Lightweight Entity Layer"

Start with what works (OpenClaw simplicity + auditability) and add what's missing (entity extraction + hybrid search). Ship this as default. Everything else is a plugin.

### Memory Tiers

```
┌─────────────────────────────────────────────────────────┐
│                   IN-CONTEXT (loaded every turn)         │
│                                                          │
│  ┌──────────────┐  ┌──────────────┐  ┌───────────────┐  │
│  │ identity.md  │  │  policy.md   │  │ context.md    │  │
│  │ (who am I)   │  │ (learned     │  │ (curated      │  │
│  │              │  │  behaviors)  │  │  working mem)  │  │
│  └──────────────┘  └──────────────┘  └───────────────┘  │
│                                                          │
│  Token budget enforced. Combined max: ~4K tokens default │
└─────────────────────────────────────────────────────────┘
                           │
                    search / retrieve
                           │
┌─────────────────────────────────────────────────────────┐
│                  OUT-OF-CONTEXT (on-demand)              │
│                                                          │
│  ┌──────────────────────────────────────────────────┐   │
│  │  Notes (append-only daily logs)                   │   │
│  │  notes/YYYY-MM-DD.md                              │   │
│  │  Accessed via memory_search tool                  │   │
│  └──────────────────────────────────────────────────┘   │
│                                                          │
│  ┌──────────────────────────────────────────────────┐   │
│  │  Entities (structured knowledge)                  │   │
│  │  entities/{name}/facts.jsonl                      │   │
│  │  entities/{name}/summary.md                       │   │
│  │  entities/index.json                              │   │
│  │  Accessed via entity_search / entity_state tools  │   │
│  └──────────────────────────────────────────────────┘   │
│                                                          │
│  ┌──────────────────────────────────────────────────┐   │
│  │  Archive (compacted old conversations)            │   │
│  │  archive/session-{id}.jsonl                       │   │
│  │  Accessed via archive_search tool                 │   │
│  └──────────────────────────────────────────────────┘   │
│                                                          │
│  Hybrid search: BM25 + vector (configurable weights)    │
│  Default: 70/30 keyword/semantic                        │
└─────────────────────────────────────────────────────────┘
```

### Lightweight Entity Extraction

File-based, async, inspired by Martian Engineering's agent-memory pattern.

**On every conversation turn (async, post-response):**
1. LLM-driven NER extracts entities from conversation
2. Check `entities/index.json` for existing match
3. New → create `entities/{name}/facts.jsonl` + `summary.md`
4. Exists → append fact, check contradictions (same predicate, different value), mark old as `historical`
5. Periodically synthesize: regenerate `summary.md` from active facts

**Entity fact format (JSONL):**
```json
{"id": "f001", "subject": "ACME Corp", "predicate": "CEO", "object": "Jane Smith", "source": "session-abc-turn-42", "ts": "2026-02-14T10:30:00Z", "status": "active", "confidence": 0.9}
{"id": "f002", "subject": "ACME Corp", "predicate": "CEO", "object": "John Doe", "source": "session-abc-turn-98", "ts": "2026-02-14T14:00:00Z", "status": "active", "supersedes": "f001"}
```

**Why this approach:** Files, not databases. Auditable, git-diffable, portable. Async (doesn't slow responses). Contradiction detection via predicate matching. Temporal via timestamps + `supersedes` chains. Swappable to Neo4j/Graphiti via plugin interface.

### Memory Tools (Exposed to Agent)

| Tool | Purpose |
|------|---------|
| `memory_read` | Read context.md |
| `memory_write` | Write/update context.md entries |
| `memory_search` | Hybrid search across notes + entities + archive |
| `entity_search` | Find entities by name or attribute |
| `entity_state` | Get current state of a specific entity |
| `note_append` | Add to today's daily notes |

### Plugin Interface for Alternative Backends

```python
class MemoryProvider(Protocol):
    name: str
    
    async def initialize(self, config: MemoryConfig) -> None: ...
    async def shutdown(self) -> None: ...
    
    async def store(self, entry: MemoryEntry) -> str: ...
    async def search(self, query: str, options: SearchOptions | None = None) -> list[MemoryResult]: ...
    async def retrieve(self, id: str) -> MemoryEntry | None: ...
    
    # Optional entity operations
    async def extract_entities(self, text: str, context: str | None = None) -> list[Entity]: ...
    async def get_entity_state(self, name: str) -> EntityFacts | None: ...
    async def search_entities(self, query: str) -> list[Entity]: ...
    
    async def compact(self, options: CompactOptions | None = None) -> None: ...
    async def export(self, format: str = "json") -> str: ...
```

---

## 3. Session Files: OpenClaw 8 → ArcAgent 3

### What OpenClaw Has (Complete Inventory)

**AGENTS.md** — Primary instruction file
- Priority ordering (what matters most)
- Workflow boundaries (what to do, what NOT to do)
- Quality bar and output standards
- Platform-specific formatting rules (Discord vs WhatsApp vs email)
- Heartbeat behavior instructions
- Memory management rules (when to read MEMORY.md, when not to)
- Security rules (don't exfiltrate data, ask before destructive actions)
- Group chat behavior (when to speak, when to stay silent)
- Bootstrap instructions (read BOOTSTRAP.md on first run)
- Session startup ritual (which files to read on wake)
- Skill usage instructions (check SKILL.md when needed)
- Tool notes (references to TOOLS.md)
- Cron job behavior guidelines

**SOUL.md** — Behavioral core / personality
- Voice and temperament ("be genuinely helpful, not performatively helpful")
- Values and ethics ("you're a guest in someone's life")
- Personality traits ("have opinions, disagree, find things amusing")
- Non-negotiable constraints ("don't exfiltrate data. ever.")
- Resourcefulness philosophy ("try to figure it out before asking")
- Trust-earning guidance ("be careful with external actions, bold with internal ones")
- Memory philosophy ("each session you wake up fresh, these files are your memory")
- Self-modification note ("if you change this file, tell the user")

**USER.md** — User preferences
- Communication tone preferences
- Output formatting preferences
- Recurring preferences (timezone, language, formality level)
- Known constraints (availability, tools they use, platforms)
- Personal context (role, company, projects)

**IDENTITY.md** — Presentation identity
- Name
- Role/title
- Short bio / one-liner
- Goals
- Voice description (for TTS)
- Applied via CLI (`openclaw agents set-identity --from-identity`)

**TOOLS.md** — Tool-specific notes
- Local tool configurations (SSH details, camera names, API endpoints)
- Tool-specific quirks and workarounds
- Voice preferences (TTS settings)
- Custom tool usage notes that don't belong in skills

**HEARTBEAT.md** — Heartbeat checklist
- What to check on each heartbeat (inboxes, tasks, blockers)
- Frequency guidance (2-4 times per day)
- Memory maintenance tasks (review daily files, update MEMORY.md)
- Quiet time rules (respect when not to ping)

**BOOT.md** — Session startup hook
- Files to read on startup
- Initial orientation tasks
- Optional — only runs when hooks enabled

**BOOTSTRAP.md** — First-run interview
- Conversational discovery script
- Guides agent through initial setup
- Writes identity/soul/user files from conversation
- Self-deletes after completion

**MEMORY.md** — Long-term curated memory
- Persistent facts and compressed history
- Agent-maintained (read/write in main session only)
- Security: NOT loaded in group/shared sessions

### The Consolidation: ArcAgent's identity.md

All 8 files → 1 file with clear sections. Comments show what came from where.

```markdown
# Identity

<!-- ============================================================ -->
<!-- SECTION: Core Identity                                        -->
<!-- Source: IDENTITY.md (name, role, bio, goals)                  -->
<!-- Source: SOUL.md (values, voice, temperament)                  -->
<!-- Mutability: READ-ONLY to agent. Admin edits only.             -->
<!-- ============================================================ -->

## Who I Am
- **Name**: [agent name]
- **Role**: [agent role / title]
- **Organization**: [org name]
- **Bio**: [one-liner description]

## Personality & Voice
<!-- from SOUL.md: behavioral core, temperament, values -->
- Be genuinely helpful, not performatively helpful
- Skip filler phrases — just help
- Have opinions. Disagree when warranted. Be direct.
- Be resourceful before asking — read the file, check context, search first
- Earn trust through competence, not compliance

## Values & Ethics
<!-- from SOUL.md: non-negotiable constraints -->
- You're a guest with access to someone's work. Treat it accordingly.
- Never exfiltrate private data
- Be careful with external actions (emails, messages, anything public)
- Be bold with internal actions (reading, organizing, learning, analyzing)
- When in doubt, ask before acting externally

<!-- ============================================================ -->
<!-- SECTION: User Preferences                                     -->
<!-- Source: USER.md (tone, formatting, constraints, context)       -->
<!-- Mutability: READ-ONLY to agent. User/admin edits.             -->
<!-- ============================================================ -->

## User Preferences
- **Communication tone**: [direct/formal/casual/etc]
- **Output format**: [numbered lists for actions, prose for analysis, etc]
- **Timezone**: [timezone]
- **Language**: [language]
- **Formality**: [match their level / always formal / etc]

## User Context
- **Name**: [user name]
- **Role**: [user role]
- **Key projects**: [active projects]
- **Known constraints**: [availability, platforms, tools they use]

<!-- ============================================================ -->
<!-- SECTION: Operating Instructions                                -->
<!-- Source: AGENTS.md (priorities, workflow, boundaries, quality)   -->
<!-- Mutability: READ-ONLY to agent. Admin edits only.             -->
<!-- ============================================================ -->

## Priority Order
<!-- from AGENTS.md: what matters most -->
1. [highest priority tasks/domains]
2. [second priority]
3. [third priority]

## Workflow & Boundaries
<!-- from AGENTS.md: what to do and NOT do -->
- [key workflow rules]
- [what requires approval vs autonomous action]
- [domains/actions that are off-limits]

## Quality Bar
<!-- from AGENTS.md: output standards -->
- [quality expectations for deliverables]
- [formatting standards]
- [review/verification requirements]

## Platform Behavior
<!-- from AGENTS.md: channel-specific rules -->
- [how to behave in different channels/contexts]
- [group chat rules: when to speak, when to stay silent]
- [formatting rules per platform if multi-channel]

## Security Rules
<!-- from AGENTS.md + SOUL.md: data protection, action gating -->
- [what requires human approval]
- [data handling constraints]
- [classification/clearance rules]

<!-- ============================================================ -->
<!-- SECTION: Available Tools                                       -->
<!-- Source: TOOLS.md (tool notes, configs, quirks)                 -->
<!-- Mutability: READ-ONLY to agent. Admin edits.                  -->
<!-- ============================================================ -->

## Tools
<!-- from TOOLS.md: what tools are available and tool-specific notes -->
- [tool_name]: [what it does, any quirks or config notes]
- [tool_name]: [endpoint, access notes]
- [tool_name]: [usage constraints]

<!-- ============================================================ -->
<!-- SECTION: Startup Behavior                                      -->
<!-- Source: BOOT.md (session startup ritual)                       -->
<!-- Source: BOOTSTRAP.md (first-run only — handled by plugin)     -->
<!-- Source: AGENTS.md (session startup instructions)               -->
<!-- Mutability: READ-ONLY to agent. Admin edits.                  -->
<!-- ============================================================ -->

## On Session Start
<!-- from BOOT.md + AGENTS.md session startup instructions -->
- Read identity.md, context.md, policy.md (automatic — handled by runtime)
- [any additional startup tasks specific to this agent]
- [orientation checks to run on fresh session]

## On First Run
<!-- from BOOTSTRAP.md — typically handled by provisioning, not interactive -->
<!-- For interactive first-run, use the bootstrap extension/plugin -->
- [first-run provisioning notes if needed]
```

### context.md — Working Memory (Agent-Maintained)

```markdown
# Context

<!-- ============================================================ -->
<!-- Agent-maintained working memory. Curated, compact.            -->
<!-- Source: Replaces MEMORY.md from OpenClaw                      -->
<!-- Hard limit: configurable max tokens (default ~2K)             -->
<!-- Agent MUST curate — remove stale, compress verbose entries    -->
<!-- Mutability: AGENT READ/WRITE                                  -->
<!-- ============================================================ -->

## Active Projects
- [project name]: [status, key details, deadlines]

## Key Facts
- [important facts the agent needs to remember across sessions]

## Pending Items
- [things waiting on user input, blocked tasks, queued actions]

## Recent Decisions
- [decisions made that affect future work]
```

### policy.md — Self-Learning Behaviors (Agent-Maintained)

```markdown
# Policy

<!-- ============================================================ -->
<!-- Agent-maintained behavioral learning file.                    -->
<!-- Starts EMPTY for every new agent.                             -->
<!-- Agent evaluates its work, writes lessons learned here.        -->
<!-- Ranked by effectiveness. Top = most impactful.                -->
<!-- Agent promotes, modifies, or removes bullets based on results -->
<!-- Mutability: AGENT READ/WRITE                                  -->
<!--                                                               -->
<!-- Bullet format:                                                -->
<!-- - [lesson text] [score:N, reviewed:DATE, uses:N]              -->
<!--                                                               -->
<!-- Scoring:                                                      -->
<!--   Positive outcome → score++ and move up                     -->
<!--   Partially helpful → rewrite text, keep score               -->
<!--   Not helpful/harmful → score-- (remove at ≤0)              -->
<!--   Unused 30+ days → score decays by 1/period                -->
<!-- ============================================================ -->
```

---

## 4. Scheduling: Prompts Live With Schedule Entries

### No HEARTBEAT.md — Schedule IS the Instruction

Each schedule entry carries its own prompt. The agent reads/writes these via tools. No separate file to maintain.

```python
@dataclass
class ScheduleEntry:
    id: str                          # Auto-generated, used by arcTeam to manage
    type: Literal["cron", "once", "interval"]
    prompt: str                      # What the agent should do when triggered
    enabled: bool = True
    
    # Type-specific fields
    expression: str | None = None    # Cron expression (type="cron")
    at: datetime | None = None       # ISO datetime (type="once")
    every: str | None = None         # Duration string (type="interval", e.g. "30m")
    
    # Constraints
    active_hours: ActiveHours | None = None  # start/end/timezone
    max_retries: int = 1
    timeout_seconds: int = 300
    
    # Audit
    metadata: ScheduleMetadata = field(default_factory=ScheduleMetadata)

@dataclass
class ScheduleMetadata:
    created_by: Literal["agent", "admin", "user", "system"] = "agent"
    created_at: str = ""
    reason: str = ""                 # Why this was scheduled
    source_session: str = ""         # Originating session
    last_run: str | None = None
    last_result: str | None = None   # "ok", "action_taken", "error"
    run_count: int = 0
```

### Example: Three Different Crons

```json
[
  {
    "id": "heartbeat_default",
    "type": "interval",
    "every": "30m",
    "prompt": "Quick scan: review context.md for anything time-sensitive. If nothing needs attention, reply HEARTBEAT_OK.",
    "enabled": true,
    "active_hours": {"start": "08:00", "end": "18:00", "timezone": "America/Chicago"},
    "metadata": {"created_by": "system", "reason": "Default heartbeat"}
  },
  {
    "id": "sched_email_check",
    "type": "cron",
    "expression": "0 8 * * 1-5",
    "prompt": "Check email inbox for anything urgent from NNSA contacts. Summarize findings. If anything needs response today, draft replies for user review.",
    "enabled": true,
    "metadata": {"created_by": "agent", "reason": "User asked for daily morning email triage"}
  },
  {
    "id": "sched_procurement_deadlines",
    "type": "cron",
    "expression": "0 15 * * 5",
    "prompt": "Review active procurement deadlines in context.md. Any due within 7 days? Draft reminder emails for user review. Update context.md with current status.",
    "enabled": true,
    "metadata": {"created_by": "agent", "reason": "Deadlines were nearly missed in February"}
  }
]
```

### Agent Creates Its Own Schedules

The agent has full CRUD on schedules:

| Tool | Purpose |
|------|---------|
| `schedule_create` | Create new schedule entry (expression + prompt + config) |
| `schedule_list` | List all active/inactive schedules |
| `schedule_update` | Modify prompt, timing, active_hours, enable/disable |
| `schedule_cancel` | Disable or delete a schedule |

**The agent writes both the WHEN and the WHAT.** It decides "I should check procurement deadlines every Friday" and creates the cron + the instructions in one tool call.

### arcTeam Visibility (Future)

Schedule store is `workspace/schedules.json` — readable/writable by both agent and arcTeam. arcTeam will:
- View all agent schedules across fleet
- Override/cancel agent-created schedules
- Set global limits (max per agent, max frequency)
- Monitor execution (did it run? what happened?)
- Aggregate schedules for capacity planning

---

## 5. Skills vs Tools Architecture

### Clean Separation (Pi-style)

**Tools** = executable functions the LLM can call. They DO things.  
**Skills** = knowledge files that teach the agent HOW to use tools. They INFORM.

Skills are documentation. They can declare tool requirements (metadata) but can't define tools or triggers. The agent reads a skill, learns a workflow, then uses real tools to execute.

### Skill Format

```markdown
---
name: email-triage
description: Triage and categorize incoming emails
requires:
  tools: [email_read, email_label, email_move]
  mcps: [gmail-mcp]
---

# Email Triage Skill

## When to Use
When the user asks you to check, triage, or organize email.

## Workflow
1. Use `email_read` to fetch unread messages (last 24h)
2. Categorize each: Urgent / Action / FYI / Noise
3. Label and archive accordingly
4. Summarize results

## Quality Notes
- Never auto-delete. Only archive.
- If unsure about urgency, err toward "action"
```

### Tool Interface (Language-Agnostic)

```python
@dataclass
class ToolDefinition:
    name: str
    description: str
    parameters: dict                  # JSON Schema
    transport: Literal["native", "mcp", "http", "process"]
    
    # Transport-specific
    handler: Callable | None = None   # For native (Python in-process)
    endpoint: str | None = None       # MCP URI, HTTP URL, or CLI command
    
    # Security & observability
    permissions: ToolPermissions = field(default_factory=ToolPermissions)
    audit: bool = True
    timeout_ms: int = 30000
    sandbox: Literal["container", "wasm", "none"] = "none"
```

**Transport types:**
- `native`: Python function in-process. Fastest. For core tools.
- `mcp`: MCP server (any language). Standard protocol. For extensions/third-party.
- `http`: REST API. For remote services.
- `process`: Spawn subprocess. Language-agnostic. For CLI tools.

### MCP Integration (First-Class, Secure)

```python
@dataclass
class MCPRegistration:
    name: str
    uri: str
    transport: Literal["stdio", "sse", "http"]
    
    permissions: MCPPermissions = field(default_factory=MCPPermissions)
    telemetry: MCPTelemetry = field(default_factory=MCPTelemetry)
    health_check: HealthCheck = field(default_factory=HealthCheck)

@dataclass
class MCPPermissions:
    allowed_tools: list[str] | Literal["*"] = "*"
    blocked_tools: list[str] = field(default_factory=list)
    require_approval: list[str] = field(default_factory=list)
    max_calls_per_minute: int = 60
    sandbox: bool = False

@dataclass
class MCPTelemetry:
    log_calls: bool = True
    log_latency: bool = True
    alert_on_error: bool = True
    audit_trail: bool = True          # Full audit for compliance
```

---

## 6. Agent Workspace: Where Does Everything Live?

### The Complete Workspace Structure

```
workspace/
│
├── identity.md              # WHO — read-only to agent (admin-controlled)
├── context.md               # WHAT — agent working memory (agent read/write)
├── policy.md                # HOW — learned behaviors (agent read/write)
│
├── notes/                   # Daily logs (agent append-only)
│   ├── 2026-02-14.md
│   └── 2026-02-15.md
│
├── entities/                # Extracted knowledge (agent-maintained, async)
│   ├── index.json
│   └── acme-corp/
│       ├── facts.jsonl
│       └── summary.md
│
├── skills/                  # Knowledge files — loaded on demand
│   ├── email-triage/
│   │   └── SKILL.md
│   ├── procurement-analysis/
│   │   ├── SKILL.md
│   │   └── references/
│   │       └── far-clauses.md
│   └── _agent-created/      # Skills the agent built itself
│       └── vendor-scoring/
│           └── SKILL.md
│
├── library/                 # Agent-created reusable artifacts
│   ├── scripts/             # Utility scripts the agent wrote
│   │   ├── parse-sam-data.py
│   │   └── format-itar-report.py
│   ├── templates/           # Reusable templates
│   │   ├── weekly-report.md
│   │   ├── vendor-eval.md
│   │   └── email-followup.md
│   └── prompts/             # Saved prompt patterns that worked
│       ├── extract-entities-from-rfp.md
│       └── summarize-meeting-notes.md
│
├── sessions/                # Active session transcripts (JSONL)
│   └── {session-id}.jsonl
│
├── archive/                 # Compacted old sessions
│   └── {session-id}.jsonl
│
├── schedules.json           # All cron/interval/once schedule entries
│
└── config.json              # Agent-level config (model, extensions, etc.)
```

### Why `library/` Instead of Putting Everything in `skills/`?

Skills have a specific meaning: they teach the agent workflows via SKILL.md. But the agent will also create things that aren't skills:

- **Scripts**: A Python script to parse SAM.gov data isn't a skill — it's a utility. The agent wrote it, tested it, and wants to reuse it.
- **Templates**: A markdown template for weekly reports isn't a skill — it's a starting point the agent fills in each time.
- **Prompts**: A carefully crafted prompt for extracting entities from RFPs isn't a skill — it's a reusable prompt pattern.

Putting these in `skills/` would dilute the meaning. `library/` is the agent's personal toolkit — things it built for itself, organized by type.

### How the Agent Uses `library/`

The agent knows `library/` exists (referenced in the system prompt). When it creates something reusable:

1. **Scripts**: Writes to `library/scripts/`, makes it executable, references it in future tool calls.
2. **Templates**: Writes to `library/templates/`, loads and fills in when generating similar documents.
3. **Prompts**: Saves to `library/prompts/`, reuses in entity extraction, summarization, analysis tasks.

The agent can also write a **skill** that references library artifacts:

```markdown
---
name: weekly-procurement-report
description: Generate the weekly procurement status report
requires:
  tools: [doc_read, doc_write, email_draft]
---

# Weekly Procurement Report

## Workflow
1. Load template from `library/templates/weekly-report.md`
2. Run `library/scripts/parse-sam-data.py` to get current stats
3. Fill in template sections with current data
4. Save to output and draft email to stakeholders
```

### Skills the Agent Creates

When the agent identifies a repeating workflow, it can create a new skill in `skills/_agent-created/`:

```
skills/_agent-created/vendor-scoring/
├── SKILL.md                 # The workflow documentation
└── references/
    └── scoring-criteria.md  # Supporting reference material
```

The `_agent-created/` prefix makes it clear these weren't admin-provisioned. They're discoverable by the skill system like any other skill, but auditable as agent-generated.

### Marketplace Implications

When we get to marketplace:
- `skills/` = installable from marketplace (or agent-created)
- `library/` = agent-local, not shareable (unless explicitly published)
- An agent could promote a `library/` artifact to a `skill/` for sharing

---

## 7. Self-Learning: The Policy System

### How It Works

```
AGENT DOES WORK → SELF-EVALUATION (async) → POLICY UPDATE
```

**Evaluation triggers** (not every turn):
- After completing a multi-step task
- After explicit user feedback (correction, approval, rejection)
- After session ends
- Every N turns (configurable, default 10)

**Evaluation prompt** (lightweight internal call):
```
Review the last task/response. Consider:
1. Did it achieve the user's goal?
2. What worked well? What could improve?
3. Is there a generalizable lesson for policy.md?
4. Review existing policy bullets — promote, modify, or remove?
```

### Scoring & Ranking

Each bullet tracks: `score` (1-10), `reviewed` (date), `uses` (count), `source` (session/turn).

- **Promotion**: Positive outcome → score++, move up
- **Modification**: Partially right → rewrite text, keep score
- **Removal**: Negative outcome → score-- (remove at ≤0)
- **Decay**: Unused 30+ days → score decreases by 1/period

### Safety Boundaries

| Action | Agent Can? | Requires |
|--------|-----------|----------|
| Add/modify/remove policy bullets | ✅ | Nothing |
| Create/edit skills in `_agent-created/` | ✅ | Nothing |
| Write to `library/` | ✅ | Nothing |
| Write to `context.md` | ✅ | Nothing |
| Write to `notes/` | ✅ | Nothing |
| Create/modify schedules | ✅ | Nothing |
| Modify `identity.md` | ❌ | Admin approval |
| Change tool permissions | ❌ | Admin approval |
| Override security constraints | ❌ | Never |
| Disable audit logging | ❌ | Never |

---

## 8. Extensions / Plugin Lifecycle

```python
class ArcAgentExtension(Protocol):
    name: str
    version: str
    
    # Lifecycle hooks
    async def on_agent_start(self, agent: AgentContext) -> None: ...
    async def on_agent_stop(self, agent: AgentContext) -> None: ...
    async def on_session_start(self, session: SessionContext) -> None: ...
    async def on_session_end(self, session: SessionContext) -> None: ...
    
    # Tool/turn hooks
    async def before_tool_call(self, call: ToolCall) -> ToolCall | None: ...
    async def after_tool_call(self, call: ToolCall, result: ToolResult) -> ToolResult: ...
    async def before_llm_call(self, messages: list[Message]) -> list[Message]: ...
    async def after_llm_response(self, response: LLMResponse) -> LLMResponse: ...
    
    # Self-evaluation hooks
    async def on_evaluation(self, eval_result: EvaluationResult) -> None: ...
    
    # Registration
    tools: list[ToolDefinition]
    memory_provider: MemoryProvider | None
    skills: list[str]                 # Skill files this extension bundles
```

---

## 9. Summary

### ArcAgent = ArcRun (agent-core) + This Layer (Python)

| Component | Description |
|-----------|-------------|
| **Memory** | File-based default (markdown + entity JSONL + hybrid search). Plugin interface for alternatives. Per-agent. Auditable. |
| **Identity** | Single `identity.md`. Consolidated from 8 OpenClaw files. Read-only to agent. Commented sections for traceability. |
| **Context** | Agent-maintained `context.md`. Token-budgeted working memory. Replaces MEMORY.md. |
| **Policy** | Self-learning `policy.md`. Ranked behavioral notes. Novel core differentiator. |
| **Skills** | Markdown knowledge files. Loaded on-demand. Agent can create in `_agent-created/`. |
| **Tools** | Standard interface. Native Python, MCP, HTTP, process transports. Full audit + permissions. |
| **Library** | `library/scripts/`, `library/templates/`, `library/prompts/` — agent's reusable artifacts. |
| **Scheduling** | Prompt lives WITH schedule entry. Agent self-schedules via tools. arcTeam reads same store. |
| **Sessions** | JSONL transcripts. Compaction with memory flush to context.md. Archive searchable. |
| **Extensions** | Python lifecycle hooks. Tool registration. Memory providers. Skill bundles. Marketplace-ready. |

### Design Principles

1. **Simple**: 3 core files. File-based defaults. No mandatory infrastructure.
2. **Secure**: Read-only identity. Permission-controlled tools/MCPs. Full audit trail.
3. **Scalable**: Async entity extraction. Plugin architecture. arcTeam piping ready for 10K agents.
4. **Open**: Standard interfaces. Language-agnostic tools via transport types. MCP native. Marketplace-ready.
5. **Federal-ready**: Everything auditable. Everything file-based (git-diffable). Classification-aware. NIST-compatible logging.
