"""The normalised inbound envelope and its part vocabulary (SPEC-065 COMP-001).

REQ-296: a payload carrying any combination of text, images and files
normalises into ONE envelope holding an ordered list of typed parts, in which
text is a part like any other. Media is therefore never a branch — a text-only
message and a photo-plus-caption travel the same path.

The envelope that carries these parts is ``InboundEvent`` (executor.py), whose
``parts`` field is the canonical form of a message and whose ``message`` field
is the flattened text projection every text-only consumer reads. The two are
held in step by a validator there, so they cannot drift into two messages.

Security property, enforced here by construction: a ``MediaPart`` carries a
*workspace reference*, never bytes. No field on either the media part or the
envelope can hold a payload, and ``extra="forbid"`` means bytes cannot be
smuggled in as an undeclared field either. That is what keeps a 5MB image out
of the session jsonl, out of the queue, and out of the prompt — the bytes stay
in the workspace and are materialised only for the provider call that needs
them (COMP-009).
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field


class TextPart(BaseModel):
    """Words from the sender.

    Attributes:
        kind: Discriminator; always "text".
        text: The message text as the platform delivered it.
    """

    model_config = ConfigDict(extra="forbid")

    kind: Literal["text"] = "text"
    text: str


class MediaPart(BaseModel):
    """An artefact already written to the agent workspace, named by reference.

    The four things a media part is allowed to know. ``declared_name`` is
    metadata only — the sender does not get to choose where the artefact lands,
    because the gateway composes ``ref`` itself (REQ-298).

    Attributes:
        kind: Artefact class the agent dispatches on ("image", "file", "audio").
        mime: Media type as resolved at storage time.
        declared_name: Sender-supplied filename, retained for display only.
        ref: Workspace-relative path to the stored bytes (MediaStore output).
    """

    model_config = ConfigDict(extra="forbid")

    kind: Literal["image", "file", "audio"]
    mime: str
    declared_name: str
    ref: str


# Discriminated on `kind` — pydantic resolves a raw platform dict to the right
# typed part, so adapters hand up plain payloads and round-trips through the
# session log keep their concrete types.
Part = Annotated[TextPart | MediaPart, Field(discriminator="kind")]


def flatten_text(parts: Sequence[Part]) -> str:
    """The words of a message, for the surfaces that only understand words.

    Media contributes a readable line naming the artefact rather than nothing,
    because a photo-only message whose text projection is ``""`` reads to every
    downstream consumer as an empty message and is dropped as noise.
    """
    lines: list[str] = []
    for part in parts:
        if isinstance(part, TextPart):
            lines.append(part.text)
        else:
            lines.append(f"{part.kind}: {part.declared_name} ({part.mime}) at {part.ref}")
    return "\n".join(line for line in lines if line)
