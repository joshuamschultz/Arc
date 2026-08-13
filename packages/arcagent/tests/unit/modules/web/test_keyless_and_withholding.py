"""A web tool with no working provider must not register; extraction ships keyless.

Two failures, one root cause. Every scaffolded agent named ``tavily`` and
``firecrawl`` — paid services nobody had bought — so ``web_search`` and
``web_extract`` registered, the model saw them, called them, and each call died
on a missing API key. Turns burned on capabilities the deployment never had
(LLM06 excessive agency, LLM10 unbounded consumption).

The fix has two halves and both are exercised here against the real code paths:

* **Withholding.** A provider whose credential does not resolve means its tool
  is dropped from the module namespace before the capability loader sees it, so
  it reaches neither the tool registry nor the prompt manifest. Asserted through
  the real :class:`CapabilityLoader` over the real module directory and the real
  :func:`bridge_capability_tools_to_registry` into a real
  :class:`ToolRegistry` — not by inspecting the frozenset the decision produced.
* **Keyless extraction.** The default ``browser`` provider reads pages through
  the browser module's ``BrowserBackend``/``BrowserSession`` seam, so basic site
  lookup works with no account. Stubbed **at that seam** — no Chrome is launched
  and nothing reaches the network.

Credential resolution is pinned in both directions: the environment variable is
removed *and* the personal-tier secrets directory is redirected into ``tmp_path``,
so neither a developer's real key nor a real ``~/.arc/secrets`` file can decide
the outcome. A case that only deleted the env var would pass on a laptop and
fail on the box that actually has the file.
"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from arcagent.capabilities.capability_loader import CapabilityLoader
from arcagent.capabilities.capability_registry import CapabilityRegistry
from arcagent.core.agent_lifecycle import bridge_capability_tools_to_registry
from arcagent.core.config import ToolsConfig
from arcagent.core.module_bus import ModuleBus
from arcagent.core.tool_registry import ToolRegistry
from arcagent.modules.web import _runtime, capabilities
from arcagent.modules.web.errors import ExtractFailed
from arcagent.modules.web.providers.browser_page import BrowserPageProvider

_WEB_MODULE_DIR = Path(capabilities.__file__).parent

#: Every environment variable the module would accept a key from. All are
#: cleared per test so "the key is missing" is a fact about the run, not a fact
#: about the machine.
_PROVIDER_ENV_VARS = ("TAVILY_API_KEY", "FIRECRAWL_API_KEY", "PARALLEL_API_KEY")


@pytest.fixture(autouse=True)
def _no_ambient_credentials(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Neither the environment nor ``~/.arc/secrets`` may resolve a provider key."""
    for var in _PROVIDER_ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(
        "arcagent.core.vault.backends.file._DEFAULT_SECRETS_DIR", tmp_path / "no-secrets"
    )


@pytest.fixture(autouse=True)
def _reset_runtime() -> Any:
    _runtime.reset()
    yield
    _runtime.reset()


# --- Doubles ------------------------------------------------------------------


