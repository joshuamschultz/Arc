"""Ed25519 manifest signature verification for allowed_backends (M3 gap-close).

Covers the contract of
``arcrun.backends.loader._verify_allowed_backends_signature``:

- Valid issuer signature → verified dict returned with all backend entries.
- Bad signature → BackendSignatureError("did not verify against issuer").
- Tampered [meta] after signing → BackendSignatureError (bytes mismatch).
- Tampered [[backends]] after signing → BackendSignatureError.
- Tampered content_hash of a backend's module → BackendSignatureError.
- Unknown issuer_did → BackendSignatureError via trust store.
- Missing manifest file → BackendSignatureError.
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

from arcrun.backends.loader import (
    BackendSignatureError,
    _verify_allowed_backends_signature,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _canonical(meta: Mapping[str, Any], backends: Sequence[Mapping[str, Any]]) -> bytes:
    """Mirror the loader's canonical-JSON encoding used for signing."""
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
    manifest_path: Path,
    *,
    meta: Mapping[str, Any],
    backends: Sequence[Mapping[str, Any]],
    signature_b64: str,
    algorithm: str = "ed25519",
) -> None:
    """Write a TOML manifest file with the given components."""
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
    lines.append(f'algorithm = "{algorithm}"')
    lines.append(f'signature = "{signature_b64}"')
    manifest_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _flush_trust_cache() -> Iterator[None]:
    from arcagent.core.trust_store import invalidate_cache  # type: ignore[import-not-found]

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
    return "did:arc:org:trust-authority/cafebabe"


@pytest.fixture
def trusted_trust_dir(
    trust_dir: Path, issuer_did: str, issuer_key: SigningKey
) -> Path:
    _write_issuers(trust_dir, issuer_did, bytes(issuer_key.verify_key))
    return trust_dir


@pytest.fixture
def real_backend_module() -> tuple[str, str, str]:
    """Return (module_dotted, module_name, content_hash_of_local_backend_module).

    We sign the real ``arcrun.backends.local`` module's content hash so the
    content-hash branch of ``load_backend`` is exercisable end-to-end.
    """
    import arcrun.backends.local as local_mod

    file_bytes = Path(local_mod.__file__).read_bytes()
    content_hash = "sha256:" + hashlib.sha256(file_bytes).hexdigest()
    return (
        "arcrun.backends.local:LocalBackend",
        "local_shadow",
        content_hash,
    )


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_valid_signature_verifies(
    tmp_path: Path,
    trusted_trust_dir: Path,
    issuer_did: str,
    issuer_key: SigningKey,
    real_backend_module: tuple[str, str, str],
) -> None:
    """Well-formed signed manifest → verified dict returned."""
    module_path, name, content_hash = real_backend_module
    meta = {"issued_at": "2026-04-18T00:00:00Z", "issuer_did": issuer_did}
    backends = [{"name": name, "module": module_path, "content_hash": content_hash}]

    payload = _canonical(meta, backends)
    sig_b64 = base64.b64encode(issuer_key.sign(payload).signature).decode()

    manifest = tmp_path / "allowed_backends.toml"
    _emit_manifest(manifest, meta=meta, backends=backends, signature_b64=sig_b64)

    verified = _verify_allowed_backends_signature(
        manifest_path=manifest, federal=True, trust_dir=trusted_trust_dir
    )
    assert name in verified
    assert module_path in verified  # Also indexed by module path for convenience


def test_tampered_meta_fails(
    tmp_path: Path,
    trusted_trust_dir: Path,
    issuer_did: str,
    issuer_key: SigningKey,
    real_backend_module: tuple[str, str, str],
) -> None:
    """Editing [meta] after signing → signature does not verify."""
    module_path, name, content_hash = real_backend_module
    meta = {"issued_at": "2026-04-18T00:00:00Z", "issuer_did": issuer_did}
    backends = [{"name": name, "module": module_path, "content_hash": content_hash}]

    # Sign the ORIGINAL meta
    sig_b64 = base64.b64encode(
        issuer_key.sign(_canonical(meta, backends)).signature
    ).decode()

    # Emit the manifest with a DIFFERENT issued_at — tamper
    tampered_meta = {"issued_at": "2099-01-01T00:00:00Z", "issuer_did": issuer_did}
    manifest = tmp_path / "tampered_meta.toml"
    _emit_manifest(manifest, meta=tampered_meta, backends=backends, signature_b64=sig_b64)

    with pytest.raises(BackendSignatureError, match="did not verify"):
        _verify_allowed_backends_signature(
            manifest_path=manifest, federal=True, trust_dir=trusted_trust_dir
        )


