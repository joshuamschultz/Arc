"""An operator-provided TLS certificate so the dashboard itself can serve https.

For a LAN or tailnet deployment reached directly (no reverse proxy, no
``tailscale serve`` in front). The operator pastes a certificate chain and its
private key in Settings; the dashboard serves https from the next start.

The private key never rests on disk in the clear. It is stored as an encrypted
PKCS#8 PEM under a random passphrase, and that passphrase is sealed by the
deployment's custody cipher (the operator key in process, or the Vault transit at
enterprise/federal). Starting the dashboard opens the passphrase through custody
and hands uvicorn the encrypted key with it. A key that cannot be opened stops
the start rather than serving plain http.
"""

from __future__ import annotations

import os
import secrets
import tempfile
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import arcagent
from arctrust.paths import config_file, operator_root
from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa

#: Refusal code for a certificate or key the dashboard will not serve.
TLS_INVALID = "UI_TLS_INVALID"
#: Refusal code for removing the certificate where TLS is mandatory.
TLS_REQUIRED = "UI_TLS_REQUIRED"

_SCOPE = "arcui_tls"
_SLOT = "key_passphrase"
_MAX_PEM_BYTES = 32 * 1024
_MIN_RSA_BITS = 2048


def _cert_path() -> Path:
    return config_file("ui-tls.cert.pem")


def _key_path() -> Path:
    return config_file("ui-tls.key.pem")


def _sealed_path() -> Path:
    return config_file("ui-tls.key.sealed")


@dataclass(frozen=True)
class TlsStatus:
    """What the Settings card shows. Never carries key material."""

    configured: bool
    subject: str | None = None
    not_after: str | None = None
    dns_names: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class ServingTls:
    """The uvicorn arguments for an https start. ``password`` lives only in memory."""

    certfile: Path
    keyfile: Path
    password: str = field(repr=False)


def _invalid(message: str) -> arcagent.ExtensionError:
    return arcagent.ExtensionError(code=TLS_INVALID, message=message, details={})


def _cipher() -> Any:
    root = operator_root()
    return arcagent.deployment_cipher(root, tier=arcagent.deployment_tier(root))


def _load_chain(cert_pem: str) -> list[x509.Certificate]:
    try:
        chain = x509.load_pem_x509_certificates(cert_pem.encode("ascii"))
    except (ValueError, UnicodeEncodeError) as exc:
        raise _invalid("the certificate is not a PEM certificate") from exc
    return chain


def _load_key(key_pem: str) -> rsa.RSAPrivateKey | ec.EllipticCurvePrivateKey:
    try:
        key = serialization.load_pem_private_key(key_pem.encode("ascii"), password=None)
    except TypeError as exc:
        raise _invalid("paste the private key without a password; Arc encrypts it") from exc
    except (ValueError, UnicodeEncodeError) as exc:
        raise _invalid("the private key is not a PEM private key") from exc
    if isinstance(key, rsa.RSAPrivateKey) and key.key_size >= _MIN_RSA_BITS:
        return key
    if isinstance(key, ec.EllipticCurvePrivateKey):
        return key
    raise _invalid("the private key must be RSA (2048 bits or more) or EC")


def _spki(public_key: Any) -> bytes:
    return bytes(
        public_key.public_bytes(
            serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
        )
    )


def checked_material(
    cert_pem: str, key_pem: str, *, now: datetime | None = None
) -> tuple[list[x509.Certificate], rsa.RSAPrivateKey | ec.EllipticCurvePrivateKey]:
    """Parse and check a certificate chain and its key, or raise ``UI_TLS_INVALID``.

    The leaf must be in date and must belong to the key. Messages never quote
    what was pasted.
    """
    if len(cert_pem) > _MAX_PEM_BYTES or len(key_pem) > _MAX_PEM_BYTES:
        raise _invalid("the certificate or key is too large")
    chain = _load_chain(cert_pem)
    key = _load_key(key_pem)
    leaf = chain[0]
    if _spki(leaf.public_key()) != _spki(key.public_key()):
        raise _invalid("the private key does not belong to the certificate")
    moment = now or datetime.now(UTC)
    if not leaf.not_valid_before_utc <= moment <= leaf.not_valid_after_utc:
        raise _invalid("the certificate is expired or not yet valid")
    return chain, key


