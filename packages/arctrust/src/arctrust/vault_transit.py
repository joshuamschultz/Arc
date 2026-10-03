"""HashiCorp Vault Transit as the deployment's custody transit (sign + encrypt by reference).

:class:`VaultTransit` implements both custody seams against a real Vault:

* :class:`arctrust.transit_cipher.TransitCipher`: ``POST /v1/<mount>/encrypt/<key>``
  and ``/decrypt/<key>`` under an ``aes256-gcm96`` key. Vault Transit authenticates
  ``associated_data`` for its AEAD key types (verified against Vault 1.20: a
  decrypt with other associated data fails ``cipher: message authentication
  failed``), so the caller's AAD is bound directly. No derived-key ``context``
  workaround is needed and none is used (derived keys are refused).
* :class:`arctrust.signer.VaultTransit`: ``POST /v1/<mount>/sign/<key>`` and
  ``/verify/<key>`` under an ``ed25519`` or ``ecdsa-p256`` key, chosen by the
  deployment's ``signing_algorithm``. Federal tier pins ``ecdsa-p256``
  (FIPS 186-4 approved). FIPS 140 validation then rests on Vault itself: run the
  Vault Enterprise FIPS 140-3 build (or an HSM-backed seal-wrapped key); this
  client only sees signatures and ciphertext. ECDSA signatures are returned in
  the canonical low-S form Arc's verifier requires.

Nothing secret crosses this module's edges: the key stays in Vault
(non-exportable, no plaintext backup, deletion disallowed, checked before
first use), the Vault token stays in memory (:mod:`arctrust.vault_auth`), TLS is
mandatory with an operator-supplied CA bundle, and errors never carry a
response body, URL, token, plaintext or ciphertext.

Failure is fail-closed and typed: :class:`VaultTransitUnavailableError` (a
:class:`TransitUnavailableError` and a :class:`SignerError`) when Vault cannot
answer, after bounded retries and behind a circuit breaker; connector custody
maps it to ``CREDENTIAL_CUSTODY_UNAVAILABLE``. A decrypt Vault refuses is
:class:`TransitDecryptError`. Every operation emits one audit event (key name,
outcome, attempts, duration; never material).
"""

from __future__ import annotations

import base64
import binascii
import logging
import re
import ssl
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal
from urllib.parse import urlsplit

import httpx
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from arctrust.audit import AuditEvent, AuditSink, emit
from arctrust.signer import ECDSA_P256, ED25519, SignerError, verify_signature
from arctrust.transit_cipher import TransitDecryptError, TransitUnavailableError
from arctrust.vault_auth import K8S_TOKEN_SOURCE, TokenSession, read_secret_source

_logger = logging.getLogger(__name__)

_NAME = r"^[a-z0-9][a-z0-9_-]{0,127}$"
_MOUNT = r"^[a-z0-9][a-z0-9_-]{0,63}(/[a-z0-9][a-z0-9_-]{0,63}){0,3}$"
_CIPHER_TYPE = "aes256-gcm96"
_MAX_AAD = 1024
_PREFIX = "vault:v"
_P256_ORDER = 0xFFFFFFFF00000000FFFFFFFFFFFFFFFFBCE6FAADA7179E84F3B9CAC2FC632551
_RETRYABLE = frozenset({408, 429, 500, 502, 503, 504})
ACTOR_DID = "did:arc:custody:vault-transit"


class VaultTransitUnavailableError(TransitUnavailableError, SignerError):
    """Vault cannot answer (network, TLS, auth, policy, breaker open). Fail closed."""


class _RefusedError(Exception):
    """Vault answered 400: the request itself was rejected (bad ciphertext, AAD, input)."""


