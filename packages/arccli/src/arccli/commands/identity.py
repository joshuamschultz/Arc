"""``arc identity`` — manage the standalone signing authority for direct CLI use.

When you drive ``arcrun`` or ``arcllm`` directly from the terminal (not through
an ``arc agent``), there is no per-agent identity to attribute and sign the run.
This command creates ONE Ed25519 keypair + DID up front and stores it under
``~/.arc/identity/``. Direct ``arcrun``/``arcllm`` invocations load it so every
action is attributable and audited — the same Identity + Audit pillars an agent
gets, for ad-hoc terminal work.

The key dir defaults to ``${ARC_CONFIG_DIR:-~/.arc}/identity``; ``--dir`` sets the
Arc config base (key lands under ``<dir>/identity``), and ``--key-dir`` pins the
exact directory.

Subcommands:
    arc identity init   [--org ORG] [--type TYPE] [--dir DIR] [--key-dir DIR] [--force]
    arc identity show   [--dir DIR] [--key-dir DIR]
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from arctrust import AgentIdentity

from arccli.commands._shared import err as _err
from arccli.commands._shared import write as _out

# Standalone signing authority lives here (distinct from per-agent key_dirs).
DEFAULT_KEY_DIR = Path("~/.arc/identity").expanduser()
_ACTIVE_FILE = "active.did"


def _default_key_dir() -> Path:
    """Resolve the signing-authority dir: ``${ARC_CONFIG_DIR:-~/.arc}/identity``.

    Mirrors ``arc init``'s config-dir resolution so an isolated ``ARC_CONFIG_DIR``
    keeps the key alongside the rest of the Arc config instead of leaking to
    ``~/.arc``.
    """
    env = os.environ.get("ARC_CONFIG_DIR")
    base = Path(env).expanduser() if env else Path.home() / ".arc"
    return base / "identity"


def _resolve_key_dir(args: list[str]) -> Path:
    """Pick the key dir from flags/env: ``--key-dir`` > ``--dir`` > ``ARC_CONFIG_DIR``."""
    explicit = _parse_opt(args, "--key-dir", "")
    if explicit:
        return Path(explicit).expanduser()
    base = _parse_opt(args, "--dir", "")
    if base:
        return Path(base).expanduser() / "identity"
    return _default_key_dir()


def _resolve_trust_dir(args: list[str], key_dir: Path) -> Path | None:
    """Trust dir for operator self-registration.

    Priority: ``--dir`` (sibling ``trust`` under the same config base) >
    ``--key-dir`` (nested ``trust`` subdir — keeps a custom key location
    self-contained rather than silently touching the real
    ``${ARC_CONFIG_DIR:-~/.arc}/trust``) > None, which lets
    ``arctrust.trust_store`` resolve its own default so identity and
    trust-store resolution stay in sync without duplicating that logic here.
    """
    base = _parse_opt(args, "--dir", "")
    if base:
        return Path(base).expanduser() / "trust"
    if "--key-dir" in args:
        return key_dir / "trust"
    return None


def _active_did_path(key_dir: Path) -> Path:
    return key_dir / _ACTIVE_FILE


def load_signing_authority(key_dir: Path | None = None) -> AgentIdentity | None:
    """Load the stored signing authority, or None if none has been created.

    This is the function ``arcrun``/``arcllm`` direct-CLI entry points call to
    obtain an identity to sign/attribute a terminal run.
    """
    key_dir = _default_key_dir() if key_dir is None else Path(key_dir).expanduser()
    active = _active_did_path(key_dir)
    if not active.exists():
        return None
    did = active.read_text(encoding="utf-8").strip()
    if not did:
        return None
    return AgentIdentity.load_keys(did, key_dir)


def _parse_opt(args: list[str], name: str, default: str) -> str:
    """Read ``--name value`` from args, returning default if absent."""
    if name in args:
        i = args.index(name)
        if i + 1 < len(args):
            return args[i + 1]
    return default


def _init(args: list[str]) -> None:
    key_dir = _resolve_key_dir(args)
    org = _parse_opt(args, "--org", "default")
    agent_type = _parse_opt(args, "--type", "operator")
    force = "--force" in args

    existing = load_signing_authority(key_dir)
    if existing is not None and not force:
        _err(
            f"A signing authority already exists: {existing.did}\n"
            f"  key dir: {key_dir}\n"
            "Use --force to replace it (this revokes the old DID's attribution)."
        )
        sys.exit(1)

    identity = AgentIdentity.generate(org=org, agent_type=agent_type)
    identity.save_keys(key_dir)
    _active_did_path(key_dir).write_text(identity.did, encoding="utf-8")

    # Register as a trusted pairing-approval operator (personal-tier trust
    # anchor: "self-signed key accepted" per arcgateway.pairing_signature).
    # Without this, `arc gateway pair approve` can never produce a signature
    # PairingStore.verify_and_consume() will accept — every tier requires a
    # verifiable Ed25519 signature, and this is the only DID→pubkey record
    # that makes one.
    from arctrust import register_operator

    trust_dir = _resolve_trust_dir(args, key_dir)
    register_operator(identity.did, identity.public_key, trust_dir=trust_dir)

    _out(f"Created signing authority: {identity.did}")
    _out(f"  key dir: {key_dir}  (private key is 0600)")
    _out("Direct `arcrun` / `arcllm` terminal runs will sign and attribute to this DID.")
    _out("Registered as a trusted operator for `arc gateway pair approve`.")


def _show(args: list[str]) -> None:
    key_dir = _resolve_key_dir(args)
    identity = load_signing_authority(key_dir)
    if identity is None:
        _out("No signing authority yet. Run: arc identity init")
        return
    _out(f"Signing authority: {identity.did}")
    _out(f"  key dir: {key_dir}")


def identity_handler(args: list[str]) -> None:
    """Dispatch ``arc identity <subcommand>``."""
    if not args or args[0] in ("-h", "--help", "help"):
        _out("Usage: arc identity <init|show> [--dir DIR] [--key-dir DIR]")
        _out("  init   Create + store the signing authority (one time).")
        _out("  show   Show the current signing authority DID.")
        _out("  Key dir defaults to ${ARC_CONFIG_DIR:-~/.arc}/identity.")
        return
    sub, rest = args[0], args[1:]
    if sub == "init":
        _init(rest)
    elif sub == "show":
        _show(rest)
    else:
        _err(f"arc identity: unknown subcommand {sub!r}. Use init or show.")
        sys.exit(1)