def _dns_names(leaf: x509.Certificate) -> list[str]:
    try:
        extension = leaf.extensions.get_extension_for_class(x509.SubjectAlternativeName)
    except x509.ExtensionNotFound:
        return []
    return [str(name) for name in extension.value.get_values_for_type(x509.DNSName)]


def _status_of(leaf: x509.Certificate) -> TlsStatus:
    return TlsStatus(
        configured=True,
        subject=leaf.subject.rfc4514_string(),
        not_after=leaf.not_valid_after_utc.isoformat(),
        dns_names=_dns_names(leaf),
    )


def tls_status() -> TlsStatus:
    """The stored certificate's public facts, or ``configured=False``."""
    path = _cert_path()
    if not (path.is_file() and _key_path().is_file() and _sealed_path().is_file()):
        return TlsStatus(configured=False)
    return _status_of(x509.load_pem_x509_certificates(path.read_bytes())[0])


def save_tls(cert_pem: str, key_pem: str) -> TlsStatus:
    """Check, encrypt and store a certificate chain and key for the next start.

    Raises:
        ExtensionError: ``UI_TLS_INVALID`` for bad material; custody refusals as-is.
    """
    chain, key = checked_material(cert_pem, key_pem)
    passphrase = secrets.token_urlsafe(32)
    try:
        sealed = _cipher().seal(passphrase.encode("ascii"), scope=_SCOPE, slot=_SLOT)
    except (ValueError, RuntimeError) as exc:
        raise _invalid("custody could not seal the key; check the deployment's custody") from exc
    encrypted = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.BestAvailableEncryption(passphrase.encode("ascii")),
    )
    certs = b"".join(cert.public_bytes(serialization.Encoding.PEM) for cert in chain)
    _atomic_write(_key_path(), encrypted, mode=0o600)
    _atomic_write(_sealed_path(), sealed.encode("ascii"), mode=0o600)
    _atomic_write(_cert_path(), certs, mode=0o644)
    return _status_of(chain[0])


def remove_tls(*, tier: str) -> TlsStatus:
    """Forget the certificate. Refused at federal, where the dashboard must serve TLS.

    Raises:
        ExtensionError: ``UI_TLS_REQUIRED`` at the federal tier.
    """
    if tier == "federal":
        raise arcagent.ExtensionError(
            code=TLS_REQUIRED,
            message="the federal tier requires https; replace the certificate instead",
            details={},
        )
    for path in (_cert_path(), _key_path(), _sealed_path()):
        path.unlink(missing_ok=True)
    return TlsStatus(configured=False)


def serving_tls() -> ServingTls | None:
    """What uvicorn needs to serve https, or ``None`` when no certificate is set.

    Raises:
        ExtensionError: The stored key can not be opened through custody, or the
            stored files are inconsistent. The start must stop, not fall back to http.
    """
    if not tls_status().configured:
        return None
    sealed = _sealed_path().read_text(encoding="ascii").strip()
    try:
        password = _cipher().open(sealed, scope=_SCOPE, slot=_SLOT).decode("ascii")
        serialization.load_pem_private_key(
            _key_path().read_bytes(), password=password.encode("ascii")
        )
    except (ValueError, RuntimeError, OSError) as exc:
        # CredentialSealError is a ValueError; a transit that can not answer is a
        # RuntimeError (CredentialCustodyUnavailableError). Both stop the start.
        raise _invalid("the stored dashboard TLS key can not be opened") from exc
    return ServingTls(certfile=_cert_path(), keyfile=_key_path(), password=password)


def _atomic_write(path: Path, data: bytes, *, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


__all__ = [
    "TLS_INVALID",
    "TLS_REQUIRED",
    "ServingTls",
    "TlsStatus",
    "checked_material",
    "remove_tls",
    "save_tls",
    "serving_tls",
    "tls_status",
]
