# Data Flow Pathways

> **Walkthrough**  ·  Understand  ·  page 13 of 14  
> **For** Anyone who needs to understand how Arc works  
> [← 12. Configuration](12-configuration.md)  ·  [Docs home](../README.md)  ·  [Diagrams →](diagrams.md)

---

## Architecture Layers

```mermaid
flowchart TB
    classDef surface fill:#5A9CFF,stroke:#0073FE,color:#002550
    classDef agent fill:#0073FE,stroke:#0055BC,color:#FFFFFF
    classDef runtime fill:#0055BC,stroke:#003B82,color:#FFFFFF
    classDef llm fill:#003B82,stroke:#002550,color:#FFFFFF
    classDef found fill:#002550,stroke:#001A38,color:#FFFFFF

    subgraph "Surface Layer"
        arcgateway[arcgateway<br/>Chat platforms]:::surface
        arcui[arcui<br/>Dashboard]:::surface
    end

    subgraph "Entry Layer"
        arccli[arccli<br/>CLI commands]:::surface
    end

    subgraph "Agent Layer"
        arcagent[arcagent<br/>Agent framework]:::agent
        arcteam[arcteam<br/>Multi-agent]:::agent
        arcmemory[arcmemory<br/>Memory system]:::agent
        arcskill[arcskill<br/>Skill hub]:::agent
    end

    subgraph "Runtime Layer"
        arcrun[arcrun<br/>Execution loop]:::runtime
        arcprompt[arcprompt<br/>Prompt engine]:::runtime
    end

    subgraph "LLM Layer"
        arcllm[arcllm<br/>LLM client]:::llm
    end

    subgraph "Foundation Layer"
        arctrust[arctrust<br/>Security primitives]:::found
        arcstore[arcstore<br/>Storage backend]:::found
    end

    arcgateway --> arcagent
    arcui --> arcagent
    arccli --> arcteam
    arccli --> arcagent
    arcagent --> arcrun
    arcagent --> arcteam
    arcagent --> arcmemory
    arcagent --> arcskill
    arcrun --> arcllm
    arcprompt --> arcllm
    arcllm --> arctrust
    arcllm --> arcstore
    arcteam --> arctrust
    arcteam --> arcstore
    arcskill --> arctrust
    arcagent --> arctrust
    arcagent --> arcstore
```

---

## Single-Agent Turn Flow

When an agent processes a message, data flows through this pathway:

```mermaid
sequenceDiagram
    participant U as User
    participant CLI as arccli
    participant Agent as arcagent
    participant Run as arcrun
    participant LLM as arcllm
    participant Trust as arctrust
    participant Store as arcstore

    U->>CLI: "Analyze this data"
    CLI->>Agent: load_config()
    Agent->>Trust: verify_identity()
    Agent->>Store: load_memory()
    Agent->>Agent: build_prompt()
    
    Agent->>Run: run_turn()
    Run->>LLM: chat_completion()
    LLM->>LLM: select_provider()
    LLM->>Store: record_llm_call()
    LLM-->>Run: response
    Run->>Agent: parse_tool_calls()
    Agent->>Trust: authorize_action()
    Agent->>Store: execute_tool()
    Agent->>Trust: audit_log()
    Agent-->>CLI: result
    CLI-->>U: formatted output
```

### Detailed Turn Steps

```mermaid
flowchart TD
    classDef step fill:#0073FE,stroke:#0055BC,color:#FFFFFF
    classDef check fill:#002550,stroke:#001A38,color:#FFFFFF
    classDef store fill:#D6E6FF,stroke:#0073FE,color:#002550

    Start[User Input]:::step --> Prompt[Build Prompt<br/>arcprompt]:::step
    Prompt --> LLM[LLM Call<br/>arcllm]:::step
    LLM --> Parse[Parse Response<br/>Tool calls?]:::check
    Parse -->|No| Return[Return Result]:::step
    Parse -->|Yes| Auth[Authorize<br/>arctrust]:::check
    Auth --> Exec[Execute Tool<br/>arcstore]:::step
    Exec --> Audit[Audit Log<br/>arctrust]:::step
    Audit --> Prompt
```

---

## Capability Loading Flow

Capabilities are loaded from multiple sources with security checks:

