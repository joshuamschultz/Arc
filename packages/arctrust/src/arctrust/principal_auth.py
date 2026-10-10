"""App principal request verification (SPEC-087 COMP-004).

Pure verification with no HTTP types: the ArcUI auth middleware hands this
module a stored :class:`~arctrust.principals.Principal` and the presented
credential, and gets back an :class:`AuthenticatedPrincipal` or a
:class:`PrincipalRejected`. The caller owns audit emission and the HTTP reply.

Two credential forms:

* **Bearer** ``arcp_<id>.<secret>`` where ``<id>`` is the principal's 32-hex
  id and ``<secret>`` is base64url (no padding) of 32 random bytes. The store
  keeps only ``sha256(secret)`` hex; comparison is constant time over digests.
* **Signed request** — the one Arc request-signing envelope (SPEC-086 COMP-004
  adopts it unchanged): Ed25519 over ``canonical_json({method, path, query,
  body_sha256, nonce, ts, on_behalf_of})`` carried in the ``X-Arc-*`` headers,
  with a 300 s replay window.

Every refusal carries a distinct internal ``reason`` (for audit) and one shared
external ``code`` (for the wire), so a caller cannot probe which principal ids
exist or which are revoked. Status and expiry run before any crypto and before
the replay cache, so a revoked key never reaches verification or burns a nonce.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import re
import secrets
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from arctrust.canonical import canonical_json
from arctrust.identity import did_matches_pubkey, parse_did
from arctrust.keypair import sign, verify
from arctrust.principals import APP_AGENT_TYPE, Principal, Scope
from arctrust.replay import CLOCK_SKEW_SECONDS, ReplayCache

__all__ = [
    "CLOCK_SKEW_SECONDS",
    "EXTERNAL_CODE",
    "HEADER_NONCE",
    "HEADER_ON_BEHALF_OF",
    "HEADER_PRINCIPAL",
    "HEADER_SIGNATURE",
    "HEADER_TIMESTAMP",
    "REPLAY_WINDOW_SECONDS",
    "AuthenticatedPrincipal",
    "PrincipalRejected",
    "SignedRequest",
    "format_bearer",
    "issue_secret",
    "parse_bearer",
    "secret_digest",
    "sign_request",
    "verify_bearer",
    "verify_signed",
]

REPLAY_WINDOW_SECONDS = 300
# The one wire code every refusal shares; the reason stays internal.
EXTERNAL_CODE = "principal_auth_failed"

HEADER_PRINCIPAL = "X-Arc-Principal"
HEADER_NONCE = "X-Arc-Nonce"
HEADER_TIMESTAMP = "X-Arc-Timestamp"
HEADER_SIGNATURE = "X-Arc-Signature"
HEADER_ON_BEHALF_OF = "X-Arc-On-Behalf-Of"

_BEARER_PREFIX = "arcp_"
_SECRET_BYTES = 32
_NONCE_BYTES = 32
_MIN_NONCE_BYTES = 16
_MAX_NONCE_CHARS = 128
_MAX_BEARER_CHARS = 128
_SIGNATURE_BYTES = 64
# 32 bytes base64url without padding is exactly 43 characters.
_BEARER_RE = re.compile(r"arcp_([0-9a-f]{32})\.([A-Za-z0-9_-]{43})")
_B64URL_RE = re.compile(r"[A-Za-z0-9_-]+")


class PrincipalRejected(Exception):  # noqa: N818 — name is the verifier's public contract (tests import it)
    """A principal credential was refused.

    ``reason`` is the internal cause (audit only); ``code`` is the same for
    every refusal and is the only thing that may reach the caller. Neither the
    message nor the repr ever carries the presented credential.
    """

    def __init__(self, reason: str) -> None:
        super().__init__(EXTERNAL_CODE)
        self.reason = reason
        self.code = EXTERNAL_CODE


@dataclass(frozen=True)
class AuthenticatedPrincipal:
    """The verified identity and grants of an app principal for one request."""

    did: str
    principal_id: str
    scopes: Mapping[Scope, tuple[str, ...]]
    subjects: tuple[str, ...]


# ---------------------------------------------------------------------------
# Bearer secrets
# ---------------------------------------------------------------------------


def secret_digest(secret: str) -> str:
    """SHA-256 hex of ``secret``: the only form the store keeps."""
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def issue_secret() -> tuple[str, str]:
    """Mint a fresh bearer secret and its stored digest."""
    secret = _b64url(secrets.token_bytes(_SECRET_BYTES))
    return secret, secret_digest(secret)


def format_bearer(principal_id: str, secret: str) -> str:
    """Wire form of a bearer token; inverse of :func:`parse_bearer`."""
    return f"{_BEARER_PREFIX}{principal_id}.{secret}"


def parse_bearer(token: str) -> tuple[str, str]:
    """Split ``arcp_<id>.<secret>`` into ``(principal_id, secret)``.

    Any other shape, including non-canonical base64url, is ``malformed``.
    """
    if len(token) > _MAX_BEARER_CHARS:
        raise PrincipalRejected("malformed")
    match = _BEARER_RE.fullmatch(token)
    if match is None:
        raise PrincipalRejected("malformed")
    principal_id, secret = match.groups()
    if _b64url(_b64url_decode(secret)) != secret:
        raise PrincipalRejected("malformed")
    return principal_id, secret


def verify_bearer(
    record: Principal | None, secret: str, *, now: datetime
) -> AuthenticatedPrincipal:
    """Verify ``secret`` against ``record``; lifecycle first, then digests."""
    principal = _check_lifecycle(record, now)
    presented = secret_digest(secret).encode("ascii")
    if not hmac.compare_digest(principal.secret_digest.encode("utf-8"), presented):
        raise PrincipalRejected("bad_secret")
    return _authenticated(principal)


# ---------------------------------------------------------------------------
# Signed requests
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SignedRequest:
    """One request's signed envelope; the body is kept only as its digest."""

    principal_did: str
    nonce: str
    ts: str
    signature: str
    method: str
    path: str
    query: str
    body_sha256: str
    on_behalf_of: str | None

    @classmethod
    def from_headers(
        cls, headers: Mapping[str, str], method: str, path: str, query: str, body: bytes
    ) -> SignedRequest:
        """Build the envelope from request parts; header names are case-insensitive."""
        lowered = {name.lower(): value for name, value in headers.items()}
        return cls(
            principal_did=_required_header(lowered, HEADER_PRINCIPAL),
            nonce=_required_header(lowered, HEADER_NONCE),
            ts=_required_header(lowered, HEADER_TIMESTAMP),
            signature=_required_header(lowered, HEADER_SIGNATURE),
            method=method,
            path=path,
            query=query,
            body_sha256=hashlib.sha256(body).hexdigest(),
            on_behalf_of=lowered.get(HEADER_ON_BEHALF_OF.lower()),
        )

    def signing_bytes(self) -> bytes:
        """The canonical bytes the signature covers."""
        return _signing_payload(
            method=self.method,
            path=self.path,
            query=self.query,
            body_sha256=self.body_sha256,
            nonce=self.nonce,
            ts=self.ts,
            on_behalf_of=self.on_behalf_of,
        )


