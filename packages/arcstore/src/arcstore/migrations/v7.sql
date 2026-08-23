ALTER TABLE connected_source_sync
    ADD COLUMN IF NOT EXISTS generation bigint NOT NULL DEFAULT 1 CHECK (generation >= 1);
