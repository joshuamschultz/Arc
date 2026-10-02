"""Renewal policy — when a credential is due and which failures need a human (COMP-011).

Covers REQ-287 (renew before expiry rather than waiting for a call to fail) and the
terminal-failure vocabulary of REQ-289. The single-flight, persist-before-use and
crash-window behavior of renewal itself lives in ``test_renewal_planner.py``; this
file holds the two pure policy pieces it relies on.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from arcagent.extension.credentials import (
    EXPIRY_FLOOR,
    TERMINAL_ERROR_CODES,
    CredentialRenewalError,
    is_due,
)
from arcagent.extension.custody import CredentialRow, SealedAccess

NOW = datetime(2026, 10, 2, 12, 0, tzinfo=UTC)
LIFETIME = timedelta(hours=1)


def _row(*, issued_at: datetime, expires_at: datetime | None) -> CredentialRow:
    access = (
        None
        if expires_at is None
        else SealedAccess(
            sealed="opaque", issued_at=issued_at.isoformat(), expires_at=expires_at.isoformat()
        )
    )
    return CredentialRow(connection="atlassian_work", cipher="xc1", access=access)


@pytest.mark.parametrize(
    "elapsed_fraction,expected",
    [(0.0, False), (0.5, False), (0.74, False), (0.8, True), (1.0, True), (1.5, True)],
)
def test_renewal_is_due_at_three_quarters_of_lifetime(
    elapsed_fraction: float, expected: bool
) -> None:
    """Renewal is a function of elapsed lifetime, not of a call having failed."""
    issued = NOW - LIFETIME * elapsed_fraction
    row = _row(issued_at=issued, expires_at=issued + LIFETIME)

    assert is_due(row, now=NOW) is expected


def test_a_token_inside_the_expiry_floor_is_always_due() -> None:
    """A short-lived token is renewed with a floor of lead time, whatever its lifetime."""
    issued = NOW - timedelta(seconds=10)
    row = _row(issued_at=issued, expires_at=NOW + EXPIRY_FLOOR - timedelta(seconds=1))

    assert is_due(row, now=NOW) is True


def test_a_connection_with_no_access_token_is_due() -> None:
    """Nothing is cached, so the next caller must renew before it can use the account."""
    assert is_due(_row(issued_at=NOW, expires_at=None), now=NOW) is True


@pytest.mark.parametrize("code", sorted(TERMINAL_ERROR_CODES))
def test_terminal_codes_need_a_human(code: str) -> None:
    error = CredentialRenewalError(error_code=code, message="x")

    assert error.terminal is True


@pytest.mark.parametrize("code", ["temporarily_unavailable", "server_error", "network_error"])
def test_other_codes_are_retryable(code: str) -> None:
    error = CredentialRenewalError(error_code=code, message="x")

    assert error.terminal is False
