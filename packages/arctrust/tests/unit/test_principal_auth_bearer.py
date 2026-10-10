"""SPEC-087 T-1275: app principal bearer verification (COMP-004, bearer path).

``arctrust.principal_auth`` is pure verification with no HTTP types. The bearer
wire format is ``arcp_<id>.<base64url 32 bytes>`` where ``<id>`` is the
principal's uuid4 hex id. The store keeps only ``sha256(secret)`` hex.

Contract pinned here:
- ``parse_bearer(token) -> (principal_id, secret)`` refuses every malformed
  shape with reason ``malformed``.
- ``issue_secret() -> (secret, secret_digest)`` mints a fresh base64url secret
  of 32 random bytes and its SHA-256 hex digest (``secret_digest(secret)``).
- ``format_bearer(principal_id, secret)`` is the inverse of ``parse_bearer``.
- ``verify_bearer(record, secret, *, now) -> AuthenticatedPrincipal`` refuses
  unknown / pending / revoked / expired / bad_secret, each with a distinct
  internal ``reason`` and the SAME external ``code`` (REQ-555), so a caller
  cannot probe which principal ids exist or which are revoked.
- The secret comparison is ``hmac.compare_digest`` over digests (REQ-555
  "constant time"), and status/expiry run first, so a revoked or expired
  principal never reaches the digest comparison.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from arctrust.principal_auth import (
    AuthenticatedPrincipal,
    PrincipalRejected,
    format_bearer,
    issue_secret,
    parse_bearer,
    secret_digest,
    verify_bearer,
)
from nacl.signing import SigningKey

from arctrust import principal_auth
from arctrust.identity import did_from_public_key
from arctrust.principals import Principal, Scope, derive_app_did

ORG = "default"
NOW = datetime(2026, 10, 10, 12, 0, 0, tzinfo=UTC)
PRINCIPAL_ID = "0123456789abcdef0123456789abcdef"

BEARER_REASONS = ("unknown", "pending", "revoked", "expired", "bad_secret", "malformed")


def _did(agent_type: str) -> str:
    return did_from_public_key(
        bytes(SigningKey.generate().verify_key), org=ORG, agent_type=agent_type
    )


AGENT_A = _did("executor")
PERSON_1 = _did("user")
OPERATOR = _did("user")


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64url_decode(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def make_principal(secret: str, **overrides: Any) -> Principal:
    """An active principal whose stored digest matches ``secret``."""
    public_key = bytes(SigningKey.generate().verify_key)
    fields: dict[str, Any] = {
        "id": PRINCIPAL_ID,
        "name": "billing-bot",
        "did": derive_app_did(public_key, org=ORG),
        "public_key": public_key.hex(),
        "secret_digest": hashlib.sha256(secret.encode("ascii")).hexdigest(),
        "scopes": {Scope.RUNS_READ: (AGENT_A,), Scope.WORKFLOWS_RUN: (AGENT_A,)},
        "subjects": (PERSON_1,),
        "status": "active",
        "created_by": OPERATOR,
        "approved_by": OPERATOR,
        "created_at": NOW - timedelta(days=1),
        "expires_at": NOW + timedelta(days=7),
        "last_used_at": None,
        "revoked_by": None,
        "revoked_at": None,
    }
    fields.update(overrides)
    return Principal(**fields)


def _reject(call: Callable[[], object]) -> PrincipalRejected:
    with pytest.raises(PrincipalRejected) as info:
        call()
    return info.value


@pytest.fixture
def compare_calls(monkeypatch: pytest.MonkeyPatch) -> list[tuple[Any, Any]]:
    """Record every ``hmac.compare_digest`` call made through principal_auth."""
    real = hmac.compare_digest
    calls: list[tuple[Any, Any]] = []

    def spy(a: Any, b: Any) -> bool:
        calls.append((a, b))
        return real(a, b)

    monkeypatch.setattr(principal_auth.hmac, "compare_digest", spy)
    return calls


# ---------------------------------------------------------------------------
# Secret minting and wire format
# ---------------------------------------------------------------------------


class TestIssueSecret:
    def test_issue_secret_returns_base64url_of_32_random_bytes(self) -> None:
        secret, _ = issue_secret()
        assert "=" not in secret and "+" not in secret and "/" not in secret
        assert len(_b64url_decode(secret)) == 32

    def test_issue_secret_digest_is_sha256_hex_of_secret(self) -> None:
        secret, digest = issue_secret()
        assert digest == hashlib.sha256(secret.encode("ascii")).hexdigest()
        assert digest == secret_digest(secret)

    def test_issue_secret_is_fresh_every_call(self) -> None:
        secrets_seen = {issue_secret()[0] for _ in range(50)}
        assert len(secrets_seen) == 50

    def test_format_and_parse_bearer_round_trip(self) -> None:
        secret, _ = issue_secret()
        token = format_bearer(PRINCIPAL_ID, secret)
        assert token == f"arcp_{PRINCIPAL_ID}.{secret}"
        assert parse_bearer(token) == (PRINCIPAL_ID, secret)


class TestParseBearerRefusesMalformed:
    GOOD_SECRET = _b64url(b"\x07" * 32)

    @pytest.mark.parametrize(
        "token",
        [
            pytest.param("", id="empty"),
            pytest.param("arcp_", id="prefix-only"),
            pytest.param(f"arcp_{PRINCIPAL_ID}", id="no-dot"),
            pytest.param(f"arcp_{PRINCIPAL_ID}.", id="empty-secret"),
            pytest.param(f"arcp_.{_b64url(b'x' * 32)}", id="empty-id"),
            pytest.param(f"arcx_{PRINCIPAL_ID}.{_b64url(b'x' * 32)}", id="wrong-prefix"),
            pytest.param(f"ARCP_{PRINCIPAL_ID}.{_b64url(b'x' * 32)}", id="upper-prefix"),
            pytest.param(f"Bearer arcp_{PRINCIPAL_ID}.{_b64url(b'x' * 32)}", id="scheme-kept"),
            pytest.param(f"arcp_{PRINCIPAL_ID}.{_b64url(b'x' * 32)}.extra", id="extra-dot"),
            pytest.param(f"arcp_{'z' * 32}.{_b64url(b'x' * 32)}", id="non-hex-id"),
            pytest.param(f"arcp_{PRINCIPAL_ID[:-1]}.{_b64url(b'x' * 32)}", id="short-id"),
            pytest.param(f"arcp_{PRINCIPAL_ID}0.{_b64url(b'x' * 32)}", id="long-id"),
            pytest.param(f"arcp_../../etc.{_b64url(b'x' * 32)}", id="path-id"),
            pytest.param(f"arcp_{PRINCIPAL_ID}.{_b64url(b'x' * 16)}", id="short-secret"),
            pytest.param(f"arcp_{PRINCIPAL_ID}.{_b64url(b'x' * 64)}", id="long-secret"),
            pytest.param(
                f"arcp_{PRINCIPAL_ID}." + base64.b64encode(b"\xfb" * 32).decode(),
                id="std-base64-secret",
            ),
            pytest.param(f"arcp_{PRINCIPAL_ID}.{_b64url(b'x' * 32)}\n", id="trailing-newline"),
            pytest.param(f" arcp_{PRINCIPAL_ID}.{_b64url(b'x' * 32)}", id="leading-space"),
            pytest.param("arcp_" + "a" * 10_000 + "." + "b" * 10_000, id="oversized"),
        ],
    )
    def test_parse_bearer_malformed_token_refused(self, token: str) -> None:
        rejected = _reject(lambda: parse_bearer(token))
        assert rejected.reason == "malformed"

    def test_parse_bearer_refusal_never_echoes_token(self) -> None:
        token = f"arcp_{PRINCIPAL_ID}.{_b64url(b'x' * 16)}"
        rejected = _reject(lambda: parse_bearer(token))
        assert token not in str(rejected)
        assert token not in repr(rejected)


# ---------------------------------------------------------------------------
# verify_bearer: acceptance and each refusal reason (REQ-555)
# ---------------------------------------------------------------------------


class TestVerifyBearerAccepts:
    def test_verify_bearer_correct_secret_returns_authenticated_principal(self) -> None:
        secret, _ = issue_secret()
        record = make_principal(secret)

        result = verify_bearer(record, secret, now=NOW)

        assert isinstance(result, AuthenticatedPrincipal)
        assert result.did == record.did
        assert dict(result.scopes) == dict(record.scopes)
        assert tuple(result.subjects) == record.subjects

    def test_verify_bearer_no_expiry_personal_record_accepted(self) -> None:
        secret, _ = issue_secret()
        record = make_principal(secret, expires_at=None)
        assert verify_bearer(record, secret, now=NOW).did == record.did

    def test_verify_bearer_one_second_before_expiry_accepted(self) -> None:
        secret, _ = issue_secret()
        record = make_principal(secret, expires_at=NOW + timedelta(seconds=1))
        assert verify_bearer(record, secret, now=NOW).did == record.did


class TestVerifyBearerRefuses:
    def test_verify_bearer_unknown_record_refused(self) -> None:
        secret, _ = issue_secret()
        assert _reject(lambda: verify_bearer(None, secret, now=NOW)).reason == "unknown"

    def test_verify_bearer_pending_refused_even_with_correct_secret(self) -> None:
        secret, _ = issue_secret()
        record = make_principal(secret, status="pending", approved_by=None)
        assert _reject(lambda: verify_bearer(record, secret, now=NOW)).reason == "pending"

    def test_verify_bearer_revoked_refused_even_with_correct_secret(self) -> None:
        secret, _ = issue_secret()
        record = make_principal(
            secret, status="revoked", revoked_by=OPERATOR, revoked_at=NOW - timedelta(hours=1)
        )
        assert _reject(lambda: verify_bearer(record, secret, now=NOW)).reason == "revoked"

    def test_verify_bearer_expired_refused_even_with_correct_secret(self) -> None:
        secret, _ = issue_secret()
        record = make_principal(secret, expires_at=NOW - timedelta(seconds=1))
        assert _reject(lambda: verify_bearer(record, secret, now=NOW)).reason == "expired"

    def test_verify_bearer_expiry_instant_is_already_expired(self) -> None:
        secret, _ = issue_secret()
        record = make_principal(secret, expires_at=NOW)
        assert _reject(lambda: verify_bearer(record, secret, now=NOW)).reason == "expired"

    def test_verify_bearer_wrong_secret_refused(self) -> None:
        secret, _ = issue_secret()
        other, _ = issue_secret()
        record = make_principal(secret)
        assert _reject(lambda: verify_bearer(record, other, now=NOW)).reason == "bad_secret"

    def test_verify_bearer_digest_presented_as_secret_refused(self) -> None:
        """A leaked store digest must not work as a credential."""
        secret, digest = issue_secret()
        record = make_principal(secret)
        assert _reject(lambda: verify_bearer(record, digest, now=NOW)).reason == "bad_secret"

    def test_verify_bearer_empty_secret_refused(self) -> None:
        secret, _ = issue_secret()
        record = make_principal(secret)
        rejected = _reject(lambda: verify_bearer(record, "", now=NOW))
        assert rejected.reason in {"bad_secret", "malformed"}

    def test_verify_bearer_revoked_reported_before_wrong_secret(self) -> None:
        secret, _ = issue_secret()
        record = make_principal(secret, status="revoked", revoked_by=OPERATOR, revoked_at=NOW)
        assert _reject(lambda: verify_bearer(record, "nope", now=NOW)).reason == "revoked"


class TestRejectionCodes:
    def _all_rejections(self) -> dict[str, PrincipalRejected]:
        secret, _ = issue_secret()
        wrong, _ = issue_secret()
        return {
            "unknown": _reject(lambda: verify_bearer(None, secret, now=NOW)),
            "pending": _reject(
                lambda: verify_bearer(
                    make_principal(secret, status="pending", approved_by=None), secret, now=NOW
                )
            ),
            "revoked": _reject(
                lambda: verify_bearer(
                    make_principal(secret, status="revoked", revoked_by=OPERATOR, revoked_at=NOW),
                    secret,
                    now=NOW,
                )
            ),
            "expired": _reject(
                lambda: verify_bearer(
                    make_principal(secret, expires_at=NOW - timedelta(days=1)), secret, now=NOW
                )
            ),
            "bad_secret": _reject(lambda: verify_bearer(make_principal(secret), wrong, now=NOW)),
            "malformed": _reject(lambda: parse_bearer("arcp_garbage")),
        }

    def test_each_refusal_has_a_distinct_internal_reason(self) -> None:
        rejections = self._all_rejections()
        assert {name: exc.reason for name, exc in rejections.items()} == {
            name: name for name in BEARER_REASONS
        }

    def test_every_refusal_has_the_same_external_code(self) -> None:
        codes = {exc.code for exc in self._all_rejections().values()}
        assert len(codes) == 1
        (code,) = codes
        assert isinstance(code, str) and code
        assert code not in BEARER_REASONS

    def test_external_code_and_message_never_reveal_reason(self) -> None:
        for exc in self._all_rejections().values():
            assert exc.reason not in exc.code

    def test_refusal_never_echoes_presented_secret(self) -> None:
        secret, _ = issue_secret()
        wrong, _ = issue_secret()
        rejected = _reject(lambda: verify_bearer(make_principal(secret), wrong, now=NOW))
        assert wrong not in str(rejected) and wrong not in repr(rejected)
        assert secret not in str(rejected) and secret not in repr(rejected)


# ---------------------------------------------------------------------------
# Constant-time comparison and check ordering
# ---------------------------------------------------------------------------


class TestDigestComparison:
    def test_correct_secret_compared_with_compare_digest_on_digests(
        self, compare_calls: list[tuple[Any, Any]]
    ) -> None:
        secret, digest = issue_secret()
        record = make_principal(secret)

        verify_bearer(record, secret, now=NOW)

        assert len(compare_calls) == 1
        compared = {_as_text(arg) for arg in compare_calls[0]}
        assert compared == {digest}
        assert secret not in {_as_text(arg) for arg in compare_calls[0]}

    def test_wrong_secret_compared_with_compare_digest_on_digests(
        self, compare_calls: list[tuple[Any, Any]]
    ) -> None:
        secret, digest = issue_secret()
        wrong, wrong_digest = issue_secret()
        record = make_principal(secret)

        _reject(lambda: verify_bearer(record, wrong, now=NOW))

        assert len(compare_calls) == 1
        assert {_as_text(arg) for arg in compare_calls[0]} == {digest, wrong_digest}

    @pytest.mark.parametrize(
        "overrides",
        [
            pytest.param({"status": "pending", "approved_by": None}, id="pending"),
            pytest.param(
                {"status": "revoked", "revoked_by": OPERATOR, "revoked_at": NOW}, id="revoked"
            ),
            pytest.param({"expires_at": NOW - timedelta(minutes=1)}, id="expired"),
        ],
    )
    def test_status_and_expiry_checked_before_digest(
        self, compare_calls: list[tuple[Any, Any]], overrides: dict[str, Any]
    ) -> None:
        secret, _ = issue_secret()
        record = make_principal(secret, **overrides)

        _reject(lambda: verify_bearer(record, secret, now=NOW))

        assert compare_calls == []


def _as_text(value: Any) -> str:
    """compare_digest accepts str or bytes; normalise to hex text for asserts."""
    if isinstance(value, bytes):
        return value.decode("ascii") if len(value) == 64 else value.hex()
    return str(value)
