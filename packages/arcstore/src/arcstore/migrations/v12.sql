SET LOCAL lock_timeout = '2s';
CREATE INDEX IF NOT EXISTS mutable_tasks_created_page_idx
    ON mutable_records ((coalesce(value->>'created_at','')) DESC, key DESC)
    WHERE collection = 'tasks';
CREATE INDEX IF NOT EXISTS mutable_tasks_owner_created_idx
    ON mutable_records ((value->>'owner_did'), (coalesce(value->>'created_at','')) DESC, key DESC)
    WHERE collection = 'tasks';
CREATE INDEX IF NOT EXISTS mutable_tasks_status_created_idx
    ON mutable_records ((value->>'status'), (coalesce(value->>'created_at','')) DESC, key DESC)
    WHERE collection = 'tasks';
CREATE INDEX IF NOT EXISTS mutable_tasks_priority_created_idx
    ON mutable_records ((value->>'priority'), (coalesce(value->>'created_at','')) DESC, key DESC)
    WHERE collection = 'tasks';

CREATE TABLE IF NOT EXISTS task_board_revision (
    singleton boolean PRIMARY KEY DEFAULT true CHECK (singleton),
    revision bigint NOT NULL DEFAULT 0
);
INSERT INTO task_board_revision(singleton, revision) VALUES (true, 0)
    ON CONFLICT (singleton) DO NOTHING;

CREATE OR REPLACE FUNCTION arcstore_guard_task_created_at() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF OLD.collection = 'tasks' AND
       (OLD.value->>'created_at') IS DISTINCT FROM (NEW.value->>'created_at') THEN
        RAISE EXCEPTION 'task creation time is immutable';
    END IF;
    RETURN NEW;
END;
$$;
DROP TRIGGER IF EXISTS mutable_tasks_created_guard ON mutable_records;
CREATE TRIGGER mutable_tasks_created_guard BEFORE UPDATE ON mutable_records
FOR EACH ROW EXECUTE FUNCTION arcstore_guard_task_created_at();

CREATE OR REPLACE FUNCTION arcstore_bump_task_board_revision() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF (TG_OP = 'DELETE' AND OLD.collection = 'tasks') OR
       (TG_OP <> 'DELETE' AND NEW.collection = 'tasks') THEN
        UPDATE task_board_revision SET revision = revision + 1 WHERE singleton = true;
    END IF;
    RETURN COALESCE(NEW, OLD);
END;
$$;
DROP TRIGGER IF EXISTS mutable_tasks_board_revision ON mutable_records;
CREATE TRIGGER mutable_tasks_board_revision
AFTER INSERT OR UPDATE OR DELETE ON mutable_records
FOR EACH ROW EXECUTE FUNCTION arcstore_bump_task_board_revision();
