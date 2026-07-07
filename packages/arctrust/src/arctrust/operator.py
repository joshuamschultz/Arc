"""OperatorKey — the deployment audit authority, separate from any agent DID.

Every WORM audit chain in Arc (policy, skill-improver, trace/checkpoint) is
signed with an *operator* key that no agent identity ever holds. This closes
the SPEC-034 finding where the audited subject WAS the audit authority: an
actor with the agent's DID seed could re-sign its own tamper-evident history
and ``verify_chain`` still passed. The operator key is custodied operator-side
and loaded read-only by the agent (SPEC-053 REQ-001..007).

Design invariant: an :class:`OperatorKey` is deliberately NOT an
:class:`arctrust.identity.AgentIdentity`. It exposes a ``seed`` and a
``public_key`` and nothing else — no ``sign``, no ``did`` — so the type system
prevents any code from using an agent identity as the audit authority, or the
audit authority as an agent identity. It is a notary seed, not an entity.

Custody hardening (SPEC-053 hardening pass):

- **No silent regeneration.** A missing key file when a prior operator was
  recorded (its ``.pub`` sentinel survives, or an audit chain exists) is a
  covert-erasure attempt (``rm operator.key`` + restart). :meth:`load` fails
  closed instead of minting a fresh key that would orphan every prior chain
  and hand an attacker a clean forward chain (AU-9 audit repudiation). Only a
  genuine first-ever bootstrap — no key, no ``.pub``, no chain — may generate.
- **Atomic bootstrap.** The seed is created with ``O_CREAT|O_EXCL`` so two
  racing bootstrappers (agent + CLI) converge on one key rather than a split
  chain; the loser adopts the winner's key.
- **Symlink / TOCTOU safe.** Reads and the exclusive create use ``O_NOFOLLOW``
  so an agent-planted symlink cannot redirect the seed read/write to attacker
  bytes, and the key is created ``0600`` in one syscall (no world-readable
  window). Ownership and parent-directory permissions are verified on read.

Custody note (CLAUDE.md tension — "credentials never touch the filesystem"):
the ``0600`` on-disk key is the INTERIM posture for personal/enterprise. The
:meth:`load` ``vault_resolver`` seam (SPEC-037) is the compliant path; federal
SHOULD resolve the seed from a vault/HSM so it never materialises on disk.
"""

from __future__ import annotations

import logging
import os
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from arctrust.keypair import KeyPair, generate_keypair

_logger = logging.getLogger("arctrust.operator")

# Identifier handed to the vault resolver for the operator seed (mirrors the
# ``resolve_secret(vault_path, id)`` shape used by AgentIdentity._load_from_vault).
_VAULT_OPERATOR_ID = "operator"

_FILE_MODE = 0o600
_DIR_MODE = 0o700
# Suffix of the public-key sentinel written beside the key. Its presence proves
# a key was once bootstrapped here (anti-covert-erasure); its contents pin the
# expected pubkey (anti-key-swap).
_PUB_SUFFIX = ".pub"


class OperatorKeyIntegrityError(RuntimeError):
    """The operator key is missing-after-present, symlinked, mis-owned, or swapped.

    Raised fail-closed at load time rather than regenerating or trusting the
    on-disk bytes: a fresh key would orphan every prior audit chain, and a
    redirected/swapped key would let an attacker forge the audit authority.
    """


def _pub_path(path: Path) -> Path:
    """Path of the public-key sentinel recorded beside the operator key."""
    return path.with_suffix(path.suffix + _PUB_SUFFIX)


def _reject_insecure_parent(directory: Path) -> None:
    """Fail closed if the operator directory is traversable by group/other."""
    mode = stat.S_IMODE(directory.stat().st_mode)
    if mode & (stat.S_IRWXG | stat.S_IRWXO):
        raise OperatorKeyIntegrityError(
            f"Operator key directory has insecure permissions: {oct(mode)}. "
            f"Expected 0o700 (owner only): {directory}"
        )