```mermaid
flowchart TB
    classDef source fill:#0073FE,stroke:#0055BC,color:#FFFFFF
    classDef scan fill:#002550,stroke:#001A38,color:#FFFFFF
    classDef active fill:#D6E6FF,stroke:#0073FE,color:#002550

    subgraph "Capability Sources"
        S1[Builtins<br/>packages/arcagent/tools]:::source
        S2[Global<br/>~/.arc/state/capabilities]:::source
        S3[Agent<br/>agent/capabilities]:::source
        S4[Workspace<br/>workspace/.capabilities]:::source
    end

    subgraph "Security Pipeline"
        Scan[Static Scan<br/>arcskill]:::scan
        Sign[Signature Verify<br/>arctrust]:::scan
        CRL[CRL Check<br/>arcskill]:::scan
    end

    S1 --> Scan
    S2 --> Scan
    S3 --> Scan
    S4 --> Scan
    Scan --> Sign
    Sign --> CRL
    CRL --> Active[Active Capabilities]:::active
```

---

## Skill Installation Pipeline

The 8-gate skill install pipeline:

```mermaid
flowchart LR
    classDef gate fill:#002550,stroke:#001A38,color:#FFFFFF
    classDef term fill:#D6E6FF,stroke:#0073FE,color:#002550
    classDef fail fill:#F68D2E,stroke:#C06000,color:#FFFFFF

    A[1. Fetch<br/>to quarantine]:::gate --> B[2. Sigstore<br/>signature]:::gate
    B --> C[3. Rekor<br/>inclusion proof]:::gate
    C --> D[4. CRL check]:::gate
    D --> E[5. Static scan<br/>regex+AST+semgrep+bandit]:::gate
    E --> F[6. Sandboxed<br/>dry-run]:::gate
    F --> G[7. Atomic<br/>activation]:::gate
    G --> H[8. Lock file<br/>entry]:::term
    
    B -.-> X[SignatureInvalid]:::fail
    D -.-> Y[CRLUnreachable]:::fail
    E -.-> Z[ScanVerdictFailed]:::fail
```

---

## Multi-Agent Communication Flow

```mermaid
sequenceDiagram
    participant A1 as Agent 1
    participant Team as arcteam
    participant NATS as NATS Backend
    participant A2 as Agent 2
    participant Audit as arctrust

    A1->>Team: send(sender, to, body, type, priority)
    Team->>Audit: sign_message()
    Team->>NATS: publish(channel, message)
    NATS->>A2: deliver(message)
    A2->>Team: ack(message_id)
    Team->>Audit: log_ack()
```

### Team Message Flow

```mermaid
flowchart LR
    classDef agent fill:#0073FE,stroke:#0055BC,color:#FFFFFF
    classDef msg fill:#002550,stroke:#001A38,color:#FFFFFF
    classDef backend fill:#D6E6FF,stroke:#0073FE,color:#002550

    A1[Agent 1]:::agent -->|task| Team[arcteam]:::msg
    Team -->|signed message| NATS[NATS JetStream]:::backend
    NATS -->|deliver| A2[Agent 2]:::agent
    A2 -->|ack| Team
    Team -->|log| Audit[Audit Trail]:::msg
```

---

## Memory System Flow

```mermaid
flowchart TB
    classDef mem fill:#0073FE,stroke:#0055BC,color:#FFFFFF
    classDef store fill:#002550,stroke:#001A38,color:#FFFFFF
    classDef active fill:#D6E6FF,stroke:#0073FE,color:#002550

    subgraph "Memory Types"
        M1[Episodic<br/>Recent events]:::mem
        M2[Entity<br/>Facts graph]:::mem
        M3[Daily Log<br/>Long-term]:::mem
    end

    subgraph "Storage"
        S1[workspace/sessions/]:::store
        S2[workspace/memory/]:::store
        S3[workspace/daily/]:::store
    end

    subgraph "Access"
        Active[Active Memory<br/>in prompt]:::active
        Query[Memory Query]:::active
    end

    M1 --> S1
    M2 --> S2
    M3 --> S3
    S1 --> Active
    S2 --> Query
    S3 --> Query
```

---

## LLM Provider Selection Flow

