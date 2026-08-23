"""SPEC-062 COMP-006 — ``NativeAttachment``, the direct-implementation shape of the hook.

A protocol-server extension (COMP-005) reaches its service through a framed wire
protocol that already does the adapting. Some services have no acceptable upstream in
that shape at all — a vendor SDK that only ships a client library, or a REST API where
nothing vetted covers it — so their extension ships its own implementation instead.
``NativeAttachment`` is how that implementation still attaches through the SAME
:class:`~arcagent.extension.attachment.ExtensionAttachment` hook (REQ-278, REQ-279)
without arcagent ever learning what the service is (REQ-264).

The mechanism is the generic lazy-import-plus-well-known-factory shape
``arcagent.extension.select`` already uses for the select-one extension points
(``_try_provider``): the extension's own dotted entrypoint module is imported, and a
FIXED, well-known attribute on it — never a name that varies per extension — is called
with the caller's opaque context to build the extension's implementation. Fixed rather
than manifest-supplied because the point of the convention is that arcagent asks every
native-attachment extension for the exact same thing; a per-manifest attribute name
would just be a second dial with nothing to turn.

Resolution fails closed at each step (ASI04): an unimportable module, a module missing
the factory, or a factory whose return value does not structurally satisfy
``ExtensionAttachment`` all refuse before ``NativeAttachment`` is usable, rather than
handing the agent something that only breaks mid-call.
"""

from __future__ import annotations

import importlib
from typing import Any

from arcagent.core.errors import ExtensionError
from arcagent.extension.attachment import (
    ExtensionAttachment,
    ProbeResult,
    Requirement,
    ToolResult,
    ToolSpec,
)
from arcagent.extension.source import SourceAdapter

#: The well-known factory attribute every native-attachment extension module exposes:
#: ``def build_native_attachment(context: dict[str, Any]) -> ExtensionAttachment``.
#: Mirrors the fixed-attribute convention ``extension/select.py``'s provider path uses.
NATIVE_ENTRYPOINT_ATTR = "build_native_attachment"


class NativeAttachment:
    """Adapts an extension's own implementation to the ``ExtensionAttachment`` hook.

    All four Protocol methods simply forward to whatever the extension's factory
    built — this class owns no service knowledge and makes no calls of its own.
    """

    def __init__(self, entrypoint: str, context: dict[str, Any]) -> None:
        """Resolve ``entrypoint`` immediately so a bad extension refuses at load time.

        Args:
            entrypoint: The extension-declared dotted module path to import.
            context: Opaque data handed to the extension's factory unmodified —
                arcagent interprets none of it.

        Raises:
            ExtensionError: The module cannot be imported, exposes no
                ``build_native_attachment`` factory, or that factory's return value
                does not satisfy :class:`ExtensionAttachment`.
        """
        self._entrypoint = entrypoint
        self._delegate = _resolve(entrypoint, context)

    def requirements(self) -> list[Requirement]:
        return self._delegate.requirements()

    async def probe(self) -> ProbeResult:
        return await self._delegate.probe()

    async def describe_tools(self) -> list[ToolSpec]:
        return await self._delegate.describe_tools()

    async def invoke(self, tool: str, args: dict[str, Any]) -> ToolResult:
        return await self._delegate.invoke(tool, args)

    def source_adapter(self) -> SourceAdapter | None:
        """Expose optional source synchronization without widening the tool hook.

        Interactive tools remain the universal attachment contract. A native
        extension that also implements the independent source contract can be
        enrolled by the optional connected-data module; every other extension
        remains unaffected and removable.
        """
        return self._delegate if isinstance(self._delegate, SourceAdapter) else None


def _resolve(entrypoint: str, context: dict[str, Any]) -> ExtensionAttachment:
    """Import ``entrypoint`` and build its delegate, refusing at the first failure."""
    try:
        module = importlib.import_module(entrypoint)
    except ImportError as exc:
        raise ExtensionError(
            code="EXTENSION_REFUSED",
            message=f"native attachment entrypoint {entrypoint!r} could not be imported: {exc}",
            details={"reason": "entrypoint_import_failed", "entrypoint": entrypoint},
        ) from exc
    factory = getattr(module, NATIVE_ENTRYPOINT_ATTR, None)
    if factory is None:
        raise ExtensionError(
            code="EXTENSION_REFUSED",
            message=f"{entrypoint!r} exposes no {NATIVE_ENTRYPOINT_ATTR!r} factory",
            details={"reason": "entrypoint_missing_factory", "entrypoint": entrypoint},
        )
    instance = factory(context)
    if not isinstance(instance, ExtensionAttachment):
        raise ExtensionError(
            code="EXTENSION_REFUSED",
            message=(
                f"{entrypoint!r}.{NATIVE_ENTRYPOINT_ATTR} did not return an ExtensionAttachment"
            ),
            details={"reason": "entrypoint_wrong_shape", "entrypoint": entrypoint},
        )
    return instance


__all__ = ["NATIVE_ENTRYPOINT_ATTR", "NativeAttachment"]
