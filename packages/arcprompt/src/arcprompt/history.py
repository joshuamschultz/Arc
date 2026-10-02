"""PromptHistory — durable, signed, immutable prior versions of a signed prompt (J2 F3).

Every operator save of a signed prompt overlay (or of a signed workspace document
such as ``identity.md``) appends the exact signed bytes and their detached
``.arcsig`` sidecar here. Nothing in this store is ever rewritten or deleted by
the save path: a revert is just another save, so it appends a NEW version.

Layout, under the agent's ``context/`` root (the operator-only tree the agent's
own tools cannot reach)::

    <agent_root>/context/.history/<package>/<name>/000001.md
                                                   000001.md.arcsig
                                                   000002.md ...

A version is addressed by its number or by a unique prefix of its sha256. Reading
a version for revert goes through :meth:`PromptHistory.read_verified`, which
re-checks the stored signature against the pinned operator key: an attacker who
edits an archived file cannot get it laundered into a fresh signature.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from arctrust.artifact import ArtifactSignature, content_sha256

from arcprompt.errors import PromptMissing, PromptUnsigned, PromptVersionMissing
from arcprompt.verifier import SignatureVerifier

HISTORY_DIRNAME = ".history"
_SIDECAR_SUFFIX = ".arcsig"
_VERSION_DIGITS = 6
_UNSAFE_CHARS = ("/", "\\", "\x00")


@dataclass(frozen=True)
class PromptVersion:
    """One stored, signed version: its number, digest, signer and signing time."""

    version: int
    sha256: str
    signer_did: str
    signed_at: str | None


class PromptHistory:
    """The append-only version store for one ``package/name`` of one agent."""

    def __init__(self, agent_root: Path, package: str, name: str) -> None:
        for value in (package, name):
            if not value or value in (".", "..") or any(c in value for c in _UNSAFE_CHARS):
                raise PromptMissing(package, name)
        self._dir = agent_root / "context" / HISTORY_DIRNAME / package / name

    def _path(self, version: int) -> Path:
        return self._dir / f"{version:0{_VERSION_DIGITS}d}.md"

    def versions(self) -> list[PromptVersion]:
        """Every readable stored version, oldest first. A damaged sidecar is skipped."""
        if not self._dir.is_dir():
            return []
        found: list[PromptVersion] = []
        for path in sorted(self._dir.glob("*.md")):
            if not path.stem.isdigit():
                continue
            manifest = self._manifest(path)
            if manifest is not None:
                found.append(
                    PromptVersion(
                        version=int(path.stem),
                        sha256=manifest.artifact_sha256,
                        signer_did=manifest.signer_did,
                        signed_at=manifest.signed_at,
                    )
                )
        return found

    def record(self, data: bytes, signature_json: str) -> PromptVersion:
        """Append ``data`` and its signature as the next version (never overwrites)."""
        manifest = ArtifactSignature.from_json(signature_json)
        self._dir.mkdir(parents=True, exist_ok=True)
        # Number past every file on disk, readable or not, so a damaged entry is never reused.
        taken = [int(p.stem) for p in self._dir.glob("*.md") if p.stem.isdigit()]
        number = (max(taken) if taken else 0) + 1
        while True:
            try:
                _create_durable(self._path(number), data)
                break
            except FileExistsError:  # reason: a concurrent save took this number
                number += 1
        _create_durable(
            self._path(number).with_name(self._path(number).name + _SIDECAR_SUFFIX),
            signature_json.encode("utf-8"),
        )
        return PromptVersion(
            version=number,
            sha256=manifest.artifact_sha256,
            signer_did=manifest.signer_did,
            signed_at=manifest.signed_at,
        )

    def resolve_ref(self, ref: str) -> int:
        """Map a version number or sha256 prefix to a stored version number."""
        stored = self.versions()
        if ref.isdigit() and int(ref) in {v.version for v in stored}:
            return int(ref)
        needle = ref.removeprefix("sha256:")
        hits = [v.version for v in stored if v.sha256.removeprefix("sha256:").startswith(needle)]
        if needle and len(hits) == 1:
            return hits[0]
        raise PromptVersionMissing(f"no unique stored version matches {ref!r}")

    def read_verified(self, version: int, verifier: SignatureVerifier) -> bytes:
        """Return a version's bytes only if its stored signature verifies, else raise."""
        path = self._path(version)
        if not path.is_file():
            raise PromptVersionMissing(f"no stored version {version}")
        manifest = self._manifest(path)
        raw = path.read_bytes()
        if manifest is None or not verifier.verify(raw, manifest):
            raise PromptUnsigned(f"stored version {version} failed signature verification")
        return raw

    @staticmethod
    def _manifest(path: Path) -> ArtifactSignature | None:
        sidecar = path.with_name(path.name + _SIDECAR_SUFFIX)
        try:
            return ArtifactSignature.from_json(sidecar.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None


def record_if_unseen(history: PromptHistory, data: bytes, signature_json: str) -> None:
    """Record ``data`` unless the newest stored version already holds exactly these bytes.

    Used to capture the signed text that is live on disk BEFORE a first tracked save
    overwrites it, so an operator's pre-history edit is not lost to their next save.
    """
    stored = history.versions()
    if stored and stored[-1].sha256 == content_sha256(data):
        return
    history.record(data, signature_json)


def _create_durable(path: Path, data: bytes) -> None:
    """Create ``path`` exclusively (fails if present), write and fsync it."""
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())


__all__ = [
    "HISTORY_DIRNAME",
    "PromptHistory",
    "PromptVersion",
    "PromptVersionMissing",
    "record_if_unseen",
]
