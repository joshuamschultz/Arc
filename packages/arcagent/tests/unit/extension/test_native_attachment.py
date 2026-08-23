"""NativeAttachment — the direct-implementation shape of the hook (SPEC-062 COMP-006).

Some services have no acceptable protocol-server upstream — a vendor SDK client, or a
REST client where nothing vetted exists. ``NativeAttachment`` is how those still reach
the agent through the SAME :class:`ExtensionAttachment` hook (REQ-278, REQ-279) without
teaching core anything about them (REQ-264): it imports a dotted module named by the
extension's own entrypoint and calls a well-known factory attribute on it — the same
lazy-import-plus-fixed-attribute shape ``extension/select.py``'s ``_try_provider``
already uses for the select-one seams. The extension owns all service knowledge
(argv, auth headers, SDK calls, whatever); this file never learns any of it, and the
fake delegate below stands in for "any vendor client at all" to prove that.

Failure is fail-closed at every step: an unimportable entrypoint, a module missing the
factory, or a factory whose return value does not structurally satisfy
``ExtensionAttachment`` all refuse loudly (:class:`ExtensionError`) rather than handing
the agent a half-built attachment that would only fail later, mid-call.
"""

from __future__ import annotations

import sys
from types import ModuleType
from typing import Any

import pytest

from arcagent.core.errors import ExtensionError
from arcagent.extension.attachment import (
    ExtensionAttachment,
    ProbeResult,
    Requirement,
    RequirementKind,
    ToolOutcome,
    ToolResult,
    ToolSpec,
)
from arcagent.extension.native_attachment import NATIVE_ENTRYPOINT_ATTR, NativeAttachment
from arcagent.extension.source import (
    FetchSourceObject,
    InspectSource,
    SourceContent,
    SourceDescription,
    SyncSource,
    SyncSourcePage,
)

_ENTRYPOINT = "fake_native_extension_module"


class _FakeDelegate:
    """Stands in for "some extension's own vendor client" — arbitrary service
    knowledge that must never leak into ``NativeAttachment`` itself."""

    def __init__(self, context: dict[str, Any]) -> None:
        self.context = context
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def requirements(self) -> list[Requirement]:
        return [Requirement(kind=RequirementKind.CREDENTIAL, name="api_token")]

    async def probe(self) -> ProbeResult:
        return ProbeResult(reachable=True, tools=[ToolSpec(name="get_item")])

    async def describe_tools(self) -> list[ToolSpec]:
        return [ToolSpec(name="get_item", description="Fetch an item", classification="read_only")]

    async def invoke(self, tool: str, args: dict[str, Any]) -> ToolResult:
        self.calls.append((tool, dict(args)))
        return ToolResult(tool=tool, outcome=ToolOutcome.OK, content="42")


class _FakeSourceDelegate(_FakeDelegate):
    async def inspect_source(self, request: InspectSource) -> SourceDescription:
        return SourceDescription(
            connection_id=request.connection_id,
            source_kind="fake",
            account_id="account",
        )

    async def sync_source(self, request: SyncSource) -> SyncSourcePage:
        return SyncSourcePage(next_checkpoint=request.checkpoint or "initial")

    async def fetch_source(self, request: FetchSourceObject) -> SourceContent:
        return SourceContent(
            object_id=request.object_id,
            version=request.version,
            media_type="text/plain",
            content=b"body",
        )

    async def close_source(self) -> None:
        return None


def _register_module(
    monkeypatch: pytest.MonkeyPatch, factory: Any, *, name: str = _ENTRYPOINT
) -> None:
    """Puts a fake extension module in ``sys.modules`` under a dotted name.

    A real extension bundle ships a real file on disk; a module object registered
    directly in ``sys.modules`` is what ``importlib.import_module`` returns for one
    too, so this is a faithful stand-in without touching the filesystem.
    """
    module = ModuleType(name)
    if factory is not None:
        setattr(module, NATIVE_ENTRYPOINT_ATTR, factory)
    monkeypatch.setitem(sys.modules, name, module)


@pytest.fixture
def delegates(monkeypatch: pytest.MonkeyPatch) -> list[_FakeDelegate]:
    """Registers a working entrypoint and returns the list its factory appends to."""
    built: list[_FakeDelegate] = []

    def build_native_attachment(context: dict[str, Any]) -> _FakeDelegate:
        delegate = _FakeDelegate(context)
        built.append(delegate)
        return delegate

    _register_module(monkeypatch, build_native_attachment)
    return built


