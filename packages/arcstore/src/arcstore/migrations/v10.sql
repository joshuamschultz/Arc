SET LOCAL lock_timeout = '2s';
CREATE INDEX IF NOT EXISTS mutable_tasks_open_idx
    ON mutable_records (updated_at DESC, key DESC)
    WHERE collection = 'tasks' AND (value->>'status') NOT IN ('done', 'failed');
CREATE INDEX IF NOT EXISTS mutable_tasks_history_idx
    ON mutable_records (updated_at DESC, key DESC)
    WHERE collection = 'tasks' AND (value->>'status') IN ('done', 'failed');
CREATE INDEX IF NOT EXISTS mutable_tasks_recent_idx
    ON mutable_records (updated_at DESC)
    WHERE collection = 'tasks';
