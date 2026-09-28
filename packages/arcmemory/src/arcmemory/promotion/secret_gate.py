"""Pre-egress secret gate (SPEC-083 COMP-012).

Runs before any memory item is sent to the third-party classifier. A hit keeps
the item on the box: it is never sent and never promoted. Email addresses, phone
numbers and names are company knowledge, not secrets, so they pass (decision 3).

Pure and deterministic; no model. Fails closed: any internal error is a hit.
"""

from __future__ import annotations

import logging
import re

from arctrust import secrets as arctrust_secrets

logger = logging.getLogger(__name__)

#: Credential keywords. Conservative on purpose: the WORD alone keeps the item
#: private, even when the secret value itself is absent.
_SECRET_KEYWORD_RE = re.compile(
    r"\b(passwd|password|secret|api[_-]?key|apikey|access[_-]?key|"
    r"private[_-]?key|token|credential|bearer)\b",
    re.IGNORECASE,
)


def contains_secret(text: str) -> bool:
    """True when ``text`` names a credential or carries a credential value.

    Consults the canonical ``arctrust.secrets.SECRET_PATTERNS`` list on every
    call, so a pattern added there is enforced here without a private copy.
    """
    try:
        if _SECRET_KEYWORD_RE.search(text):
            return True
        return any(pattern.search(text) for _, pattern in arctrust_secrets.SECRET_PATTERNS)
    except Exception:
        # Deliberate broad catch at a security boundary: an exploding scan must
        # never wave an item through to a third-party classifier.
        logger.warning("promotion secret gate failed; treating item as secret", exc_info=True)
        return True


__all__ = ["contains_secret"]
