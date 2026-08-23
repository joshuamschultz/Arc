ALTER TABLE mail_outbox DROP CONSTRAINT IF EXISTS mail_outbox_status_check;
ALTER TABLE mail_outbox
    ADD CONSTRAINT mail_outbox_status_check
    CHECK (status IN ('pending', 'leased', 'delivered', 'dead_lettered'));
ALTER TABLE mail_outbox ADD COLUMN IF NOT EXISTS failure_reason text;
ALTER TABLE mail_outbox ADD COLUMN IF NOT EXISTS dead_lettered_at timestamptz;
DROP INDEX IF EXISTS mail_outbox_ready_idx;
CREATE INDEX IF NOT EXISTS mail_outbox_ready_idx
    ON mail_outbox (available_at, created_at)
    WHERE status IN ('pending', 'leased');
