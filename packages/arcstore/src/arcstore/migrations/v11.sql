SET LOCAL lock_timeout = '2s';
CREATE INDEX IF NOT EXISTS mutable_tasks_status_page_idx
    ON mutable_records ((value->>'status'), updated_at DESC, key DESC)
    WHERE collection = 'tasks';
CREATE INDEX IF NOT EXISTS mutable_tasks_priority_page_idx
    ON mutable_records ((value->>'priority'), updated_at DESC, key DESC)
    WHERE collection = 'tasks';
CREATE INDEX IF NOT EXISTS mutable_tasks_owner_page_idx
    ON mutable_records ((value->>'owner_did'), updated_at DESC, key DESC)
    WHERE collection = 'tasks';
CREATE INDEX IF NOT EXISTS mutable_tasks_parent_idx
    ON mutable_records ((value->>'parent_id'))
    WHERE collection = 'tasks';
CREATE INDEX IF NOT EXISTS mutable_tasks_tags_idx
    ON mutable_records USING gin ((value->'tags'))
    WHERE collection = 'tasks';
