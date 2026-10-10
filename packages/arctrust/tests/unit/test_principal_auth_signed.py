"""SPEC-087 T-1276: signed principal requests with replay protection (COMP-004).

This is THE Arc request-signing envelope (README cross-spec note; SPEC-086
COMP-004 adopts it unchanged): Ed25519 over
``canonical_json({method, path, query, body_sha256, nonce, ts, on_behalf_of})``
carried in headers ``X-Arc-Principal`` (the app DID), ``X-Arc-Nonce`` (>= 16
random bytes, base64url), ``X-Arc-Timestamp`` (ISO 8601 UTC),
``X-Arc-Signature`` (base64 Ed25519) and optional ``X-Arc-On-Behalf-Of``.

Contract pinned here:
- ``sign_request(private_key, *, principal_did, method, path, query, body,
  on_behalf_of, nonce=None, ts=None) -> dict[str, str]`` of headers.
- ``SignedRequest.from_headers(headers, method, path, query, body)`` builds the
  envelope (header names are case-insensitive; body is hashed, not kept).
- ``verify_signed(record, envelope, replay_cache, *, now)`` returns an
  ``AuthenticatedPrincipal`` or raises ``PrincipalRejected``.
- REQ-559: every bound field is covered by the signature.
- REQ-560: reused nonce or ts outside the 300 s window is refused.
- Status/expiry run before any crypto and before the replay cache, so a
  revoked key never reaches verification and never burns a nonce.

Real Ed25519 keys (arctrust.keypair) and a real ``ReplayCache`` throughout.
The cache reads the wall clock, so ``now`` is the real current time.
"""

from __future__ import annotations

import base64
import hashlib
import json
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from arctrust import replay as replay_module
from arctrust.canonical import canonical_json
from arctrust.identity import did_from_public_key
from arctrust.keypair import KeyPair, generate_keypair, verify
from arctrust.principal_auth import (
    CLOCK_SKEW_SECONDS,
    REPLAY_WINDOW_SECONDS,
    AuthenticatedPrincipal,
    PrincipalRejected,
    SignedRequest,
    sign_request,
    verify_signed,
)
from arctrust.principals import Principal, Scope, derive_app_did
from arctrust.replay import ReplayCache

ORG = "default"
METHOD = "POST"
PATH = "/api/workflows/wf-42/run"
QUERY = "dry_run=false&tag=nightly"
BODY = b'{"inputs":{"invoice":"INV-7"}}'

HEADER_NAMES = {
    "X-Arc-Principal",
    "X-Arc-Nonce",
    "X-Arc-Timestamp",
    "X-Arc-Signature",
}


def _did(agent_type: str) -> str:
    return did_from_public_key(generate_keypair().public_key, org=ORG, agent_type=agent_type)


AGENT_A = _did("executor")
PERSON_1 = _did("user")
PERSON_2 = _did("user")
OPERATOR = _did("user")


def _now() -> datetime:
    return datetime.now(UTC)


