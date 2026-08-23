"""SPEC-062 COMP-012 — attachment tool specs become named, governed capabilities.

This is the whole cost of riding the existing governance envelope: convert each
:class:`~arcagent.extension.attachment.ToolSpec` into a
:class:`~arcagent.core.tool_registry.RegisteredTool` and register it. Everything a
connector call must survive — schema validation, the signed ``ToolCall`` bearing the
caller DID, the policy pipeline under the admission lock, the human gate on a
forbidden composition, the timeout, and the audit emission — already lives in
``ToolRegistry._create_wrapped_execute`` and is inherited rather than re-implemented
(REQ-267). No second dispatch path exists to bypass.

Three properties make that inheritance worth anything:

* **One tool, one name** (REQ-266). Registering a generic ``connector_call`` verb
  would collapse every distinct action into one policy subject, and a rule about it
  could say nothing more precise than "the connector may act". A per-verb deny is
  only expressible because ``create_issue`` and ``delete_issue`` are separate
  registry entries.
* **Classification and tags travel with the tool** (REQ-269). ``capability_tags``
  are what ``legs_for_call`` reads to resolve a call's trifecta legs, so losing them
  in translation silently opens the lethal-trifecta gate. An absent classification
  defaults to ``state_modifying`` in :class:`ToolSpec` itself, so silence never buys
  a tool the parallel-dispatch fast path.
* **Two allowlists compose; deny wins** (REQ-268/REQ-266). The manifest's per-server
  allowlist bounds what this extension may offer at all; the agent's existing
  ``ToolConfig.allow``/``deny`` bounds what this deployment permits. A tool must
  clear both, and the agent's deny list overrides either allowlist because
  ``ToolRegistry.register`` checks deny first. That filter is not duplicated here —
  registration is attempted and the registry's own verdict is read back, which is
  also why a policy-denied tool is skipped with the registry's ``tool.policy_denied``
  audit event instead of crashing a least-privilege agent at startup.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Coroutine, Iterable, Sequence
from dataclasses import dataclass
from typing import Any

from arcagent.core.tool_registry import RegisteredTool, ToolRegistry, ToolTransport
from arcagent.extension.attachment import (
    ExtensionAttachment,
    ToolOutcome,
    ToolResult,
    ToolSpec,
)
from arcagent.tools._egress_policy import extension_root

_logger = logging.getLogger("arcagent.extension.bridge")

#: The allowlist entry asking for whatever the upstream serves. An omitted allowlist
#: means the same thing; :func:`~arcagent.extension.manifest.load_manifest` already
#: refuses either above personal tier (REQ-268).
_WILDCARD = "*"


@dataclass(frozen=True)
class BridgeReport:
    """The outcome of registering one attachment's tools.

    Attributes:
        registered: Names now callable as individual capabilities.
        denied: Names an allowlist or deny list excluded. Informational — the
            operator asked for exactly this, so it is never an error.
    """

    registered: tuple[str, ...] = ()
    denied: tuple[str, ...] = ()


class CapabilityBridge:
    """Registers one attachment's tools as individually named capabilities.

    Args:
        registry: The agent's tool registry — the sole owner of the dispatch
            envelope these tools will ride.
        attachment: The live connection each registered verb dispatches to.
        transport: How the attachment is reached (``PROCESS`` for a CLI-hosted
            server), recorded on every tool it supplies.
        source: The catalog identity of the supplying extension, so an operator
            reading the tool catalog can see which extension gave the agent a verb.
        allow: The manifest's per-server allowlist. ``None`` or a list containing
            ``"*"`` places no manifest-side bound; the agent's own tool policy
            still applies.
    """

    def __init__(
        self,
        *,
        registry: ToolRegistry,
        attachment: ExtensionAttachment,
        transport: ToolTransport,
        source: str,
        allow: Sequence[str] | None = None,
    ) -> None:
        self._registry = registry
        self._attachment = attachment
        self._transport = transport
        self._source = source
        self._allow: frozenset[str] | None = (
            None if allow is None or _WILDCARD in allow else frozenset(allow)
        )

    def register(self, specs: Iterable[ToolSpec]) -> BridgeReport:
        """Register every allowed spec under its own name.

        Args:
            specs: The tools the attachment offers. An empty list is a live
                connection with nothing to offer, not an error.

        Returns:
            Which names became capabilities and which an allowlist or deny list
            excluded.
        """
        registered: list[str] = []
        denied: list[str] = []
        for spec in specs:
            if self._allow is not None and spec.name not in self._allow:
                _logger.debug(
                    "manifest allowlist excluded tool %r from %s", spec.name, self._source
                )
                denied.append(spec.name)
                continue
            tool = self._to_registered_tool(spec)
            self._registry.register(tool)
            # Read the registry's verdict rather than re-deriving it: the deny/allow
            # semantics stay in the one place that owns them.
            if self._registry.tools.get(spec.name) is tool:
                registered.append(spec.name)
            else:
                denied.append(spec.name)
        return BridgeReport(registered=tuple(registered), denied=tuple(denied))

    def replace_owned(self, owned_names: set[str], specs: Iterable[ToolSpec]) -> BridgeReport:
        """Atomically replace one attachment's governed tool contribution."""
        replacements: list[RegisteredTool] = []
        denied: list[str] = []
        for spec in specs:
            if self._allow is not None and spec.name not in self._allow:
                denied.append(spec.name)
                continue
            replacements.append(self._to_registered_tool(spec))
        accepted = self._registry.replace_owned(owned_names, replacements)
        registered = tuple(tool.name for tool in replacements if tool.name in accepted)
        denied.extend(tool.name for tool in replacements if tool.name not in accepted)
        return BridgeReport(registered=registered, denied=tuple(denied))

    def _to_registered_tool(self, spec: ToolSpec) -> RegisteredTool:
        """Translate one spec, carrying the fields the trifecta gate reads."""
        return RegisteredTool(
            name=spec.name,
            description=spec.description,
            input_schema=dict(spec.input_schema),
            transport=self._transport,
            execute=self._dispatcher(spec.name),
            source=self._source,
            # Everything this bridge registers came out of an extension bundle, so
            # the origin the egress gate reads is structural here rather than a
            # string convention the caller has to remember to get right (D-580).
            scan_root=extension_root(self._source),
            classification=spec.classification,
            capability_tags=list(spec.capability_tags),
        )

    def _dispatcher(self, tool: str) -> Callable[..., Coroutine[Any, Any, str]]:
        """Build the executor for one verb.

        A factory, not a closure written inline in the loop: sharing one closure over
        a loop variable would route every registered tool to the last spec's name.
        """
        attachment = self._attachment

        async def execute(**args: Any) -> str:
            return _render(await attachment.invoke(tool, args))

        return execute


def _render(result: ToolResult) -> str:
    """Render a result for the model, keeping a non-success visibly non-success.

    An ``INPUT_REQUIRED`` round trip returning bare content would read as a completed
    call, so the outcome leads whenever it is not ``OK``.
    """
    if result.outcome is ToolOutcome.OK:
        return result.content
    return f"{result.outcome.value}: {result.content}"


__all__ = ["BridgeReport", "CapabilityBridge"]
