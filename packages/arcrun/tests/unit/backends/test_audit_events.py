"""Phase C: arctrust AuditEvent emission from backend loader.

Every backend load attempt must emit an AuditEvent via arctrust.audit.emit().
This replaces the logger-only audit helpers in loader.py.

Covers:
- backend.loaded event on successful builtin load
- backend.loaded event on successful manifest-verified load
- backend.denied event when manifest is missing at non-federal tier (post phase-C)
- backend.denied event when backend is not in the manifest
- backend.signature_verified event after manifest verification passes
- backend.signature_invalid event when signature verification fails
- backend.content_hash_mismatch event when content hash does not match
"""

from __future__ import annotations

import base64
import hashlib
import json
from collections.abc import Iterator, Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest
from arctrust import AuditEvent, NullSink
from nacl.signing import SigningKey

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class CaptureSink:
    """Audit sink that captures every event for assertion."""

    def __init__(self) -> None:
        self._events: list[AuditEvent] = []

    def write(self, event: AuditEvent) -> None:
        self._events.append(event)

    @property
    def events(self) -> list[AuditEvent]:
        return list(self._events)

    def actions(self) -> list[str]:
        return [e.action for e in self._events]


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
    path: Path,
    *,
    meta: Mapping[str, Any],
    backends: Sequence[Mapping[str, Any]],
    signature_b64: str,
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


def _build_signed_manifest(
    tmp_path: Path,
    issuer_did: str,
    issuer_key: SigningKey,
    backends: Sequence[Mapping[str, Any]],
) -> Path:
    meta = {"issued_at": "2026-04-18T00:00:00Z", "issuer_did": issuer_did}
    sig_b64 = base64.b64encode(issuer_key.sign(_canonical(meta, backends)).signature).decode()
    manifest = tmp_path / "allowed_backends.toml"
    _emit_manifest(manifest, meta=meta, backends=backends, signature_b64=sig_b64)
    return manifest


def _local_content_hash() -> str:
    import arcrun.backends.local as local_mod

    raw = Path(local_mod.__file__).read_bytes()
    return "sha256:" + hashlib.sha256(raw).hexdigest()


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
def issuer_key() -> SigningKey:
    return SigningKey.generate()


@pytest.fixture
def issuer_did() -> str:
    return "did:arc:org:trust-authority/audit-test"


@pytest.fixture
def trust_dir(tmp_path: Path) -> Path:
    d = tmp_path / "trust"
    d.mkdir()
    return d


@pytest.fixture
def trusted_trust_dir(trust_dir: Path, issuer_did: str, issuer_key: SigningKey) -> Path:
    _write_issuers(trust_dir, issuer_did, bytes(issuer_key.verify_key))
    return trust_dir


# ---------------------------------------------------------------------------
# Tests: backend load events
# ---------------------------------------------------------------------------