# --- the hook contract (REQ-278, REQ-279) -----------------------------------


def test_satisfies_the_extension_attachment_protocol(delegates: list[_FakeDelegate]) -> None:
    """A native attachment attaches through the same four methods as everything else."""
    attachment = NativeAttachment(_ENTRYPOINT, {})

    assert isinstance(attachment, ExtensionAttachment)


def test_the_entrypoint_module_is_imported_and_its_factory_called_once(
    delegates: list[_FakeDelegate],
) -> None:
    NativeAttachment(_ENTRYPOINT, {"instance": "work"})

    assert len(delegates) == 1
    assert delegates[0].context == {"instance": "work"}


def test_context_reaches_the_factory_unmodified(delegates: list[_FakeDelegate]) -> None:
    """Core hands the extension whatever it was given and interprets none of it."""
    context = {"agent_did": "did:arc:test", "instance": "work-jira"}

    NativeAttachment(_ENTRYPOINT, context)

    assert delegates[0].context == context


# --- delegation: every call reaches the extension's own implementation ------


async def test_requirements_delegates_to_the_extension(delegates: list[_FakeDelegate]) -> None:
    attachment = NativeAttachment(_ENTRYPOINT, {})

    requirements = attachment.requirements()

    assert [r.name for r in requirements] == ["api_token"]
    assert requirements[0].kind is RequirementKind.CREDENTIAL


async def test_probe_delegates_to_the_extension(delegates: list[_FakeDelegate]) -> None:
    attachment = NativeAttachment(_ENTRYPOINT, {})

    result = await attachment.probe()

    assert result.reachable is True
    assert [t.name for t in result.tools] == ["get_item"]


async def test_describe_tools_delegates_to_the_extension(delegates: list[_FakeDelegate]) -> None:
    attachment = NativeAttachment(_ENTRYPOINT, {})

    specs = await attachment.describe_tools()

    assert specs[0].name == "get_item"
    assert specs[0].classification == "read_only"


async def test_invoke_delegates_to_the_extension_and_returns_its_result(
    delegates: list[_FakeDelegate],
) -> None:
    attachment = NativeAttachment(_ENTRYPOINT, {})

    result = await attachment.invoke("get_item", {"id": "1"})

    assert result.outcome is ToolOutcome.OK
    assert result.content == "42"
    assert delegates[0].calls == [("get_item", {"id": "1"})]


def test_non_source_delegate_exposes_no_source_adapter(delegates: list[_FakeDelegate]) -> None:
    attachment = NativeAttachment(_ENTRYPOINT, {})

    assert attachment.source_adapter() is None


def test_source_delegate_is_exposed_through_the_generic_optional_seam(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _register_module(
        monkeypatch,
        lambda context: _FakeSourceDelegate(context),
        name="fake_source_extension",
    )

    attachment = NativeAttachment("fake_source_extension", {})

    assert isinstance(attachment.source_adapter(), _FakeSourceDelegate)


# --- fail-closed resolution (ASI04 — no half-built attachment) --------------


def test_an_unimportable_entrypoint_refuses(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delitem(sys.modules, "no_such_native_extension_module", raising=False)

    with pytest.raises(ExtensionError) as excinfo:
        NativeAttachment("no_such_native_extension_module", {})

    assert "no_such_native_extension_module" in str(excinfo.value)


def test_an_entrypoint_with_no_factory_attribute_refuses(monkeypatch: pytest.MonkeyPatch) -> None:
    _register_module(monkeypatch, None, name="no_factory_native_extension")

    with pytest.raises(ExtensionError) as excinfo:
        NativeAttachment("no_factory_native_extension", {})

    assert NATIVE_ENTRYPOINT_ATTR in str(excinfo.value)


def test_a_factory_returning_the_wrong_shape_refuses(monkeypatch: pytest.MonkeyPatch) -> None:
    _register_module(monkeypatch, lambda context: object(), name="wrong_shape_native_extension")

    with pytest.raises(ExtensionError) as excinfo:
        NativeAttachment("wrong_shape_native_extension", {})

    assert "wrong_shape_native_extension" in str(excinfo.value)


def test_no_partial_attachment_is_left_behind_on_refusal(monkeypatch: pytest.MonkeyPatch) -> None:
    """A refused resolution must not leave something that half-answers later calls."""
    _register_module(monkeypatch, None, name="no_factory_native_extension_2")

    try:
        NativeAttachment("no_factory_native_extension_2", {})
    except ExtensionError:
        pass
    else:
        pytest.fail("expected ExtensionError")
