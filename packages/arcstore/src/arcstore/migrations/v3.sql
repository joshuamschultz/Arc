-- Keep the migration bounded if a hot table cannot acquire its brief DDL lock.
-- CREATE INDEX CONCURRENTLY cannot run inside the transaction used by the
-- startup migrator, so the lock timeout is deliberately fail-fast instead.
SET LOCAL lock_timeout = '3s';

CREATE INDEX IF NOT EXISTS inbox_threads_inbox_updated_idx
    ON inbox_threads (inbox_id, updated_at DESC, thread_id DESC);
CREATE INDEX IF NOT EXISTS inbox_messages_thread_created_idx
    ON inbox_messages (thread_id, created_at ASC, message_id ASC);
CREATE INDEX IF NOT EXISTS inbox_handoffs_thread_created_idx
    ON inbox_handoffs (thread_id, created_at ASC, handoff_id ASC);
CREATE INDEX IF NOT EXISTS approval_outbox_approval_id_idx
    ON approval_outbox (approval_id);
