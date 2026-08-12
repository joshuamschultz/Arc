"""Media plumbing every platform adapter shares (SPEC-065 COMP-004).

Beside ``_text.py``, and for the same reason: REQ-310 says an adapter does
lifecycle, translation and delivery, so anything all of them need — classifying
a MIME type, wording a degrade, reading bytes within a bound — is written once
here rather than three times over there. Each of these started as per-adapter
copies that were already byte-identical; a fourth platform inherits them.

``base.py`` keeps the *contract* (the types and the Protocol). This module
keeps the shared *behaviour*, so the contract stays readable on one screen.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from arcgateway.adapters.base import MediaKind, MediaTooLargeOnWireError
from arcgateway.parts import MediaPart

#: Read granularity for :func:`read_bounded`. Small enough that the overshoot
#: past the ceiling is bounded by one chunk, large enough not to thrash.
_CHUNK_BYTES = 64 * 1024


def kind_for(mime: str) -> MediaKind:
    """Classify an artefact by its declared media type.

    Here rather than per adapter: a MIME class added for one platform and
    forgotten on the next two is a classification that silently disagrees with
    itself, and the agent dispatches on the answer.
    """
    if mime.startswith("image/"):
        return "image"
    if mime.startswith("audio/"):
        return "audio"
    return "file"


def describe_undeliverable(part: MediaPart, platform: str) -> str:
    """Say what an artefact was and where it still is, when a platform can't carry it.

    The degrade text of REQ-311, written once. Deliberately prose, not a repr:
    the operator reads this on their phone. It names the file because
    "something went wrong" tells them nothing they can act on, and it names the
    path because the artefact is not lost — only the delivery of it was.
    """
    return (
        f"[{part.kind}] {part.declared_name} could not be delivered on {platform}; "
        f"it is saved at {part.ref}"
    )


def read_artefact(part: MediaPart) -> bytes:
    """Read an outbound artefact's bytes for delivery, refusing a relative ref.

    ``MediaPart.ref`` is workspace-relative by contract, and an adapter has no
    workspace — deliberately, since resolving one would make it the second
    place that knows where an agent lives (REQ-310). So whatever hands parts to
    ``send`` must resolve the ref first, and a relative one arriving here is a
    caller that did not.

    The refusal is explicit rather than a best-effort ``Path(ref)``: read
    relative to the process cwd, ``inbox/x.png`` silently means a *different*
    file on every launch path, and the failure would be a wrong attachment
    rather than an error.
    """
    path = Path(part.ref)
    if not path.is_absolute():
        msg = (
            f"outbound media ref {part.ref!r} is workspace-relative; the caller must "
            "resolve it against the agent's workspace before delivery"
        )
        raise ValueError(msg)
    return path.read_bytes()


async def read_bounded(response: Any, limit_bytes: int) -> bytes:
    """Read an aiohttp response body, abandoning it the moment it passes ``limit_bytes``.

    The one bounded read every HTTP adapter shares. ``response.read()`` is the
    shape to avoid: it allocates whatever the remote sends, so a 1GB attachment
    is 1GB resident *before* any ceiling gets a say.

    Raises:
        MediaTooLargeOnWireError: The body passed the ceiling. Nothing past the
            bound was accumulated.
    """
    chunks: list[bytes] = []
    total = 0
    async for chunk in response.content.iter_chunked(_CHUNK_BYTES):
        total += len(chunk)
        if total > limit_bytes:
            raise MediaTooLargeOnWireError(limit_bytes)
        chunks.append(chunk)
    return b"".join(chunks)


__all__ = [
    "describe_undeliverable",
    "kind_for",
    "read_artefact",
    "read_bounded",
]
