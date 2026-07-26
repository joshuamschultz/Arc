# Arc — How It All Fits Together

## In one breath

- **arcagent** holds skills, tools, files, identity. It *uses* **arcrun** to
  execute, and reaches LLM providers *through* **arcllm**.
- **arcrun** is the execution loop: it dispatches the tools handed to it and is
  the single runtime path to **arcllm**. It owns the loop, not the capabilities.
- **arcgateway** is how you interact with an agent from outside (web / slack /
  telegram channels) — anything but a direct CLI call or the `serve` TUI.
- **arcui** is for observing (reading the durable record) and some interaction (chat).
- **arctrust** is the security nucleus underneath all of it (identity, signing,
  policy, the WORM audit chain); **arcstore** is the durable record everything
  writes to and arcui reads from.

How the packages run together after SPEC-026 (arcstore operational storage + the
arcui push teardown). Two ideas to hold onto:

1. **One write path, one read path, no wire between them.** Everything an agent
   does is *recorded* to durable files the moment it happens. The dashboard
   *reads* those files (via a SQLite mirror) on demand. There is no live push
   from agent → UI anymore — the thing that used to drop events.
2. **arctrust is the nucleus.** Identity, signing, authorization, and the audit
   WORM live there. It imports no other Arc package; everything depends on it.

---

## The packages

| Package | Job | Depends on |
|---|---|---|
| **arctrust** | Security nucleus: DID identity, Ed25519 keypairs, policy pipeline, **WORM** audit chain | nothing (leaf) |
| **arcllm** | Provider-agnostic LLM calls (OpenAI/Anthropic/…), telemetry, budgets, circuit breakers. **Reached only through arcrun** (except a custom out-of-agent caller like `arc llm`) | arcstore (spool) |
| **arcrun** | **The execution loop — and the single runtime path to arcllm.** Drives each turn: calls arcllm and dispatches the **tools it was handed**, emitting run events. Owns none of those tools — memory, skills, web, etc. arrive as opaque handlers passed in by the caller | arcstore (spool) |
| **arcagent** | **The agent.** Holds skills, tools, files, identity. Uses **arcrun** to execute; reaches LLM providers through **arcllm**. Does not run the loop or call providers itself — it owns the agent, delegates the doing | arctrust, arcllm, arcrun, arcstore |
| **arcstore** | Operational/observability storage: always-on **spool** + **StorageBackend** query layer (SQLite) | arctrust |
| **arcgateway** | **How you interact with an agent from outside** — channels + platform **adapters** (web / slack / telegram), sessions, executor. (Direct `arc` CLI calls and the `serve` TUI talk to the agent without it) | arcagent, arctrust |
| **arcui** | **Observe** (read agent history from arcstore) **+ some interaction** (chat through the gateway) | arcstore, arcgateway¹ |
| **arccli** | `arc …` commands: create/serve/run agents, `arc ui`, `arc store`, `arc team` | all of the above |

¹ Narrowed, not violated: the Knowledge/Capabilities "Reality Mirror" views
(arcui-reality-mirror) need to mirror what an agent actually loads, so arcui
also reaches `arcagent.capabilities.inventory` — one lazily-imported seam,
enforced by an AST architecture test (`test_arcui_imports_arcagent_only_via_inventory_seam`
in `packages/arcgateway/tests/architecture/test_imports.py`). Every other
`arcagent` import from arcui is still a forbidden layering violation per
SPEC-023 SDD §2.2.

---

## The big picture