```mermaid
flowchart TD
    classDef req fill:#0073FE,stroke:#0055BC,color:#FFFFFF
    classDef sel fill:#002550,stroke:#001A38,color:#FFFFFF
    classDef prov fill:#D6E6FF,stroke:#0073FE,color:#002550

    Start[LLM Request]:::req --> Features[Check required<br/>features]:::sel
    Features -->|tools| ToolCheck[Tool support]:::sel
    Features -->|vision| VisionCheck[Vision support]:::sel
    Features -->|json| JSONCheck[JSON mode support]:::sel
    ToolCheck --> Provider[Select Provider<br/>arcllm]:::prov
    VisionCheck --> Provider
    JSONCheck --> Provider
    Provider -->|HTTP direct| API[LLM API]:::req
    API --> Provider
    Provider --> Start
```

---

## Turn Flow — From Message to Response

When an agent processes a message, data flows through this pathway:

```mermaid
sequenceDiagram
    participant U as User
    participant Entry as Entry Surface
    participant Agent as ArcAgent.run
    participant Dispatch as dispatch_stream
    participant RunStream as arcrun.run_stream
    participant Loop as react_loop
    participant LLM as arcllm
    participant Tool as Tool (policy-wrapped)
    participant Spool as arcstore spool
    participant UI as arcui

    U->>Entry: message (CLI, TUI, or Gateway)
    Entry->>Agent: agent.run(input_text, session)
    Agent->>Dispatch: build_run_context (prompt, tools, provider)
    Dispatch->>RunStream: model, capabilities, actor_did, run_id
    RunStream->>Loop: same run_id (mint if None)
    Loop->>Loop: registry.freeze() - tool-set lock
    Loop-->>RunStream: RunState (run_id fixed for the whole run)
    RunStream-->>Dispatch: AsyncIterator[StreamEvent]
    
    loop each turn
        Dispatch->>LLM: model.invoke(messages, tools)
        LLM-->>Dispatch: LLMResponse
        LLM->>Spool: llm_call record (request_id=run_id)
        Dispatch->>Tool: dispatch tool_call
        Tool->>Tool: policy.evaluate (DENY short-circuits)
        Tool-->>Dispatch: tool_result
        Dispatch->>Spool: run_event / tool_event (request_id=run_id)
        Tool->>Spool: audit tool.executed (WormSink)
    end
    
    Dispatch-->>Agent: TokenEvent... TurnEndEvent
    Agent-->>Entry: StreamEvent stream
    Entry-->>U: reply
    UI->>Spool: query (list_traces / get_trace)
```

### Three Entry Surfaces, One Path

The three entry surfaces converge on the same function almost immediately:

**`arc` CLI.** `arc agent chat` loads the agent's config, opens a session, and drives the streaming entry directly:
```python
session = await arc_agent.session(current_session_id)
result = await collect(arc_agent.run(user_input, session=session))
```

**The `arctui` TUI.** Holds its own `ArcAgent` instance in-process and calls the identical entry point when the user submits text:
```python
session = await self._agent.session("tui:main")
async for event in self._agent.run(text, session=session):
    if isinstance(event, TokenEvent):
        self._transcript.append_delta(event.text)
```

**A gateway channel.** Telegram/Slack/Mattermost adapters receive platform updates, normalize them into `InboundEvent`, and route through `SessionRouter`:
```
TelegramAdapter → SessionRouter.handle → Executor.run → ArcAgent.run
```

### Tool Execution Pipeline

```mermaid
flowchart LR
    classDef runtime fill:#0055BC,stroke:#003B82,color:#FFFFFF
    classDef found   fill:#002550,stroke:#001A38,color:#FFFFFF

    A["0 Schema validate"] --> B["1 Policy pipeline evaluate"]
    B -->|"DENY"| X["PolicyDenied raised"]
    B -->|"ALLOW"| C["2 agent:pre_tool event"]
    C -->|"vetoed"| Y["ToolVetoedError raised"]
    C --> D["3 Execute handler"]
    D --> E["4 agent:post_tool event"]
    E --> F["5 telemetry.audit_event tool.executed"]

    class A,C,D,E runtime
    class B,F found
```

