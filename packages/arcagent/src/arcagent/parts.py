"""PartTranslator — a workspace media reference becomes a model block, once.

SPEC-065 COMP-009 (REQ-301, REQ-316). The agent is the lowest layer allowed to
know model types at all: ``arcgateway`` imports no model package, and this
module reaches content blocks through the ``arcrun`` facade (never
``arcllm``), so the one-way dependency graph holds.

Two directions, and the asymmetry between them is the whole design:

* :meth:`PartTranslator.to_history_content` turns inbound parts into what gets
  **logged** — a reference, always kilobytes.
* :meth:`PartTranslator.to_model_content` materialises the bytes for **one
  provider call** and keeps nothing.

Inlining base64 into the envelope was rejected in the SDD's Alternatives
Considered: the model API is stateless, so the bytes would re-ride every turn,
sit in the session jsonl forever, and the agent still could not re-open the
file on a later turn.

A media part is stored as a *text block carrying a media sidecar*. The block
validates as a plain ``arcrun`` text block — which matters, because
``SessionManager`` silently skips any history line the model layer rejects —
and the sidecar holds the four structured fields needed to re-materialise it.
Storing the reference in an image block's ``source`` was rejected: any code
path that handed history to a provider without translating it would then ship
a filename as image data. This shape degrades to a readable line naming the
file instead.
"""

from __future__ import annotations

import base64
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import arcrun
from pydantic import TypeAdapter

from arcagent import _pdf_worker

# One adapter for the whole discriminated union, so a stored block of any kind
# (text, tool_use, tool_result, …) is revived by the model layer's own rules
# rather than by a hand-written isinstance ladder that would drift from them.
_BLOCK_ADAPTER: TypeAdapter[arcrun.ContentBlock] = TypeAdapter(arcrun.ContentBlock)

# Key under which the structured media fields ride on a stored text block.
_MEDIA = "media"

# Attachment content is context, not a second unbounded input channel.  These
# limits apply at the last boundary before a provider call, even when a stale
# or hand-authored session record bypassed gateway custody.
MAX_PDF_BYTES = _pdf_worker.MAX_PDF_BYTES
MAX_PDF_TEXT_CHARS = _pdf_worker.MAX_PDF_TEXT_CHARS
MAX_PDF_PAGES = _pdf_worker.MAX_PDF_PAGES


class AttachmentExtractionError(ValueError):
    """A document cannot be safely materialised for a model call."""