class _FakeSession:
    """A BrowserSession that answers CDP commands from a canned document."""

    def __init__(self, document: dict[str, Any] | None = None, fail_on: str = "") -> None:
        self.url = "ws://fake/devtools/page/1"
        self.calls: list[tuple[str, str, dict[str, Any] | None]] = []
        self._document = document if document is not None else _DOCUMENT
        self._fail_on = fail_on

    async def send(
        self, domain: str, method: str, params: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        self.calls.append((domain, method, params))
        if self._fail_on and method == self._fail_on:
            raise RuntimeError(f"CDP {domain}.{method} exploded")
        if (domain, method) == ("Runtime", "evaluate"):
            return {"result": {"type": "string", "value": json.dumps(self._document)}}
        return {}


class _FakeBackend:
    """A BrowserBackend handing out one :class:`_FakeSession`."""

    name = "cdp"

    def __init__(self, session: _FakeSession) -> None:
        self.session = session
        self.opened = 0
        self.closed = 0

    async def open(self) -> _FakeSession:
        self.opened += 1
        return self.session

    async def close(self) -> None:
        self.closed += 1


_DOCUMENT: dict[str, Any] = {
    "url": "https://example.com/page",
    "title": "Example Page",
    "text": "The quick brown fox.",
    "links": ["https://example.com/a", "https://example.com/b"],
}


def _patch_seam(backend: _FakeBackend) -> Any:
    """Stub ``build_backend`` — the browser module's own seam, nothing below it."""
    return patch(
        "arcagent.modules.browser.backends.build_backend",
        return_value=backend,
    )


# --- Real registration path ---------------------------------------------------


async def _registered_tool_names() -> set[str]:
    """Scan the real web module with the real loader; return what registered."""
    registry = CapabilityRegistry()
    loader = CapabilityLoader(scan_roots=[("module:web", _WEB_MODULE_DIR)], registry=registry)
    await loader.scan_and_register()
    return {entry.meta.name for entry in registry.tool_entries()}


async def _agent_tool_registry() -> ToolRegistry:
    """Scan, then bridge into a real ToolRegistry exactly as agent startup does."""
    capability_registry = CapabilityRegistry()
    loader = CapabilityLoader(
        scan_roots=[("module:web", _WEB_MODULE_DIR)], registry=capability_registry
    )
    await loader.scan_and_register()

    tool_registry = ToolRegistry(ToolsConfig(), ModuleBus(), MagicMock())
    agent = SimpleNamespace(
        _capability_registry=capability_registry,
        _tool_registry=tool_registry,
        _capability_tool_names=set[str](),
    )
    await bridge_capability_tools_to_registry(agent)  # type: ignore[arg-type]  # structural stand-in for ArcAgent
    return tool_registry


class TestMissingCredentialWithholdsTheTool:
    async def test_search_tool_is_not_registered_without_its_key(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.WARNING, logger="arcagent.modules.web._runtime"):
            _runtime.configure(
                config={"search_provider": "tavily", "extract_provider": "browser"},
                telemetry=MagicMock(),
                agent_name="a",
            )

        assert "web_search" not in await _registered_tool_names()
        # Degrade LOUDLY: the operator is told the tool AND the exact key.
        assert any(
            "web_search" in record.message and "TAVILY_API_KEY" in record.message
            for record in caplog.records
        ), caplog.text

    async def test_extract_tool_is_not_registered_without_its_paid_key(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.WARNING, logger="arcagent.modules.web._runtime"):
            _runtime.configure(
                config={"extract_provider": "firecrawl"},
                telemetry=MagicMock(),
                agent_name="a",
            )

        assert "web_extract" not in await _registered_tool_names()
        assert any(
            "web_extract" in record.message and "FIRECRAWL_API_KEY" in record.message
            for record in caplog.records
        ), caplog.text

    async def test_a_resolvable_key_does_register_the_tool(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Without this, "not registered" would also pass if nothing ever registered."""
        monkeypatch.setenv("TAVILY_API_KEY", "sk-live-not-a-real-key")
        _runtime.configure(
            config={"search_provider": "tavily"}, telemetry=MagicMock(), agent_name="a"
        )

        assert "web_search" in await _registered_tool_names()

    async def test_unset_search_provider_withholds_search_and_keeps_extract(self) -> None:
        """The shipped default: extraction works, search is simply absent."""
        _runtime.configure(config={}, telemetry=MagicMock(), agent_name="a")

        assert await _registered_tool_names() == {"web_extract"}

    async def test_withheld_tool_is_absent_from_a_real_agent_tool_registry(self) -> None:
        """The registry the model is actually offered tools from."""
        _runtime.configure(
            config={"search_provider": "tavily"}, telemetry=MagicMock(), agent_name="a"
        )

        registry = await _agent_tool_registry()

        assert "web_search" not in registry.tools
        assert "web_extract" in registry.tools
        assert "web_search" not in registry.format_for_prompt()


# --- Keyless extraction through the browser seam -------------------------------


class TestKeylessExtraction:
    async def test_provider_extracts_with_no_api_key(self) -> None:
        backend = _FakeBackend(_FakeSession())
        provider = BrowserPageProvider.create()

        with _patch_seam(backend):
            result = await provider.extract("https://example.com/page")

        assert result.title == "Example Page"
        assert result.content == "The quick brown fox."
        assert result.links == ["https://example.com/a", "https://example.com/b"]
        assert backend.opened == 1
        assert backend.closed == 1

    async def test_it_drives_the_page_through_the_session_seam(self) -> None:
        """Navigation and read go over ``BrowserSession.send``, not a second engine."""
        session = _FakeSession()
        backend = _FakeBackend(session)

        with _patch_seam(backend):
            await BrowserPageProvider.create().extract("https://example.com/page")

        assert ("Page", "navigate", {"url": "https://example.com/page"}) in session.calls
        assert any(
            domain == "Runtime" and method == "evaluate" for domain, method, _ in session.calls
        )

    async def test_the_browser_is_released_even_when_the_page_fails(self) -> None:
        backend = _FakeBackend(_FakeSession(fail_on="navigate"))

        with _patch_seam(backend), pytest.raises(ExtractFailed):
            await BrowserPageProvider.create().extract("https://example.com/page")

        assert backend.closed == 1

    async def test_web_extract_tool_runs_end_to_end_on_the_default_config(self) -> None:
        """Default config, no keys anywhere, and the tool returns page content."""
        _runtime.configure(config={}, telemetry=MagicMock(), agent_name="a")
        backend = _FakeBackend(_FakeSession())

        with _patch_seam(backend):
            payload = json.loads(await capabilities.web_extract("https://example.com/page"))

        assert payload["content"] == "The quick brown fox."
        assert payload["title"] == "Example Page"


# --- The browser module is optional --------------------------------------------


@pytest.fixture
def _browser_module_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make every ``arcagent.modules.browser`` import raise ImportError.

    ``None`` in ``sys.modules`` is what CPython treats as "this import is
    blocked". Set on the canonical dotted paths rather than only on keys that
    happen to be loaded already, so the block holds whether or not an earlier
    test imported the module.
    """
    for dotted in (
        "arcagent.modules.browser",
        "arcagent.modules.browser.backends",
        "arcagent.modules.browser.config",
    ):
        monkeypatch.setitem(sys.modules, dotted, None)  # type: ignore[misc]  # None blocks the import


@pytest.mark.usefixtures("_browser_module_absent")
class TestBrowserModuleAbsent:
    def test_the_web_module_still_imports(self) -> None:
        """`web` may not carry a hard dependency on `browser` — both are optional."""
        import importlib

        assert importlib.reload(
            importlib.import_module("arcagent.modules.web.providers.browser_page")
        )
        assert importlib.import_module("arcagent.modules.web").WebConfig is not None

    async def test_extraction_is_withheld_loudly_rather_than_offered(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.WARNING, logger="arcagent.modules.web._runtime"):
            _runtime.configure(config={}, telemetry=MagicMock(), agent_name="a")

        assert await _registered_tool_names() == set()
        assert any(
            "web_extract" in record.message and "browser" in record.message
            for record in caplog.records
        ), caplog.text

    async def test_calling_the_provider_directly_fails_with_a_typed_error(self) -> None:
        """Belt and braces: if the module vanishes after startup, the call says so."""
        with pytest.raises(ExtractFailed, match="browser module"):
            await BrowserPageProvider.create().extract("https://example.com/page")


# --- Tier controls apply to the new provider unchanged --------------------------


class TestTierControlsStillApply:
    def test_federal_still_refuses_an_empty_allowlist(self) -> None:
        """Unchanged by the new default — federal must name its destinations."""
        with pytest.raises(RuntimeError, match="url_allowlist"):
            _runtime.configure(
                config={"tier": "federal", "url_allowlist": []},
                telemetry=MagicMock(),
                agent_name="a",
            )

    async def test_federal_withholds_the_keyless_provider_without_a_remote_endpoint(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Federal forbids auto-launching a local Chrome, so the tool is not offered."""
        with caplog.at_level(logging.WARNING, logger="arcagent.modules.web._runtime"):
            _runtime.configure(
                config={"tier": "federal", "url_allowlist": ["https://ok.example.com/*"]},
                telemetry=MagicMock(),
                agent_name="a",
            )

        assert await _registered_tool_names() == set()
        assert any("web_extract" in record.message for record in caplog.records), caplog.text

    async def test_federal_with_a_remote_endpoint_does_register_extraction(self) -> None:
        _runtime.configure(
            config={
                "tier": "federal",
                "url_allowlist": ["https://ok.example.com/*"],
                "browser_cdp_url": "ws://sandboxed-browser:9222/devtools/browser/x",
            },
            telemetry=MagicMock(),
            agent_name="a",
        )

        assert await _registered_tool_names() == {"web_extract"}

    async def test_the_allowlist_blocks_the_browser_provider_before_it_opens(self) -> None:
        """A new provider must not become a way around the URL policy."""
        from arcagent.modules.web.errors import URLNotAllowed

        _runtime.configure(
            config={
                "tier": "federal",
                "url_allowlist": ["https://ok.example.com/*"],
                "browser_cdp_url": "ws://sandboxed-browser:9222/devtools/browser/x",
            },
            telemetry=MagicMock(),
            agent_name="a",
        )
        backend = _FakeBackend(_FakeSession())

        with _patch_seam(backend), pytest.raises(URLNotAllowed):
            await capabilities.web_extract("https://blocked.example.net/page")

        assert backend.opened == 0, "the browser opened for a URL the policy denied"

    async def test_the_size_cap_truncates_browser_content(self) -> None:
        _runtime.configure(
            config={"max_content_bytes": 1024},
            telemetry=MagicMock(),
            agent_name="a",
        )
        backend = _FakeBackend(_FakeSession({**_DOCUMENT, "text": "x" * 5000}))

        with _patch_seam(backend):
            payload = json.loads(await capabilities.web_extract("https://example.com/page"))

        assert len(payload["content"].encode("utf-8")) == 1024
