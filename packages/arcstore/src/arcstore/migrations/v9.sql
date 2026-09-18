ALTER TABLE connected_source_sync
    ADD COLUMN IF NOT EXISTS budget_reached boolean NOT NULL DEFAULT false;
