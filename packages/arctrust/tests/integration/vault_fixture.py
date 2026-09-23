"""Disposable, TLS-enabled Vault with file storage for provider integration tests.

Only test-generated transport material is written to a temporary Docker mount.
Vault unseal and authorization credentials stay in this fixture's memory.
"""

from __future__ import annotations

import datetime as dt
import ipaddress
import secrets
import shutil
import socket
import ssl
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

import httpx
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

IMAGE = "hashicorp/vault:1.20.4"


def _docker(*args: str, timeout: int = 30) -> str:
    executable = shutil.which("docker")
    if executable is None:
        raise RuntimeError("Docker command unavailable")
    result = subprocess.run(  # noqa: S603 - fixed Docker executable and fixture-owned arguments
        [executable, *args], capture_output=True, text=True, timeout=timeout, check=False
    )
    if result.returncode:
        raise RuntimeError(f"Docker command failed ({args[0]}, exit {result.returncode})")
    return result.stdout.strip()


def docker_available() -> bool:
    try:
        _docker("info", "--format", "{{.ServerVersion}}", timeout=5)
    except (OSError, RuntimeError, subprocess.TimeoutExpired):
        return False
    return True


def _tls_files(directory: Path) -> tuple[Path, bytes]:
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    now = dt.datetime.now(dt.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(minutes=1))
        .not_valid_after(now + dt.timedelta(hours=1))
        .add_extension(
            x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )
    private_key = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    cert_path = directory / "tls.crt"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    return cert_path, private_key