def sign_request(
    private_key: bytes,
    *,
    principal_did: str,
    method: str,
    path: str,
    query: str,
    body: bytes,
    on_behalf_of: str | None,
    nonce: str | None = None,
    ts: str | None = None,
) -> dict[str, str]:
    """Sign a request and return its ``X-Arc-*`` headers (client helper)."""
    nonce = nonce if nonce is not None else _b64url(secrets.token_bytes(_NONCE_BYTES))
    ts = ts if ts is not None else datetime.now(UTC).isoformat()
    payload = _signing_payload(
        method=method,
        path=path,
        query=query,
        body_sha256=hashlib.sha256(body).hexdigest(),
        nonce=nonce,
        ts=ts,
        on_behalf_of=on_behalf_of,
    )
    headers = {
        HEADER_PRINCIPAL: principal_did,
        HEADER_NONCE: nonce,
        HEADER_TIMESTAMP: ts,
        HEADER_SIGNATURE: base64.b64encode(sign(payload, private_key)).decode("ascii"),
    }
    if on_behalf_of is not None:
        headers[HEADER_ON_BEHALF_OF] = on_behalf_of
    return headers


def verify_signed(
    record: Principal | None,
    envelope: SignedRequest,
    replay_cache: ReplayCache,
    *,
    now: datetime,
) -> AuthenticatedPrincipal:
    """Verify a signed request for ``record``.

    Order: lifecycle, envelope shape and freshness, DID-to-key binding,
    signature, then the replay cache — so neither a refused principal nor a
    forged request ever records (burns) a nonce.
    """
    principal = _check_lifecycle(record, now)
    if envelope.principal_did != principal.did:
        raise PrincipalRejected("principal_mismatch")
    _check_nonce(envelope.nonce)
    _check_fresh(envelope.ts, now)
    public_key = _bound_public_key(principal)
    if not verify(envelope.signing_bytes(), _decode_signature(envelope.signature), public_key):
        raise PrincipalRejected("bad_signature")
    if not replay_cache.check_and_record(envelope.nonce, envelope.ts):
        raise PrincipalRejected("replay")
    return _authenticated(principal)


