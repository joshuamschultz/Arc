"""Self-contained I/O + text utilities for the improver (provider-free).

``arcskill.improver`` sits below arcagent in the DAG (REQ-004), so it cannot
reach up into ``arcagent.utils``. These four helpers -- atomic write, JSON
extraction from LLM prose, ASI-06 text sanitization, and frontmatter parsing --
are the minimal surface the relocated logic needs, copied here so the subpackage
stays import-clean.
"""

from __future__ import annotations

import os
import re
import unicodedata
from pathlib import Path
from typing import Any

import yaml

_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*\n(.*?)\n```", re.DOTALL)

# Zero-width / invisible / control chars -- instruction-smuggling vectors (ASI-06).
_ZERO_WIDTH_RE = re.compile(r"[\u200b-\u200f\u2028-\u202f\u2060-\u206f\ufeff]")
_INVISIBLE_RE = re.compile(r"[\u00ad\u180e\ufe00-\ufe0f\U000e0000-\U000e007f]")
_CONTROL_CHAR_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")

# Secret-looking tokens redacted before args ever persist (LLM02): sk-* API keys
# and eyJ*-prefixed dotted JWTs (with or without a Bearer prefix).
_SECRET_TOKEN_RE = re.compile(
    r"\bsk-[A-Za-z0-9_-]{16,}\b|\beyJ[A-Za-z0-9_=-]+(?:\.[A-Za-z0-9_=-]+)+\b"
)


def atomic_write_text(path: Path, content: str) -> None:
    """Write *content* to *path* atomically via tmp + rename (POSIX same-device)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    os.rename(str(tmp), str(path))


def _balanced_block(text: str, open_ch: str, close_ch: str) -> str | None:
    """First string-aware balanced ``open_ch..close_ch`` block, or None."""
    start = text.find(open_ch)
    if start == -1:
        return None
    depth = 0
    in_str = False
    escape = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == open_ch:
            depth += 1
        elif ch == close_ch:
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    return None


def extract_json(text: str | None) -> str:
    """Recover JSON from an LLM response wrapped in prose or ``` fences."""
    if not text:
        return ""
    match = _JSON_FENCE_RE.search(text)
    if match:
        return match.group(1).strip()
    stripped = text.strip()
    if not stripped or stripped[0] in "{[":
        return stripped
    obj = _balanced_block(stripped, "{", "}")
    arr = _balanced_block(stripped, "[", "]")
    candidates = [c for c in (obj, arr) if c is not None]
    if candidates:
        return min(candidates, key=stripped.find)
    return stripped


def sanitize_text(text: str, max_length: int = 2000, truncation_suffix: str = "") -> str:
    """NFKC-normalize + strip invisible/control chars + length-cap (ASI-06)."""
    clean = unicodedata.normalize("NFKC", text)
    clean = _ZERO_WIDTH_RE.sub("", clean)
    clean = _INVISIBLE_RE.sub("", clean)
    clean = _CONTROL_CHAR_RE.sub("", clean)
    if len(clean) > max_length:
        return clean[:max_length] + truncation_suffix
    return clean


def _scrub_value(value: Any) -> Any:
    if isinstance(value, str):
        return _SECRET_TOKEN_RE.sub("[REDACTED]", sanitize_text(value))
    if isinstance(value, dict):
        return {key: _scrub_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_scrub_value(item) for item in value]
    return value


def scrub_args(args: dict[str, Any]) -> dict[str, Any]:
    """Non-mutating recursive copy of *args* safe to persist (LLM02 + ASI-06).

    Every string rides :func:`sanitize_text` (invisible/zero-width chars stripped),
    then secret-looking tokens are replaced token-level with ``[REDACTED]`` so
    surrounding prose survives.
    """
    return {key: _scrub_value(value) for key, value in args.items()}


def read_frontmatter(path: Path) -> dict[str, Any] | None:
    """Parse YAML frontmatter from a markdown file, or None if absent/invalid."""
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    if not text.startswith("---"):
        return None
    end = text.find("\n---", 3)
    if end == -1:
        return None
    try:
        result = yaml.safe_load(text[4:end])
        return result if isinstance(result, dict) else None
    except yaml.YAMLError:
        return None


__all__ = ["atomic_write_text", "extract_json", "read_frontmatter", "sanitize_text", "scrub_args"]
