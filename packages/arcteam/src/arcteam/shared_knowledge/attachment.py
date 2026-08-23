"""ArcAgent extension attachment for fleet shared-knowledge capabilities."""

from __future__ import annotations

from typing import Any, Protocol

from arcteam.shared_knowledge.service import (
    FleetSharedKnowledgeService,
    SharedKnowledgeUnavailableError,
)


class _Access(Protocol):
    caller_did: str
    clearance: str


class _PersonalKnowledge(Protocol):
    async def export_for_promotion(self, reference: str, access: _Access) -> object: ...


class _Signer(Protocol):
    @property
    def public_key(self) -> bytes: ...

    @property
    def algorithm(self) -> str: ...

    def sign(self, message: bytes) -> bytes: ...


class SharedKnowledgeAttachment:
    """A public ArcAgent extension attachment, with no ArcAgent core dependency."""

    def __init__(
        self,
        service: FleetSharedKnowledgeService,
        *,
        personal_knowledge: _PersonalKnowledge,
        access: _Access,
        signer: _Signer,
        audit_sink: Any = None,
    ) -> None:
        self._service = service
        self._personal = personal_knowledge
        self._access = access
        self._signer = signer
        self._audit_sink = audit_sink

    def requirements(self) -> list[Any]:
        return []

    async def probe(self) -> Any:
        from arcagent.extension.attachment import ProbeResult

        return ProbeResult(reachable=True, tools=await self.describe_tools())

    async def describe_tools(self) -> list[Any]:
        from arcagent.extension.attachment import ToolSpec

        return [
            ToolSpec(
                name="shared_knowledge_promote",
                description="Promote owned personal knowledge to the signed fleet collection.",
                input_schema=_schema({"reference": {"type": "string"}}, ["reference"]),
                capability_tags=["knowledge", "fleet"],
            ),
            ToolSpec(
                name="shared_knowledge_retrieve",
                description="Retrieve one authorized fleet knowledge document.",
                input_schema=_schema({"reference": {"type": "string"}}, ["reference"]),
                classification="read_only",
                capability_tags=["knowledge", "fleet"],
            ),
            ToolSpec(
                name="shared_knowledge_search",
                description="Search fleet knowledge visible at the caller's clearance.",
                input_schema=_schema({"query": {"type": "string"}}, ["query"]),
                classification="read_only",
                capability_tags=["knowledge", "fleet"],
            ),
            ToolSpec(
                name="shared_knowledge_revoke",
                description="Revoke an owned fleet knowledge document.",
                input_schema=_schema({"reference": {"type": "string"}}, ["reference"]),
                capability_tags=["knowledge", "fleet"],
            ),
        ]

    async def invoke(self, tool: str, args: dict[str, Any]) -> Any:
        from arcagent.extension.attachment import ToolOutcome, ToolResult

        try:
            if tool == "shared_knowledge_promote":
                result: Any = await self._service.promote(
                    self._personal,
                    _required(args, "reference"),
                    self._access,
                    self._signer,
                    audit_sink=self._audit_sink,
                )
                content = f"Promoted shared knowledge {result.identifier}."
            elif tool == "shared_knowledge_retrieve":
                document = await self._service.read(_required(args, "reference"), self._access)
                content = f"# {document.title}\n\n{document.content}"
            elif tool == "shared_knowledge_search":
                hits = await self._service.search(_required(args, "query"), self._access)
                content = (
                    "\n".join(
                        f"- {hit.reference.identifier}: {hit.title} — {hit.excerpt}"
                        for hit in hits
                    )
                    or "No shared knowledge results found."
                )
            elif tool == "shared_knowledge_revoke":
                reference = _required(args, "reference")
                await self._service.revoke(reference, self._access)
                content = f"Revoked shared knowledge {reference}."
            else:
                return ToolResult(
                    tool=tool, outcome=ToolOutcome.ERROR, content="unknown shared tool"
                )
        except SharedKnowledgeUnavailableError as error:
            return ToolResult(tool=tool, outcome=ToolOutcome.ERROR, content=str(error))
        except (FileNotFoundError, PermissionError, ValueError) as error:
            return ToolResult(tool=tool, outcome=ToolOutcome.ERROR, content=str(error))
        return ToolResult(tool=tool, content=content)


def _schema(properties: dict[str, object], required: list[str]) -> dict[str, object]:
    return {"type": "object", "properties": properties, "required": required}


def _required(args: dict[str, Any], field: str) -> str:
    value = args.get(field)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} is required")
    return value


__all__ = ["SharedKnowledgeAttachment"]