**Authorize.** The policy pipeline builds a signed `ToolCall` (tool_name, arguments, agent_did, session_id, classification) and signs it with the agent's identity key. The pipeline's `evaluate()` loop calls each configured layer in order and returns on the first `DENY` (first-DENY-wins, fail-closed). A denied call raises `PolicyDenied` before the handler runs.

**Audit.** Every successful execute emits `tool.executed` with `actor_did`, `tier`, `transport`, and duration to `WormSink` — a hash-chained, append-only JSONL file. The audit system is fail-open by design: it must never interrupt the operation being audited.

---

## Memory Lifecycle

Dual-speed, four-store, analogical memory: markdown source + SQLite index.

```mermaid
flowchart LR
    classDef entry   fill:#D6E6FF,stroke:#0073FE,color:#002550
    classDef agent   fill:#0073FE,stroke:#0055BC,color:#FFFFFF
    classDef llm     fill:#003B82,stroke:#002550,color:#FFFFFF
    classDef runtime fill:#0055BC,stroke:#003B82,color:#FFFFFF

    See["1 See — FastCapture.capture"]:::entry --> Curate["2 Curate — curate_for_distillation"]:::agent
    Curate --> Distill["3 Distill — extract_facts / mint_insights / extract_procedures"]:::llm
    Distill --> Consolidate["Consolidator.run — orchestrates 4-7"]:::agent
    Consolidate --> Dedup["4 Dedup / hygiene — merge_entities, merge_cues, dedup_workspace"]:::agent
    Dedup --> Index["5 Index — SurfaceIndex + StructuralIndex"]:::runtime
    Index --> Retrieve["6 Retrieve / fuse — Retriever.retrieve"]:::runtime
    Retrieve --> Enrich["7 Enrich — spot then enrich"]:::runtime
    Enrich --> Prompt["boundary-marked memory-result block"]:::entry

    classDef entry   fill:#D6E6FF,stroke:#0073FE,color:#002550
    classDef surface fill:#5A9CFF,stroke:#003B82,color:#002550
    classDef agent   fill:#0073FE,stroke:#0055BC,color:#FFFFFF
    classDef runtime fill:#0055BC,stroke:#003B82,color:#002550
    classDef llm     fill:#003B82,stroke:#002550,color:#FFFFFF
    classDef found   fill:#002550,stroke:#001A38,color:#FFFFFF
```

### Dual-Speed Memory Cadence

```mermaid
timeline
    title Dual-speed memory cadence
    section Fast path — every turn
        Capture : sanitize, dedup, tag, Hebbian-bump : zero LLM
        Retrieve : one bounded fuse-and-gate pass, cached once per turn
    section Slow path — trigger fires
        Consolidate light : distill facts/insights/procedures/days : decay edges : cue + entity merge
    section Slow path — first call after local date rolls
        Consolidate hygiene : + alias fold : + backlink repair : + workspace file dedup
```

### Four Stores

| Store | File | Holds | Written by | Read for |
|---|---|---|---|---|
| Episodic | `stores/episodic.py` | Raw event stream (SQLite `episodic` table) | `FastCapture.capture` | Consolidation input, enrichment context |
| Semantic | `stores/semantic.py` | Entity cards (`memory/entities/<slug>.md`) | Distillation, agentic tools | Facts, structural enrichment |
| Insight | `stores/insight.py` | Minted abstractions (`memory/insights/<id>.md`) | `mint_insights` / agentic tools | Structural recall |
| Procedural | `stores/procedural.py` | How-to cards (`memory/procedures/<slug>.md`) | `extract_procedures` / `record_procedure` | Recall, skill improvement |
| Daily notes | `stores/daily.py` | Curated per-day rollup | `_summarize_days` | Human/operator review, surface index source |

### Insight Confidence Lifecycle

```mermaid
stateDiagram-v2
    [*] --> guessed: first mint — hits = 1
    guessed --> guessed: re-mint, confidence < known_threshold
    guessed --> known: confidence crosses known_threshold
    known --> known: further corroboration
    note right of guessed: surfaced with verify_first = true
    note right of known: actionable anchor, no verify flag
```

---

## Session Lifecycle

