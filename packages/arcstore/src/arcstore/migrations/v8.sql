ALTER TABLE connected_source_sync
    ADD COLUMN IF NOT EXISTS last_synced_at timestamptz;