class TestBackendLoadAuditEvents:
    """AuditEvents are emitted by load_backend() for every outcome."""

    def test_builtin_load_emits_backend_loaded(self) -> None:
        """Loading a built-in backend emits executor.backend.loaded."""
        from arcrun.backends.loader import load_backend

        sink = CaptureSink()
        load_backend("local", tier="personal", audit_sink=sink)
        assert "executor.backend.loaded" in sink.actions()

    def test_builtin_load_event_has_correct_target(self) -> None:
        """executor.backend.loaded event carries the backend name as target."""
        from arcrun.backends.loader import load_backend

        sink = CaptureSink()
        load_backend("local", tier="federal", audit_sink=sink)
        loaded_events = [e for e in sink.events if e.action == "executor.backend.loaded"]
        assert len(loaded_events) >= 1
        assert loaded_events[0].target == "local"

    def test_manifest_verified_load_emits_backend_loaded(
        self,
        tmp_path: Path,
        trusted_trust_dir: Path,
        issuer_did: str,
        issuer_key: SigningKey,
    ) -> None:
        """Manifest-verified load emits backend.loaded AND backend.signature_verified."""
        from arcrun.backends.loader import load_backend

        content_hash = _local_content_hash()
        backends = [
            {
                "name": "local_alias",
                "module": "arcrun.backends.local:LocalBackend",
                "content_hash": content_hash,
            }
        ]
        manifest = _build_signed_manifest(tmp_path, issuer_did, issuer_key, backends)

        sink = CaptureSink()
        load_backend(
            "arcrun.backends.local:LocalBackend",
            tier="federal",
            manifest_path=manifest,
            trust_dir=trusted_trust_dir,
            audit_sink=sink,
        )

        actions = sink.actions()
        assert "executor.backend.loaded" in actions
        assert "backend.signature_verified" in actions

    def test_missing_manifest_emits_backend_denied(self) -> None:
        """No manifest at non-federal tier (post phase-C) emits executor.backend.denied."""
        from arcrun.backends.loader import BackendSignatureError, load_backend

        sink = CaptureSink()
        with pytest.raises(BackendSignatureError):
            load_backend(
                "somepackage:SomeBackend",
                tier="enterprise",
                manifest_path=None,
                audit_sink=sink,
            )
        assert "executor.backend.denied" in sink.actions()

    def test_backend_not_in_manifest_emits_denied(
        self,
        tmp_path: Path,
        trusted_trust_dir: Path,
        issuer_did: str,
        issuer_key: SigningKey,
    ) -> None:
        """Backend absent from signed manifest emits executor.backend.denied."""
        from arcrun.backends.loader import BackendSignatureError, load_backend

        content_hash = _local_content_hash()
        backends = [
            {
                "name": "other_backend",
                "module": "arcrun.backends.local:LocalBackend",
                "content_hash": content_hash,
            }
        ]
        manifest = _build_signed_manifest(tmp_path, issuer_did, issuer_key, backends)

        sink = CaptureSink()
        with pytest.raises(BackendSignatureError):
            load_backend(
                "arcrun.backends.docker:DockerBackend",
                tier="federal",
                manifest_path=manifest,
                trust_dir=trusted_trust_dir,
                audit_sink=sink,
            )
        assert "executor.backend.denied" in sink.actions()

    def test_bad_manifest_signature_emits_signature_invalid(
        self,
        tmp_path: Path,
        trusted_trust_dir: Path,
        issuer_did: str,
        issuer_key: SigningKey,
    ) -> None:
        """Invalid manifest signature emits backend.signature_invalid."""
        from arcrun.backends.loader import BackendSignatureError, load_backend

        content_hash = _local_content_hash()
        backends = [
            {
                "name": "local_alias",
                "module": "arcrun.backends.local:LocalBackend",
                "content_hash": content_hash,
            }
        ]
        meta = {"issued_at": "2026-04-18T00:00:00Z", "issuer_did": issuer_did}
        bogus_sig_b64 = base64.b64encode(b"\x00" * 64).decode()
        manifest = tmp_path / "bad_sig.toml"
        _emit_manifest(manifest, meta=meta, backends=backends, signature_b64=bogus_sig_b64)

        sink = CaptureSink()
        with pytest.raises(BackendSignatureError):
            load_backend(
                "arcrun.backends.local:LocalBackend",
                tier="federal",
                manifest_path=manifest,
                trust_dir=trusted_trust_dir,
                audit_sink=sink,
            )
        assert "backend.signature_invalid" in sink.actions()

    def test_content_hash_mismatch_emits_content_hash_mismatch(
        self,
        tmp_path: Path,
        trusted_trust_dir: Path,
        issuer_did: str,
        issuer_key: SigningKey,
    ) -> None:
        """Wrong content_hash in manifest emits backend.content_hash_mismatch."""
        from arcrun.backends.loader import BackendSignatureError, load_backend

        backends = [
            {
                "name": "bad_hash",
                "module": "arcrun.backends.local:LocalBackend",
                "content_hash": "sha256:" + "0" * 64,  # Deliberately wrong
            }
        ]
        manifest = _build_signed_manifest(tmp_path, issuer_did, issuer_key, backends)

        sink = CaptureSink()
        with pytest.raises(BackendSignatureError):
            load_backend(
                "arcrun.backends.local:LocalBackend",
                tier="federal",
                manifest_path=manifest,
                trust_dir=trusted_trust_dir,
                audit_sink=sink,
            )
        assert "backend.content_hash_mismatch" in sink.actions()

    def test_null_sink_accepted(self) -> None:
        """NullSink from arctrust is accepted as audit_sink (type compatibility)."""
        from arcrun.backends.loader import load_backend

        sink = NullSink()
        # Should not raise — NullSink satisfies AuditSink Protocol
        load_backend("local", tier="personal", audit_sink=sink)

    def test_no_sink_uses_logger_fallback(self) -> None:
        """When no audit_sink is provided the loader falls back to logger — no error."""
        from arcrun.backends.loader import load_backend

        # Must not raise when audit_sink is omitted
        load_backend("local", tier="personal")
