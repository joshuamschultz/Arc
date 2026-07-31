# ArcTeam Build Standards

> Build like a top 1% developer. No shortcuts. Root causes, not workarounds.

> Don't Mix concerns
- all llm calls are arcllm
- loop execution is arcrun
- agent with tools, skills, extensions, memory, etc is arcagent
- **multi-agent teams, coordination, communication, lifecycle is arcteam**
Don't have arcteam do things that belong to arcagent, arcrun, or arcllm.
arcteam orchestrates multiple ArcAgent instances -- it does not replace or duplicate agent internals.

---

## What ArcTeam Owns

| Concern | ArcTeam's Job | NOT ArcTeam's Job |
|---------|---------------|-------------------|
| Team formation | Create, join, leave, dissolve teams | Individual agent lifecycle |
| Task distribution | Assign, delegate, track work across agents | Execute tasks (that's arcrun) |
| Inter-agent comms | Route messages, signed channels, broadcast | LLM calls (that's arcllm) |
| Consensus | Voting, agreement protocols, conflict resolution | Tool execution (that's arcagent) |
| Roster management | Membership, roles, capability discovery | Agent identity (that's arcagent/identity) |
| Team lifecycle | Formation -> Active -> Dissolving -> Dissolved | Agent config (that's arcagent/config) |
| Team telemetry | Team-level spans, metrics, audit events | Agent-level telemetry (that's arcagent) |

---

## Build Principles

### 1. Simplicity

The core must be simple: easy to read, hard to break, robust, no confusion.

- Favor flat, explicit code over clever abstractions
- Core stays under 2,000 LOC.
- Complexity lives in extensions and strategies -- never in the nucleus
- If you need a comment to explain control flow, the code is too complex. Refactor.
- No nested logic deeper than 2 levels. Extract to named methods.
- One class, one responsibility. One method, one job.

### 2. Security

Federal-first. This runs on DOE machines, in labs, in SCIFs.

- Secure by default, not by configuration
- Zero-trust everything: identity, comms, data, modules
- Full observability: OpenTelemetry traces, metrics, structured logs on every action
- Audit trail on every operation. Every message, every task assignment, every state change
- All inter-agent communication signed and verified (Ed25519)
- mTLS on all internal communications
- Team membership changes are audit events

### 3. Scalability

Built for teams of 100s of agents, with 1000s of teams.

- Shared-nothing per team. Coordinate via message bus (NATS).
- Async-first. Use `asyncio` everywhere.
- Fail gracefully: circuit breakers, exponential backoff
- Teams form and dissolve dynamically
- No singleton bottlenecks. Teams are independent.

---

## Code Standards

### Readability

- Clean, readable code is non-negotiable
- Methods should be short enough to read without scrolling
- Use descriptive names: `assign_task_to_member`, not `assign`
- Comment the WHY, not the WHAT

### Maintainability

- Strong typing everywhere. `mypy --strict` must pass.
- Pydantic models for all data boundaries (config, messages, events)
- Interfaces over implementations. Depend on protocols, not concrete classes.

### Project Structure

```
src/arcteam/
    __init__.py
    types.py            # Shared types: TeamId, MemberId, TaskId, messages
    config.py           # Team configuration, Pydantic validation
    team.py             # Team formation, lifecycle management
    coordinator.py      # Task distribution, delegation, work tracking
    messenger.py        # Inter-agent communication, message routing
    roster.py           # Membership, roles, capability discovery
    consensus.py        # Agreement protocols, voting, conflict resolution
    events.py           # Team-level events (audit, telemetry)
tests/
    unit/               # 70% of tests
    integration/        # 20% of tests
    e2e/                # 10% of tests
```

---

## Dependencies

### Foundations

| Project | Purpose | Relationship |
|---------|---------|--------------|
| ArcAgent | Agent nucleus | arcteam orchestrates ArcAgent instances |
| ArcLLM | LLM calls | Transitive via arcagent |
| ArcRun | Execution loop | Transitive via arcagent |

### Key Libraries

| Library | Purpose |
|---------|---------|
| Pydantic 2.x | Data validation, config schemas |
| OpenTelemetry API | Traces, metrics, audit |
| NATS.py | Inter-agent message bus (added when needed) |

---

## Quality Gates

| Gate | Threshold |
|------|-----------|
| Line coverage | >= 80% |
| Branch coverage | >= 75% |
| Core component coverage | >= 90% |
| Cyclomatic complexity | <= 10 per function |
| Ruff errors | 0 |
| mypy errors | 0 |
| Critical/high vulnerabilities | 0 |
| Core LOC | < 2,000 |

---

## Threat Surface Awareness (Team-Specific)

| Threat | Mitigation |
|--------|------------|
| **Rogue agent in team (ASI10)** | Behavioral monitoring, anomaly detection, team-level revocation |
| **Inter-agent message tampering (ASI07)** | Ed25519 signed messages, mTLS channels, replay protection |
| **Task injection** | Validate all task assignments against policy. No agent self-assigns privileged tasks. |
| **Cascading failures (ASI08)** | Circuit breakers between team members. Blast radius containment. Graceful degradation. |
| **Privilege escalation via team role** | Roles are read-only to agents. Role changes require team coordinator approval + audit. |
| **Consensus manipulation** | Quorum requirements. Vote integrity verification. Timeout on consensus rounds. |
