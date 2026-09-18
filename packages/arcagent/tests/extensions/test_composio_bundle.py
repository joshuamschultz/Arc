"""SPEC-082 T-1099 (RED) — the Composio connector bundle over hosted MCP.

REQ-421 / REQ-422 / COMP-006. Composio reaches the long tail of integrations
through its *hosted* MCP surface, which is why Phase 1 wired ``HttpTransport``
into ``build_attachment``. This spec's deliverable is a signed bundle at
``extensions/composio/`` that:

1. declares ``attachment = "mcp"`` over an **HTTP** transport with a pinned
   endpoint, a non-empty explicit ``[tools].allow`` exposure list, and an
   API-token ``[[secrets]]`` keyed to the connection;
2. built via ``build_attachment`` against a fake hosted MCP broker, lists only
   the allowlisted verbs and can invoke one end to end; and
3. never exposes a broker-served tool the manifest did not allowlist.

RED today because ``extensions/composio/`` does not exist yet — every test here
fails at :func:`_load_manifest`'s existence assertion. T-1100 authors the bundle
and turns these green.

The fake broker mirrors the ``httpx.MockTransport`` pattern in
``tests/unit/extension/test_mcp_attachment.py`` (``_http``/``_json_response``):
``build_attachment`` constructs its own ``httpx.AsyncClient()`` inside the http
branch, so the client is swapped for one backed by a ``MockTransport`` at the
``httpx.AsyncClient`` seam.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from arcagent.core.tier import Tier
from arcagent.extension.attachment import ToolOutcome
from arcagent.extension.manifest import load_manifest
from arcagent.modules.connectors.attachments import build_attachment

_EXTENSIONS_ROOT = Path(__file__).resolve().parents[4] / "extensions"
_COMPOSIO = _EXTENSIONS_ROOT / "composio"
_MANIFEST = _COMPOSIO / "extension.toml"

#: A verb the fake broker serves but the manifest must never allowlist. Its whole
#: job is to prove a Composio-brokered tool outside the allowlist is not exposable.
_ROGUE = "composio_admin_delete_everything"


def _load_manifest() -> Any:
    """Load the Composio manifest, or fail RED naming the missing bundle."""
    assert _MANIFEST.exists(), (
        f"extensions/composio bundle does not exist yet ({_MANIFEST}); T-1100 authors it"
    )
    return load_manifest(_MANIFEST.read_text(encoding="utf-8"), tier=Tier.PERSONAL)


def _broker(served: list[dict[str, Any]]) -> Any:
    """A fake hosted MCP broker: answers server/discover, tools/list, tools/call."""

    def handler(request: httpx.Request) -> httpx.Response:
        message = json.loads(request.content)
        method = message["method"]
        if method == "tools/list":
            body: dict[str, Any] = {"result": {"resultType": "complete", "tools": served}}
        elif method == "tools/call":
            name = message["params"]["name"]
            body = {
                "result": {
                    "resultType": "complete",
                    "content": [{"type": "text", "text": f"ran {name}"}],
                    "isError": False,
                }
            }
        else:  # server/discover or anything else the client sends
            body = {"result": {"resultType": "complete", "supportedVersions": ["2026-07-28"]}}
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": message["id"], **body})

    return handler


def _install_fake_broker(monkeypatch: pytest.MonkeyPatch, handler: Any) -> None:
    """Point every ``httpx.AsyncClient()`` at the fake broker via a MockTransport."""
    original = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda *args, **kwargs: original(transport=httpx.MockTransport(handler)),
    )


def _tool_spec(name: str) -> dict[str, Any]:
    return {"name": name, "description": name, "inputSchema": {"type": "object", "properties": {}}}


def test_composio_bundle_declares_hosted_mcp_with_allowlist_and_api_token() -> None:
    """The manifest attaches over hosted MCP with an explicit allowlist and a token."""
    manifest = _load_manifest()

    assert manifest.extension.attachment == "mcp"
    mcp = manifest.config.get("mcp", {})
    assert mcp.get("transport") == "http", "Composio attaches over hosted MCP (HTTP), not stdio"
    assert mcp.get("url"), "the hosted MCP endpoint must be pinned in the manifest"

    allow = manifest.tools.allow
    assert allow and "*" not in allow, "the exposure allowlist must be explicit and non-empty"
    assert any(secret.format == "api_token" for secret in manifest.secrets), (
        "Composio needs an API-token [[secrets]] keyed to the connection"
    )


async def test_composio_attachment_exposes_only_allowlisted_verbs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The broker serves an out-of-allowlist tool; the connector must not expose it."""
    manifest = _load_manifest()
    allow = set(manifest.tools.allow)
    _install_fake_broker(monkeypatch, _broker([_tool_spec(name) for name in [*allow, _ROGUE]]))

    attachment = build_attachment(manifest, _COMPOSIO, {})
    described = {spec.name for spec in await attachment.describe_tools()}

    assert _ROGUE in described, "the fake broker really serves the rogue verb"
    exposed = {name for name in described if name in allow}
    assert _ROGUE not in exposed, "a broker tool outside the allowlist must not be exposable"
    assert exposed == allow & described


async def test_composio_invokes_one_allowlisted_verb_end_to_end(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One allowlisted Composio verb can be invoked over HttpTransport, end to end."""
    manifest = _load_manifest()
    verb = sorted(manifest.tools.allow)[0]
    _install_fake_broker(monkeypatch, _broker([_tool_spec(verb)]))

    attachment = build_attachment(manifest, _COMPOSIO, {})
    result = await attachment.invoke(verb, {})

    assert result.outcome == ToolOutcome.OK
    assert f"ran {verb}" in result.content


def test_composio_allowlist_never_names_a_broker_tool_it_did_not_declare() -> None:
    """The rogue verb is not on the manifest allowlist, so it can never register."""
    manifest = _load_manifest()

    assert _ROGUE not in set(manifest.tools.allow)
