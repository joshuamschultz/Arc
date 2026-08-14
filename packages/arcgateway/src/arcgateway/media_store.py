"""MediaStore — custody of every artefact crossing the gateway boundary.

SPEC-065 COMP-002 (REQ-297, REQ-298, REQ-299, REQ-300).

Three invariants hold this module together:

ADR-029 — an inbound attachment is *agent state*, so it is written with direct
filesystem I/O into the agent's workspace. It never travels through the
LLM-facing ``write``/``bash``/``edit`` tools, which would let the agent's brain
leak into whatever project directory the tools happen to be pointed at.

REQ-298 — the sender names the file, the **gateway** composes the path. The
declared name is untrusted remote input (LLM05 improper output handling, ASI02
tool misuse) and is retained as metadata only; it contributes a sanitised stem
and extension and nothing else. See ``_safe_stem_and_ext`` for the reasoning.

REQ-300 — one audit event per artefact, through the gateway's arctrust
chokepoint. The event records *custody*: who, which channel, what kind, how
big, and where it landed. Never the bytes.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from arcgateway.audit import emit_event

# POSIX NAME_MAX: a single path component may not exceed 255 bytes. Every
# component we compose is restricted to _SAFE_CHARS, which is ASCII, so byte
# length and character length are the same and slicing cannot split a rune.
_NAME_MAX = 255

# Anything outside this set is remote-controlled noise: separators, control
# characters, quoting, shell metacharacters, unicode homoglyphs. Collapse it.
_UNSAFE_CHARS = re.compile(r"[^A-Za-z0-9._-]+")

# Bounds on the parts the sender influences, so the composed name always has
# room left for the gateway's own prefix inside _NAME_MAX.
_MAX_SENDER = 64
_MAX_EXT = 16

# Used when the declared name yields nothing usable ("...", "", "/etc/").
_FALLBACK_STEM = "file"

# Distinct artefacts can share a second, a sender and a declared name (a photo
# album). Rather than overwrite one with the other, disambiguate — bounded, so
# a flood cannot spin here forever.
_MAX_COLLISIONS = 1000


class MediaTooLargeError(Exception):
    """An inbound artefact exceeded the configured ceiling and was refused.

    Typed rather than generic because the caller has to answer the sender on
    the channel the artefact arrived from (REQ-299), which needs all four
    fields carried here.
    """

    def __init__(
        self,
        *,
        channel: str,
        declared_name: str,
        size_bytes: int,
        limit_bytes: int,
    ) -> None:
        super().__init__(
            f"artefact {declared_name!r} from {channel} is {size_bytes} bytes, "
            f"over the {limit_bytes} byte ceiling"
        )
        self.channel = channel
        self.declared_name = declared_name
        self.size_bytes = size_bytes
        self.limit_bytes = limit_bytes


class StoredMedia(BaseModel):
    """Where an artefact landed, and what the sender claimed it was."""

    model_config = ConfigDict(frozen=True)

    path: Path
    """Absolute path the gateway composed and wrote to."""

    ref: str
    """``path`` relative to the workspace root — what travels in a MediaPart.

    Relative because a reference outlives the machine that made it: history is
    replayed, moved between hosts and rendered in a browser, and an absolute
    path would both break on relocation and publish the host's filesystem
    layout to every one of those readers.
    """

    declared_name: str
    """Verbatim sender-supplied filename — metadata only, never a path input."""

    kind: str
    """Artefact class: ``image``, ``file``, ``audio`` — the MediaPart vocabulary."""

    mime: str
    """Declared MIME type."""

    size_bytes: int
    """Bytes written."""


def _sanitise(raw: str) -> str:
    """Collapse untrusted text to a single safe path-component fragment.

    Unsafe runs become a hyphen rather than vanishing, so ``a/b`` cannot
    silently become the different-looking ``ab``. Leading and trailing
    ``-._`` are then stripped, which is what disarms the dot-only names
    (``..``, ``...``) that would otherwise mean "parent directory".
    """
    return _UNSAFE_CHARS.sub("-", raw).strip("-._")


def _safe_stem_and_ext(declared_name: str) -> tuple[str, str]:
    """Derive a stem and extension from an untrusted declared filename.

    The declared name is treated as data, not as a path, at every step:

    1. Both separator conventions are folded and only the last component is
       kept, so ``../../../etc/passwd`` and ``..\\..\\x`` contribute a leaf.
    2. Non-printables go, which removes NUL bytes (filename truncation in the
       C layer) and CR/LF (audit- and log-injection).
    3. The remainder is collapsed to ``_SAFE_CHARS``; anything left over that
       still means nothing falls back to ``_FALLBACK_STEM``.

    Length is *not* bounded here — the final bound depends on the prefix the
    gateway prepends, so ``_compose_name`` owns it.
    """
    leaf = declared_name.replace("\\", "/").rsplit("/", 1)[-1]
    leaf = "".join(char for char in leaf if char.isprintable())

    stem, dot, ext = leaf.rpartition(".")
    if not dot:  # rpartition puts the whole string in `ext` when there is none
        stem, ext = leaf, ""

    return _sanitise(stem) or _FALLBACK_STEM, _sanitise(ext)[:_MAX_EXT]


class MediaStore:
    """Writes inbound artefacts into an agent workspace and audits both directions.

    Args:
        workspace: The agent's workspace root. Artefacts land under
            ``<workspace>/inbox/<YYYY-MM-DD>/``.
        max_bytes: Inclusive size ceiling; an artefact exactly at the ceiling
            is accepted.
    """

    def __init__(self, *, workspace: Path, max_bytes: int) -> None:
        self._workspace = workspace
        self._inbox = workspace / "inbox"
        self._max_bytes = max_bytes

    @property
    def max_bytes(self) -> int:
        """The ceiling, readable so a caller can refuse before spending bytes."""
        return self._max_bytes

    def store(
        self,
        *,
        data: bytes,
        declared_name: str,
        mime: str,
        kind: str,
        sender: str,
        channel: str,
        actor_did: str,
    ) -> StoredMedia:
        """Write one inbound artefact to the workspace and audit its arrival.

        Args:
            data: The artefact bytes.
            declared_name: What the sender called it. Untrusted.
            mime: Declared MIME type.
            kind: Artefact class (``image``, ``file``, ``audio``).
            sender: Resolved sender identity, used in the composed filename.
            channel: Channel the artefact arrived on, e.g. ``telegram:9001``.
            actor_did: DID of the entity that sent it.

        Returns:
            Where the bytes landed, plus the declared metadata.

        Raises:
            MediaTooLargeError: The artefact is over the ceiling. Nothing is
                written and no ``media.received`` event is emitted — the
                refusal is decided before any file is opened, so there is no
                window in which a rejected artefact exists on disk.
        """
        size_bytes = len(data)
        if size_bytes > self._max_bytes:
            raise MediaTooLargeError(
                channel=channel,
                declared_name=declared_name,
                size_bytes=size_bytes,
                limit_bytes=self._max_bytes,
            )

        path = self._write(data=data, declared_name=declared_name, sender=sender)

        emit_event(
            "media.received",
            str(path),
            "allow",
            actor_did=actor_did,
            extra={
                "channel": channel,
                "kind": kind,
                "mime": mime,
                "size_bytes": size_bytes,
                "declared_name": declared_name,
            },
        )

        return StoredMedia(
            path=path,
            ref=path.relative_to(self._workspace).as_posix(),
            declared_name=declared_name,
            kind=kind,
            mime=mime,
            size_bytes=size_bytes,
        )

    def record_sent(
        self,
        *,
        path: Path,
        mime: str,
        kind: str,
        channel: str,
        actor_did: str,
    ) -> None:
        """Audit one artefact leaving the agent for a channel (REQ-300).

        Args:
            path: The workspace file that was sent.
            mime: MIME type sent to the platform.
            kind: Artefact class.
            channel: Channel it was sent on.
            actor_did: DID of the agent that sent it.
        """
        emit_event(
            "media.sent",
            str(path),
            "allow",
            actor_did=actor_did,
            extra={
                "channel": channel,
                "kind": kind,
                "mime": mime,
                "size_bytes": path.stat().st_size,
            },
        )

    def _write(self, *, data: bytes, declared_name: str, sender: str) -> Path:
        """Compose the path, prove containment, then write directly (ADR-029)."""
        now = datetime.now(UTC)
        day_dir = self._inbox / now.strftime("%Y-%m-%d")
        day_dir.mkdir(parents=True, exist_ok=True)

        stem, ext = _safe_stem_and_ext(declared_name)
        safe_sender = _sanitise(sender)[:_MAX_SENDER] or "unknown"
        clock = now.strftime("%H%M%S")

        for ordinal in range(1, _MAX_COLLISIONS + 1):
            name = _compose_name(
                clock=clock, sender=safe_sender, stem=stem, ext=ext, ordinal=ordinal
            )
            path = day_dir / name
            self._assert_inside_inbox(path)
            try:
                # Exclusive create: the collision check and the claim are one
                # atomic step, so two concurrent artefacts cannot both win.
                with path.open("xb") as handle:
                    handle.write(data)
            except FileExistsError:
                continue
            return path

        raise FileExistsError(f"{_MAX_COLLISIONS} artefacts already share {day_dir / name}")

    def _assert_inside_inbox(self, path: Path) -> None:
        """Belt and braces: prove by ancestry that the target is inside the inbox.

        Sanitisation should already make escape impossible, but a string prefix
        check would happily accept ``inbox/../../etc/passwd``. Resolving first
        and testing genuine ancestry is the check that cannot be talked around.
        """
        resolved = path.resolve()
        if self._inbox.resolve() not in resolved.parents:
            raise ValueError(f"composed path {resolved} escaped the inbox")


def _compose_name(*, clock: str, sender: str, stem: str, ext: str, ordinal: int) -> str:
    """Build the single filename component, bounded to NAME_MAX.

    The gateway's own fields (time, sender, collision ordinal, extension) are
    laid down first and the sender-influenced stem absorbs whatever budget is
    left, so a 500-character declared name shortens the stem instead of
    pushing the filename past what the filesystem will accept.
    """
    tag = "" if ordinal == 1 else f"-{ordinal}"
    suffix = f".{ext}" if ext else ""
    prefix = f"{clock}-{sender}-"
    budget = _NAME_MAX - len(prefix) - len(tag) - len(suffix)
    return f"{prefix}{stem[:budget]}{tag}{suffix}"
