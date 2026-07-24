"""PromptDocument + frontmatter model (COMP-002).

A prompt on disk is markdown with a YAML frontmatter block::

    ---
    name: consolidation_system
    description: Instructs the memory consolidation pass.
    tunable: true
    ---
    <prompt body...>

The version identity of a prompt is a sha256 digest *derived from the raw file
bytes* (REQ-127) — never an authored ``version`` field, which is ignored. The
digest is computed over the same bytes an overlay's ``.arcsig`` signs, so a
document's ``sha256`` and its signature manifest agree by construction.

Newline rule (REQ-138): the stored ``body`` is the text after the frontmatter
with exactly one trailing newline removed. A stock file is authored as
``<frontmatter> + constant + "\\n"``, so ``load(author(C)).body == C`` for any
constant ``C`` — including one that already ends in a newline. This is the seam
that makes the byte-identity migration provably faithful.
"""

from __future__ import annotations

from typing import Any, Literal

import yaml
from arctrust.artifact import content_sha256
from pydantic import BaseModel, ConfigDict, ValidationError

from arcprompt.errors import PromptUnparseable

_FRONTMATTER_DELIM = "---\n"

Source = Literal["stock", "overlay"]


class PromptFrontmatter(BaseModel):
    """Validated frontmatter carried by every prompt file.

    ``extra="ignore"`` means an authored ``version:`` field is silently dropped
    rather than honored (REQ-127) — version identity is always the derived
    digest. ``tunable`` is inert metadata in v1: it is parsed and preserved but
    enforces nothing until the v2 optimizer exists.
    """

    model_config = ConfigDict(frozen=True, extra="ignore")

    name: str
    description: str
    tunable: bool = True


class PromptDocument(BaseModel):
    """A resolved prompt: its validated frontmatter, body, and derived identity."""

    model_config = ConfigDict(frozen=True)

    name: str
    description: str
    tunable: bool
    body: str
    sha256: str
    source: Source
    signer_did: str | None = None
    """The overlay signer's DID; ``None`` for stock (nothing signs packaged text)."""


def _split_frontmatter(text: str) -> tuple[str, str]:
    """Return ``(frontmatter_yaml, body)`` or raise if the block is malformed."""
    if not text.startswith(_FRONTMATTER_DELIM):
        raise PromptUnparseable("prompt file does not begin with a '---' frontmatter block")
    rest = text[len(_FRONTMATTER_DELIM) :]
    end = rest.find("\n" + _FRONTMATTER_DELIM)
    if end == -1:
        raise PromptUnparseable("prompt frontmatter block is not terminated by a '---' line")
    frontmatter = rest[:end]
    body = rest[end + len("\n" + _FRONTMATTER_DELIM) :]
    return frontmatter, body


def parse_prompt(raw: bytes, *, source: Source, signer_did: str | None = None) -> PromptDocument:
    """Parse raw prompt-file bytes into a validated, frozen :class:`PromptDocument`.

    Raises :class:`PromptUnparseable` for undecodable bytes, malformed
    frontmatter, or an empty body. The digest is over ``raw`` so it matches an
    overlay's signature manifest exactly.
    """
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise PromptUnparseable("prompt file is not valid UTF-8") from exc

    frontmatter, body = _split_frontmatter(text)
    if body.endswith("\n"):
        body = body[:-1]
    if not body.strip():
        raise PromptUnparseable("prompt body is empty")

    try:
        loaded: Any = yaml.safe_load(frontmatter)
    except yaml.YAMLError as exc:
        raise PromptUnparseable(f"prompt frontmatter is not valid YAML: {exc}") from exc
    if not isinstance(loaded, dict):
        raise PromptUnparseable("prompt frontmatter must be a YAML mapping")

    try:
        meta = PromptFrontmatter(**loaded)
    except ValidationError as exc:
        raise PromptUnparseable(f"prompt frontmatter failed validation: {exc}") from exc

    return PromptDocument(
        name=meta.name,
        description=meta.description,
        tunable=meta.tunable,
        body=body,
        sha256=content_sha256(raw),
        source=source,
        signer_did=signer_did,
    )


def render_prompt(body: str, *, name: str, description: str, tunable: bool = True) -> bytes:
    """Author a stock/overlay file from a body + frontmatter, honoring the newline rule.

    The body is written verbatim followed by a single terminating newline, so
    ``parse_prompt(render_prompt(C, ...)).body == C`` for any ``C`` (REQ-138).
    """
    meta = yaml.safe_dump(
        {"name": name, "description": description, "tunable": tunable},
        sort_keys=False,
    )
    return f"{_FRONTMATTER_DELIM}{meta}{_FRONTMATTER_DELIM}{body}\n".encode()


__all__ = [
    "PromptDocument",
    "PromptFrontmatter",
    "Source",
    "parse_prompt",
    "render_prompt",
]