class PartTranslator:
    """Translate inbound parts to session history, and history to model blocks.

    Args:
        workspace: The agent's workspace root. Every media reference is read
            relative to it and fenced inside it.
    """

    def __init__(self, *, workspace: Path) -> None:
        self._workspace = workspace

    def to_history_content(self, parts: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
        """Turn inbound parts into content safe to append to the session log.

        Parts arrive as plain dicts — the gateway's ``MediaPart`` type is not
        imported here, because ``arcagent`` must not depend on ``arcgateway``.

        Args:
            parts: Ordered parts as the sender composed them, each a mapping
                with a ``kind`` of "text", "image", "file" or "audio".

        Returns:
            JSON-serialisable content blocks holding references, never bytes.

        Raises:
            ValueError: If a part declares a kind this vocabulary has no
                translation for.
        """
        return [self._history_block(part) for part in parts]

    def to_model_content(self, content: Sequence[Mapping[str, Any]]) -> list[arcrun.ContentBlock]:
        """Materialise stored content into model blocks for a single call.

        Bytes are read fresh on every call and are never cached: a cached
        artefact would make the reference a lie the moment the file changed.

        Args:
            content: Stored history content, as produced by
                :meth:`to_history_content` or replayed from the session jsonl.

        Returns:
            ``arcrun`` content blocks for this call only. The caller's
            ``content`` is not modified and nothing is written back to it.

        Raises:
            ValueError: If a media reference resolves outside the workspace.
        """
        return [self._model_block(block) for block in content]

    def _history_block(self, part: Mapping[str, Any]) -> dict[str, Any]:
        """One inbound part as one storable block."""
        kind = part.get("kind")
        if kind == "text":
            return {"type": "text", "text": part["text"]}
        if kind in ("image", "file", "audio"):
            media = {
                "kind": kind,
                "mime": part["mime"],
                "declared_name": part["declared_name"],
                "ref": part["ref"],
            }
            return {"type": "text", "text": _readable_line(media), _MEDIA: media}
        msg = f"unknown part kind: {kind!r}"
        raise ValueError(msg)

    def _model_block(self, block: Mapping[str, Any]) -> arcrun.ContentBlock:
        """One stored block as one model block, reading bytes only if needed."""
        media = block.get(_MEDIA)
        if not isinstance(media, Mapping):
            return _BLOCK_ADAPTER.validate_python(block)
        if media.get("kind") != "image":
            if self._looks_like_pdf(media):
                try:
                    text = self._read_pdf_content(str(media["ref"]))
                except AttachmentExtractionError as exc:
                    # A failed extraction is deliberately a bounded, metadata
                    # only block. The provider never sees malformed/encrypted
                    # bytes and the turn remains deliverable.
                    return arcrun.TextBlock(
                        text=f"{_readable_line(media)}\n[content unavailable: {exc}]"
                    )
                return arcrun.TextBlock(
                    text=(
                        f"{_readable_line(media)}\n"
                        "[begin untrusted attachment content]\n"
                        f"{text}\n"
                        "[end untrusted attachment content]"
                    )
                )
            # Non-PDF files retain the historical reference-only behaviour.
            return arcrun.TextBlock(text=_readable_line(media))
        payload = self._read_artefact(str(media["ref"]))
        return arcrun.ImageBlock(
            source=base64.b64encode(payload).decode("ascii"),
            media_type=str(media["mime"]),
        )

    def _read_artefact(self, ref: str) -> bytes:
        """Read a referenced artefact from the workspace, fenced by ancestry.

        ADR-029: agent state is read with direct filesystem I/O against the
        workspace, never through the LLM-facing tools.

        The fence sits here rather than at ingest because this is the only
        place a reference becomes a file read — and references also arrive
        from the session log, which this translator did not write. A ``ref``
        originates in a remote sender's message, so an unfenced read of
        ``../../../etc/passwd``, an absolute path, or a symlink pointing out
        of the workspace would let a crafted message ship an arbitrary file to
        a model provider (LLM02 / ASI06). Resolving first and proving genuine
        ancestry is the check a string prefix comparison cannot make.
        """
        resolved = (self._workspace / ref).resolve()
        if self._workspace.resolve() not in resolved.parents:
            msg = f"media reference {ref!r} escapes the workspace"
            raise ValueError(msg)
        return resolved.read_bytes()

    def _read_pdf_content(self, ref: str) -> str:
        """Extract bounded text in a resource-limited child process."""
        resolved = self._resolve_artefact(ref)
        try:
            size = resolved.stat().st_size
        except OSError as exc:
            raise AttachmentExtractionError("file is unavailable") from exc
        if size > MAX_PDF_BYTES:
            raise AttachmentExtractionError("file exceeds the PDF extraction limit")
        try:
            return _pdf_worker.extract_pdf(resolved)
        except _pdf_worker.PdfWorkerError as exc:
            raise AttachmentExtractionError(str(exc)) from exc

    def _looks_like_pdf(self, media: Mapping[str, Any]) -> bool:
        """Detect PDF content by magic, with MIME as a compatibility hint."""
        mime = str(media.get("mime", "")).split(";", 1)[0].strip().lower()
        if mime == "application/pdf":
            return True
        try:
            with self._resolve_artefact(str(media["ref"])).open("rb") as handle:
                return _pdf_worker._has_pdf_magic(handle.read(1024))
        except (AttachmentExtractionError, OSError, KeyError, TypeError):
            return False

    def _resolve_artefact(self, ref: str) -> Path:
        """Resolve a workspace reference and enforce the symlink-aware fence."""
        resolved = (self._workspace / ref).resolve()
        if self._workspace.resolve() not in resolved.parents:
            raise AttachmentExtractionError("media reference escapes the workspace")
        if not resolved.is_file():
            raise AttachmentExtractionError("file is unavailable")
        return resolved


def _readable_line(media: Mapping[str, Any]) -> str:
    """Name an artefact in one line — for the log, the model and the operator."""
    return f"{media['kind']}: {media['declared_name']} ({media['mime']}) at {media['ref']}"