def test_tampered_backends_fails(
    tmp_path: Path,
    trusted_trust_dir: Path,
    issuer_did: str,
    issuer_key: SigningKey,
    real_backend_module: tuple[str, str, str],
) -> None:
    """Swapping a backend module path after signing → signature fails."""
    module_path, name, content_hash = real_backend_module
    meta = {"issued_at": "2026-04-18T00:00:00Z", "issuer_did": issuer_did}
    backends_original = [
        {"name": name, "module": module_path, "content_hash": content_hash}
    ]
    sig_b64 = base64.b64encode(
        issuer_key.sign(_canonical(meta, backends_original)).signature
    ).decode()

    tampered = [
        {
            "name": name,
            "module": "attacker_pkg:EvilBackend",
            "content_hash": content_hash,
        }
    ]
    manifest = tmp_path / "tampered_backends.toml"
    _emit_manifest(manifest, meta=meta, backends=tampered, signature_b64=sig_b64)

    with pytest.raises(BackendSignatureError, match="did not verify"):
        _verify_allowed_backends_signature(
            manifest_path=manifest, federal=True, trust_dir=trusted_trust_dir
        )


def test_bogus_signature_bytes_fails(
    tmp_path: Path,
    trusted_trust_dir: Path,
    issuer_did: str,
    real_backend_module: tuple[str, str, str],
) -> None:
    """Random 64-byte signature → BackendSignatureError."""
    module_path, name, content_hash = real_backend_module
    meta = {"issued_at": "2026-04-18T00:00:00Z", "issuer_did": issuer_did}
    backends = [{"name": name, "module": module_path, "content_hash": content_hash}]
    bogus_sig_b64 = base64.b64encode(b"\x00" * 64).decode()

    manifest = tmp_path / "bogus_sig.toml"
    _emit_manifest(
        manifest, meta=meta, backends=backends, signature_b64=bogus_sig_b64
    )

    with pytest.raises(BackendSignatureError):
        _verify_allowed_backends_signature(
            manifest_path=manifest, federal=True, trust_dir=trusted_trust_dir
        )


def test_unknown_issuer_fails(
    tmp_path: Path,
    trust_dir: Path,  # Empty trust dir — no issuers.toml
    issuer_key: SigningKey,
    real_backend_module: tuple[str, str, str],
) -> None:
    """issuer_did not in trust store → BackendSignatureError."""
    module_path, name, content_hash = real_backend_module
    meta = {
        "issued_at": "2026-04-18T00:00:00Z",
        "issuer_did": "did:arc:org:trust-authority/rogue",
    }
    backends = [{"name": name, "module": module_path, "content_hash": content_hash}]
    sig_b64 = base64.b64encode(
        issuer_key.sign(_canonical(meta, backends)).signature
    ).decode()

    manifest = tmp_path / "unknown_issuer.toml"
    _emit_manifest(manifest, meta=meta, backends=backends, signature_b64=sig_b64)

    with pytest.raises(BackendSignatureError, match="issuer"):
        _verify_allowed_backends_signature(
            manifest_path=manifest, federal=True, trust_dir=trust_dir
        )


def test_missing_manifest_file(
    tmp_path: Path, trusted_trust_dir: Path
) -> None:
    missing = tmp_path / "not_there.toml"
    with pytest.raises(BackendSignatureError, match="not found"):
        _verify_allowed_backends_signature(
            manifest_path=missing, federal=True, trust_dir=trusted_trust_dir
        )


def test_bad_toml_fails(
    tmp_path: Path, trusted_trust_dir: Path
) -> None:
    manifest = tmp_path / "bad.toml"
    manifest.write_text("this is not = = valid toml [[", encoding="utf-8")
    with pytest.raises(BackendSignatureError, match="invalid TOML"):
        _verify_allowed_backends_signature(
            manifest_path=manifest, federal=True, trust_dir=trusted_trust_dir
        )


def test_unsupported_algorithm_fails(
    tmp_path: Path,
    trusted_trust_dir: Path,
    issuer_did: str,
    real_backend_module: tuple[str, str, str],
) -> None:
    """Only ed25519 is supported; rsa/other → BackendSignatureError."""
    module_path, name, content_hash = real_backend_module
    meta = {"issued_at": "2026-04-18T00:00:00Z", "issuer_did": issuer_did}
    backends = [{"name": name, "module": module_path, "content_hash": content_hash}]

    manifest = tmp_path / "rsa.toml"
    _emit_manifest(
        manifest,
        meta=meta,
        backends=backends,
        signature_b64=base64.b64encode(b"\x00" * 64).decode(),
        algorithm="rsa",
    )
    with pytest.raises(BackendSignatureError, match="algorithm"):
        _verify_allowed_backends_signature(
            manifest_path=manifest, federal=True, trust_dir=trusted_trust_dir
        )