# ---------------------------------------------------------------------------
# Checks
# ---------------------------------------------------------------------------


def _check_lifecycle(record: Principal | None, now: datetime) -> Principal:
    """Refuse unknown, pending, revoked or expired principals before any crypto."""
    if record is None:
        raise PrincipalRejected("unknown")
    match record.status:
        case "active":
            pass
        case "pending":
            raise PrincipalRejected("pending")
        case _:
            # "revoked", and any unexpected state fails closed the same way.
            raise PrincipalRejected("revoked")
    if record.expires_at is not None and now >= record.expires_at:
        raise PrincipalRejected("expired")
    return record


def _check_nonce(nonce: str) -> None:
    if len(nonce) > _MAX_NONCE_CHARS or _B64URL_RE.fullmatch(nonce) is None:
        raise PrincipalRejected("malformed")
    try:
        raw = _b64url_decode(nonce)
    except (binascii.Error, ValueError):
        raise PrincipalRejected("malformed") from None
    if len(raw) < _MIN_NONCE_BYTES:
        raise PrincipalRejected("malformed")


def _check_fresh(ts: str, now: datetime) -> None:
    """``ts`` must be ISO 8601 UTC, at most the window old and the skew ahead."""
    try:
        sent_at = datetime.fromisoformat(ts)
    except ValueError:
        raise PrincipalRejected("malformed") from None
    if sent_at.utcoffset() != timedelta(0):
        raise PrincipalRejected("malformed")
    age = (now - sent_at).total_seconds()
    if age > REPLAY_WINDOW_SECONDS or -age > CLOCK_SKEW_SECONDS:
        raise PrincipalRejected("stale_timestamp")


def _bound_public_key(principal: Principal) -> bytes:
    """The record's key, only if its DID is an ``app`` DID derived from it."""
    try:
        public_key = bytes.fromhex(principal.public_key)
        agent_type = parse_did(principal.did)["agent_type"]
    except ValueError:
        raise PrincipalRejected("did_mismatch") from None
    if agent_type != APP_AGENT_TYPE or not did_matches_pubkey(principal.did, public_key):
        raise PrincipalRejected("did_mismatch")
    return public_key


def _decode_signature(signature: str) -> bytes:
    try:
        raw = base64.b64decode(signature, validate=True)
    except (binascii.Error, ValueError):
        raise PrincipalRejected("malformed") from None
    if len(raw) != _SIGNATURE_BYTES:
        raise PrincipalRejected("malformed")
    return raw


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _signing_payload(
    *,
    method: str,
    path: str,
    query: str,
    body_sha256: str,
    nonce: str,
    ts: str,
    on_behalf_of: str | None,
) -> bytes:
    return canonical_json(
        {
            "method": method,
            "path": path,
            "query": query,
            "body_sha256": body_sha256,
            "nonce": nonce,
            "ts": ts,
            "on_behalf_of": on_behalf_of,
        }
    )


def _required_header(lowered: Mapping[str, str], name: str) -> str:
    value = lowered.get(name.lower())
    if not value:
        raise PrincipalRejected("malformed")
    return value


def _authenticated(principal: Principal) -> AuthenticatedPrincipal:
    return AuthenticatedPrincipal(
        did=principal.did,
        principal_id=principal.id,
        scopes=dict(principal.scopes),
        subjects=principal.subjects,
    )


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64url_decode(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