class DisposableVault:
    def __init__(self) -> None:
        self._temp = tempfile.TemporaryDirectory(prefix="arc-vault-test-")
        self.directory = Path(self._temp.name)
        self.name = "arc-vault-test-" + secrets.token_hex(8)
        self.cert_path, self._tls_key = _tls_files(self.directory)
        (self.directory / "data").mkdir()
        (self.directory / "vault.hcl").write_text(
            'disable_mlock = true\n'
            'storage "file" { path = "/vault/local/data" }\n'
            'listener "tcp" { address = "0.0.0.0:8200" '
            'tls_cert_file = "/vault/local/tls.crt" '
            'tls_key_file = "/vault/local/tls.key" }\n'
        )
        self._unseal_key: str | None = None
        self._root_token: str | None = None
        self.base_url = ""
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            self._host_port = probe.getsockname()[1]

    def __enter__(self) -> DisposableVault:
        try:
            self._write_tls_key()
            _docker(
                "run", "-d", "--name", self.name, "--user", "0",
                "-p", f"127.0.0.1:{self._host_port}:8200",
                "-v", f"{self.directory}:/vault/local:rw",
                IMAGE, "server", "-config=/vault/local/vault.hcl", timeout=120,
            )
            self._refresh_port()
            self._wait_for_health({501})
            (self.directory / "tls.key").unlink()
            with self.client() as client:
                data = self._request(client, "POST", "/v1/sys/init", {
                    "secret_shares": 1, "secret_threshold": 1,
                })
                self._unseal_key = data["keys"][0]
                self._root_token = data["root_token"]
                self._request(client, "POST", "/v1/sys/unseal", {"key": self._unseal_key})
            self._wait_for_health({200})
            self._configure()
            return self
        except BaseException:
            self.close()
            raise

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def close(self) -> None:
        try:
            _docker("rm", "-f", self.name, timeout=20)
        except (OSError, RuntimeError, subprocess.TimeoutExpired):
            pass
        self._root_token = None
        self._unseal_key = None
        self._tls_key = b""
        self._temp.cleanup()

    def _write_tls_key(self) -> None:
        path = self.directory / "tls.key"
        path.write_bytes(self._tls_key)
        path.chmod(0o600)

    def client(self, token: str | None = None) -> httpx.Client:
        headers = {"X-Vault-Token": token} if token is not None else {}
        return httpx.Client(
            base_url=self.base_url,
            verify=ssl.create_default_context(cafile=str(self.cert_path)),
            headers=headers,
        )

    @staticmethod
    def _request(client: httpx.Client, method: str, path: str, body: dict[str, Any]) -> dict[str, Any]:
        response = client.request(method, path, json=body, timeout=5)
        response.raise_for_status()
        return response.json() if response.content else {}

    def _wait_for_health(self, expected: set[int]) -> int:
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            try:
                with self.client() as client:
                    response = client.get("/v1/sys/health", timeout=1)
                if response.status_code in expected:
                    return response.status_code
            except httpx.HTTPError:
                pass
            time.sleep(0.1)
        state = _docker("inspect", self.name, "--format", "{{.State.Status}} {{.State.ExitCode}}")
        raise RuntimeError(f"disposable Vault did not reach expected health state: {state}")

    def _refresh_port(self) -> None:
        # Docker may allocate a different published port after container restart.
        port = _docker("port", self.name, "8200/tcp").rsplit(":", 1)[1]
        self.base_url = f"https://127.0.0.1:{port}"

    def _configure(self) -> None:
        assert self._root_token is not None
        with self.client(self._root_token) as root:
            self._request(root, "POST", "/v1/sys/mounts/transit", {"type": "transit"})
            self._request(root, "POST", "/v1/sys/mounts/anchor", {
                "type": "kv", "options": {"version": "2"},
            })
            self._request(root, "POST", "/v1/anchor/config", {"cas_required": True})
            self._request(root, "POST", "/v1/transit/keys/account-seal", {
                "type": "aes256-gcm96", "exportable": False,
                "allow_plaintext_backup": False,
            })
            policies = {
                "issuer": '''path "transit/keys/arc-user-*" { capabilities = ["create", "update", "read"] }
path "transit/sign/arc-user-*" { capabilities = ["update"] }''',
                "cipher": '''path "transit/keys/account-seal" { capabilities = ["read"] }
path "transit/encrypt/account-seal" { capabilities = ["update"] }
path "transit/decrypt/account-seal" { capabilities = ["update"] }''',
                "anchor": '''path "anchor/metadata/users" { capabilities = ["read"] }
path "anchor/data/users" { capabilities = ["create", "read", "update"] }
path "anchor/config" { capabilities = ["read"] }''',
            }
            for name, policy in policies.items():
                self._request(root, "PUT", f"/v1/sys/policies/acl/arc-test-{name}", {
                    "policy": policy,
                })

    def token(self, policy: str, *, ttl: str = "30m") -> str:
        assert self._root_token is not None
        with self.client(self._root_token) as root:
            data = self._request(root, "POST", "/v1/auth/token/create", {
                "policies": [f"arc-test-{policy}"], "no_default_policy": True,
                "ttl": ttl, "renewable": False,
            })
        return data["auth"]["client_token"]

    def configure_account_namespace(self, tenant: str, deployment: str) -> None:
        """Install disposable per-deployment mounts and narrow actual ACLs."""
        assert self._root_token is not None
        namespace = f"arc-{tenant}-{deployment}"
        transit, anchor = f"{namespace}-transit", f"{namespace}-anchor"
        with self.client(self._root_token) as root:
            self._request(root, "POST", f"/v1/sys/mounts/{transit}", {"type": "transit"})
            self._request(root, "POST", f"/v1/sys/mounts/{anchor}", {
                "type": "kv", "options": {"version": "2"},
            })
            self._request(root, "POST", f"/v1/{anchor}/config", {"cas_required": True})
            self._request(root, "POST", f"/v1/{transit}/keys/account-seal", {
                "type": "aes256-gcm96", "exportable": False,
                "allow_plaintext_backup": False,
            })
            self._request(root, "POST", f"/v1/{transit}/keys/audit-signing", {
                "type": "ed25519", "exportable": False,
                "allow_plaintext_backup": False,
            })
            policies = {
                "issuer": f'''path "{transit}/keys/arc-user-*" {{ capabilities = ["create", "update", "read"] }}
path "{transit}/sign/arc-user-*" {{ capabilities = ["update"] }}''',
                "cipher": f'''path "{transit}/keys/account-seal" {{ capabilities = ["read"] }}
path "{transit}/encrypt/account-seal" {{ capabilities = ["update"] }}
path "{transit}/decrypt/account-seal" {{ capabilities = ["update"] }}''',
                "anchor": f'''path "{anchor}/metadata/users" {{ capabilities = ["read"] }}
path "{anchor}/data/users" {{ capabilities = ["create", "read", "update"] }}
path "{anchor}/config" {{ capabilities = ["read"] }}''',
                "audit-signer": f'''path "{transit}/keys/audit-signing" {{ capabilities = ["read"] }}
path "{transit}/sign/audit-signing" {{ capabilities = ["update"] }}''',
                "config-anchor": f'''path "{anchor}/metadata/config" {{ capabilities = ["read"] }}
path "{anchor}/data/config" {{ capabilities = ["read"] }}
path "{anchor}/config" {{ capabilities = ["read"] }}''',
            }
            for capability, policy in policies.items():
                if capability != "config-anchor":
                    policy += '''
path "auth/token/lookup-self" { capabilities = ["read"] }
path "auth/token/renew-self" { capabilities = ["update"] }
path "auth/token/revoke-self" { capabilities = ["update"] }'''
                self._request(root, "PUT", f"/v1/sys/policies/acl/{namespace}-{capability}", {
                    "policy": policy,
                })

    def token_for_policy(self, policy_name: str, *, ttl: str = "30s") -> str:
        """Fixture-only root operation; token stays in test memory and TLS client."""
        assert self._root_token is not None
        if not policy_name.startswith("arc-acme-dgx-"):
            raise ValueError("unexpected fixture policy")
        with self.client(self._root_token) as root:
            data = self._request(root, "POST", "/v1/auth/token/create", {
                "policies": [policy_name], "no_default_policy": True,
                "ttl": ttl, "renewable": True,
            })
        return data["auth"]["client_token"]

    def seed_config_anchor(self, mount: str, digest: str) -> None:
        """Fixture-only enrollment under root custody; no root token escapes."""
        from arctrust.vault_anchor import VaultKVAnchor

        assert self._root_token is not None

        class Once:
            def consume(self, scope: str) -> bool:
                return scope == f"{mount}/config"

        with self.client(self._root_token) as root:
            VaultKVAnchor(root, mount=mount, record="config",
                          bootstrap_authority=Once()).compare_and_advance(
                None, digest, "signed enrollment"
            )

    def restart(self) -> None:
        assert self._unseal_key is not None
        self._write_tls_key()
        _docker("restart", self.name, timeout=30)
        self._refresh_port()
        state = self._wait_for_health({200, 503})
        (self.directory / "tls.key").unlink()
        if state == 503:
            with self.client() as client:
                self._request(client, "POST", "/v1/sys/unseal", {"key": self._unseal_key})
        self._wait_for_health({200})
