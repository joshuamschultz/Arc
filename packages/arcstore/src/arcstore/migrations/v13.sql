SET LOCAL lock_timeout = '2s';
-- Connection health authority (P18-1): the stored ``health`` field is replaced by
-- ``status`` and the CAS/notice fields. Every migrated row starts ``unknown`` and
-- is probed by the first monitor tick (``next_check_at`` null means due). Every
-- CAS'd key must be present, including when null: ``update_if`` compiles to
-- ``value #> path IS NOT DISTINCT FROM $json`` and a missing key never matches.
UPDATE mutable_records
   SET value = (value - 'health') || jsonb_build_object(
         'status', 'unknown', 'reason_code', null, 'reason_text', null, 'action', 'none',
         'last_checked_at', null, 'failing_since', null, 'consecutive_failures', 0,
         'checked_by', null, 'next_check_at', null, 'custody', 'none',
         'credential_generation', null, 'revision', 0,
         'transition_seq', 0, 'notice_seq', 0, 'notified_seq', 0,
         'notice_claim', null, 'last_notice', null,
         'notice_window_start', null, 'notice_window_count', 0)
 WHERE collection = 'connections';
