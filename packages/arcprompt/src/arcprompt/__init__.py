"""arcprompt — editable, signed, inspectable system prompts for Arc.

A leaf package (imports only arctrust) that owns prompt storage and resolution
for every Arc package. Stock prompts ship as markdown under ``src/<pkg>/context/``;
an agent may override any prompt with a signed markdown overlay in its config
root. Resolution is two layers, overlay-over-stock, first-match-wins, and frozen
once per run. See ``.claude/specs/editable-system-prompts/``.
"""

from __future__ import annotations

from arcprompt.catalog import (
    DEFAULT_PROMPT_PACKAGES,
    PromptCatalog,
    PromptRef,
    PromptResolve,
    load_stock,
    load_stock_document,
)
from arcprompt.document import (
    PromptDocument,
    PromptFrontmatter,
    parse_prompt,
    render_prompt,
)
from arcprompt.errors import (
    PromptError,
    PromptMissing,
    PromptUnparseable,
    PromptUnsigned,
)
from arcprompt.resolver import PromptResolver
from arcprompt.snapshot import PromptSnapshot, snapshot
from arcprompt.verifier import SignatureVerifier, TrustPosture

__version__ = "0.1.0"

__all__ = [
    "DEFAULT_PROMPT_PACKAGES",
    "PromptCatalog",
    "PromptDocument",
    "PromptError",
    "PromptFrontmatter",
    "PromptMissing",
    "PromptRef",
    "PromptResolve",
    "PromptResolver",
    "PromptSnapshot",
    "PromptUnparseable",
    "PromptUnsigned",
    "SignatureVerifier",
    "TrustPosture",
    "__version__",
    "load_stock",
    "load_stock_document",
    "parse_prompt",
    "render_prompt",
    "snapshot",
]
