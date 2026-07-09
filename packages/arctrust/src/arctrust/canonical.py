"""Canonical-JSON serialization — the one deterministic byte form a signature binds.

A signature over structured data is only meaningful if the signer and every
verifier serialize that data to the *same* bytes. arctrust is the sign/verify
home, so the canonicalization primitive a signature commits to lives here rather
than being hand-rolled per package (where compact-vs-default separators or
``ensure_ascii`` could silently diverge and break cross-package verification).

Fixed contract: ``sort_keys=True`` (key order is deterministic), the most
compact separators (no insignificant whitespace), and the ``json`` default
``ensure_ascii=True`` (output is pure ASCII, so the UTF-8 encoding is stable
regardless of the platform's locale). The input must be JSON-serialisable with
the stdlib encoder — no ``default=`` coercion, so a non-serialisable value fails
loudly rather than being silently stringified into a different byte form.
"""

from __future__ import annotations

import json
from typing import Any

__all__ = ["canonical_json"]


def canonical_json(obj: Any) -> bytes:
    """Serialize ``obj`` to deterministic canonical-JSON bytes for signing."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":")).encode("utf-8")
