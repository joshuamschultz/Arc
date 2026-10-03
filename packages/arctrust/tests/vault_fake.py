"""A strict, real-TLS fake of the Vault endpoints :class:`arctrust.vault_transit.VaultTransit` uses.

It speaks HTTPS with a throwaway CA, does real AES-256-GCM / Ed25519 / ECDSA-P256
crypto (so AAD swaps and tampering fail for real), and refuses anything a real
Vault would: a missing or expired token, a wrong namespace, an unknown body
field, a login with the wrong role or secret. Knobs let a test inject latency,
error statuses, short TTLs and refused renewals.
"""

from __future__ import annotations

import base64
import datetime as dt
import ipaddress
import json
import secrets
import ssl
import tempfile
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.x509.oid import NameOID
from nacl.signing import SigningKey, VerifyKey


def _cert_pair(directory: Path) -> tuple[Path, Path, Path]:
    """CA + a 127.0.0.1 server cert it signed; returns (ca, cert, key) paths."""
    now = dt.datetime.now(dt.UTC)
    ca_key = ec.generate_private_key(ec.SECP256R1())
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "arc-fake-vault-ca")])
    ca = (
        x509.CertificateBuilder()
        .subject_name(ca_name)
        .issuer_name(ca_name)
        .public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(minutes=1))
        .not_valid_after(now + dt.timedelta(hours=2))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .sign(ca_key, hashes.SHA256())
    )
    key = ec.generate_private_key(ec.SECP256R1())
    cert = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "127.0.0.1")]))
        .issuer_name(ca_name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(minutes=1))
        .not_valid_after(now + dt.timedelta(hours=2))
        .add_extension(
            x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]),
            critical=False,
        )
        .sign(ca_key, hashes.SHA256())
    )
    pem = serialization.Encoding.PEM
    (directory / "ca.pem").write_bytes(ca.public_bytes(pem))
    (directory / "cert.pem").write_bytes(cert.public_bytes(pem))
    (directory / "key.pem").write_bytes(
        key.private_bytes(pem, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    )
    return directory / "ca.pem", directory / "cert.pem", directory / "key.pem"


@dataclass
class _Key:
    type: str
    material: Any
    exportable: bool = False
    derived: bool = False
    allow_plaintext_backup: bool = False
    deletion_allowed: bool = False

    def public_key(self) -> str:
        if self.type == "ed25519":
            return base64.b64encode(bytes(self.material.verify_key)).decode()
        if self.type == "ecdsa-p256":
            return (
                self.material.public_key()
                .public_bytes(
                    serialization.Encoding.PEM,
                    serialization.PublicFormat.SubjectPublicKeyInfo,
                )
                .decode()
            )
        return ""


@dataclass
class FakeVaultState:
    role_id: str = "role-123"
    secret_id: str = field(default_factory=lambda: secrets.token_hex(16))
    k8s_role: str = "arc"
    jwt: str = "header.payload.signature"
    namespace: str = ""
    ttl: int = 60
    max_ttl: int = 3600
    renewable: bool = True
    renew_fails: bool = False
    delay_s: float = 0.0
    sign_version: int = 1
    redirect_to: str = ""
    fail_statuses: list[int] = field(default_factory=list)
    tokens: dict[str, float] = field(default_factory=dict)
    token_born: dict[str, float] = field(default_factory=dict)
    calls: list[str] = field(default_factory=list)
    keys: dict[str, _Key] = field(default_factory=dict)
    lock: threading.Lock = field(default_factory=threading.Lock)


_BODIES = {
    "encrypt": {"plaintext", "associated_data"},
    "decrypt": {"ciphertext", "associated_data"},
    "sign": {"input", "hash_algorithm", "marshaling_algorithm"},
    "verify": {"input", "signature", "hash_algorithm", "marshaling_algorithm"},
}


class _Handler(BaseHTTPRequestHandler):
    server: _Server

    def log_message(self, *_args: Any) -> None:  # silence the default stderr log
        return

    def do_GET(self) -> None:
        self._dispatch("GET")

    def do_POST(self) -> None:
        self._dispatch("POST")

    def _reply(self, status: int, body: dict[str, Any]) -> None:
        raw = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _error(self, status: int, message: str) -> None:
        self._reply(status, {"errors": [message]})

    def _dispatch(self, method: str) -> None:
        state = self.server.state
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        with state.lock:
            state.calls.append(f"{method} {self.path}")
            injected = state.fail_statuses.pop(0) if state.fail_statuses else None
        if state.delay_s:
            time.sleep(state.delay_s)
        if injected is not None:
            return self._error(injected, "injected")
        if state.redirect_to:
            self.send_response(307)
            self.send_header("Location", state.redirect_to + self.path)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return None
        if state.namespace and self.headers.get("X-Vault-Namespace") != state.namespace:
            return self._error(403, "namespace not authorized")
        try:
            body = json.loads(raw) if raw else {}
        except ValueError:
            return self._error(400, "invalid json")
        if self.path.endswith("/login") and method == "POST":
            return self._login(body)
        if not self._authorized():
            return self._error(403, "permission denied")
        if self.path.startswith("/v1/auth/token/"):
            return self._token_op(self.path.rsplit("/", 1)[1])
        return self._transit(method, body)

    # -- auth ------------------------------------------------------------------

    def _login(self, body: dict[str, Any]) -> None:
        state = self.server.state
        if self.path == "/v1/auth/approle/login":
            ok = body == {"role_id": state.role_id, "secret_id": state.secret_id}
        elif self.path in ("/v1/auth/kubernetes/login", "/v1/auth/jwt/login"):
            ok = body == {"role": state.k8s_role, "jwt": state.jwt}
        else:
            return self._error(404, "no handler")
        if not ok:
            return self._error(400, "invalid credentials")
        token = "hvs." + secrets.token_hex(12)
        now = time.monotonic()
        with state.lock:
            state.tokens[token] = now + state.ttl
            state.token_born[token] = now
        self._reply(
            200,
            {
                "auth": {
                    "client_token": token,
                    "lease_duration": state.ttl,
                    "renewable": state.renewable,
                }
            },
        )

    def _authorized(self) -> bool:
        token = self.headers.get("X-Vault-Token", "")
        with self.server.state.lock:
            expiry = self.server.state.tokens.get(token)
        return expiry is not None and time.monotonic() < expiry

    def _token_op(self, op: str) -> None:
        state = self.server.state
        token = self.headers["X-Vault-Token"]
        if op == "revoke-self":
            with state.lock:
                state.tokens.pop(token, None)
            return self._reply(204, {})
        if op != "renew-self" or state.renew_fails or not state.renewable:
            return self._error(400, "lease is not renewable")
        now = time.monotonic()
        with state.lock:
            left_of_max = state.token_born[token] + state.max_ttl - now
            ttl = int(max(1, min(state.ttl, left_of_max)))
            state.tokens[token] = now + ttl
        self._reply(
            200, {"auth": {"client_token": token, "lease_duration": ttl, "renewable": True}}
        )

    # -- transit -----------------------------------------------------------------

    def _transit(self, method: str, body: dict[str, Any]) -> None:
        parts = self.path.split("/")  # ['', 'v1', mount, op, name]
        if len(parts) != 5 or parts[2] != self.server.mount:
            return self._error(404, "no handler")
        op, name = parts[3], parts[4]
        key = self.server.state.keys.get(name)
        if key is None:
            return self._error(400, "encryption key not found")
        if op == "keys" and method == "GET":
            return self._reply(200, {"data": self._metadata(name, key)})
        if method != "POST" or op not in _BODIES or not set(body) <= _BODIES[op]:
            return self._error(400, "unexpected request")
        try:
            data = getattr(self, f"_{op}")(key, body)
        except (ValueError, KeyError, TypeError):
            return self._error(400, "cipher: message authentication failed")
        if op == "sign":
            version = self.server.state.sign_version
            data["signature"] = data["signature"].replace("vault:v1:", f"vault:v{version}:")
        self._reply(200, {"data": data})

    @staticmethod
    def _metadata(name: str, key: _Key) -> dict[str, Any]:
        return {
            "name": name,
            "type": key.type,
            "exportable": key.exportable,
            "derived": key.derived,
            "allow_plaintext_backup": key.allow_plaintext_backup,
            "deletion_allowed": key.deletion_allowed,
            "latest_version": 1,
            "keys": {"1": {"public_key": key.public_key()} if key.public_key() else 1},
        }

    @staticmethod
    def _encrypt(key: _Key, body: dict[str, Any]) -> dict[str, Any]:
        if key.type != "aes256-gcm96":
            raise ValueError("not an encryption key")
        nonce = secrets.token_bytes(12)
        aad = base64.b64decode(body.get("associated_data", ""), validate=True) or None
        sealed = nonce + AESGCM(key.material).encrypt(
            nonce, base64.b64decode(body["plaintext"], validate=True), aad
        )
        return {"ciphertext": "vault:v1:" + base64.b64encode(sealed).decode()}

    @staticmethod
    def _decrypt(key: _Key, body: dict[str, Any]) -> dict[str, Any]:
        prefix, version, encoded = str(body["ciphertext"]).split(":", 2)
        if prefix != "vault" or version != "v1" or key.type != "aes256-gcm96":
            raise ValueError("invalid ciphertext")
        raw = base64.b64decode(encoded, validate=True)
        aad = base64.b64decode(body.get("associated_data", ""), validate=True) or None
        try:
            plain = AESGCM(key.material).decrypt(raw[:12], raw[12:], aad)
        except Exception as exc:  # reason: InvalidTag -> the Vault 400 answer
            raise ValueError("authentication failed") from exc
        return {"plaintext": base64.b64encode(plain).decode()}

    @staticmethod
    def _sign(key: _Key, body: dict[str, Any]) -> dict[str, Any]:
        message = base64.b64decode(body["input"], validate=True)
        if key.type == "ed25519":
            signature = key.material.sign(message).signature
        elif key.type == "ecdsa-p256":
            if body.get("hash_algorithm") != "sha2-256":
                raise ValueError("hash algorithm required")
            signature = key.material.sign(message, ec.ECDSA(hashes.SHA256()))
        else:
            raise ValueError("not a signing key")
        return {"signature": "vault:v1:" + base64.b64encode(signature).decode(), "key_version": 1}

    @staticmethod
    def _verify(key: _Key, body: dict[str, Any]) -> dict[str, Any]:
        message = base64.b64decode(body["input"], validate=True)
        signature = base64.b64decode(str(body["signature"]).split(":", 2)[2], validate=True)
        try:
            if key.type == "ed25519":
                VerifyKey(bytes(key.material.verify_key)).verify(message, signature)
            else:
                key.material.public_key().verify(signature, message, ec.ECDSA(hashes.SHA256()))
        except Exception:  # reason: any verification failure is valid=false
            return {"valid": False}
        return {"valid": True}


class _Server(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, state: FakeVaultState, mount: str) -> None:
        super().__init__(("127.0.0.1", 0), _Handler)
        self.state = state
        self.mount = mount


class FakeVault:
    """Context manager: a strict fake Vault on https://127.0.0.1:<port>."""

    def __init__(self, *, mount: str = "transit", namespace: str = "") -> None:
        self._temp = tempfile.TemporaryDirectory(prefix="arc-fake-vault-")
        self.ca_bundle, cert, key = _cert_pair(Path(self._temp.name))
        self.state = FakeVaultState(namespace=namespace)
        self.mount = mount
        self._server = _Server(self.state, mount)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(cert, key)
        self._server.socket = context.wrap_socket(self._server.socket, server_side=True)
        self.addr = f"https://127.0.0.1:{self._server.server_address[1]}"
        self.add_key("arc-connector-credentials", "aes256-gcm96")
        self.add_key("arc-operator", "ed25519")
        self.add_key("arc-operator-p256", "ecdsa-p256")

    def add_key(self, name: str, key_type: str, **flags: bool) -> None:
        if key_type == "aes256-gcm96":
            material: Any = secrets.token_bytes(32)
        elif key_type == "ed25519":
            material = SigningKey.generate()
        else:
            material = ec.generate_private_key(ec.SECP256R1())
        self.state.keys[name] = _Key(key_type, material, **flags)

    def calls(self, needle: str) -> int:
        with self.state.lock:
            return sum(needle in call for call in self.state.calls)

    def __enter__(self) -> FakeVault:
        threading.Thread(
            target=self._server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True
        ).start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._temp.cleanup()
