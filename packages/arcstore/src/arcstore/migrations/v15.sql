SET LOCAL lock_timeout = '2s';
-- SPEC-088 COMP-012: ordered work-event log plus its callback outbox.
-- New tables only; nothing existing is rewritten or locked.
CREATE TABLE IF NOT EXISTS work_events (
    seq bigserial NOT NULL UNIQUE,
    event_id text PRIMARY KEY CHECK (length(event_id) BETWEEN 1 AND 512),
    kind text NOT NULL CHECK (kind IN (
        'run.finished', 'run.failed', 'gate.waiting', 'human_input.waiting', 'gate.escalated'
    )),
    occurred_at timestamptz NOT NULL,
    agent_did text NOT NULL,
    workflow_id text,
    run_id text,
    node_id text,
    task_id text,
    correlation_id text,
    status text NOT NULL,
    classification text NOT NULL,
    envelope jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);
-- The per-agent stream pages by seq.
CREATE INDEX IF NOT EXISTS work_events_agent_seq_idx ON work_events (agent_did, seq);

CREATE TABLE IF NOT EXISTS work_event_deliveries (
    event_id text PRIMARY KEY REFERENCES work_events(event_id) ON DELETE CASCADE,
    callback_url text NOT NULL,
    state text NOT NULL DEFAULT 'pending'
        CHECK (state IN ('pending', 'delivered', 'withheld', 'dead')),
    attempts integer NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    next_attempt_at timestamptz NOT NULL,
    lease_owner text,
    lease_expires_at timestamptz,
    last_error text
);
CREATE INDEX IF NOT EXISTS work_event_deliveries_due_idx
    ON work_event_deliveries (next_attempt_at) WHERE state = 'pending';
