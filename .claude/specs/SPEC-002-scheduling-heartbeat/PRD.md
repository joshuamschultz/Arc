# PRD: Scheduling / Heartbeat / Cron

**Spec**: SPEC-002 | **Date**: 2026-02-16

## Problem Statement

ArcAgent currently only works when a user is actively chatting. Agents cannot perform recurring work (heartbeats, daily checks, periodic maintenance) autonomously. This limits the agent's utility in federal environments where continuous monitoring, compliance checks, and proactive work are expected — even outside business hours.

## Goal

Build a scheduling system that lets agents manage their own recurring work. The agent writes both the WHEN (cron/interval/once) and the WHAT (prompt describing the task). Schedules fire even when no user is engaged, via a long-running daemon process.

## User Stories

### US-1: Agent Self-Scheduling
**As** an agent, **I want to** create, update, and cancel my own schedules **so that** I can automate recurring work I discover during conversations.

**Acceptance Criteria**:
- Agent can call `schedule_create` with type (cron/interval/once), prompt, and timing
- Agent can call `schedule_list` to see all active/inactive schedules
- Agent can call `schedule_update` to modify timing, prompt, or enable/disable
- Agent can call `schedule_cancel` to disable or remove a schedule
- All schedule operations emit audit events

### US-2: Heartbeat
**As** an operator, **I want** my agent to periodically check for time-sensitive work **so that** nothing falls through the cracks.

**Acceptance Criteria**:
- Interval schedules fire at configured frequency (e.g., every 30 minutes)
- Active hours restrict firing to business hours only
- Each fire creates a fresh agent session with the schedule's prompt
- Heartbeat results recorded in schedule metadata

### US-3: Cron Jobs
**As** a user, **I want** my agent to run tasks on a cron schedule **so that** I get daily email triage at 8am, weekly deadline reviews on Fridays, etc.

**Acceptance Criteria**:
- Cron expressions evaluated correctly (including DST-aware timezone handling)
- Missed schedules (agent was down) are detected on startup
- Active hours respected

### US-4: One-Time Tasks
**As** a user, **I want** to schedule a task for a specific future time **so that** I can set reminders and deferred actions.

**Acceptance Criteria**:
- Once-type schedules fire at the specified datetime
- After firing, schedule is auto-disabled
- Expired once-schedules (past datetime) are skipped on startup

### US-5: Daemon Mode
**As** an operator, **I want** to run `arc agent serve` **so that** the agent stays warm and schedules fire continuously without user interaction.

**Acceptance Criteria**:
- `arc agent serve` starts agent, loads scheduler module, enters serve loop
- Graceful shutdown on SIGTERM/SIGINT (drains queue, persists state)
- Health monitoring (memory, active schedules, queue depth)
- Agent stays warm in-process (no cold-start per fire)

### US-6: Sequential Execution
**As** the system, **I want** overlapping schedule fires to queue sequentially **so that** there are no concurrency issues or resource contention.

**Acceptance Criteria**:
- FIFO queue processes one execution at a time
- No schedule fire is dropped — all queue up
- Queue depth observable via telemetry

## Requirements

### Functional

| ID | Requirement | Priority | Source |
|----|-------------|----------|--------|
| FR-01 | 3 schedule types: cron, interval, once | Must | Design v3 §4 |
| FR-02 | 4 CRUD tools: create, list, update, cancel | Must | Decision D5 |
| FR-03 | `agent.run(prompt)` per schedule fire with fresh session | Must | Decision D4 |
| FR-04 | Active hours constraint (start/end/timezone) | Must | Decision D6 |
| FR-05 | Per-execution timeout | Must | Decision D6 |
| FR-06 | Sequential FIFO execution queue | Must | Decision D7 |
| FR-07 | Atomic JSON persistence (workspace/schedules.json) | Must | Decision D3 |
| FR-08 | Module Bus events: fired/completed/failed/skipped | Must | Decision D10 |
| FR-09 | Schedule metadata: last_run, last_result, run_count | Must | Design v3 §4 |
| FR-10 | `arc agent serve` CLI command | Must | Decision D8 |
| FR-11 | Graceful shutdown (SIGTERM/SIGINT) | Must | Research |
| FR-12 | croniter for cron expression parsing | Must | Decision D11 |

### Non-Functional

| ID | Requirement | Threshold |
|----|-------------|-----------|
| NFR-01 | Scheduler check loop interval | <= 60 seconds |
| NFR-02 | Cold start (module load) | < 500ms |
| NFR-03 | Memory overhead | < 10MB above baseline |
| NFR-04 | Max active schedules per agent | 50 (configurable) |
| NFR-05 | Minimum schedule interval | 60 seconds (configurable) |
| NFR-06 | NIST AU-2/AU-3 audit compliance | All 7 required fields |
| NFR-07 | Test coverage | >= 80% line, >= 90% core |

### Security

| ID | Requirement | Rationale |
|----|-------------|-----------|
| SEC-01 | Prompt validation on schedule create/update | Prevent prompt injection via schedules (OWASP LLM01) |
| SEC-02 | Token budget per scheduled execution | Prevent unbounded consumption (OWASP LLM10) |
| SEC-03 | Circuit breaker on repeated failures | Prevent cascading failures (ASI-08) |
| SEC-04 | Audit trail on all schedule operations | NIST 800-53 AU-2/AU-3 compliance |
| SEC-05 | Schedule provenance tracking | Tag origin: agent/user/admin/system |

## Out of Scope

- Multi-agent schedule coordination (arcTeam future work)
- Database-backed schedule store (JSON file for MVP)
- Schedule-level retry logic (arcllm handles LLM retries)
- TOML seed schedules (runtime-only via tools or JSON editing)
- Distributed locking (single-process daemon for MVP)
- Web dashboard for schedule management

## Dependencies

| Dependency | Type | Status |
|------------|------|--------|
| arcagent Module Bus | Internal | Exists |
| arcagent ToolRegistry | Internal | Exists |
| arcagent agent.run() | Internal | Exists |
| croniter | External | PyPI (proven: Airflow, Celery) |
| pytz | External | PyPI (standard) |
