CREATE TABLE IF NOT EXISTS llm_calls (
    record_key text PRIMARY KEY,
    payload jsonb NOT NULL,
    ts timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS run_events (LIKE llm_calls INCLUDING ALL);
CREATE TABLE IF NOT EXISTS agent_events (LIKE llm_calls INCLUDING ALL);
CREATE TABLE IF NOT EXISTS tool_events (LIKE llm_calls INCLUDING ALL);
CREATE TABLE IF NOT EXISTS spawn_events (LIKE llm_calls INCLUDING ALL);
CREATE TABLE IF NOT EXISTS audit_chain (LIKE llm_calls INCLUDING ALL);
CREATE TABLE IF NOT EXISTS skill_candidates (LIKE llm_calls INCLUDING ALL);
CREATE TABLE IF NOT EXISTS skill_candidate_bodies (LIKE llm_calls INCLUDING ALL);
CREATE INDEX IF NOT EXISTS llm_calls_ts_idx ON llm_calls (ts DESC);
CREATE INDEX IF NOT EXISTS run_events_ts_idx ON run_events (ts DESC);
CREATE INDEX IF NOT EXISTS agent_events_ts_idx ON agent_events (ts DESC);
CREATE INDEX IF NOT EXISTS tool_events_ts_idx ON tool_events (ts DESC);
CREATE INDEX IF NOT EXISTS spawn_events_ts_idx ON spawn_events (ts DESC);
CREATE INDEX IF NOT EXISTS audit_chain_ts_idx ON audit_chain (ts DESC);
CREATE INDEX IF NOT EXISTS skill_candidates_ts_idx ON skill_candidates (ts DESC);
CREATE INDEX IF NOT EXISTS skill_candidate_bodies_ts_idx ON skill_candidate_bodies (ts DESC);
CREATE TABLE IF NOT EXISTS arcstore_cursors (
    name text PRIMARY KEY,
    value bigint NOT NULL CHECK (value >= 0)
);
CREATE TABLE IF NOT EXISTS mutable_records (
    collection text NOT NULL,
    key text NOT NULL,
    value jsonb NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (collection, key)
);
CREATE INDEX IF NOT EXISTS mutable_records_collection_idx ON mutable_records (collection);
CREATE TABLE IF NOT EXISTS inboxes (
    inbox_id text PRIMARY KEY,
    payload jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS inbox_threads (
    thread_id text PRIMARY KEY,
    inbox_id text NOT NULL REFERENCES inboxes(inbox_id) ON DELETE CASCADE,
    payload jsonb NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS inbox_messages (
    message_id text PRIMARY KEY,
    thread_id text NOT NULL REFERENCES inbox_threads(thread_id) ON DELETE CASCADE,
    payload jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS inbox_handoffs (
    handoff_id text PRIMARY KEY,
    thread_id text NOT NULL REFERENCES inbox_threads(thread_id) ON DELETE CASCADE,
    payload jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);