@dataclass(frozen=True)
class OperatorKey:
    """Ed25519 audit-signing credential for a deployment.

    Attributes:
        seed: 32-byte Ed25519 private key seed. Feeds
            ``WormSink(operator_private_key=...)``.
        public_key: 32-byte Ed25519 verify key. Feeds ``verify_chain`` /
            ``read_verified_anchor``.
    """

    seed: bytes
    public_key: bytes

    @classmethod
    def generate(cls) -> OperatorKey:
        """Generate a fresh operator keypair using a secure RNG."""
        kp = generate_keypair()
        return cls(seed=kp.private_key, public_key=kp.public_key)

    @classmethod
    def load(
        cls,
        path: Path,
        *,
        vault_resolver: Any | None = None,
        vault_path: str = "",
        generate_if_absent: bool = False,
        prior_chain_exists: bool = False,
    ) -> OperatorKey:
        """Load the operator key from a vault (preferred) or the ``0600`` file.

        Resolution order:
        1. ``vault_resolver`` supplied (+ ``vault_path``) → read the seed via
           the resolver (SPEC-037 seam); the on-disk file is ignored.
        2. Key file present → read it under ``O_NOFOLLOW`` with ownership,
           permission, and recorded-pubkey checks.
        3. Key file missing:
           - a prior operator was recorded (``.pub`` sentinel exists) OR
             ``prior_chain_exists`` → fail closed (covert erasure — never
             regenerate).
           - else ``generate_if_absent`` → atomically bootstrap a fresh key.
           - else → ``FileNotFoundError``.

        Raises:
            OperatorKeyIntegrityError: missing-after-present, symlinked,
                mis-owned, insecure directory, or swapped key.
            ValueError: on-disk key file has insecure permissions.
            FileNotFoundError: file missing, no prior record, and
                ``generate_if_absent`` is False.
        """
        if vault_resolver is not None and vault_path:
            seed = bytes.fromhex(vault_resolver.resolve_secret(vault_path, _VAULT_OPERATOR_ID))
            return cls(seed=seed, public_key=KeyPair.from_seed(seed).public_key)

        path = Path(path)
        if path.exists():
            return cls._read_existing(path)

        # Key file absent — distinguish first-ever bootstrap from covert erasure.
        if _pub_path(path).exists() or prior_chain_exists:
            _logger.error(
                "Operator key %s is missing but a prior operator was recorded — "
                "refusing to regenerate (fail-closed, AU-9). Restore from custody.",
                path,
            )
            raise OperatorKeyIntegrityError(
                f"Operator key missing but a prior operator was recorded ({path}). "
                "Refusing to regenerate — a fresh key would orphan every existing "
                "audit chain (AU-9 audit repudiation). Restore the key from "
                "custody/vault or investigate tampering."
            )
        if generate_if_absent:
            return cls._bootstrap(path)
        raise FileNotFoundError(f"Operator key not found: {path}")

    @classmethod
    def _read_existing(cls, path: Path) -> OperatorKey:
        """Read a present key file safely and verify it against its record."""
        _reject_insecure_parent(path.parent)
        seed = _read_secure_seed(path)
        op = cls(seed=seed, public_key=KeyPair.from_seed(seed).public_key)
        _verify_recorded_pubkey(op, path)
        return op

    @classmethod
    def _bootstrap(cls, path: Path) -> OperatorKey:
        """Atomically create a fresh key; adopt the winner on a bootstrap race."""
        op = cls.generate()
        try:
            op.save(path)
        except FileExistsError:
            # Lost the check-then-act race (#4): a concurrent bootstrapper (agent
            # or CLI) created the key first. Adopt it — a second key would split
            # the chain. The winner's file is now present, so load reads it.
            return cls._read_existing(path)
        return op

    def save(self, path: Path) -> None:
        """Persist the seed to ``path`` at ``0600`` via an atomic exclusive publish.

        The seed is written in full to a private ``0600`` temp file, then
        ``os.link``-ed into place: the final path appears atomically AND
        fully-formed, so a racing bootstrapper that lost never reads a
        half-written key (#4). ``link`` raises :class:`FileExistsError` if the
        path already exists — including a squatting symlink — so an existing
        operator key is never clobbered (#4/#5) and there is no world-readable
        window (the temp is ``0600`` from creation, #6). The public key is
        recorded beside it (``<name>.pub``, ``0644``) as the anti-erasure /
        anti-swap sentinel and for out-of-band verification.
        """
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.parent.chmod(_DIR_MODE)
        fd, tmp_name = tempfile.mkstemp(
            dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp"
        )
        tmp = Path(tmp_name)
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(self.seed)
            os.link(tmp, path)  # atomic exclusive publish; FileExistsError if lost
        finally:
            tmp.unlink(missing_ok=True)
        pub = _pub_path(path)
        pub.write_bytes(self.public_key)
        pub.chmod(0o644)


def _read_secure_seed(path: Path) -> bytes:
    """Open ``path`` with ``O_NOFOLLOW`` and read the seed after custody checks.

    Rejects a symlinked path (an agent-planted redirect), a non-regular file, a
    file not owned by the current user, and group/other-accessible permissions.
    """
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError as exc:  # ELOOP: the path is a symlink (O_NOFOLLOW)
        raise OperatorKeyIntegrityError(
            f"Operator key path is a symlink or not a regular file: {path}"
        ) from exc
    with os.fdopen(fd, "rb") as fh:
        st = os.fstat(fh.fileno())
        if not stat.S_ISREG(st.st_mode):
            raise OperatorKeyIntegrityError(f"Operator key is not a regular file: {path}")
        if st.st_uid != os.getuid():
            raise OperatorKeyIntegrityError(
                f"Operator key is not owned by the current user: {path}"
            )
        file_mode = stat.S_IMODE(st.st_mode)
        if file_mode & (stat.S_IRWXG | stat.S_IRWXO):
            raise ValueError(
                f"Operator key has insecure permissions: {oct(file_mode)}. "
                "Expected 0o600 (owner read/write only)."
            )
        return fh.read()


def _verify_recorded_pubkey(op: OperatorKey, path: Path) -> None:
    """Fail closed if the loaded key disagrees with its recorded ``.pub``.

    Detects an out-of-band key swap: the private key file replaced while the
    recorded public key still pins the original operator (SPEC-053 #3).
    """
    pub = _pub_path(path)
    if not pub.exists():
        return
    recorded = pub.read_bytes()
    if recorded != op.public_key:
        raise OperatorKeyIntegrityError(
            f"Operator key fingerprint does not match the recorded pubkey ({path}). "
            "The key file was swapped out-of-band — refusing to trust it."
        )


__all__ = ["OperatorKey", "OperatorKeyIntegrityError"]
