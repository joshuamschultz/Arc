CREATE TABLE IF NOT EXISTS connected_source_sync (
    agent_did text NOT NULL,
    source_id text NOT NULL,
    cursor text,
    status text NOT NULL DEFAULT 'idle',
    pages bigint NOT NULL DEFAULT 0 CHECK (pages >= 0),
    bytes_processed bigint NOT NULL DEFAULT 0 CHECK (bytes_processed >= 0),
    fencing_token bigint NOT NULL DEFAULT 0 CHECK (fencing_token >= 0),
    error_code text,
    lease_owner text,
    lease_expires_at timestamptz,
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (agent_did, source_id)
);
CREATE TABLE IF NOT EXISTS connected_source_pages (
    agent_did text NOT NULL,
    source_id text NOT NULL,
    page_id text NOT NULL,
    cursor_after text,
    page_count bigint NOT NULL CHECK (page_count >= 0),
    page_bytes bigint NOT NULL CHECK (page_bytes >= 0),
    committed_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (agent_did, source_id, page_id),
    FOREIGN KEY (agent_did, source_id)
        REFERENCES connected_source_sync(agent_did, source_id) ON DELETE CASCADE
);