def _b64url_decode(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def make_principal(keys: KeyPair, **overrides: Any) -> Principal:
    """An active principal whose DID is bound to ``keys``."""
    now = _now()
    fields: dict[str, Any] = {
        "id": keys.public_key[:16].hex(),
        "name": "billing-bot",
        "did": derive_app_did(keys.public_key, org=ORG),
        "public_key": keys.public_key.hex(),
        "secret_digest": hashlib.sha256(b"unused-in-signed-mode").hexdigest(),
        "scopes": {Scope.WORKFLOWS_RUN: (AGENT_A,)},
        "subjects": (PERSON_1, PERSON_2),
        "status": "active",
        "created_by": OPERATOR,
        "approved_by": OPERATOR,
        "created_at": now - timedelta(days=1),
        "expires_at": now + timedelta(days=7),
        "last_used_at": None,
        "revoked_by": None,
        "revoked_at": None,
    }
    fields.update(overrides)
    return Principal(**fields)


def signed_headers(keys: KeyPair, record: Principal, **overrides: Any) -> dict[str, str]:
    kwargs: dict[str, Any] = {
        "principal_did": record.did,
        "method": METHOD,
        "path": PATH,
        "query": QUERY,
        "body": BODY,
        "on_behalf_of": PERSON_1,
    }
    kwargs.update(overrides)
    return sign_request(keys.private_key, **kwargs)


def envelope(
    headers: dict[str, str],
    *,
    method: str = METHOD,
    path: str = PATH,
    query: str = QUERY,
    body: bytes = BODY,
) -> SignedRequest:
    return SignedRequest.from_headers(headers, method, path, query, body)


def _reject(call: Callable[[], object]) -> PrincipalRejected:
    with pytest.raises(PrincipalRejected) as info:
        call()
    return info.value


@pytest.fixture
def keys() -> KeyPair:
    return generate_keypair()


@pytest.fixture
def record(keys: KeyPair) -> Principal:
    return make_principal(keys)


@pytest.fixture
def cache() -> ReplayCache:
    return ReplayCache(window_seconds=REPLAY_WINDOW_SECONDS)


# ---------------------------------------------------------------------------
# Wire format
# ---------------------------------------------------------------------------


class TestSignRequestWireFormat:
    def test_sign_request_emits_the_four_headers_plus_on_behalf_of(
        self, keys: KeyPair, record: Principal
    ) -> None:
        headers = signed_headers(keys, record)
        assert set(headers) == HEADER_NAMES | {"X-Arc-On-Behalf-Of"}
        assert headers["X-Arc-Principal"] == record.did
        assert headers["X-Arc-On-Behalf-Of"] == PERSON_1

    def test_sign_request_without_on_behalf_of_omits_header(
        self, keys: KeyPair, record: Principal
    ) -> None:
        headers = signed_headers(keys, record, on_behalf_of=None)
        assert set(headers) == HEADER_NAMES

    def test_sign_request_default_nonce_is_at_least_16_random_bytes(
        self, keys: KeyPair, record: Principal
    ) -> None:
        nonces = {signed_headers(keys, record)["X-Arc-Nonce"] for _ in range(20)}
        assert len(nonces) == 20
        assert all(len(_b64url_decode(n)) >= 16 for n in nonces)

    def test_sign_request_default_timestamp_is_iso8601_utc_now(
        self, keys: KeyPair, record: Principal
    ) -> None:
        ts = datetime.fromisoformat(signed_headers(keys, record)["X-Arc-Timestamp"])
        assert ts.utcoffset() == timedelta(0)
        assert abs((_now() - ts).total_seconds()) < 5

    def test_signature_is_base64_ed25519_over_canonical_envelope(
        self, keys: KeyPair, record: Principal
    ) -> None:
        """Pins the cross-spec envelope so SPEC-086 can sign without this code."""
        headers = signed_headers(keys, record)
        payload = canonical_json(
            {
                "method": METHOD,
                "path": PATH,
                "query": QUERY,
                "body_sha256": hashlib.sha256(BODY).hexdigest(),
                "nonce": headers["X-Arc-Nonce"],
                "ts": headers["X-Arc-Timestamp"],
                "on_behalf_of": PERSON_1,
            }
        )
        signature = base64.b64decode(headers["X-Arc-Signature"], validate=True)
        assert len(signature) == 64
        assert verify(payload, signature, keys.public_key)

    def test_from_headers_builds_envelope_with_body_digest(
        self, keys: KeyPair, record: Principal
    ) -> None:
        headers = signed_headers(keys, record)
        env = envelope(headers)
        assert env.principal_did == record.did
        assert env.nonce == headers["X-Arc-Nonce"]
        assert env.ts == headers["X-Arc-Timestamp"]
        assert env.signature == headers["X-Arc-Signature"]
        assert (env.method, env.path, env.query) == (METHOD, PATH, QUERY)
        assert env.body_sha256 == hashlib.sha256(BODY).hexdigest()
        assert env.on_behalf_of == PERSON_1

    def test_from_headers_is_case_insensitive(
        self, keys: KeyPair, record: Principal, cache: ReplayCache
    ) -> None:
        lowered = {name.lower(): value for name, value in signed_headers(keys, record).items()}
        result = verify_signed(record, envelope(lowered), cache, now=_now())
        assert result.did == record.did

    @pytest.mark.parametrize("missing", sorted(HEADER_NAMES))
    def test_from_headers_missing_required_header_refused(
        self, keys: KeyPair, record: Principal, missing: str
    ) -> None:
        headers = signed_headers(keys, record)
        del headers[missing]
        assert _reject(lambda: envelope(headers)).reason == "malformed"


# ---------------------------------------------------------------------------
# Round trip and field binding (REQ-559)
# ---------------------------------------------------------------------------


class TestRoundTrip:
    def test_sign_then_verify_returns_authenticated_principal(
        self, keys: KeyPair, record: Principal, cache: ReplayCache
    ) -> None:
        result = verify_signed(record, envelope(signed_headers(keys, record)), cache, now=_now())
        assert isinstance(result, AuthenticatedPrincipal)
        assert result.did == record.did
        assert dict(result.scopes) == dict(record.scopes)
        assert tuple(result.subjects) == record.subjects

    def test_round_trip_with_empty_body_and_query(
        self, keys: KeyPair, record: Principal, cache: ReplayCache
    ) -> None:
        headers = signed_headers(
            keys, record, method="GET", path="/api/workflow-runs/r1", query="", body=b""
        )
        env = envelope(headers, method="GET", path="/api/workflow-runs/r1", query="", body=b"")
        assert verify_signed(record, env, cache, now=_now()).did == record.did

    def test_round_trip_without_on_behalf_of(
        self, keys: KeyPair, record: Principal, cache: ReplayCache
    ) -> None:
        env = envelope(signed_headers(keys, record, on_behalf_of=None))
        assert env.on_behalf_of is None
        assert verify_signed(record, env, cache, now=_now()).did == record.did


class TestTamperedFieldsRefused:
    @pytest.mark.parametrize(
        "presented",
        [
            pytest.param({"method": "DELETE"}, id="method"),
            pytest.param({"path": "/api/workflows/wf-43/run"}, id="path"),
            pytest.param({"path": PATH + "/"}, id="path-trailing-slash"),
            pytest.param({"query": "dry_run=true&tag=nightly"}, id="query"),
            pytest.param({"query": ""}, id="query-dropped"),
            pytest.param({"body": b'{"inputs":{"invoice":"INV-8"}}'}, id="body"),
            pytest.param({"body": b""}, id="body-dropped"),
        ],
    )
    def test_changed_request_field_refused(
        self,
        keys: KeyPair,
        record: Principal,
        cache: ReplayCache,
        presented: dict[str, Any],
    ) -> None:
        env = envelope(signed_headers(keys, record), **presented)
        assert _reject(lambda: verify_signed(record, env, cache, now=_now())).reason == (
            "bad_signature"
        )

    def test_changed_on_behalf_of_refused(
        self, keys: KeyPair, record: Principal, cache: ReplayCache
    ) -> None:
        headers = signed_headers(keys, record)
        headers["X-Arc-On-Behalf-Of"] = PERSON_2
        rejected = _reject(lambda: verify_signed(record, envelope(headers), cache, now=_now()))
        assert rejected.reason == "bad_signature"

    def test_stripped_on_behalf_of_refused(
        self, keys: KeyPair, record: Principal, cache: ReplayCache
    ) -> None:
        headers = signed_headers(keys, record)
        del headers["X-Arc-On-Behalf-Of"]
        rejected = _reject(lambda: verify_signed(record, envelope(headers), cache, now=_now()))
        assert rejected.reason == "bad_signature"

    def test_added_on_behalf_of_refused(
        self, keys: KeyPair, record: Principal, cache: ReplayCache
    ) -> None:
        headers = signed_headers(keys, record, on_behalf_of=None)
        headers["X-Arc-On-Behalf-Of"] = PERSON_1
        rejected = _reject(lambda: verify_signed(record, envelope(headers), cache, now=_now()))
        assert rejected.reason == "bad_signature"

    def test_changed_timestamp_refused(
        self, keys: KeyPair, record: Principal, cache: ReplayCache
    ) -> None:
        headers = signed_headers(keys, record)
        headers["X-Arc-Timestamp"] = (_now() - timedelta(seconds=1)).isoformat()
        rejected = _reject(lambda: verify_signed(record, envelope(headers), cache, now=_now()))
        assert rejected.reason == "bad_signature"

    def test_changed_nonce_refused(
        self, keys: KeyPair, record: Principal, cache: ReplayCache
    ) -> None:
        headers = signed_headers(keys, record)
        headers["X-Arc-Nonce"] = _b64url(b"\x01" * 16)
        rejected = _reject(lambda: verify_signed(record, envelope(headers), cache, now=_now()))
        assert rejected.reason == "bad_signature"

    def test_garbage_signature_refused(
        self, keys: KeyPair, record: Principal, cache: ReplayCache
    ) -> None:
        headers = signed_headers(keys, record)
        headers["X-Arc-Signature"] = "!!not base64!!"
        rejected = _reject(lambda: verify_signed(record, envelope(headers), cache, now=_now()))
        assert rejected.reason in {"malformed", "bad_signature"}


# ---------------------------------------------------------------------------
# Replay and freshness (REQ-560)
# ---------------------------------------------------------------------------


class TestReplayAndFreshness:
    def test_replay_window_is_300_seconds(self) -> None:
        assert REPLAY_WINDOW_SECONDS == 300
        assert 0 < CLOCK_SKEW_SECONDS < REPLAY_WINDOW_SECONDS

    def test_reused_nonce_refused(
        self, keys: KeyPair, record: Principal, cache: ReplayCache
    ) -> None:
        headers = signed_headers(keys, record)
        verify_signed(record, envelope(headers), cache, now=_now())
        rejected = _reject(lambda: verify_signed(record, envelope(headers), cache, now=_now()))
        assert rejected.reason == "replay"

    def test_reused_nonce_with_fresh_signature_refused(
        self, keys: KeyPair, record: Principal, cache: ReplayCache
    ) -> None:
        """The nonce, not the signature bytes, is the replay key."""
        nonce = _b64url(b"\x42" * 16)
        verify_signed(
            record, envelope(signed_headers(keys, record, nonce=nonce)), cache, now=_now()
        )
        second = signed_headers(keys, record, nonce=nonce, query="other=1")
        rejected = _reject(
            lambda: verify_signed(record, envelope(second, query="other=1"), cache, now=_now())
        )
        assert rejected.reason == "replay"

    def test_timestamp_older_than_window_refused(
        self, keys: KeyPair, record: Principal, cache: ReplayCache
    ) -> None:
        now = _now()
        stale = (now - timedelta(seconds=REPLAY_WINDOW_SECONDS + 1)).isoformat()
        env = envelope(signed_headers(keys, record, ts=stale))
        assert _reject(lambda: verify_signed(record, env, cache, now=now)).reason == (
            "stale_timestamp"
        )

    def test_timestamp_just_inside_window_accepted(
        self, keys: KeyPair, record: Principal, cache: ReplayCache
    ) -> None:
        now = _now()
        recent = (now - timedelta(seconds=REPLAY_WINDOW_SECONDS - 5)).isoformat()
        env = envelope(signed_headers(keys, record, ts=recent))
        assert verify_signed(record, env, cache, now=now).did == record.did

    def test_timestamp_beyond_future_skew_refused(
        self, keys: KeyPair, record: Principal, cache: ReplayCache
    ) -> None:
        now = _now()
        future = (now + timedelta(seconds=CLOCK_SKEW_SECONDS + 1)).isoformat()
        env = envelope(signed_headers(keys, record, ts=future))
        assert _reject(lambda: verify_signed(record, env, cache, now=now)).reason == (
            "stale_timestamp"
        )

    def test_timestamp_far_future_refused(
        self, keys: KeyPair, record: Principal, cache: ReplayCache
    ) -> None:
        now = _now()
        future = (now + timedelta(days=365)).isoformat()
        env = envelope(signed_headers(keys, record, ts=future))
        assert _reject(lambda: verify_signed(record, env, cache, now=now)).reason == (
            "stale_timestamp"
        )

    def test_timestamp_within_future_skew_accepted(
        self, keys: KeyPair, record: Principal, cache: ReplayCache
    ) -> None:
        now = _now()
        future = (now + timedelta(seconds=CLOCK_SKEW_SECONDS - 1)).isoformat()
        env = envelope(signed_headers(keys, record, ts=future))
        assert verify_signed(record, env, cache, now=now).did == record.did

    @pytest.mark.parametrize(
        "ts",
        [
            pytest.param("2026-10-10T12:00:00", id="naive"),
            pytest.param("2026-10-10T12:00:00+05:00", id="non-utc"),
            pytest.param("yesterday", id="not-iso"),
            pytest.param("", id="empty"),
        ],
    )
    def test_non_utc_or_unparseable_timestamp_refused(
        self, keys: KeyPair, record: Principal, cache: ReplayCache, ts: str
    ) -> None:
        headers = signed_headers(keys, record, ts=ts)
        rejected = _reject(lambda: verify_signed(record, envelope(headers), cache, now=_now()))
        assert rejected.reason in {"malformed", "stale_timestamp"}

    @pytest.mark.parametrize(
        "nonce",
        [
            pytest.param(_b64url(b"\x09" * 15), id="15-bytes"),
            pytest.param(_b64url(b"\x09" * 8), id="8-bytes"),
            pytest.param("", id="empty"),
            pytest.param("a" * 4096, id="oversized"),
        ],
    )
    def test_short_or_oversized_nonce_refused(
        self, keys: KeyPair, record: Principal, cache: ReplayCache, nonce: str
    ) -> None:
        headers = signed_headers(keys, record, nonce=nonce)
        rejected = _reject(lambda: verify_signed(record, envelope(headers), cache, now=_now()))
        assert rejected.reason == "malformed"

    def test_exactly_16_byte_nonce_accepted(
        self, keys: KeyPair, record: Principal, cache: ReplayCache
    ) -> None:
        headers = signed_headers(keys, record, nonce=_b64url(b"\x09" * 16))
        assert verify_signed(record, envelope(headers), cache, now=_now()).did == record.did

    def test_forged_request_does_not_burn_the_nonce(
        self, keys: KeyPair, record: Principal, cache: ReplayCache
    ) -> None:
        """An attacker who sees a nonce in flight must not pre-burn it (DoS)."""
        nonce = _b64url(b"\x55" * 16)
        attacker = generate_keypair()
        forged = signed_headers(attacker, record, nonce=nonce)
        _reject(lambda: verify_signed(record, envelope(forged), cache, now=_now()))

        genuine = signed_headers(keys, record, nonce=nonce)
        assert verify_signed(record, envelope(genuine), cache, now=_now()).did == record.did

    def test_future_skewed_request_cannot_be_replayed_after_cache_prune(
        self,
        keys: KeyPair,
        record: Principal,
        cache: ReplayCache,
        frozen_clock: Callable[[datetime], None],
    ) -> None:
        """A nonce must be remembered for as long as its ts is still acceptable.

        A request stamped at the edge of the future skew is accepted at t0.
        Once the window has passed since t0 the cache may forget the nonce,
        but the ts is still inside the window measured from itself, so the
        verifier must still refuse the replay.
        """
        t0 = _now()
        frozen_clock(t0)
        ts = (t0 + timedelta(seconds=CLOCK_SKEW_SECONDS - 1)).isoformat()
        headers = signed_headers(keys, record, ts=ts)
        verify_signed(record, envelope(headers), cache, now=t0)

        later = t0 + timedelta(seconds=REPLAY_WINDOW_SECONDS + 2)
        frozen_clock(later)
        rejected = _reject(lambda: verify_signed(record, envelope(headers), cache, now=later))
        assert rejected.reason in {"replay", "stale_timestamp"}


@pytest.fixture
def frozen_clock(monkeypatch: pytest.MonkeyPatch) -> Iterator[Callable[[datetime], None]]:
    """Pin the wall clock ``ReplayCache`` reads (the clock is an external boundary)."""
    current: list[datetime] = [_now()]

    class _Clock(datetime):
        @classmethod
        def now(cls, tz: Any = None) -> datetime:  # type: ignore[override]  # fake clock keeps datetime's signature
            return current[0] if tz is None else current[0].astimezone(tz)

    monkeypatch.setattr(replay_module, "datetime", _Clock)

    def set_time(moment: datetime) -> None:
        current[0] = moment

    yield set_time


# ---------------------------------------------------------------------------
# Identity binding and impersonation
# ---------------------------------------------------------------------------


class TestIdentityBinding:
    def test_record_did_not_bound_to_its_key_refused(
        self, keys: KeyPair, cache: ReplayCache
    ) -> None:
        unrelated = generate_keypair()
        record = make_principal(keys, did=derive_app_did(unrelated.public_key, org=ORG))
        env = envelope(signed_headers(keys, record))
        assert _reject(lambda: verify_signed(record, env, cache, now=_now())).reason == (
            "did_mismatch"
        )

    def test_record_did_with_non_app_agent_type_refused(
        self, keys: KeyPair, cache: ReplayCache
    ) -> None:
        record = make_principal(
            keys, did=did_from_public_key(keys.public_key, org=ORG, agent_type="executor")
        )
        env = envelope(signed_headers(keys, record))
        assert _reject(lambda: verify_signed(record, env, cache, now=_now())).reason == (
            "did_mismatch"
        )

    def test_app_a_signing_as_app_b_refused(self, cache: ReplayCache) -> None:
        keys_a, keys_b = generate_keypair(), generate_keypair()
        record_b = make_principal(keys_b)
        headers = sign_request(
            keys_a.private_key,
            principal_did=record_b.did,
            method=METHOD,
            path=PATH,
            query=QUERY,
            body=BODY,
            on_behalf_of=PERSON_1,
        )
        rejected = _reject(lambda: verify_signed(record_b, envelope(headers), cache, now=_now()))
        assert rejected.reason == "bad_signature"

    def test_envelope_principal_differs_from_record_refused(
        self, keys: KeyPair, record: Principal, cache: ReplayCache
    ) -> None:
        """Confused deputy: a valid envelope for A checked against record B."""
        other_keys = generate_keypair()
        other_record = make_principal(other_keys)
        env = envelope(signed_headers(keys, record))
        _reject(lambda: verify_signed(other_record, env, cache, now=_now()))

    def test_unknown_record_refused(
        self, keys: KeyPair, record: Principal, cache: ReplayCache
    ) -> None:
        env = envelope(signed_headers(keys, record))
        assert _reject(lambda: verify_signed(None, env, cache, now=_now())).reason == "unknown"


# ---------------------------------------------------------------------------
# Status and expiry run before crypto and before the replay cache
# ---------------------------------------------------------------------------


_LIFECYCLE_REFUSALS = [
    pytest.param({"status": "pending", "approved_by": None}, "pending", id="pending"),
    pytest.param(
        {
            "status": "revoked",
            "revoked_by": OPERATOR,
            "revoked_at": datetime(2026, 1, 1, tzinfo=UTC),
        },
        "revoked",
        id="revoked",
    ),
    pytest.param(
        {"expires_at": datetime(2026, 1, 1, tzinfo=UTC)},
        "expired",
        id="expired",
    ),
]


class TestLifecycleBeforeCrypto:
    @pytest.mark.parametrize(("overrides", "reason"), _LIFECYCLE_REFUSALS)
    def test_lifecycle_refusal_with_valid_signature(
        self, keys: KeyPair, cache: ReplayCache, overrides: dict[str, Any], reason: str
    ) -> None:
        record = make_principal(keys, **overrides)
        env = envelope(signed_headers(keys, record))
        assert _reject(lambda: verify_signed(record, env, cache, now=_now())).reason == reason

    @pytest.mark.parametrize(("overrides", "reason"), _LIFECYCLE_REFUSALS)
    def test_lifecycle_refusal_reported_before_bad_signature(
        self, keys: KeyPair, cache: ReplayCache, overrides: dict[str, Any], reason: str
    ) -> None:
        record = make_principal(keys, **overrides)
        headers = signed_headers(generate_keypair(), record)
        env = envelope(headers)
        assert _reject(lambda: verify_signed(record, env, cache, now=_now())).reason == reason

    @pytest.mark.parametrize(("overrides", "reason"), _LIFECYCLE_REFUSALS)
    def test_lifecycle_refusal_never_touches_crypto(
        self,
        keys: KeyPair,
        cache: ReplayCache,
        overrides: dict[str, Any],
        reason: str,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        record = make_principal(keys, **overrides)
        env = envelope(signed_headers(keys, record))
        calls: list[str] = []
        _spy_verifiers(monkeypatch, calls)

        _reject(lambda: verify_signed(record, env, cache, now=_now()))

        assert calls == []

    @pytest.mark.parametrize(("overrides", "reason"), _LIFECYCLE_REFUSALS)
    def test_lifecycle_refusal_does_not_consume_nonce(
        self, keys: KeyPair, cache: ReplayCache, overrides: dict[str, Any], reason: str
    ) -> None:
        nonce = _b64url(b"\x77" * 16)
        refused = make_principal(keys, **overrides)
        _reject(
            lambda: verify_signed(
                refused, envelope(signed_headers(keys, refused, nonce=nonce)), cache, now=_now()
            )
        )
        active = make_principal(keys)
        env = envelope(signed_headers(keys, active, nonce=nonce))
        assert verify_signed(active, env, cache, now=_now()).did == active.did


def _spy_verifiers(monkeypatch: pytest.MonkeyPatch, calls: list[str]) -> None:
    """Record any Ed25519 verify or DID/key binding reached by principal_auth."""
    from nacl.signing import VerifyKey

    from arctrust import principal_auth

    real_verify = VerifyKey.verify

    def spy_verify(self: VerifyKey, *args: Any, **kwargs: Any) -> Any:
        calls.append("ed25519")
        return real_verify(self, *args, **kwargs)

    monkeypatch.setattr(VerifyKey, "verify", spy_verify)
    for name in ("verify", "did_matches_pubkey"):
        if hasattr(principal_auth, name):
            real = getattr(principal_auth, name)

            def spy(*args: Any, _real: Any = real, _name: str = name, **kwargs: Any) -> Any:
                calls.append(_name)
                return _real(*args, **kwargs)

            monkeypatch.setattr(principal_auth, name, spy)


# ---------------------------------------------------------------------------
# External code and leakage
# ---------------------------------------------------------------------------


class TestRejectionShape:
    def test_signed_refusals_share_one_external_code(
        self, keys: KeyPair, record: Principal, cache: ReplayCache
    ) -> None:
        headers = signed_headers(keys, record)
        verify_signed(record, envelope(headers), cache, now=_now())
        replayed = _reject(lambda: verify_signed(record, envelope(headers), cache, now=_now()))
        tampered = _reject(
            lambda: verify_signed(
                record, envelope(signed_headers(keys, record), method="PUT"), cache, now=_now()
            )
        )
        unknown = _reject(
            lambda: verify_signed(None, envelope(signed_headers(keys, record)), cache, now=_now())
        )
        assert replayed.code == tampered.code == unknown.code
        assert {replayed.reason, tampered.reason, unknown.reason} == {
            "replay",
            "bad_signature",
            "unknown",
        }

    def test_refusal_never_echoes_signature(
        self, keys: KeyPair, record: Principal, cache: ReplayCache
    ) -> None:
        headers = signed_headers(keys, record)
        env = envelope(headers, body=b"tampered")
        rejected = _reject(lambda: verify_signed(record, env, cache, now=_now()))
        assert headers["X-Arc-Signature"] not in str(rejected)
        assert headers["X-Arc-Signature"] not in repr(rejected)

    def test_canonical_json_is_the_signed_byte_form(self) -> None:
        """Guard: the envelope relies on canonical_json staying sort_keys + compact."""
        assert (
            canonical_json({"b": 1, "a": "x"})
            == json.dumps({"a": "x", "b": 1}, separators=(",", ":")).encode()
        )
