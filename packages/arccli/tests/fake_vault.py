"""An in-memory HTTPS Vault for account-authority tests.

Real TLS (a throwaway CA the tests pin), real AppRole login, real token policies,
KV v2 with CAS, and Transit sign/encrypt. Only the storage is fake. Each policy
may touch only the paths its production HCL grants, so a capability wired to the
wrong role is refused here exactly as OpenBao would refuse it.
"""

from __future__ import annotations

import base64
import datetime
import ipaddress
import json
import re
import secrets
import ssl
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
from nacl.signing import SigningKey

ADMIN_TOKEN = "admin-token-never-printed"


_POLICY_DIR = Path(__file__).resolve().parents[3] / "deploy" / "openbao" / "policies"
_RULE = re.compile(r'path "([^"]+)"\s*\{\s*capabilities\s*=\s*\[([^\]]*)\]')


def _load_policies(namespace: str) -> dict[str, list[tuple[str, set[str]]]]:
    """The REAL policy files, so a path the code needs but the HCL lacks fails a test."""
    policies: dict[str, list[tuple[str, set[str]]]] = {}
    for file in _POLICY_DIR.glob("*.hcl"):
        rules = [
            (f"/v1/{path.replace('@NS@', namespace)}", set(re.findall(r'"(\w+)"', caps)))
            for path, caps in _RULE.findall(file.read_text())
            if not path.startswith("auth/")
        ]
        policies[f"{namespace}-{file.stem}"] = rules
    return policies


def _permits(rules: list[tuple[str, set[str]]], method: str, path: str) -> bool:
    """Vault ACL: the longest matching rule wins; ``deny`` or a missing capability refuses."""
    matching = [
        (pattern, caps)
        for pattern, caps in rules
        if (path.startswith(pattern[:-1]) if pattern.endswith("*") else path == pattern)
    ]
    if not matching:
        return False
    _, caps = max(matching, key=lambda rule: len(rule[0]))
    needed = {"read"} if method == "GET" else {"create", "update"}
    return "deny" not in caps and bool(needed & caps)


