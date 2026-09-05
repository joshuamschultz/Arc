"""T-011 (RED) — model artifact verification (SPEC-077 COMP-017, REQ-022).

Every loaded model artifact (STT, TTS, wake, and any future PersonaPlex) is
signed, provenance-checked and version-pinned before use — MIT licensing does not
exempt it (LLM03/ASI04). An unpinned, undigested or unsigned artifact is refused
at load, not warned about.
"""

from __future__ import annotations

import pytest

from arcgateway.adapters.voice.engine.verify import (
    ArtifactVerifier,
    ModelArtifact,
    UnverifiedArtifactError,
)


def _good() -> ModelArtifact:
    return ModelArtifact(name="whisper", version="1.2.3", sha256="a" * 64, signature_ok=True)


def test_a_pinned_signed_digested_artifact_passes() -> None:
    ArtifactVerifier().verify(_good())  # must not raise


def test_unpinned_version_is_refused() -> None:
    art = ModelArtifact(name="whisper", version=None, sha256="a" * 64, signature_ok=True)
    with pytest.raises(UnverifiedArtifactError):
        ArtifactVerifier().verify(art)


def test_missing_digest_is_refused() -> None:
    art = ModelArtifact(name="whisper", version="1.2.3", sha256=None, signature_ok=True)
    with pytest.raises(UnverifiedArtifactError):
        ArtifactVerifier().verify(art)


def test_bad_signature_is_refused() -> None:
    art = ModelArtifact(name="whisper", version="1.2.3", sha256="a" * 64, signature_ok=False)
    with pytest.raises(UnverifiedArtifactError):
        ArtifactVerifier().verify(art)