```mermaid
flowchart LR
    classDef sess fill:#0073FE,stroke:#0055BC,color:#FFFFFF
    classDef store fill:#002550,stroke:#001A38,color:#FFFFFF
    classDef replay fill:#D6E6FF,stroke:#0073FE,color:#002550

    Start[New Session]:::sess --> Store[Write to<br/>sessions/*.jsonl]:::store
    Store --> Next[Next Turn]:::sess
    Next --> Store
    Store --> End[Session End]:::sess
    End --> Replay[Replay by ID<br/>arc agent chat --session]:::replay
```

---

## Tool Execution Flow

```mermaid
sequenceDiagram
    participant Agent as arcagent
    participant Validator as arctrust
    participant Sandbox as arcrun
    participant Store as arcstore
    participant Audit as arctrust

    Agent->>Validator: authorize(tool, params)
    Validator->>Validator: check_lethal_trifecta()
    Validator->>Validator: check_allowlist()
    Validator->>Validator: check_tier_rules()
    Validator-->>Agent: approved
    
    Agent->>Sandbox: execute(tool, params)
    Sandbox->>Sandbox: select_sandbox()
    Sandbox->>Sandbox: run_in_isolation()
    Sandbox-->>Agent: result
    
    Agent->>Audit: log(tool_execution)
```

### Dynamic Tool Creation Pipeline

```mermaid
flowchart LR
    classDef check fill:#002550,stroke:#001A38,color:#FFFFFF
    classDef exec fill:#0073FE,stroke:#0055BC,color:#FFFFFF
    classDef term fill:#D6E6FF,stroke:#0073FE,color:#002550

    A[Agent Code]:::exec --> B[Encoding Check]:::check
    B --> C[AST Validate]:::check
    C --> D[Restricted Builtins]:::check
    D --> E[Egress Proxy]:::check
    E --> F[Execute]:::exec
    F --> G[Result to LLM]:::term
```

---

## Dashboard Data Flow

```mermaid
flowchart TB
    classDef ui fill:#5A9CFF,stroke:#0073FE,color:#002550
    classDef store fill:#0073FE,stroke:#0055BC,color:#FFFFFF
    classDef agent fill:#003B82,stroke:#002550,color:#FFFFFF

    subgraph "arcui"
        Observe[Observe Plane<br/>WebSocket]:::ui
        Interact[Interact Plane<br/>Live chat]:::ui
        Manage[Manage Plane<br/>Control]:::ui
    end

    subgraph "Data Source"
        Store[arcstore<br/>On-demand read]:::store
    end

    subgraph "Agents"
        A1[Agent 1]:::agent
        A2[Agent 2]:::agent
    end

    A1 -->|writes| Store
    A2 -->|writes| Store
    Store -->|reads| Observe
    Observe -->|displays| UI[Dashboard UI]:::ui
    UI -->|commands| Manage
    Manage -->|affects| A1
    Manage -->|affects| A2
```

---

## Gateway Message Flow

```mermaid
sequenceDiagram
    participant User as Chat User
    participant Gateway as arcgateway
    participant Agent as arcagent
    participant Platform as Chat Platform

    User->>Platform: Message
    Platform->>Gateway: Webhook
    Gateway->>Gateway: verify_signature()
    Gateway->>Agent: enqueue(message)
    Agent->>Agent: process_turn()
    Agent-->>Gateway: response
    Gateway->>Platform: reply
    Platform-->>User: Response
```

### Platform Adapter Flow

```mermaid
flowchart LR
    classDef plat fill:#0073FE,stroke:#0055BC,color:#FFFFFF
    classDef gw fill:#002550,stroke:#001A38,color:#FFFFFF
    classDef agent fill:#D6E6FF,stroke:#0073FE,color:#002550

    Telegram[Telegram]:::plat -->|adapter| Gateway[arcgateway]:::gw
    Slack[Slack]:::plat -->|adapter| Gateway
    Mattermost[Mattermost]:::plat -->|adapter| Gateway
    Gateway -->|unified| Agent[arcagent]:::agent
```

---

## Data Storage Layout