class FakeVault:
    def __init__(self, tenant: str, deployment: str, directory: Path) -> None:
        self.tenant, self.deployment = tenant, deployment
        self.transit = f"arc-{tenant}-{deployment}-transit"
        self.anchor = f"arc-{tenant}-{deployment}-anchor"
        self._policies = _load_policies(f"arc-{tenant}-{deployment}")
        self.roles: dict[str, tuple[str, str, str]] = {}  # role_id -> (secret, policy, name)
        self.tokens: dict[str, dict[str, Any]] = {}
        self.kv: dict[str, list[dict[str, Any]]] = {}
        self.keys: dict[str, dict[str, Any]] = {
            "account-seal": {"type": "aes256-gcm96", "nacl": None}
        }
        self.logins: list[str] = []
        self.policy_override: dict[str, list[str]] = {}
        self.cert_path = directory / "vault-ca.pem"
        self._write_cert(directory)
        self._lock = threading.Lock()
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(self.cert_path, directory / "vault-key.pem")
        self._server.socket = context.wrap_socket(self._server.socket, server_side=True)
        self.url = f"https://127.0.0.1:{self._server.server_address[1]}"
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    def _write_cert(self, directory: Path) -> None:
        key = ec.generate_private_key(ec.SECP256R1())
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "fake-vault")])
        now = datetime.datetime.now(datetime.UTC)
        cert = (
            x509.CertificateBuilder()
            .subject_name(name)
            .issuer_name(name)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(minutes=1))
            .not_valid_after(now + datetime.timedelta(days=1))
            .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
            .add_extension(
                x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]),
                critical=False,
            )
            .sign(key, hashes.SHA256())
        )
        self.cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
        (directory / "vault-key.pem").write_bytes(
            key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            )
        )

    def __enter__(self) -> FakeVault:
        self._thread.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self._server.shutdown()
        self._server.server_close()

    def add_role(self, capability: str, *, policy: str | None = None) -> tuple[str, str]:
        """Create an AppRole; returns (role_id, secret_id)."""
        name = f"arc-{self.tenant}-{self.deployment}-{capability}"
        role_id, secret_id = secrets.token_hex(8), secrets.token_hex(16)
        self.roles[role_id] = (secret_id, policy or name, name)
        return role_id, secret_id

    # ---------------------------------------------------------------- request logic

    def _allowed(self, token: dict[str, Any], method: str, path: str) -> bool:
        if token["admin"]:
            return True
        return _permits(self._policies.get(token["policies"][0], []), method, path)

    def handle(
        self, method: str, path: str, token: str | None, body: dict[str, Any]
    ) -> tuple[int, Any]:
        with self._lock:
            return self._route(method, path, token, body)

    def _route(
        self, method: str, path: str, token_value: str | None, body: dict[str, Any]
    ) -> tuple[int, Any]:
        if path == "/v1/auth/approle/login":
            return self._login(body)
        token = self.tokens.get(token_value or "")
        if token is None:
            return 403, {"errors": ["permission denied"]}
        if path == "/v1/auth/token/lookup-self":
            ttl = 900
            return 200, {"data": {"policies": token["policies"], "renewable": True, "ttl": ttl}}
        if path == "/v1/auth/token/renew-self":
            return 200, {"auth": {"lease_duration": 900, "renewable": True}}
        if path == "/v1/auth/token/revoke-self":
            self.tokens.pop(token_value or "", None)
            return 204, None
        if not self._allowed(token, method, path):
            return 403, {"errors": ["permission denied"]}
        if "/data/" in path or "/metadata/" in path or path.endswith(f"{self.anchor}/config"):
            return self._kv(method, path, body)
        return self._transit(method, path, body)

    def _login(self, body: dict[str, Any]) -> tuple[int, Any]:
        role = self.roles.get(body.get("role_id", ""))
        if role is None or role[0] != body.get("secret_id"):
            return 400, {"errors": ["invalid role or secret ID"]}
        secret_value = secrets.token_hex(16)
        policies = self.policy_override.get(role[2], [role[1]])
        self.tokens[secret_value] = {"policies": policies, "admin": False}
        self.logins.append(role[2])
        return 200, {
            "auth": {"client_token": secret_value, "lease_duration": 900, "renewable": True}
        }

    def _kv(self, method: str, path: str, body: dict[str, Any]) -> tuple[int, Any]:
        if path == f"/v1/{self.anchor}/config":
            return 200, {"data": {"cas_required": True}}
        record = path.rsplit("/", 1)[1]
        versions = self.kv.get(record, [])
        if "/metadata/" in path:
            if not versions:
                return 404, {"errors": []}
            return 200, {"data": {"current_version": len(versions), "cas_required": True}}
        if method == "GET":
            if not versions:
                return 404, {"errors": []}
            return 200, {
                "data": {
                    "metadata": {
                        "version": len(versions),
                        "deletion_time": "",
                        "destroyed": False,
                    },
                    "data": versions[-1],
                }
            }
        if body.get("options", {}).get("cas") != len(versions):
            return 400, {"errors": ["check-and-set parameter did not match"]}
        versions.append(body["data"])
        self.kv[record] = versions
        return 200, {"data": {"version": len(versions)}}

    def _transit(self, method: str, path: str, body: dict[str, Any]) -> tuple[int, Any]:
        match = re.fullmatch(rf"/v1/{self.transit}/(keys|sign|encrypt|decrypt)/([a-z0-9-]+)", path)
        if match is None:
            return 404, {"errors": []}
        action, ref = match.groups()
        if action == "keys" and method == "POST":
            self.keys[ref] = {"type": "ed25519", "nacl": SigningKey.generate()}
            return 204, None
        key = self.keys.get(ref)
        if key is None:
            return 404, {"errors": []}
        if action == "keys":
            return 200, {"data": self._key_meta(key)}
        if action == "sign":
            message = base64.b64decode(body["input"])
            signature = base64.b64encode(key["nacl"].sign(message).signature).decode()
            return 200, {"data": {"signature": f"vault:v1:{signature}"}}
        return self._seal(action, body)

    @staticmethod
    def _key_meta(key: dict[str, Any]) -> dict[str, Any]:
        meta: dict[str, Any] = {
            "type": key["type"],
            "exportable": False,
            "derived": False,
            "allow_plaintext_backup": False,
            "deletion_allowed": False,
            "latest_version": 1,
        }
        if key["nacl"] is not None:
            public = base64.b64encode(bytes(key["nacl"].verify_key)).decode()
            meta["keys"] = {"1": {"public_key": public}}
        return meta

    @staticmethod
    def _seal(action: str, body: dict[str, Any]) -> tuple[int, Any]:
        aad = body["associated_data"]
        if action == "encrypt":
            wrapped = base64.b64encode(json.dumps([aad, body["plaintext"]]).encode()).decode()
            return 200, {"data": {"ciphertext": f"vault:v1:{wrapped}"}}
        stored_aad, plaintext = json.loads(base64.b64decode(body["ciphertext"].split(":", 2)[2]))
        if stored_aad != aad:
            return 400, {"errors": ["cipher: message authentication failed"]}
        return 200, {"data": {"plaintext": plaintext}}

    def _handler(self) -> type[BaseHTTPRequestHandler]:
        vault = self

        class Handler(BaseHTTPRequestHandler):
            def _serve(self) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b""
                body = json.loads(raw) if raw else {}
                status, payload = vault.handle(
                    self.command, self.path, self.headers.get("X-Vault-Token"), body
                )
                data = b"" if payload is None else json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            do_GET = do_POST = _serve  # noqa: N815 - http.server dispatch names

            def log_message(self, *_args: object) -> None:
                return None

        return Handler

    def mint_admin(self) -> None:
        self.tokens[ADMIN_TOKEN] = {"policies": ["root"], "admin": True}
