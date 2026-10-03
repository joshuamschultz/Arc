"""A real ``vault server -dev -dev-tls`` for the Transit contract suite.

Uses the ``vault`` binary when it is on ``PATH``; otherwise the pinned
``hashicorp/vault`` image under Docker; otherwise the caller skips with the
reason :func:`dev_vault_unavailable_reason` gives. The dev server is
configured exactly as the operator runbook says: a Transit mount with
non-exportable keys, the runbook's ACL policy (parsed from
``docs/runbooks/operate/vault-transit.md``, so the doc cannot drift from what
is proven), and an AppRole whose tokens carry only that policy.
"""

from __future__ import annotations

import re
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

IMAGE = "hashicorp/vault:1.20.4"
RUNBOOK = Path(__file__).resolve().parents[3] / "docs/runbooks/operate/vault-transit.md"
KEYS = {
    "arc-operator": "ed25519",
    "arc-operator-p256": "ecdsa-p256",
    "arc-connector-credentials": "aes256-gcm96",
}


def _docker() -> str | None:
    docker = shutil.which("docker")
    if docker is None:
        return None
    probe = subprocess.run(  # noqa: S603 - fixed argv
        [docker, "image", "inspect", IMAGE], capture_output=True, check=False, timeout=15
    )
    return docker if probe.returncode == 0 else None


def dev_vault_unavailable_reason() -> str | None:
    """``None`` when a real dev Vault can start here, else why it cannot."""
    if shutil.which("vault"):
        return None
    try:
        if _docker():
            return None
    except (OSError, subprocess.SubprocessError):
        pass
    return f"no `vault` binary on PATH and no local Docker image {IMAGE}"


def runbook_policy() -> str:
    """The first ``hcl`` block of the operator runbook: the ACL the docs promise."""
    match = re.search(r"```hcl\n(.*?)```", RUNBOOK.read_text(encoding="utf-8"), re.S)
    if match is None:
        raise AssertionError("runbook has no hcl policy block")
    return match.group(1)


class DevVault:
    """A disposable real Vault (dev mode, TLS) with the runbook's AppRole setup."""

    def __init__(self) -> None:
        self._temp = tempfile.TemporaryDirectory(prefix="arc-dev-vault-")
        self.cert_dir = Path(self._temp.name)
        self.cert_dir.chmod(0o777)  # the container's vault user writes the dev CA here
        self.root = secrets.token_hex(16)
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            self.port = probe.getsockname()[1]
        self.addr = f"https://127.0.0.1:{self.port}"
        self.ca_bundle = self.cert_dir / "vault-ca.pem"
        self._process: subprocess.Popen[bytes] | None = None
        self._container = ""
        self.role_id = ""
        self.secret_id = ""

    def __enter__(self) -> DevVault:
        try:
            self._start()
            self._wait()
            self._configure()
        except BaseException:
            self.__exit__()
            raise
        return self

    def __exit__(self, *_exc: object) -> None:
        if self._process is not None:
            self._process.terminate()
            self._process.wait(timeout=10)
        if self._container:
            subprocess.run(  # noqa: S603 - fixed docker argv
                [shutil.which("docker") or "docker", "rm", "-f", self._container],
                capture_output=True,
                check=False,
                timeout=30,
            )
        self._temp.cleanup()

    def _start(self) -> None:
        dev = ["server", "-dev", "-dev-tls", f"-dev-root-token-id={self.root}"]
        binary = shutil.which("vault")
        if binary:
            self._process = subprocess.Popen(  # noqa: S603 - fixed argv
                [
                    binary,
                    *dev,
                    f"-dev-tls-cert-dir={self.cert_dir}",
                    f"-dev-listen-address=127.0.0.1:{self.port}",
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            return
        docker = _docker()
        if docker is None:
            raise RuntimeError(dev_vault_unavailable_reason() or "no dev vault")
        self._container = "arc-dev-vault-" + secrets.token_hex(6)
        subprocess.run(  # noqa: S603 - fixed docker argv
            [
                docker,
                "run",
                "-d",
                "--rm",
                "--name",
                self._container,
                "-p",
                f"127.0.0.1:{self.port}:8200",
                "-v",
                f"{self.cert_dir}:/certs",
                IMAGE,
                *dev,
                "-dev-tls-cert-dir=/certs",
                "-dev-listen-address=0.0.0.0:8200",
            ],
            capture_output=True,
            check=True,
            timeout=120,
        )

    def _wait(self) -> None:
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            if self.ca_bundle.exists():
                try:
                    with self.client() as client:
                        if client.get("/v1/sys/health").status_code == 200:
                            return
                except (httpx.HTTPError, ssl.SSLError, OSError):
                    pass
            time.sleep(0.25)
        raise RuntimeError("dev Vault did not become healthy")

    def client(self, token: str | None = None) -> httpx.Client:
        context = ssl.create_default_context(cafile=str(self.ca_bundle))
        headers = {"X-Vault-Token": token} if token else {}
        return httpx.Client(base_url=self.addr, verify=context, headers=headers, timeout=10)

    def _post(self, client: httpx.Client, path: str, body: dict[str, Any]) -> dict[str, Any]:
        response = client.post(path, json=body)
        response.raise_for_status()
        return response.json() if response.content else {}

    def _configure(self) -> None:
        policy = runbook_policy()
        policy += policy.replace("arc-operator", "arc-operator-p256")
        with self.client(self.root) as root:
            self._post(root, "/v1/sys/mounts/transit", {"type": "transit"})
            for name, key_type in KEYS.items():
                self._post(root, f"/v1/transit/keys/{name}", {"type": key_type})
            self._post(root, "/v1/sys/policies/acl/arc-custody", {"policy": policy})
            self._post(root, "/v1/sys/auth/approle", {"type": "approle"})
            self._post(
                root,
                "/v1/auth/approle/role/arc-custody",
                {
                    "token_policies": ["arc-custody"],
                    "token_no_default_policy": True,
                    "token_ttl": "60s",
                    "token_max_ttl": "1h",
                    "secret_id_ttl": "1h",
                },
            )
            role = root.get("/v1/auth/approle/role/arc-custody/role-id").json()
            self.role_id = role["data"]["role_id"]
            secret = self._post(root, "/v1/auth/approle/role/arc-custody/secret-id", {})
            self.secret_id = secret["data"]["secret_id"]
