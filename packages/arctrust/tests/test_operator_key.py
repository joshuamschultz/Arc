"""SPEC-053 T-01/T-02 — OperatorKey custody primitive.

The operator key is the deployment audit authority — deliberately NOT an
``AgentIdentity``. It has a seed and a public key and nothing that lets any
caller mistake it for an agent identity: no ``sign``, no ``did``. This proves
the type system keeps the audited subject and the audit authority apart.
"""

from __future__ import annotations

import os
import stat
import threading
from pathlib import Path

import pytest

from arctrust.keypair import KEY_SIZE, KeyPair
from arctrust.operator import OperatorKey, OperatorKeyIntegrityError


def test_generate_yields_valid_ed25519_seed_and_pubkey() -> None:
    op = OperatorKey.generate()
    assert len(op.seed) == KEY_SIZE
    assert len(op.public_key) == KEY_SIZE
    # public_key is the Ed25519 verify key derived from the seed.
    assert KeyPair.from_seed(op.seed).public_key == op.public_key


def test_save_then_load_round_trips(tmp_path: Path) -> None:
    path = tmp_path / "operator" / "operator.key"
    op = OperatorKey.generate()
    op.save(path)
    loaded = OperatorKey.load(path)
    assert loaded.seed == op.seed
    assert loaded.public_key == op.public_key


def test_save_writes_0600_file_and_0700_dir(tmp_path: Path) -> None:
    path = tmp_path / "operator" / "operator.key"
    OperatorKey.generate().save(path)
    file_mode = stat.S_IMODE(path.stat().st_mode)
    dir_mode = stat.S_IMODE(path.parent.stat().st_mode)
    assert file_mode == 0o600, f"expected 0600, got {oct(file_mode)}"
    assert dir_mode == 0o700, f"expected 0700, got {oct(dir_mode)}"


def test_load_missing_with_generate_if_absent_bootstraps(tmp_path: Path) -> None:
    path = tmp_path / "operator" / "operator.key"
    assert not path.exists()
    op = OperatorKey.load(path, generate_if_absent=True)
    assert path.exists()
    # A second load returns the same persisted key (idempotent bootstrap).
    assert OperatorKey.load(path).seed == op.seed


def test_load_missing_without_bootstrap_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        OperatorKey.load(tmp_path / "nope.key")


def test_operator_key_has_no_identity_surface() -> None:
    op = OperatorKey.generate()
    # An operator key is a notary seed, NOT an identity. The absence of these
    # attributes is what stops any code from using it as an agent identity
    # (or an agent identity as the audit authority).
    assert not hasattr(op, "sign")
    assert not hasattr(op, "did")
    assert not hasattr(op, "signing_seed")


def test_insecure_permissions_rejected_on_load(tmp_path: Path) -> None:
    path = tmp_path / "operator" / "operator.key"
    OperatorKey.generate().save(path)
    path.chmod(0o644)  # group/other readable — tampering or misconfig
    with pytest.raises(ValueError, match="insecure permissions"):
        OperatorKey.load(path)


class _FakeVaultResolver:
    """Mirrors ``AgentIdentity._load_from_vault``'s ``resolve_secret`` shape."""

    def __init__(self, seed: bytes) -> None:
        self._seed = seed
        self.calls: list[tuple[str, str]] = []

    def resolve_secret(self, vault_path: str, identifier: str) -> str:
        self.calls.append((vault_path, identifier))
        return self._seed.hex()


def test_load_reads_from_vault_resolver_when_supplied(tmp_path: Path) -> None:
    vault_seed = OperatorKey.generate().seed
    resolver = _FakeVaultResolver(vault_seed)
    # A different key sits on disk — the vault MUST win when a resolver is given.
    path = tmp_path / "operator" / "operator.key"
    OperatorKey.generate().save(path)

    op = OperatorKey.load(path, vault_resolver=resolver, vault_path="secret/operator")
    assert op.seed == vault_seed
    assert resolver.calls == [("secret/operator", "operator")]


def test_load_falls_back_to_file_when_no_vault(tmp_path: Path) -> None:
    path = tmp_path / "operator" / "operator.key"
    on_disk = OperatorKey.generate()
    on_disk.save(path)
    op = OperatorKey.load(path, vault_resolver=None)
    assert op.seed == on_disk.seed


# ---------------------------------------------------------------------------
# SPEC-053 #3 — covert erasure via silent key regeneration
# ---------------------------------------------------------------------------