```mermaid
erDiagram
    LLM_CALLS ||--o{ RUN_EVENTS : "request_id"
    LLM_CALLS {
        text record_id PK
        text kind
        text actor_did
        text ts
        text request_id
        text model
        text provider
        integer prompt_tokens
        integer completion_tokens
        real cost_usd
        real latency_ms
        text outcome
    }
    RUN_EVENTS {
        text record_id PK
        text actor_did
        text ts
        text request_id
        text name
    }
    AGENT_EVENTS {
        text record_id PK
        text actor_did
        text ts
        text name
    }
    TOOL_EVENTS {
        text record_id PK
        text tool_name
        text phase
        text args_digest
        text result_digest
    }
    SPAWN_EVENTS {
        text record_id PK
        text parent_did
        text child_did
        text role
        integer depth
    }
    AUDIT_CHAIN {
        text record_id PK
        integer seq
        text actor_did
        text action
        text target
        text outcome
        text event_hash
        text prev_hash
        text signature
        integer verified
    }
    MUTABLE_RECORDS {
        text collection PK
        text key PK
        text value
        text updated_at
    }
    SKILL_CANDIDATES {
        text record_id PK
        text skill_name
        text candidate_id
        integer generation
        text body_hash
    }
    SKILL_CANDIDATE_BODIES {
        text record_id PK
        text body
    }
```

### Disk Layout

`~/.arc` is split by **lifecycle**, so replacing the framework cannot destroy
anything irreplaceable. Every path below is resolved by exactly one named
accessor in `arctrust.paths` — no surface composes its own (enforced by
`tests/architecture/test_arc_home_single_resolver.py`).

| Root | Accessor | On update |
|---|---|---|
| `~/.arc/runtime/<version>/` (+ `current` symlink) | `arc_runtime()` | **replaced wholesale** |
| `~/.arc/config/` | `arc_config()` | preserved |
| `~/.arc/state/` | `arc_state()` | **never touched** |
| `~/.arc/team/` | `arc_team()` | **never touched** |

`resolve_data_dir()` — `${ARCSTORE_DATA_DIR}` or `store_dir()` — is arcstore's
data root (spool, WORM mirror, SQLite mirrors) and lives under `state/`.

```text
~/.arc/                                  # arc_home()
├── runtime/                             # arc_runtime_root() — disposable
│   ├── current -> 0.9.0/                # atomic symlink; an update flips it
│   └── 0.9.0/
│       └── modules/                     # module_root() — re-materialized by `arc install`
├── config/                              # arc_config() — preserved across an update
│   ├── arcllm.toml                      # provider/model defaults (shared layer)
│   ├── arcagent.toml                    # agent-runtime defaults (shared layer)
│   ├── arcrun.toml                      # loop defaults (shared layer)
│   ├── gateway.toml                     # embedded gateway: platforms, agent_did
│   ├── connections.toml                 # connected accounts + per-agent grants
│   └── arc.env                          # 0600 — viewer/operator tokens, secrets
├── state/                               # arc_state() — NEVER touched by an update
│   ├── operator/                        # operator_dir()
│   │   ├── operator.key                 # 0600 — Ed25519 seed, the audit authority
│   │   └── operator.key.pub             # 0644 — anti-erasure/anti-swap sentinel
│   ├── identity/                        # identity_dir() — signing-authority keys
│   ├── trust/                           # trust_dir()
│   │   ├── operators.toml               # 0600 — pairing-approver pubkeys
│   │   └── issuers.toml                 # 0600 — manifest-signer pubkeys
│   ├── bundles/                         # bundles_dir() — staged signed bundles
│   ├── nats/jetstream/                  # nats_dir() — team broker state
│   ├── workflows/                       # workflows_dir() — signed ArcFlow bundles
│   ├── users.json                       # users_file()
│   └── store/                           # store_dir() = resolve_data_dir() default
│       ├── spool/
│       │   └── operational-YYYY-MM-DD.jsonl # 0600 — always-on telemetry, daily rotation
│       ├── worm/
│       │   ├── audit-chain-<agent>.jsonl    # per-agent WORM chain (single-writer flock)
│       │   └── audit-chain-<agent>.<seq>.jsonl  # rotated segments (100k records / 50MB)
│       └── store/
│           ├── arcui.db                 # arcui's SQLite mirror (WAL)
│           └── arcstore.db              # agent process's SQLite mirror (WAL)
└── team/                                # arc_team() — NEVER touched by an update
    └── <agent>/                         # one dir per agent — see agent-root tree below

<agent-root>/                            # e.g. ~/.arc/team/<agent>/
├── arcagent.toml                        # per-agent config
├── .audit/
│   └── skills.worm                      # skill-improver's own WORM chain
└── workspace/
    ├── identity.md                      # read-only goal charter
    ├── context.md                       # sole writer: workpad module
    ├── policy.md                        # protected — not agent-writable
    ├── capabilities/                    # workspace-scoped tools/skills
    ├── skill_traces/<skill>/candidates/ # mutation candidates
    └── memory/
        ├── index.db                     # SQLite: episodic + indices
        ├── entities/*.md                # semantic store
        ├── procedures/*.md              # procedural store
        ├── insights/*.md                # insight store
        └── daily-log/*.md               # curated daily summaries
```