class VaultTransitConfig(BaseModel):
    """``[security.vault]``: where the transit is and how this process proves itself."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    addr: str = Field(description="https://vault.example:8200 (TLS is mandatory)")
    mount: str = Field(default="transit", pattern=_MOUNT)
    keys: dict[str, str] = Field(
        default_factory=lambda: {
            "operator": "arc-operator",
            "connector-credentials": "arc-connector-credentials",
        },
        description="Arc key reference -> Vault key name; any other reference is refused.",
    )
    auth_method: Literal["approle", "kubernetes", "jwt"] = "approle"
    auth_mount: str = Field(default="", pattern=r"^$|" + _MOUNT[1:])
    role_id: str = Field(default="", max_length=256)
    role: str = Field(default="", max_length=128)
    secret_source: str = Field(
        default="",
        description="credential:<name> | fd:<n> | env:<VAR> | file:/abs (never the secret itself)",
    )
    ca_bundle: str = Field(min_length=1, description="PEM CA bundle that signs Vault's cert")
    namespace: str = Field(default="", pattern=r"^$|^[A-Za-z0-9_-]+(/[A-Za-z0-9_-]+)*/?$")
    timeout_s: float = Field(default=5.0, gt=0, le=30)
    max_retries: int = Field(default=2, ge=0, le=5)
    breaker_failures: int = Field(default=5, ge=1, le=100)
    breaker_cooldown_s: float = Field(default=30.0, gt=0, le=600)

    @field_validator("addr")
    @classmethod
    def _https_only(cls, value: str) -> str:
        parts = urlsplit(value)
        if parts.scheme != "https" or not parts.hostname:
            raise ValueError("[security.vault] addr must be an https:// URL")
        if parts.username or parts.password or parts.query or parts.fragment:
            raise ValueError("[security.vault] addr must carry no credentials or query")
        if parts.path not in ("", "/"):
            raise ValueError("[security.vault] addr is the Vault origin, without a path")
        return value.rstrip("/")

    @field_validator("keys")
    @classmethod
    def _key_names(cls, value: dict[str, str]) -> dict[str, str]:
        pattern = re.compile(_NAME)
        if not value or not all(
            pattern.fullmatch(k) and pattern.fullmatch(v) for k, v in value.items()
        ):
            raise ValueError("[security.vault] keys must map key references to Vault key names")
        return value

    @model_validator(mode="after")
    def _auth_complete(self) -> VaultTransitConfig:
        if self.auth_method == "approle" and not (self.role_id and self.secret_source):
            raise ValueError("approle auth needs role_id and secret_source")
        if self.auth_method in ("kubernetes", "jwt") and not self.role:
            raise ValueError(f"{self.auth_method} auth needs role")
        if self.auth_method == "jwt" and not self.secret_source:
            raise ValueError("jwt auth needs secret_source for the JWT")
        return self

    def login_mount(self) -> str:
        return self.auth_mount or self.auth_method

    def login_secret_source(self) -> str:
        return self.secret_source or K8S_TOKEN_SOURCE


@dataclass(frozen=True)
class _SigningKey:
    name: str
    version: int
    public_key: bytes


class _Breaker:
    """Open after N consecutive unavailable calls; one probe after the cooldown."""

    def __init__(self, failures: int, cooldown_s: float, clock: Callable[[], float]) -> None:
        self._limit = failures
        self._cooldown = cooldown_s
        self._clock = clock
        self._lock = threading.Lock()
        self._failures = 0
        self._opened_at: float | None = None

    def check(self) -> None:
        with self._lock:
            if self._opened_at is None:
                return
            if self._clock() - self._opened_at < self._cooldown:
                raise VaultTransitUnavailableError("Vault Transit circuit is open")
            self._opened_at = self._clock()  # half-open: this caller is the probe

    def success(self) -> None:
        with self._lock:
            self._failures, self._opened_at = 0, None

    def failure(self) -> None:
        with self._lock:
            self._failures += 1
            if self._failures >= self._limit:
                self._opened_at = self._clock()


class _LogSink:
    """Default audit sink: the structured log, when the caller wires no chain."""

    def write(self, event: AuditEvent) -> None:
        _logger.info(
            "audit %s target=%s outcome=%s extra=%s",
            event.action,
            event.target,
            event.outcome,
            event.extra,
        )


_IN_AUDIT = threading.local()


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def _tls_context(ca_bundle: str) -> ssl.SSLContext:
    try:
        context = ssl.create_default_context(cafile=ca_bundle)
    except (OSError, ssl.SSLError):
        raise VaultTransitUnavailableError("Vault CA bundle is not readable") from None
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    return context


def _low_s(der: bytes) -> bytes:
    from cryptography.hazmat.primitives.asymmetric.utils import (
        decode_dss_signature,
        encode_dss_signature,
    )

    r, s = decode_dss_signature(der)
    if s > _P256_ORDER // 2:
        s = _P256_ORDER - s
    return encode_dss_signature(r, s)


def _ecdsa_der_public(pem: str) -> bytes:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec

    key = serialization.load_pem_public_key(pem.encode("ascii"))
    if not isinstance(key, ec.EllipticCurvePublicKey) or key.curve.name != "secp256r1":
        raise ValueError("not a P-256 public key")
    der: bytes = key.public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    return der


def _parse_signature(envelope: object) -> tuple[int, bytes]:
    if not isinstance(envelope, str) or not envelope.startswith(_PREFIX):
        raise ValueError("invalid signature envelope")
    version, _, encoded = envelope[len(_PREFIX) :].partition(":")
    if not version.isdigit():
        raise ValueError("invalid signature version")
    return int(version), base64.b64decode(encoded, validate=True)


class VaultTransit:
    """Encrypt, decrypt, sign and verify by reference in HashiCorp Vault Transit."""

    def __init__(
        self,
        config: VaultTransitConfig,
        *,
        algorithm: str = ED25519,
        audit_sink: AuditSink | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if algorithm not in (ED25519, ECDSA_P256):
            raise SignerError(f"unsupported Vault Transit signing algorithm {algorithm!r}")
        self._config = config
        self._algorithm = algorithm
        self._sink: AuditSink = audit_sink if audit_sink is not None else _LogSink()
        self._clock = clock
        self._sleep = sleep
        self._breaker = _Breaker(config.breaker_failures, config.breaker_cooldown_s, clock)
        self._signing: dict[str, _SigningKey] = {}
        self._checked_ciphers: set[str] = set()
        self._meta_lock = threading.Lock()
        self._secret = read_secret_source(config.login_secret_source())
        headers = {"X-Vault-Namespace": config.namespace} if config.namespace else {}
        self._client = httpx.Client(
            base_url=config.addr,
            verify=_tls_context(config.ca_bundle),
            timeout=httpx.Timeout(config.timeout_s),
            headers=headers,
            follow_redirects=False,
            trust_env=False,
        )
        self._session = TokenSession(
            login_path=f"/v1/auth/{config.login_mount()}/login",
            login_body=self._login_body,
            post=self._post_once,
            clock=clock,
            on_event=lambda action, outcome: self._audit(action, "auth", outcome),
        )

    # -- TransitCipher -----------------------------------------------------

    def encrypt(self, key_ref: str, plaintext: bytes, *, aad: bytes) -> str:
        """Encrypt in Vault, authenticating ``aad``; return ``vault:v<n>:...``."""
        name = self._cipher_key(key_ref, aad)
        body = {"plaintext": _b64(plaintext), "associated_data": _b64(aad)}
        data = self._op("encrypt", name, f"encrypt/{name}", body)
        ciphertext = data.get("ciphertext")
        if not isinstance(ciphertext, str) or not ciphertext.startswith(_PREFIX):
            raise VaultTransitUnavailableError("Vault Transit encrypt answer is invalid")
        return ciphertext

    def decrypt(self, key_ref: str, ciphertext: str, *, aad: bytes) -> bytes:
        """Decrypt in Vault; anything not sealed under this key with ``aad`` is refused."""
        name = self._cipher_key(key_ref, aad)
        if not ciphertext.startswith(_PREFIX):
            raise TransitDecryptError("ciphertext was not made by this transit")
        body = {"ciphertext": ciphertext, "associated_data": _b64(aad)}
        try:
            data = self._op("decrypt", name, f"decrypt/{name}", body, refusable=True)
        except _RefusedError:
            raise TransitDecryptError("ciphertext does not open under this key") from None
        try:
            return base64.b64decode(str(data["plaintext"]), validate=True)
        except (KeyError, binascii.Error, ValueError):
            raise VaultTransitUnavailableError("Vault Transit decrypt answer is invalid") from None

    # -- signer.VaultTransit -------------------------------------------------

    def public_key(self, key_ref: str) -> bytes:
        """The signing key's public half (raw Ed25519, or DER SPKI for ECDSA-P256)."""
        return self._signing_key(key_ref).public_key

    def sign(self, key_ref: str, message: bytes) -> bytes:
        """Sign in Vault and verify the answer against the pinned public key."""
        key = self._signing_key(key_ref)
        data = self._op("sign", key.name, f"sign/{key.name}", self._sign_body(message))
        try:
            version, raw = _parse_signature(data.get("signature"))
            if version != key.version:
                raise ValueError("signing key rotated since it was pinned")
            if self._algorithm == ECDSA_P256:
                raw = _low_s(raw)
            if not verify_signature(self._algorithm, message, raw, key.public_key):
                raise ValueError("signature does not verify under the pinned key")
        except (ValueError, binascii.Error) as exc:
            self._audit("custody.transit.sign", key.name, "refused")
            raise SignerError(f"Vault Transit signature refused: {exc}") from None
        return raw

    def verify(self, key_ref: str, message: bytes, signature: bytes) -> bool:
        """Ask Vault whether ``signature`` is valid for ``message`` under the key."""
        key = self._signing_key(key_ref)
        body = self._sign_body(message)
        body["signature"] = f"{_PREFIX}{key.version}:{_b64(signature)}"
        try:
            data = self._op("verify", key.name, f"verify/{key.name}", body, refusable=True)
        except _RefusedError:
            return False
        return data.get("valid") is True

    def close(self) -> None:
        """Revoke the in-memory token (best effort) and close the connection pool."""
        token = self._session.current()
        if token is not None:
            try:
                self._client.post("/v1/auth/token/revoke-self", headers={"X-Vault-Token": token})
            except httpx.HTTPError:
                pass
            self._session.invalidate()
        self._client.close()

    # -- keys ---------------------------------------------------------------

    def _vault_name(self, key_ref: str) -> str:
        name = self._config.keys.get(key_ref)
        if name is None:
            raise VaultTransitUnavailableError("key reference is not configured for this transit")
        return name

    def _cipher_key(self, key_ref: str, aad: bytes) -> str:
        if len(aad) > _MAX_AAD:
            raise VaultTransitUnavailableError("associated data exceeds limit")
        name = self._vault_name(key_ref)
        with self._meta_lock:
            if name in self._checked_ciphers:
                return name
        self._check_metadata(name, _CIPHER_TYPE)
        with self._meta_lock:
            self._checked_ciphers.add(name)
        return name

    def _signing_key(self, key_ref: str) -> _SigningKey:
        name = self._vault_name(key_ref)
        with self._meta_lock:
            cached = self._signing.get(name)
        if cached is not None:
            return cached
        data = self._check_metadata(name, self._algorithm)
        try:
            version = data["latest_version"]
            if type(version) is not int or version < 1:
                raise ValueError("invalid key version")
            encoded = data["keys"][str(version)]["public_key"]
            if self._algorithm == ED25519:
                public = base64.b64decode(encoded, validate=True)
                if len(public) != 32:
                    raise ValueError("invalid Ed25519 public key")
            else:
                public = _ecdsa_der_public(encoded)
        except (KeyError, TypeError, ValueError, AttributeError, binascii.Error):
            raise VaultTransitUnavailableError("Vault Transit key metadata is invalid") from None
        key = _SigningKey(name, version, public)
        with self._meta_lock:
            self._signing[name] = key
        return key

    def _check_metadata(self, name: str, key_type: str) -> dict[str, Any]:
        data = self._op("key_read", name, f"keys/{name}", None)
        if (
            data.get("type") != key_type
            or data.get("exportable") is not False
            or data.get("allow_plaintext_backup") is not False
            or data.get("deletion_allowed") is not False
            or data.get("derived") is not False
        ):
            self._audit("custody.transit.key_read", name, "refused")
            raise VaultTransitUnavailableError(
                f"Vault Transit key {name!r} is not a non-exportable {key_type} key"
            )
        return data

    def _sign_body(self, message: bytes) -> dict[str, str]:
        body = {"input": _b64(message)}
        if self._algorithm == ECDSA_P256:
            body |= {"hash_algorithm": "sha2-256", "marshaling_algorithm": "asn1"}
        return body

    # -- wire -----------------------------------------------------------------

    def _login_body(self) -> dict[str, str]:
        if self._config.auth_method == "approle":
            return {"role_id": self._config.role_id, "secret_id": self._secret}
        return {"role": self._config.role, "jwt": self._secret}

    def _post_once(self, path: str, body: dict[str, Any], token: str | None) -> dict[str, Any]:
        """One POST for the token session (login / renew). No retry, no breaker."""
        headers = {"X-Vault-Token": token} if token is not None else {}
        try:
            response = self._client.post(path, json=body, headers=headers)
        except httpx.HTTPError:
            raise VaultTransitUnavailableError("Vault did not answer") from None
        if response.status_code != 200:
            raise VaultTransitUnavailableError(f"Vault refused ({response.status_code})")
        return self._json(response)

    def _op(
        self,
        action: str,
        key_name: str,
        path: str,
        body: dict[str, str] | None,
        *,
        refusable: bool = False,
    ) -> dict[str, Any]:
        """One audited Transit call: breaker, token, bounded retry, typed failure."""
        started = self._clock()
        attempts = 0
        outcome = "error"
        try:
            self._breaker.check()
            data, attempts = self._attempts(path, body)
            outcome = "allow"
            return data
        except _RefusedError:
            outcome = "refused"
            if not refusable:
                raise VaultTransitUnavailableError("Vault Transit refused the request") from None
            raise
        finally:
            self._audit(
                f"custody.transit.{action}",
                key_name,
                outcome,
                attempts=attempts,
                duration_ms=int((self._clock() - started) * 1000),
            )

    def _attempts(self, path: str, body: dict[str, str] | None) -> tuple[dict[str, Any], int]:
        url = f"/v1/{self._config.mount}/{path}"
        method = "GET" if body is None else "POST"
        status = 0
        for attempt in range(1, self._config.max_retries + 2):
            status, response = self._send(method, url, body)
            if status == 200 and response is not None:
                self._breaker.success()
                return self._data(response), attempt
            if status == 400:
                self._breaker.success()  # Vault is healthy; the request was refused
                raise _RefusedError
            if status and status not in _RETRYABLE:
                break
            if attempt <= self._config.max_retries:
                self._sleep(min(0.1 * 2 ** (attempt - 1), 1.0))
        self._breaker.failure()
        raise VaultTransitUnavailableError(f"Vault Transit unavailable ({status or 'no answer'})")

    def _send(
        self, method: str, url: str, body: dict[str, str] | None
    ) -> tuple[int, httpx.Response | None]:
        """One request with a live token; a 403 re-mints the token once and resends."""
        for reauth in (False, True):
            try:
                token = self._session.token()
                response = self._client.request(
                    method, url, json=body, headers={"X-Vault-Token": token}
                )
            except (VaultTransitUnavailableError, httpx.HTTPError):
                return 0, None
            if response.status_code != 403 or reauth:
                return response.status_code, response
            self._session.invalidate()
        return 0, None  # pragma: no cover - the loop always returns

    def _data(self, response: httpx.Response) -> dict[str, Any]:
        data = self._json(response).get("data")
        if not isinstance(data, dict):
            raise VaultTransitUnavailableError("Vault answer carries no data")
        return data

    @staticmethod
    def _json(response: httpx.Response) -> dict[str, Any]:
        try:
            body = response.json()
        except ValueError:
            raise VaultTransitUnavailableError("Vault answer is not JSON") from None
        if not isinstance(body, dict):
            raise VaultTransitUnavailableError("Vault answer is not an object")
        return body

    def _audit(self, action: str, key_name: str, outcome: str, **extra: Any) -> None:
        if getattr(_IN_AUDIT, "active", False):
            return  # a sink that signs through this transit must not recurse
        _IN_AUDIT.active = True
        try:
            event = AuditEvent(
                actor_did=ACTOR_DID,
                action=action,
                target=f"vault-transit:{self._config.mount}/{key_name}",
                outcome=outcome,
                extra={"namespace": self._config.namespace, **extra},
            )
            emit(event, self._sink)
        finally:
            _IN_AUDIT.active = False


__all__ = [
    "ACTOR_DID",
    "VaultTransit",
    "VaultTransitConfig",
    "VaultTransitUnavailableError",
]
