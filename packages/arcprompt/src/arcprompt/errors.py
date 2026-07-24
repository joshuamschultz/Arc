"""Typed errors for prompt loading and resolution.

Arc surfaces failures as typed exceptions, never silent fallbacks
(`.claude/steering/tech.md#error-handling-pattern`). A present-but-broken
overlay must fail loud — it must never be silently replaced by stock, or an
operator's deliberate override could vanish without a trace (REQ-125).
"""

from __future__ import annotations


class PromptError(Exception):
    """Base class for every arcprompt failure."""


class PromptUnparseable(PromptError):
    """An overlay or stock file has malformed frontmatter or an empty body (REQ-125)."""


class PromptUnsigned(PromptError):
    """An overlay's Ed25519 signature is missing, invalid, or wrong-key (REQ-125, REQ-129)."""


class PromptMissing(PromptError):
    """A packaged stock prompt is absent at load — a packaging error (REQ-126)."""

    def __init__(self, package: str, name: str) -> None:
        self.package = package
        self.name = name
        super().__init__(
            f"stock prompt {package}:{name} is not packaged — the {package} wheel is "
            f"missing src/{package}/context/{name}.md (declare it in artifacts, REQ-140)"
        )


__all__ = [
    "PromptError",
    "PromptMissing",
    "PromptUnparseable",
    "PromptUnsigned",
]