def test_missing_key_with_prior_record_fails_closed(tmp_path: Path) -> None:
    """`rm operator.key` after a key existed must NOT silently regenerate.

    The pubkey record (``.pub``) survives the delete and proves a key was once
    present; regenerating would orphan every chain it signed and hand a clean
    forward chain to an attacker (AU-9 audit repudiation).
    """
    path = tmp_path / "operator" / "operator.key"
    OperatorKey.generate().save(path)
    path.unlink()  # attacker removes only the private key file
    assert path.with_suffix(path.suffix + ".pub").exists()
    with pytest.raises(OperatorKeyIntegrityError, match="missing"):
        OperatorKey.load(path, generate_if_absent=True)


def test_missing_key_with_prior_chain_fails_closed(tmp_path: Path) -> None:
    """Even if the pubkey record is also deleted, an existing audit chain
    signals a prior key — bootstrap must fail closed, never regenerate."""
    path = tmp_path / "operator" / "operator.key"
    OperatorKey.generate().save(path)
    path.unlink()
    path.with_suffix(path.suffix + ".pub").unlink()
    with pytest.raises(OperatorKeyIntegrityError, match="missing"):
        OperatorKey.load(path, generate_if_absent=True, prior_chain_exists=True)


def test_first_ever_bootstrap_still_generates(tmp_path: Path) -> None:
    """A genuinely first-ever bootstrap (no key, no record, no chain) generates."""
    path = tmp_path / "operator" / "operator.key"
    op = OperatorKey.load(path, generate_if_absent=True)
    assert path.exists()
    assert path.with_suffix(path.suffix + ".pub").exists()
    assert OperatorKey.load(path).seed == op.seed


def test_key_swap_is_detected(tmp_path: Path) -> None:
    """A key file swapped out-of-band (bypassing save) no longer matches the
    recorded pubkey — load must reject it rather than trust the imposter."""
    path = tmp_path / "operator" / "operator.key"
    OperatorKey.generate().save(path)  # records pubkey A in .pub
    imposter = OperatorKey.generate()
    # Overwrite the private key with a different seed, leaving .pub (pubkey A).
    fd = os.open(path, os.O_WRONLY | os.O_TRUNC)
    os.write(fd, imposter.seed)
    os.close(fd)
    with pytest.raises(OperatorKeyIntegrityError, match="fingerprint"):
        OperatorKey.load(path)


# ---------------------------------------------------------------------------
# SPEC-053 #4 — bootstrap race
# ---------------------------------------------------------------------------


def test_bootstrap_is_atomic(tmp_path: Path) -> None:
    """Concurrent bootstrappers converge on ONE key, never a split chain."""
    path = tmp_path / "operator" / "operator.key"
    barrier = threading.Barrier(8)
    results: list[bytes] = []
    lock = threading.Lock()

    def _boot() -> None:
        barrier.wait()  # maximise the check-then-act race window
        op = OperatorKey.load(path, generate_if_absent=True)
        with lock:
            results.append(op.seed)

    threads = [threading.Thread(target=_boot) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(results) == 8
    assert len(set(results)) == 1, "bootstrap race produced divergent operator keys"
    assert OperatorKey.load(path).seed == results[0]


# ---------------------------------------------------------------------------
# SPEC-053 #5 — symlink / TOCTOU capture
# ---------------------------------------------------------------------------


def test_key_path_symlink_rejected(tmp_path: Path) -> None:
    """A symlinked key path (agent-controlled target) must be rejected, not read
    as the operator seed."""
    real = tmp_path / "evil_seed"
    real.write_bytes(OperatorKey.generate().seed)
    real.chmod(0o600)
    op_dir = tmp_path / "operator"
    op_dir.mkdir(mode=0o700)
    link = op_dir / "operator.key"
    link.symlink_to(real)
    with pytest.raises(OperatorKeyIntegrityError, match="symlink"):
        OperatorKey.load(link)


# ---------------------------------------------------------------------------
# SPEC-053 #6 — permission window + missing checks
# ---------------------------------------------------------------------------


def test_group_accessible_parent_dir_rejected(tmp_path: Path) -> None:
    """A group/other-accessible operator directory is a custody failure."""
    path = tmp_path / "operator" / "operator.key"
    OperatorKey.generate().save(path)
    path.parent.chmod(0o750)  # group can traverse the operator dir
    with pytest.raises(OperatorKeyIntegrityError, match="insecure permissions"):
        OperatorKey.load(path)


def test_save_creates_0600_without_world_readable_window(tmp_path: Path) -> None:
    """The key is created 0600 atomically — never written then chmod-ed."""
    path = tmp_path / "operator" / "operator.key"
    OperatorKey.generate().save(path)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    # A pre-existing key (or squatting symlink) is never clobbered by save.
    with pytest.raises(FileExistsError):
        OperatorKey.generate().save(path)
