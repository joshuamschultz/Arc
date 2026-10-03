SET LOCAL lock_timeout = '2s';
-- Alpha-2 hotfix: rows left from the per-agent connection era (``agent`` +
-- ``instance`` fields, keyed ``<agent>/<instance>``) carry no ``connection`` key.
-- No code reads them and every connection-state list logged them as unreadable.
-- Delete exactly those rows; every current row names its ``connection``.
DELETE FROM mutable_records
 WHERE collection = 'connections'
   AND NOT (value ? 'connection');
