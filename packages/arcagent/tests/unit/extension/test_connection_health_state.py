"""The connection-health state machine (P18-1, design sections 1.2 and 2).

``next_health`` is pure: a record, a signal and a clock go in; a CAS patch and a
transition come out. Every rule of the design's transition table is one test, so
a rule that drifts shows up as one named failure.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from arcagent.extension.connection_health import (
    HealthSignal,
    classify,
    next_health,
    reason_text,
)
from arcagent.extension.state import ConnectionRecord

NOW = datetime(2026, 10, 2, 12, 0, tzinfo=UTC)
PROBE = "did:arc:system:connection-health"


def _record(**fields: object) -> ConnectionRecord:
    base: dict[str, object] = {"connection": "gmail", "created_at": NOW.isoformat()}
    return ConnectionRecord.model_validate({**base, **fields})


def _fail(code: str, *, source: str = "probe", detail: str = "") -> HealthSignal:
    return HealthSignal(
        ok=False,
        source=source,  # type: ignore[arg-type] # reason: parametrised literal in tests
        checked_by=PROBE,
        reason_code=code,  # type: ignore[arg-type] # reason: parametrised literal in tests
        detail=detail,
        provider="Google",
    )


def _ok(source: str = "probe") -> HealthSignal:
    return HealthSignal(ok=True, source=source, checked_by=PROBE)  # type: ignore[arg-type]


def _apply(record: ConnectionRecord, patch: dict[str, object]) -> ConnectionRecord:
    return ConnectionRecord.model_validate({**record.model_dump(mode="json"), **patch})


def test_auth_code_escalates_to_needs_you_immediately() -> None:
    record = _record(status="healthy")

    patch, transition = next_health(record, _fail("invalid_grant"), NOW)

    assert patch["status"] == "needs_you"
    assert patch["action"] == "reconnect"
    assert transition.notice == "needs_you"
    assert patch["transition_seq"] == 1
    assert patch["notice_seq"] == 1
    assert patch["revision"] == 1


def test_unknown_failure_is_counted_not_terminal() -> None:
    record = _record(status="healthy", last_success_at=NOW.isoformat())

    patch, transition = next_health(record, _fail("provider_unavailable"), NOW)

    after = _apply(record, patch)
    assert after.status == "healthy"
    assert after.consecutive_failures == 1
    assert after.reason_text is None
    assert transition.notice is None


def test_three_failures_over_ten_minutes_is_error() -> None:
    record = _record(status="healthy", last_success_at=NOW.isoformat())
    transition = None
    for minutes in (0, 5, 11):
        patch, transition = next_health(
            record, _fail("provider_unavailable", detail="503"), NOW + timedelta(minutes=minutes)
        )
        record = _apply(record, patch)

    assert record.status == "error"
    assert record.action == "wait"
    assert transition is not None
    assert transition.notice == "error"


def test_three_failures_inside_two_minutes_stay_healthy() -> None:
    record = _record(status="healthy", last_success_at=NOW.isoformat())
    for seconds in (0, 40, 100):
        patch, _ = next_health(
            record, _fail("provider_unavailable"), NOW + timedelta(seconds=seconds)
        )
        record = _apply(record, patch)

    assert record.status == "healthy"
    assert record.consecutive_failures == 3


def test_24h_without_success_is_error_on_first_failure() -> None:
    record = _record(status="healthy", last_success_at=(NOW - timedelta(hours=25)).isoformat())

    patch, transition = next_health(record, _fail("sync_failed", detail="boom"), NOW)

    assert _apply(record, patch).status == "error"
    assert transition.notice == "error"


def test_transient_never_downgrades_needs_you() -> None:
    record = _record(
        status="needs_you",
        action="reconnect",
        reason_code="auth_required",
        transition_seq=3,
        notice_seq=3,
    )

    patch, transition = next_health(record, _fail("provider_unavailable"), NOW)

    after = _apply(record, patch)
    assert after.status == "needs_you"
    assert after.reason_code == "auth_required"
    assert after.transition_seq == 3
    assert transition.notice is None


def test_success_resets_counters_and_sets_healthy_with_recovered_notice() -> None:
    record = _record(
        status="error",
        action="wait",
        reason_code="provider_unavailable",
        consecutive_failures=4,
        failing_since=(NOW - timedelta(hours=1)).isoformat(),
        transition_seq=2,
        notice_seq=2,
        notified_seq=2,
        last_notice={
            "seq": 2,
            "kind": "error",
            "delivered": True,
            "channel": "telegram",
            "at": NOW.isoformat(),
        },
    )

    patch, transition = next_health(record, _ok(), NOW)

    after = _apply(record, patch)
    assert after.status == "healthy"
    assert after.consecutive_failures == 0
    assert after.failing_since is None
    assert after.last_success_at == NOW.isoformat()
    assert transition.notice == "recovered"
    assert after.notice_seq == after.transition_seq == 3


def test_recovery_sends_no_notice_when_the_outage_was_never_reported() -> None:
    record = _record(
        status="needs_you",
        action="reconnect",
        reason_code="auth_required",
        transition_seq=1,
        notice_seq=1,
        notified_seq=0,
    )

    patch, transition = next_health(record, _ok("sync"), NOW)

    assert _apply(record, patch).status == "healthy"
    assert transition.notice is None
    assert patch["transition_seq"] == 2
    assert "notice_seq" not in patch


def test_sticky_contract_change_survives_probe_success() -> None:
    record = _record(
        status="needs_you", action="approve", reason_code="contract_changed", transition_seq=1
    )

    kept, kept_transition = next_health(record, _ok("probe"), NOW)
    cleared, cleared_transition = next_health(record, _ok("contract"), NOW)

    assert _apply(record, kept).status == "needs_you"
    assert _apply(record, kept).action == "approve"
    assert kept_transition.notice is None
    assert _apply(record, cleared).status == "healthy"
    assert cleared_transition.notice is None


def test_operator_caused_transitions_send_no_notice() -> None:
    record = _record(status="unknown")

    patch, transition = next_health(record, _fail("credential_missing", source="operator"), NOW)

    after = _apply(record, patch)
    assert after.status == "needs_you"
    assert transition.notice is None
    assert after.notice_seq == 0


def test_record_check_returns_transition_only_on_change() -> None:
    record = _record(status="healthy")
    first_patch, first = next_health(record, _fail("auth_required"), NOW)
    record = _apply(record, first_patch)

    second_patch, second = next_health(record, _fail("auth_required"), NOW + timedelta(minutes=5))

    assert first.notice == "needs_you"
    assert second.seq == first.seq
    assert second.notice is None
    assert "notice_seq" not in second_patch


def test_needs_you_with_a_different_action_is_a_new_transition_with_notice() -> None:
    record = _record(
        status="needs_you",
        action="reconnect",
        reason_code="auth_required",
        transition_seq=1,
        notice_seq=1,
        notified_seq=1,
    )

    patch, transition = next_health(record, _fail("contract_changed", detail="2"), NOW)

    assert _apply(record, patch).action == "approve"
    assert transition.notice == "needs_you"
    assert patch["transition_seq"] == 2


def test_unknown_record_with_counted_failure_stays_unknown() -> None:
    record = _record(status="unknown")

    patch, transition = next_health(record, _fail("provider_unavailable"), NOW)

    assert _apply(record, patch).status == "unknown"
    assert transition.notice is None


def test_every_write_bumps_revision_and_stamps_the_checker() -> None:
    record = _record(status="healthy", revision=7)

    patch, _ = next_health(record, _ok(), NOW)

    assert patch["revision"] == 8
    assert patch["last_checked_at"] == NOW.isoformat()
    assert patch["checked_by"] == PROBE


def test_credential_generation_is_patched_when_supplied() -> None:
    record = _record(status="healthy")
    signal = HealthSignal(ok=True, source="probe", checked_by=PROBE, credential_generation=4)

    patch, _ = next_health(record, signal, NOW)

    assert patch["credential_generation"] == 4


@pytest.mark.parametrize(
    ("code", "text", "expected"),
    [
        ("auth_required", "", "auth_required"),
        ("invalid_grant", "", "invalid_grant"),
        ("consent_required", "", "consent_required"),
        ("interaction_required", "", "consent_required"),
        (None, "No auth for gmail", "auth_required"),
        (None, "gog exited: no keyring available", "auth_required"),
        (None, "HTTP 401 Unauthorized", "auth_required"),
        (None, "token_revoked", "token_revoked"),
        (None, "invalid_auth", "token_revoked"),
        ("repeated_failures", "", "repeated_failures"),
        ("sync_stalled", "", "sync_failed"),
        (None, "LeaseLostError: lease lost", "sync_failed"),
        (None, "SyncError: time limit exceeded", "sync_failed"),
        (None, "HTTP 429 Too Many Requests", "rate_limited"),
        (None, "HTTP 503 Service Unavailable", "provider_unavailable"),
        (None, "connection refused", "provider_unavailable"),
        (None, "request timed out", "provider_unavailable"),
        ("CREDENTIAL_MISSING", "", "credential_missing"),
        (None, "something nobody has seen before", "provider_unavailable"),
        ("some_unmapped_code", "", "provider_unavailable"),
    ],
)
def test_classify_maps_every_known_producer(code: str | None, text: str, expected: str) -> None:
    assert classify(code, text) == expected


def test_reason_text_strips_query_strings_and_token_shapes() -> None:
    jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.abcdefghijklmnopqrstuvwxyz012345"
    hostile = (
        "failed https://accounts.example/o?code=4/0AbXyZ&state=x then "
        f"ghp_{'a' * 36} and xoxp-123456789012-abcdefghij and {jwt}\n\x07@everyone"
    )

    text = reason_text("provider_unavailable", provider="Google", detail=hostile)

    assert "4/0Ab" not in text
    assert "state=x" not in text
    assert "ghp_" not in text
    assert "xoxp-" not in text
    assert jwt not in text
    assert "\n" not in text
    assert "\x07" not in text
    assert len(text) <= 200


def test_reason_text_uses_the_plain_language_template() -> None:
    assert reason_text("auth_required", provider="Google", detail="") == (
        "Google sign-in expired or was revoked"
    )
    assert reason_text("credential_missing", provider="Google", detail="") == "Not connected yet"
