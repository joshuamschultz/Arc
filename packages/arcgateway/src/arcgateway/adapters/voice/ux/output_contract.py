"""OutputContract — replies built for the ear (SPEC-077 COMP-009, REQ-012, D-764).

Enforced in code, not only in a prompt: a prompt asking for brevity is
unenforceable and an injected instruction can talk past it. This transform strips
markup (which also stops injected markup being read aloud), collapses long lists,
and caps length — moving the overflow into ``detail`` for a "tell me more".
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_FENCE = re.compile(r"```.*?```", re.DOTALL)
_IMAGE = re.compile(r"!\[([^\]]*)\]\([^)]*\)")
_LINK = re.compile(r"\[([^\]]+)\]\([^)]*\)")
_INLINE_CODE = re.compile(r"`([^`]*)`")
_BOLD = re.compile(r"(\*\*|__)(.+?)\1")
_ITALIC = re.compile(r"(\*|_)(.+?)\1")
_HEADER = re.compile(r"^\s{0,3}#{1,6}\s*")
_BULLET = re.compile(r"^\s*[-*+]\s+(.*)$")
_NUMBERED = re.compile(r"^\s*\d+[.)]\s+(.*)$")
_WS = re.compile(r"\s+")


@dataclass
class SpokenReply:
    """What to voice, and the full text to offer only if asked."""

    speech: str
    detail: str | None = None


class OutputContract:
    """Turn an agent's on-screen reply into ear-friendly speech."""

    def __init__(self, *, max_words: int = 60, max_list_items: int = 3) -> None:
        self.max_words = max_words
        self.max_list_items = max_list_items

    def to_speech(self, text: str) -> SpokenReply:
        collapsed = self._collapse_lists(text)
        clean = self._strip_markup(collapsed)
        clean = " ".join(
            _HEADER.sub("", line).strip()
            for line in clean.splitlines()
            if line.strip()
        )
        clean = _WS.sub(" ", clean).strip()

        words = clean.split()
        if len(words) <= self.max_words:
            return SpokenReply(speech=clean, detail=None)
        return SpokenReply(speech=" ".join(words[: self.max_words]), detail=clean)

    def _strip_markup(self, text: str) -> str:
        text = _FENCE.sub(" ", text)
        text = _IMAGE.sub(r"\1", text)
        text = _LINK.sub(r"\1", text)
        text = _INLINE_CODE.sub(r"\1", text)
        text = _BOLD.sub(r"\2", text)
        text = _ITALIC.sub(r"\2", text)
        return text

    def _collapse_lists(self, text: str) -> str:
        lines = text.splitlines()
        out: list[str] = []
        i = 0
        while i < len(lines):
            items: list[str] = []
            j = i
            while j < len(lines):
                match = _BULLET.match(lines[j]) or _NUMBERED.match(lines[j])
                if match is None:
                    break
                items.append(match.group(1).strip())
                j += 1
            if not items:
                out.append(lines[i])
                i += 1
                continue
            if len(items) > self.max_list_items:
                kept = items[: self.max_list_items]
                more = len(items) - self.max_list_items
                out.append("; ".join(kept) + f"; and {more} more — want the rest?")
            else:
                out.append("; ".join(items))
            i = j
        return "\n".join(out)


__all__ = ["OutputContract", "SpokenReply"]