Two rules follow from the table, and both have already cost a live box: never
put the fleet inside the code checkout (every `git pull` then collides with a
running agent), and never overwrite `~/.arc` wholesale (that destroys the
operator key, and every WORM chain it signed becomes unverifiable). Replace
`runtime/` — nothing else. `arc install` migrates a pre-split flat home into
this layout once, by moving rather than copying, and rolls back on failure.

### The Spool — Always-On Operational Telemetry

**Design points:**
- Filename: `spool/operational-YYYY-MM-DD.jsonl`, daily rotation by UTC date
- Append-only, single `os.write()` syscall per record (atomic on local filesystem)
- Mode `0600` — set on open and re-asserted
- No per-record `fsync` — durability = survives process crash, not OS crash
- Fail-open — write errors logged and swallowed; telemetry must never break the call
- `request_id` — correlation id bound via `contextvars.ContextVar` for concurrent runs

**SpoolRecord kinds:** `llm_call`, `run_event`, `agent_event`, `tool_event`, `spawn_event`

### The WORM Audit Chain — Compliance System of Record

**Design points:**
- Two sinks: `NullSink` (discard) and `WormSink` (durable, signed, chained)
- Each record: `seq`, `prev_hash`, `event_hash` (SHA-256 of seq+prev+event), `signature`
- Single-writer `flock` per agent chain file
- Crash recovery: torn final line truncated, signed `audit.worm.recovery` appended
- Rotation: at 100,000 records or 50MB
- Signature: Ed25519 (personal/enterprise) or ECDSA-P256 (FIPS/federal), by operator key

### The SQLite Mirror — Queryable Read Plane

- WAL mode, `busy_timeout=5000`, `journal_size_limit=64MB`
- `INSERT OR IGNORE` keyed on content-derived `record_id` (idempotent ingest)
- Each process owns its own DB file (`arcstore.db` vs `arcui.db`)
- Mirrors both spool and WORM files

**Mutable Records:** Tasks, Approvals, Cancellations — overwritten in place, not append logs

### Memory on Disk — Glass-Box Markdown + Disposable Index

- Markdown files are the durable truth; SQLite index is disposable/re-derivable
- `sqlite-vec` optional extension; absence degrades to BM25 + graph (silent unless watched)
- Atomic writes via temp file + `os.replace`
- Insight card format: YAML frontmatter (id, trigger, cues, instances, confidence, status) + markdown body

### Keys and Secrets

| Credential | Location | Mode | Custody |
|---|---|---|---|
| Agent DID keypair | `~/.arcagent/keys/<did>.key` / `.pub` | `0700` dir | Vault resolver seam supports Azure KV, file, env backends |
| Operator key | `~/.arc/state/operator/operator.key` | `0600`, `O_NOFOLLOW` | In-process by default; VaultSigner/VaultTransit for external custody |
| Trust store | `~/.arc/state/trust/operators.toml`, `issuers.toml` | `0600` | Public keys only |
| UI tokens | `~/.arc/config/arc.env` | `0600` | Minted once, pinned |

### Retention and Deletion

- **Spool/WORM purge:** Whole rotated files only; oldest files deleted until under `max_bytes`
- **No per-record erasure:** Deleting one record breaks hash chain links
- **GDPR tombstone:** Separate workflow for user profile data only (`user_profile/tombstone.py`)

---

## Next Steps

- [API Reference](../reference/api.md) - Detailed class and method documentation
- [Package Index](../building/package-index.md) - Package-specific details
- [Implementation Guides](../building/implementation-guides.md) - Custom integrations