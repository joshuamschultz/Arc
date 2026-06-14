"""``arc identity`` — manage the standalone signing authority for direct CLI use.

When you drive ``arcrun`` or ``arcllm`` directly from the terminal (not through
an ``arc agent``), there is no per-agent identity to attribute and sign the run.
This command creates ONE Ed25519 keypair + DID up front and stores it under
``~/.arc/identity/``. Direct ``arcrun``/``arcllm`` invocations load it so every
action is attributable and audited — the same Identity + Audit pillars an agent
gets, for ad-hoc terminal work.

Subcommands:
    arc identity init   [--org ORG] [--type TYPE] [--key-dir DIR] [--force]
    arc identity show   [--key-dir DIR]
"""

from __future__ import annotations

import sys
from pathlib import Path

from arctrust import AgentIdentity

# Standalone signing authority lives here (distinct from per-agent key_dirs).
DEFAULT_KEY_DIR = Path("~/.arc/identity").expanduser()
_ACTIVE_FILE = "active.did"


def _out(msg: str = "") -> None:
    sys.stdout.write(msg + "\n")


def _err(msg: str) -> None:
    sys.stderr.write(msg + "\n")


def _active_did_path(key_dir: Path) -> Path:
    return key_dir / _ACTIVE_FILE


def load_signing_authority(key_dir: Path = DEFAULT_KEY_DIR) -> AgentIdentity | None:
    """Load the stored signing authority, or None if none has been created.

    This is the function ``arcrun``/``arcllm`` direct-CLI entry points call to
    obtain an identity to sign/attribute a terminal run.
    """
    key_dir = Path(key_dir).expanduser()
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
    key_dir = Path(_parse_opt(args, "--key-dir", str(DEFAULT_KEY_DIR))).expanduser()
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

    _out(f"Created signing authority: {identity.did}")
    _out(f"  key dir: {key_dir}  (private key is 0600)")
    _out("Direct `arcrun` / `arcllm` terminal runs will sign and attribute to this DID.")


def _show(args: list[str]) -> None:
    key_dir = Path(_parse_opt(args, "--key-dir", str(DEFAULT_KEY_DIR))).expanduser()
    identity = load_signing_authority(key_dir)
    if identity is None:
        _out("No signing authority yet. Run: arc identity init")
        return
    _out(f"Signing authority: {identity.did}")
    _out(f"  key dir: {key_dir}")


def identity_handler(args: list[str]) -> None:
    """Dispatch ``arc identity <subcommand>``."""
    if not args or args[0] in ("-h", "--help", "help"):
        _out("Usage: arc identity <init|show> [options]")
        _out("  init   Create + store the signing authority (one time).")
        _out("  show   Show the current signing authority DID.")
        return
    sub, rest = args[0], args[1:]
    if sub == "init":
        _init(rest)
    elif sub == "show":
        _show(rest)
    else:
        _err(f"arc identity: unknown subcommand {sub!r}. Use init or show.")
        sys.exit(1)
