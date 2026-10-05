"""`arc accounts enroll` — pin the account authority to this operator and deployment.

Enrollment is the one step that needs a Vault admin token (read from stdin, never
an argument, never printed). It signs the deployment authority config and three
capability grants with the operator key, anchors the config revision in Vault KV
with compare-and-swap, writes the two signed files (0600) and prints the
``[security.accounts]`` block. Every step is audited durably; a failed audit append
stops the command.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import ssl
import sys
import time
from pathlib import Path
from typing import NoReturn

import httpx
from arctrust import AuditEvent
from arctrust.authority_config import DeploymentAuthorityConfig, sign_authority_config
from arctrust.monotonic import AnchorUnavailableError
from arctrust.paths import config_file
from arctrust.signer import ED25519, Signer
from arctrust.vault_anchor import VaultKVAnchor
from arctrust.vault_lease import Capability, CapabilityGrant, sign_capability_grant

from arccli.commands._accounts import AccountsConfigError, bootstrap_marker_path
from arccli.commands.operator import operator_signer_and_did, operator_worm_sink

_CAPABILITIES = (Capability.ISSUER, Capability.CIPHER, Capability.ANCHOR)
_MAX_TOKEN_BYTES = 4096
_DAY = 86_400


class _EnrollBootstrap:
    """The operator running enrollment is the single-use authority for the config anchor."""

    def __init__(self, scope: str) -> None:
        self._scope = scope
        self._used = False

    def consume(self, scope: str) -> bool:
        if self._used or scope != self._scope:
            return False
        self._used = True
        return True


class _Audit:
    """Durable, actor-stamped audit for each enrollment step."""

    def __init__(self, sink: object, actor_did: str) -> None:
        self._sink = sink
        self._actor = actor_did

    def step(self, action: str, target: str, outcome: str = "allow") -> None:
        append = getattr(self._sink, "write_durable", None)
        if append is None:
            raise AccountsConfigError("audit sink cannot append durably")
        append(
            AuditEvent(
                actor_did=self._actor,
                action=f"accounts.enroll.{action}",
                target=target,
                outcome=outcome,
            )
        )


def _fail(message: str) -> NoReturn:
    sys.stderr.write(f"Error: {message}\n")
    raise SystemExit(1)


def _write_private(path: Path, data: bytes) -> None:
    """Atomically replace *path* with a 0600 file inside a 0700 directory."""
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        os.write(fd, data)
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(temp, path)


def _operator_ed25519() -> tuple[str, Signer]:
    did, signer = operator_signer_and_did()
    if signer.algorithm != ED25519 or len(signer.public_key) != 32:
        _fail(
            "account authority signing needs an Ed25519 operator key; "
            f"this deployment's operator key is {signer.algorithm}"
        )
    return did, signer


def _read_admin_token() -> str:
    token = sys.stdin.read(_MAX_TOKEN_BYTES + 1).strip()
    if not token or len(token) > _MAX_TOKEN_BYTES:
        _fail("no usable Vault admin token on stdin (use --admin-token-stdin)")
    return token


def _users_anchor_missing(admin: httpx.Client, anchor_mount: str) -> bool:
    try:
        status = admin.get(
            f"/v1/{anchor_mount}/metadata/users", timeout=5.0, follow_redirects=False
        ).status_code
    except httpx.HTTPError:
        _fail("Vault is unreachable while checking the users anchor")
    if status not in {200, 404}:
        _fail(f"Vault refused the users anchor check (HTTP {status})")
    return status == 404


def _toml_block(cfg: DeploymentAuthorityConfig, config_path: Path, grants_path: Path) -> str:
    lines = [
        "[security.accounts]",
        f'vault_url = "{cfg.vault_url}"',
        'ca_file = "<same --ca-file you enrolled with>"',
        f'deployment_id = "{cfg.deployment_id}"',
        f'tenant_id = "{cfg.tenant_id}"',
        f'authority_config = "{config_path}"',
        f'grants_file = "{grants_path}"',
        "# AppRole role_id of each role; the secret_id stays in the named credential.",
        f'config_reader_role_id = "<role_id of {cfg.namespace}-config-reader>"',
        'config_reader_secret = "credential:arc-accounts-config-reader"',
    ]
    for capability in _CAPABILITIES:
        lines.append(
            f'{capability.value}_role_id = "<role_id of {cfg.namespace}-{capability.value}>"'
        )
        lines.append(f'{capability.value}_secret = "credential:arc-accounts-{capability.value}"')
    return "\n".join(lines)


def _enroll(args: argparse.Namespace) -> None:
    from arcstore import resolve_data_dir

    if not args.admin_token_stdin:
        _fail("pass --admin-token-stdin and send the Vault admin token on stdin")
    ca_path = Path(args.ca_file).expanduser()
    try:
        ca_pem = ca_path.read_bytes()
        context = ssl.create_default_context(cadata=ca_pem.decode("ascii"))
    except (OSError, UnicodeError, ssl.SSLError):
        _fail(f"cannot use CA file {ca_path}")
    token = _read_admin_token()
    actor_did, signer = _operator_ed25519()
    config_path = Path(args.authority_config).expanduser()
    grants_path = Path(args.grants_file).expanduser()
    scope = f"arc-{args.tenant_id}-{args.deployment_id}-anchor/config"

    sink = operator_worm_sink(None, resolve_data_dir(None))
    audit = _Audit(sink, actor_did)
    admin = httpx.Client(
        base_url=args.vault_url.rstrip("/"), verify=context, headers={"X-Vault-Token": token}
    )
    try:
        audit.step("start", scope, "attempt")
        _run(args, signer, ca_pem, admin, audit, config_path, grants_path, scope)
    except (AnchorUnavailableError, AccountsConfigError, ValueError) as exc:
        audit.step("failed", scope, "error")
        _fail(f"{type(exc).__name__}: {exc}")
    finally:
        admin.close()
        sink.close()


def _run(
    args: argparse.Namespace,
    signer: Signer,
    ca_pem: bytes,
    admin: httpx.Client,
    audit: _Audit,
    config_path: Path,
    grants_path: Path,
    scope: str,
) -> None:
    probe = DeploymentAuthorityConfig(
        deployment_id=args.deployment_id,
        tenant_id=args.tenant_id,
        revision=1,
        vault_url=args.vault_url.rstrip("/"),
        vault_ca_sha256=hashlib.sha256(ca_pem).hexdigest(),
    )
    anchor = VaultKVAnchor(
        admin,
        mount=probe.anchor_mount,
        record="config",
        bootstrap_authority=_EnrollBootstrap(scope),
    )
    head = anchor.latest()
    if head is not None and not args.rotate:
        _fail("this deployment is already enrolled; pass --rotate to issue the next revision")
    if head is None and args.rotate:
        _fail("nothing to rotate: this deployment is not enrolled yet")
    config = probe.model_copy(update={"revision": (head.version if head else 0) + 1})

    anchor.compare_and_advance(head, config.digest, f"authority-config revision {config.revision}")
    audit.step("config_anchor", f"{scope}@{config.revision}")

    sign = signer.sign
    _write_private(config_path, json.dumps(sign_authority_config(config, sign)).encode())
    audit.step("config_file", str(config_path))

    expires_at = int(time.time()) + args.grant_days * _DAY
    grants = {
        capability.value: sign_capability_grant(
            CapabilityGrant(
                deployment_id=config.deployment_id,
                tenant_id=config.tenant_id,
                config_revision=config.revision,
                config_digest=config.digest,
                capability=capability,
                subject=f"arc-{config.tenant_id}-{config.deployment_id}-{capability.value}",
                expires_at=expires_at,
            ),
            sign,
        )
        for capability in _CAPABILITIES
    }
    _write_private(grants_path, json.dumps(grants).encode())
    audit.step("grants_file", str(grants_path))

    if _users_anchor_missing(admin, config.anchor_mount):
        _write_private(bootstrap_marker_path(grants_path), b"1\n")
        audit.step("users_bootstrap", f"{config.anchor_mount}/users")

    sys.stdout.write(
        f"Enrolled {config.namespace} at revision {config.revision}.\n"
        f"Grants expire in {args.grant_days} days; re-run with --rotate before then.\n\n"
        "Add to ~/arc/config/arcagent.toml:\n\n"
        f"{_toml_block(config, config_path, grants_path)}\n"
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="arc accounts", description="The Vault/OpenBao account authority."
    )
    inner = parser.add_subparsers(dest="subcmd")
    enroll = inner.add_parser("enroll", help="Sign and anchor this deployment's authority config.")
    enroll.add_argument("--vault-url", required=True)
    enroll.add_argument("--ca-file", required=True)
    enroll.add_argument("--deployment-id", required=True)
    enroll.add_argument("--tenant-id", required=True)
    enroll.add_argument("--admin-token-stdin", action="store_true")
    enroll.add_argument("--rotate", action="store_true", help="Issue the next config revision.")
    enroll.add_argument("--grant-days", type=int, default=365)
    enroll.add_argument("--authority-config", default=None)
    enroll.add_argument("--grants-file", default=None)
    enroll.set_defaults(func=_enroll)
    return parser


def accounts_handler(args: list[str]) -> None:
    """Top-level handler for `arc accounts <sub> [args]`."""
    parser = _build_parser()
    parsed = parser.parse_args(args)
    if getattr(parsed, "func", None) is None:
        parser.print_help()
        sys.exit(0)
    if not 1 <= parsed.grant_days <= 3650:
        _fail("--grant-days must be between 1 and 3650")
    parsed.authority_config = parsed.authority_config or str(
        config_file("accounts-authority.json")
    )
    parsed.grants_file = parsed.grants_file or str(config_file("accounts-grants.json"))
    parsed.func(parsed)


__all__ = ["accounts_handler"]