```
   arctrust (NUCLEUS)  identity (DID) · keypair · policy · WORM    ── verify/sign/authorize/audit
        ▲  every action below checks in here                          on every action; imports nothing

   user (chat: slack / telegram / web browser)
        │
        ▼
   arcgateway (DATA PLANE)   per-channel sessions · session router · executor · WebPlatformAdapter
        │   executor invokes the assembled agent for this session     (/ws/chat = the one live socket)
        ▼
   arcagent (ASSEMBLY)   identity + tools + skills + memory(as tools) + extensions
        │   hands arcrun:  (1) the turn   (2) the TOOL SET to dispatch         ┐ tool set
        ▼                                                                       │ passed in
   arcrun (EXECUTION LOOP)  ── the ONLY runtime path to arcllm                  │
        ├─► arcllm           LLM call (provider-agnostic)                       │
        └─► tool dispatch ──► invokes whatever tools it was handed  ◄───────────┘
                              (a memory write, a skill, a web fetch — all opaque
                               handlers; arcrun owns none of them)
        │   emits run_events each step
        │
        ══════════════ DURABLE RECORD (write path) ════════════════════════════════
        ▼
   arcllm.record() ─► spool   operational-YYYY-MM-DD.jsonl     (append-only, 0600,
   arcrun run_events ─► spool                                   single os.write, fail-open)
   arctrust.emit()  ─► worm   audit chain (signed)
        │  <data_dir>/spool/  +  <data_dir>/worm/
        ▼
        ══════════════ OBSERVE (read path) ════════════════════════════════════════
   arcstore StoreIngest  (backfill on startup, then tail; reads files from byte offset)
        ▼
   SqliteBackend  (per-instance mirror, WAL)  ◄── StorageBackend Protocol
        │            llm_calls · run_events · agent_events · audit_chain
        ▼
   arcui.Observe  (open_backend factory → backend; never imports a concrete DB)
        │   .traces() .stats() .timeseries() .performance() .cost_efficiency()
        ▼
   arcui REST  /api/traces /api/stats /api/cost-efficiency …   read-on-demand, no polling
        ▼
   arcui web (React + React Query)  ── the dashboard the browser renders

   ── exception (no agent, no loop):  `arc llm "…"`  ──►  arcllm   (custom out-of-agent caller; UC-1)
```

### Call-flow rule (don't mix concerns)

- **arcrun is the only runtime path to arcllm.** Chat from any channel (UI / Slack /
  Telegram) lands in the gateway, the executor invokes the agent, and the agent
  runs **through arcrun** — which is where the LLM gets called and where tools may
  fire. The lone exception is a deliberate out-of-agent caller (`arc llm`), used to
  prove the "call now, see later" guarantee.
- **arcrun owns the loop, not the capabilities.** Tools, skills, and memory are
  passed *into* arcrun as handlers by whoever drives it (normally arcagent). arcrun
  dispatches them; it never imports or owns memory/skill/eval logic. That logic
  lives in arcagent. (CLAUDE.md: "Don't have arcrun do things that belong to agent
  or arcllm.")

---

## Two planes, kept separate

**Observe (read-only, the dashboard's data).** Durable files → arcstore ingest →
SQLite mirror → arcui REST. arcui runs its *own* `StoreIngest` over the shared
`spool` + `worm` dirs into its own `store/arcui.db` (shared-nothing). The browser
can't open SQLite directly, so it goes through REST, but the server reads the DB
synchronously per request. **No `/ws` telemetry feed, no EventBuffer, no push.**
Killing/restarting arcui loses no history — it re-reads the durable files.

**Interact (chat).** The one remaining WebSocket is `/ws/chat/{agent_id}` — a
genuinely bidirectional stream handled by the gateway's `WebPlatformAdapter`.
The browser is just one *channel*; a Slack thread and a UI thread with the same
agent are distinct sessions. Live reply streams over the chat socket; the run's
telemetry lands in Observe (arcstore), never a second push wire.

---

## What happens on `arc llm "…"` then `arc ui` later (UC-1)

```
t0   arc llm "..."        arcllm builds a SpoolRecord ─► spool file (append).
                          No server, no DB, no UI running. The line is durable.
t1   arc agent serve      StoreIngest backfills the spool from offset 0 into
                          SQLite (idempotent), then tails for new appends.
t2   open arcui           GET /api/traces ─► Observe.traces() ─► backend.query()
                          ─► the t0 call is rendered.
```

The t0 record is independent of t1/t2. That independence is the whole point:
recording can't be dropped by a UI that wasn't listening yet.

---

## The Four Pillars (every tier, always on)

- **Identity** — every entity has a DID; every tool dispatch carries `caller_did`.
- **Sign** — loaded artifacts (skills, extensions, backends) are verified before use.
- **Authorize** — `arctrust.policy.PolicyPipeline` evaluates every tool call, fail-closed.
- **Audit** — `arctrust.emit()` appends to the durable, signed WORM chain. Single
  emission point, one durable sink — no UI bridge, no parallel channel.
