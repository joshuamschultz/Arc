"""SPEC-082 abuse battery — a Composio broker cannot smuggle a tool past the allowlist.

Registered in the cross-package adversarial battery
(``tests/run_adversarial_tests.py``). Composio is third-party code holding live
credentials (LLM03/ASI04 — the top supply-chain risk on this seam). A compromised
or misbehaving broker will happily *serve* a tool the manifest never named. This
abuse case proves that a broker-served tool outside the manifest ``[tools].allow``
list is **never registered as a callable capability** — the door/bridge refuses to
expose it, so policy and audit never even see a dispatcher for it.

It drives the SHIPPED path end to end: the real ``extensions/composio`` manifest,
the real ``build_attachment`` over the ``mcp`` SDK against a fake hosted broker, and
the real :class:`~arcagent.extension.bridge.CapabilityBridge` registration gate (the
production boundary that turns served specs into named capabilities). The positive
control — an allowlisted verb *is* registered and reaches the broker — proves the
deny is the allowlist doing its job, not a bridge that registers nothing.

SPEC-084 re-bases the wire on the SDK, so the broker is a real in-memory SDK server
(:func:`build_named_mcp_server`) reached through the one production factory
:meth:`SdkMcpClient.for_http` — the abuse under test (a broker serving a tool the
manifest never allowlisted) is proven against the actual protocol, not a stub.
"""

from __future__ import annotations

import contextlib
import sys
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from arcagent.core.config import ToolConfig, ToolsConfig
from arcagent.core.module_bus import ModuleBus
from arcagent.core.tier import Tier
from arcagent.core.tool_registry import ToolRegistry, ToolTransport
from arcagent.extension.bridge import CapabilityBridge
from arcagent.extension.manifest import load_manifest
from arcagent.extension.mcp_attachment import SdkMcpClient
from arcagent.modules.connectors.attachments import build_attachment

# The SDK-server fixture lives beside the SDK-client tests; put it on the path the
# same way the Microsoft e2e reaches its bundle, so this cross-suite import works
# regardless of the rootdir pytest is invoked from.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "unit" / "extension"))

from _fake_mcp_server import (
    build_named_mcp_server,
    connected_session_factory,
)

_EXTENSIONS_ROOT = Path(__file__).resolve().parents[4] / "extensions"
_COMPOSIO = _EXTENSIONS_ROOT / "composio"
_MANIFEST = _COMPOSIO / "extension.toml"

#: A verb the fake broker serves but the manifest must never allowlist.
_ROGUE = "composio_admin_delete_everything"


class _NoopSpan:
    def set_attribute(self, *args: Any, **kwargs: Any) -> None: ...
    def add_event(self, *args: Any, **kwargs: Any) -> None: ...
    def record_exception(self, *args: Any, **kwargs: Any) -> None: ...
    def set_status(self, *args: Any, **kwargs: Any) -> None: ...


class _RecordingTelemetry:
    """A real telemetry surface (not a mock, which would answer any attribute)."""

    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, Any]]] = []

    def audit_event(self, event_type: str, details: dict[str, Any]) -> None:
        self.events.append((event_type, details))

    @contextlib.asynccontextmanager
    async def tool_span(self, tool_name: str, args: dict[str, Any]) -> AsyncIterator[_NoopSpan]:
        yield _NoopSpan()


def _load_manifest() -> Any:
    assert _MANIFEST.exists(), f"extensions/composio bundle is missing ({_MANIFEST})"
    return load_manifest(_MANIFEST.read_text(encoding="utf-8"), tier=Tier.PERSONAL)


def _install_fake_broker(monkeypatch: pytest.MonkeyPatch, served: list[str]) -> None:
    """Wire the http session factory to a real in-memory SDK broker serving ``served``.

    ``build_attachment`` builds the client through :meth:`SdkMcpClient.for_http`;
    replacing that one factory drives the real SDK handshake against the fixture
    while the manifest, the allowlist, and the bridge stay exactly as they ship.
    """
    server = build_named_mcp_server(served)

    def _for_http(
        *,
        url: str,
        headers: Any = None,
        tools: Any = None,
        resilience: Any = None,
        client_name: str = "arc",
        requirements: Any = None,
    ) -> SdkMcpClient:
        return SdkMcpClient(
            connected_session_factory(server),
            tools=tools,
            resilience=resilience,
            client_name=client_name,
            requirements=requirements,
        )

    monkeypatch.setattr(SdkMcpClient, "for_http", staticmethod(_for_http))


def _registry() -> ToolRegistry:
    return ToolRegistry(
        config=ToolsConfig(policy=ToolConfig()), bus=ModuleBus(), telemetry=_RecordingTelemetry()
    )


async def test_broker_served_rogue_tool_is_never_registered_or_invocable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The broker serves both the allowlisted verbs and a rogue verb. Through the
    real manifest-bounded bridge, the rogue is DENIED and never becomes a callable
    capability; the allowlisted verbs register and reach the broker (positive control).
    """
    manifest = _load_manifest()
    allow = list(manifest.tools.allow)
    assert allow and "*" not in allow, (
        "the Composio manifest must carry an explicit, non-* allowlist"
    )

    _install_fake_broker(monkeypatch, [*allow, _ROGUE])
    attachment = build_attachment(manifest, _COMPOSIO, {})

    served = {spec.name for spec in await attachment.describe_tools()}
    assert _ROGUE in served, "positive control: the fake broker really serves the rogue verb"

    registry = _registry()
    bridge = CapabilityBridge(
        registry=registry,
        attachment=attachment,
        transport=ToolTransport.PROCESS,
        source="extension:composio",
        allow=list(manifest.tools.allow),
    )
    report = bridge.register([spec for spec in await attachment.describe_tools()])

    # The abuse is refused: the rogue verb is denied, never registered, never callable.
    assert _ROGUE in report.denied, "the manifest allowlist must exclude the rogue verb"
    assert _ROGUE not in report.registered
    assert _ROGUE not in registry.tools, (
        "a denied broker tool must have no dispatcher — not invocable"
    )

    # Positive control: the allowlisted verbs DID register and reach the broker.
    assert set(report.registered) == set(allow)
    a_verb = sorted(allow)[0]
    assert a_verb in registry.tools
    rendered = await registry.tools[a_verb].execute()
    assert f"ran {a_verb}" in rendered, "an allowlisted verb must dispatch through to the broker"


def test_composio_manifest_never_names_the_rogue_verb() -> None:
    """Belt-and-suspenders: the rogue verb is not on the shipped manifest allowlist,
    so no broker response can make it registrable in the first place.
    """
    manifest = _load_manifest()
    assert _ROGUE not in set(manifest.tools.allow)
