"""Explicit-scope LLM tools over injected knowledge ports."""

from __future__ import annotations

import json
from typing import Literal

from arcagent.knowledge import KnowledgeDraft, KnowledgePort
from arcagent.modules.knowledge import _runtime
from arcagent.tools import tool


def _port(scope: Literal["personal", "shared"]) -> KnowledgePort:
    current = _runtime.state()
    selected = current.personal if scope == "personal" else current.shared
    if selected is None:
        raise RuntimeError(f"{scope} knowledge is unavailable")
    return selected


@tool(
    name="knowledge_save", description="Save curated knowledge to an explicit scope."
)
async def knowledge_save(
    scope: Literal["personal", "shared"], title: str, content: str
) -> str:
    """Save content only to the requested scope; missing scopes never fall back."""
    current = _runtime.state()
    reference = await _port(scope).save(
        KnowledgeDraft(title=title, content=content, classification=current.access.clearance), current.access
    )
    return json.dumps(
        {"scope": reference.scope, "identifier": reference.identifier, "digest": reference.digest}
    )


@tool(
    name="knowledge_read",
    description="Read curated knowledge from an explicit scope.",
    classification="read_only",
)
async def knowledge_read(scope: Literal["personal", "shared"], reference: str) -> str:
    """Read a document using runtime identity, not model-provided authority."""
    current = _runtime.state()
    document = await _port(scope).read(reference, current.access)
    return json.dumps(
        {
            "title": document.title,
            "content": document.content,
            "classification": document.classification,
        }
    )


@tool(
    name="knowledge_search",
    description="Search curated knowledge in an explicit scope.",
    classification="read_only",
)
async def knowledge_search(scope: Literal["personal", "shared"], query: str) -> str:
    """Search exactly one selected scope."""
    current = _runtime.state()
    hits = await _port(scope).search(query, current.access)
    return json.dumps(
        [
            {"identifier": hit.reference.identifier, "title": hit.title, "excerpt": hit.excerpt}
            for hit in hits
        ]
    )
