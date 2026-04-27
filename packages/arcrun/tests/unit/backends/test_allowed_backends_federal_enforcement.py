"""End-to-end federal enforcement of signed allowed_backends manifests.

Covers the contract of ``arcrun.backends.loader.load_backend`` at federal tier
with a signed manifest:

- Backend NOT in manifest → BackendSignatureError, refused before import.
- Backend IN manifest AND content_hash matches → loads.
- Backend IN manifest but content_hash mismatches → BackendSignatureError.
- Manifest signature verification failure → BackendSignatureError.
- Built-in ``local`` / ``docker`` still bypasses manifest (always trusted).
"""

from __future__ import annotations

import base64
import hashlib
import json
from collections.abc import Iterator, Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest
from nacl.signing import SigningKey

from arcrun.backends import LocalBackend
from arcrun.backends.loader import (
    BackendSignatureError,
    load_backend,
)

# ---------------------------------------------------------------------------
# Helpers (mirror sig-verify suite)
# ---------------------------------------------------------------------------


def _canonical(meta: Mapping[str, Any], backends: Sequence[Mapping[str, Any]]) -> bytes:
    return json.dumps(
        {"meta": meta, "backends": backends},
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _write_issuers(trust_dir: Path, did: str, pubkey: bytes) -> None:
    f = trust_dir / "issuers.toml"
    f.write_text(
        f'[issuers."{did}"]\npublic_key = "{base64.b64encode(pubkey).decode()}"\n',
        encoding="utf-8",
    )
    f.chmod(0o600)


def _emit_manifest(
    path: Path, *, meta: Mapping[str, Any], backends: Sequence[Mapping[str, Any]], signature_b64: str
) -> None:
    lines = [
        "[meta]",
        f'issued_at = "{meta["issued_at"]}"',
        f'issuer_did = "{meta["issuer_did"]}"',
        "",
    ]
    for b in backends:
        lines.append("[[backends]]")
        lines.append(f'name = "{b["name"]}"')
        lines.append(f'module = "{b["module"]}"')
        lines.append(f'content_hash = "{b["content_hash"]}"')
        lines.append("")
    lines.append("[signature]")
    lines.append('algorithm = "ed25519"')
    lines.append(f'signature = "{signature_b64}"')
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _flush_cache() -> Iterator[None]:
    from arctrust import invalidate_cache

    invalidate_cache()
    yield
    invalidate_cache()


@pytest.fixture
def trust_dir(tmp_path: Path) -> Path:
    d = tmp_path / "trust"
    d.mkdir()
    return d


@pytest.fixture
def issuer_key() -> SigningKey:
    return SigningKey.generate()


@pytest.fixture
def issuer_did() -> str:
    return "did:arc:org:trust-authority/federalroot"


@pytest.fixture
def trusted_trust_dir(
    trust_dir: Path, issuer_did: str, issuer_key: SigningKey
) -> Path:
    _write_issuers(trust_dir, issuer_did, bytes(issuer_key.verify_key))
    return trust_dir


@pytest.fixture
def local_content_hash() -> str:
    """sha256 of arcrun.backends.local module file."""
    import arcrun.backends.local as local_mod

    raw = Path(local_mod.__file__).read_bytes()
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _build_signed_manifest(
    tmp_path: Path,
    issuer_did: str,
    issuer_key: SigningKey,
    backends: Sequence[Mapping[str, Any]],
) -> Path:
    meta = {"issued_at": "2026-04-18T00:00:00Z", "issuer_did": issuer_did}
    sig_b64 = base64.b64encode(
        issuer_key.sign(_canonical(meta, backends)).signature
    ).decode()
    manifest = tmp_path / "allowed_backends.toml"
    _emit_manifest(manifest, meta=meta, backends=backends, signature_b64=sig_b64)
    return manifest


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_builtin_local_bypasses_manifest(trusted_trust_dir: Path) -> None:
    """local + docker are always trusted; no manifest needed at any tier."""
    b = load_backend("local", tier="federal", trust_dir=trusted_trust_dir)
    assert isinstance(b, LocalBackend)


def test_backend_in_manifest_with_matching_hash_loads(
    tmp_path: Path,
    trusted_trust_dir: Path,
    issuer_did: str,
    issuer_key: SigningKey,
    local_content_hash: str,
) -> None:
    """Manifest lists backend with the real content_hash → loads successfully."""
    backends = [
        {
            "name": "shadowed_local",
            "module": "arcrun.backends.local:LocalBackend",
            "content_hash": local_content_hash,
        }
    ]
    manifest = _build_signed_manifest(
        tmp_path, issuer_did, issuer_key, backends
    )

    b = load_backend(
        "arcrun.backends.local:LocalBackend",
        tier="federal",
        manifest_path=manifest,
        trust_dir=trusted_trust_dir,
    )
    assert isinstance(b, LocalBackend)


def test_backend_not_in_manifest_refused(
    tmp_path: Path,
    trusted_trust_dir: Path,
    issuer_did: str,
    issuer_key: SigningKey,
    local_content_hash: str,
) -> None:
    """Manifest signed + valid, but target backend not listed → refused."""
    backends = [
        {
            "name": "other",
            "module": "arcrun.backends.local:LocalBackend",
            "content_hash": local_content_hash,
        }
    ]
    manifest = _build_signed_manifest(
        tmp_path, issuer_did, issuer_key, backends
    )

    # Request a different dotted path that is NOT in the manifest
    with pytest.raises(BackendSignatureError, match="not in the signed"):
        load_backend(
            "other.pkg:Backend",
            tier="federal",
            manifest_path=manifest,
            trust_dir=trusted_trust_dir,
        )


def test_backend_content_hash_mismatch_refused(
    tmp_path: Path,
    trusted_trust_dir: Path,
    issuer_did: str,
    issuer_key: SigningKey,
) -> None:
    """Manifest claims a hash that does NOT match the backend wheel → refused.

    The manifest is otherwise well-signed — this test isolates the
    content_hash step.  An attacker who compromises the issuer alone still
    cannot substitute a malicious wheel because the hash binds the manifest
    to the actual bytes on disk.
    """
    backends = [
        {
            "name": "tampered_local",
            "module": "arcrun.backends.local:LocalBackend",
            "content_hash": "sha256:" + "0" * 64,  # Deliberately wrong
        }
    ]
    manifest = _build_signed_manifest(
        tmp_path, issuer_did, issuer_key, backends
    )

    with pytest.raises(BackendSignatureError, match="content_hash mismatch"):
        load_backend(
            "arcrun.backends.local:LocalBackend",
            tier="federal",
            manifest_path=manifest,
            trust_dir=trusted_trust_dir,
        )


def test_manifest_with_tampered_backends_table_refused(
    tmp_path: Path,
    trusted_trust_dir: Path,
    issuer_did: str,
    issuer_key: SigningKey,
    local_content_hash: str,
) -> None:
    """Editing [[backends]] after the fact invalidates the signature.

    This is the "attacker compromises the CI bucket where the manifest
    lives" scenario.  Even with access to the file, an attacker cannot
    add a new entry without the signer's Ed25519 key.
    """
    backends = [
        {
            "name": "legit",
            "module": "arcrun.backends.local:LocalBackend",
            "content_hash": local_content_hash,
        }
    ]
    manifest = _build_signed_manifest(
        tmp_path, issuer_did, issuer_key, backends
    )

    # Tamper: append a new [[backends]] block AFTER the real signature
    tampered_text = manifest.read_text() + (
        "\n[[backends]]\n"
        'name = "attacker"\n'
        'module = "attacker:AttackerBackend"\n'
        'content_hash = "sha256:' + "f" * 64 + '"\n'
    )
    manifest.write_text(tampered_text, encoding="utf-8")

    # Even the legit backend should now fail because the signed payload
    # no longer matches the on-disk [[backends]] content.
    with pytest.raises(BackendSignatureError, match="did not verify"):
        load_backend(
            "arcrun.backends.local:LocalBackend",
            tier="federal",
            manifest_path=manifest,
            trust_dir=trusted_trust_dir,
        )


def test_unsigned_entry_in_manifest_refused(
    tmp_path: Path,
    trusted_trust_dir: Path,
    issuer_did: str,
    issuer_key: SigningKey,
    local_content_hash: str,
) -> None:
    """A backend that exists in the manifest *but without* a content_hash
    field fails — federal requires every entry to carry its hash so the
    loader can prove the wheel is the one the issuer reviewed."""
    meta = {"issued_at": "2026-04-18T00:00:00Z", "issuer_did": issuer_did}
    # Backend entry missing content_hash
    backends = [
        {"name": "no_hash", "module": "arcrun.backends.local:LocalBackend"}
    ]
    # Build canonical payload WITHOUT content_hash so the signature is valid
    sig_b64 = base64.b64encode(
        issuer_key.sign(_canonical(meta, backends)).signature
    ).decode()

    # Write manually — _emit_manifest assumes content_hash is present
    manifest = tmp_path / "no_hash.toml"
    manifest.write_text(
        f'[meta]\n'
        f'issued_at = "{meta["issued_at"]}"\n'
        f'issuer_did = "{meta["issuer_did"]}"\n\n'
        f'[[backends]]\n'
        f'name = "no_hash"\n'
        f'module = "arcrun.backends.local:LocalBackend"\n\n'
        f'[signature]\n'
        f'algorithm = "ed25519"\n'
        f'signature = "{sig_b64}"\n',
        encoding="utf-8",
    )

    with pytest.raises(BackendSignatureError, match="content_hash"):
        load_backend(
            "arcrun.backends.local:LocalBackend",
            tier="federal",
            manifest_path=manifest,
            trust_dir=trusted_trust_dir,
        )


def test_short_alias_at_federal_still_refused_with_manifest(
    tmp_path: Path,
    trusted_trust_dir: Path,
    issuer_did: str,
    issuer_key: SigningKey,
    local_content_hash: str,
) -> None:
    """Even with a signed manifest, a bare alias is refused at federal.

    Federal callers must pass the full dotted path so the manifest lookup
    has no ambiguity.
    """
    from arcrun.backends.loader import FederalBackendPolicyError

    backends = [
        {
            "name": "ssh_alias",
            "module": "arcrun.backends.local:LocalBackend",
            "content_hash": local_content_hash,
        }
    ]
    manifest = _build_signed_manifest(
        tmp_path, issuer_did, issuer_key, backends
    )

    with pytest.raises(FederalBackendPolicyError):
        load_backend(
            "ssh_alias",  # no ":" / "." → treated as alias
            tier="federal",
            manifest_path=manifest,
            trust_dir=trusted_trust_dir,
        )
