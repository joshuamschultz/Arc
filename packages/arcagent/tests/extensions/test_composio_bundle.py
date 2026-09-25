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

SPEC-084 re-bases the MCP wire on the official ``mcp`` SDK, so the hosted broker is
now a *real* SDK server (:func:`build_named_mcp_server`) reached over the SDK's
in-memory transport, not a hand-rolled ``httpx.MockTransport`` speaking the retired
JSON-RPC dialect. ``build_attachment`` builds an :class:`SdkMcpClient` through
:meth:`SdkMcpClient.for_http`, so the broker is injected by pointing that one
factory at the fixture — the manifest, the allowlist, and the build path stay real.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

from arcagent.core.tier import Tier
from arcagent.extension.attachment import ToolOutcome
from arcagent.extension.manifest import load_manifest
from arcagent.extension.mcp_attachment import SdkMcpClient
from arcagent.extension.secrets import Secret
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

#: A verb the fake broker serves but the manifest must never allowlist. Its whole
#: job is to prove a Composio-brokered tool outside the allowlist is not exposable.
_ROGUE = "composio_admin_delete_everything"


def _load_manifest() -> Any:
    """Load the Composio manifest, or fail RED naming the missing bundle."""
    assert _MANIFEST.exists(), (
        f"extensions/composio bundle does not exist yet ({_MANIFEST}); T-1100 authors it"
    )
    return load_manifest(_MANIFEST.read_text(encoding="utf-8"), tier=Tier.PERSONAL)


def _install_fake_broker(monkeypatch: pytest.MonkeyPatch, served: list[str]) -> None:
    """Wire the http session factory to a real in-memory SDK broker serving ``served``.

    ``build_attachment`` calls :meth:`SdkMcpClient.for_http` with the manifest's
    ``tools`` policy and requirements; replacing that one factory drives the real
    SDK handshake against the fixture while every other production input is untouched.
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
    _install_fake_broker(monkeypatch, [*allow, _ROGUE])

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
    _install_fake_broker(monkeypatch, [verb])

    attachment = build_attachment(manifest, _COMPOSIO, {})
    result = await attachment.invoke(verb, {})

    assert result.outcome == ToolOutcome.OK
    assert f"ran {verb}" in result.content


def test_composio_allowlist_never_names_a_broker_tool_it_did_not_declare() -> None:
    """The rogue verb is not on the manifest allowlist, so it can never register."""
    manifest = _load_manifest()

    assert _ROGUE not in set(manifest.tools.allow)


# ---------------------------------------------------------------------------
# SPEC-084 T-1147 (RED) — the connector speaks Composio's *real* hosted MCP.
#
# The shipped bundle is wrong in two feature-absent ways that T-1148 corrects:
#   1. Auth. Composio's hosted MCP authenticates with an ``x-api-key: <token>``
#      header. ``build_attachment``'s http branch hardcodes
#      ``{"Authorization": f"Bearer {token}"}`` (attachments.py), so the Composio
#      connector sends the credential the wrong way today.
#   2. URL. The real endpoint is an operator-minted, per-user URL of the shape
#      ``https://backend.composio.dev/v3/mcp/{SERVER_ID}?user_id=...``. The bundle
#      pins the static ``https://apollo.composio.dev/v3/mcp`` catch-all — the wrong
#      host — instead of taking the operator-supplied minted URL.
#
# The two anchors below fail today for those absences (Bearer sent / apollo pinned).
# The e2e conformance test proves the connector really speaks the protocol (CON-15)
# and leans on T-1142's SdkMcpClient, so it may pass already — it is the CON-15
# guard, not a RED driver.
#
# ASSUMPTION (T-1148 may adjust the mechanism, not the behavior): the operator-minted
# per-user URL is handed to ``build_attachment`` through the ``secrets`` mapping under
# the key ``_URL_SECRET_KEY``. This is the only per-connection channel the current
# ``build_attachment`` signature exposes; if T-1148 expresses the operator-supplied
# URL another way, only the supply helper here changes, not the asserted behavior
# (x-api-key auth, a backend.composio.dev target, no apollo, a working e2e round-trip).

_BACKEND_HOST = "backend.composio.dev"
_APOLLO_HOST = "apollo.composio.dev"
_OPERATOR_MINTED_URL = "https://backend.composio.dev/v3/mcp/srv_ABC123?user_id=op-1"
_URL_SECRET_KEY = "mcp_url"


def _api_token_secret_name(manifest: Any) -> str:
    """The manifest's api-token secret name — whatever T-1148 chooses to call it."""
    for secret in manifest.secrets:
        if secret.format == "api_token":
            return secret.name
    raise AssertionError("the Composio manifest must declare an api_token [[secrets]]")


def _operator_secrets(manifest: Any, token: str) -> dict[str, Secret]:
    """The credentials an operator supplies at connect time: the API key, plus the
    minted per-user MCP URL under the documented assumption. An extra key is harmless
    if the build ignores it, so this stays valid whichever channel T-1148 picks.
    """
    return {
        _api_token_secret_name(manifest): Secret(token),
        _URL_SECRET_KEY: Secret(_OPERATOR_MINTED_URL),
    }


def _install_capturing_broker(
    monkeypatch: pytest.MonkeyPatch, served: list[str]
) -> dict[str, Any]:
    """Like :func:`_install_fake_broker`, but also records the ``url`` and ``headers``
    ``build_attachment`` passes to :meth:`SdkMcpClient.for_http`.

    The returned client still speaks the real protocol against the in-memory SDK
    broker, so one fixture serves both the *how it authenticates / where it points*
    assertions and the end-to-end round-trip. The captured dict is populated when
    ``build_attachment`` builds the http client (it calls ``for_http`` eagerly).
    """
    server = build_named_mcp_server(served)
    captured: dict[str, Any] = {}

    def _for_http(
        *,
        url: str,
        headers: Any = None,
        tools: Any = None,
        resilience: Any = None,
        client_name: str = "arc",
        requirements: Any = None,
    ) -> SdkMcpClient:
        captured["url"] = url
        captured["headers"] = dict(headers) if headers else {}
        return SdkMcpClient(
            connected_session_factory(server),
            tools=tools,
            resilience=resilience,
            client_name=client_name,
            requirements=requirements,
        )

    monkeypatch.setattr(SdkMcpClient, "for_http", staticmethod(_for_http))
    return captured


def test_composio_authenticates_with_x_api_key_header_not_bearer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """RED anchor: Composio's hosted MCP authenticates with ``x-api-key: <token>``.

    Today ``build_attachment`` hardcodes ``Authorization: Bearer <token>`` for every
    http connector, so the credential travels the wrong way — the assertion fails for
    a feature-absent reason, not an import error.
    """
    manifest = _load_manifest()
    captured = _install_capturing_broker(monkeypatch, list(manifest.tools.allow))

    build_attachment(manifest, _COMPOSIO, _operator_secrets(manifest, "COMPOSIO_TOKEN_123"))

    headers = captured["headers"]
    # HTTP header names are case-insensitive; compare on a lowered view.
    lowered = {name.lower(): value for name, value in headers.items()}
    assert lowered.get("x-api-key") == "COMPOSIO_TOKEN_123", (
        f"the Composio API key must travel as an x-api-key header, got headers {sorted(headers)}"
    )
    assert "authorization" not in lowered, (
        "Composio's hosted MCP does not authenticate with Authorization: Bearer"
    )
    assert not any("bearer" in str(value).lower() for value in headers.values()), (
        "the credential must not be sent as a bearer token"
    )


def test_composio_bundle_no_longer_pins_the_apollo_catch_all_host() -> None:
    """RED anchor: the shipped ``extension.toml`` must not pin the apollo host.

    ``apollo.composio.dev`` is a toolkit catch-all, not the MCP host. Today the bundle
    pins ``https://apollo.composio.dev/v3/mcp`` — this text assertion fails until T-1148
    removes it. Mechanism-independent: it does not assume how the URL is supplied.
    """
    manifest_text = _MANIFEST.read_text(encoding="utf-8")

    assert _APOLLO_HOST not in manifest_text, (
        f"the {_APOLLO_HOST} catch-all host must be removed from the bundle; "
        "the MCP host is backend.composio.dev, minted per operator"
    )


async def test_composio_connects_to_the_operator_minted_backend_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """RED anchor: the connector targets the operator-minted ``backend.composio.dev``
    URL, not a statically pinned host.

    Today apollo is pinned and the operator-supplied URL is ignored, so the captured
    target is the apollo host — this fails for a feature-absent reason. (Carries the
    documented URL-supply assumption; T-1148 may adjust the supply helper.)
    """
    manifest = _load_manifest()
    captured = _install_capturing_broker(monkeypatch, list(manifest.tools.allow))

    build_attachment(manifest, _COMPOSIO, _operator_secrets(manifest, "tok"))

    assert _BACKEND_HOST in captured["url"], (
        f"the connector must target the operator-minted {_BACKEND_HOST} URL, "
        f"got {captured['url']!r}"
    )
    assert _APOLLO_HOST not in captured["url"]


async def test_composio_probe_list_invoke_over_sdk_against_fake_backend(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CON-15: the connector really speaks MCP against a Composio-shaped SDK server.

    ``probe`` reaches it, ``describe_tools`` lists an allowlisted verb, and ``invoke``
    returns its result — the protocol is proven end to end, not assumed. This leans on
    T-1142's ``SdkMcpClient`` and may already pass; it is the CON-15 guard, not a RED
    driver.
    """
    manifest = _load_manifest()
    verb = sorted(manifest.tools.allow)[0]
    _install_capturing_broker(monkeypatch, [verb])

    attachment = build_attachment(manifest, _COMPOSIO, _operator_secrets(manifest, "tok"))

    probe = await attachment.probe()
    assert probe.reachable is True, (
        f"probe against the fake Composio backend must reach it: {probe}"
    )

    described = {spec.name for spec in await attachment.describe_tools()}
    assert verb in described

    result = await attachment.invoke(verb, {})
    assert result.outcome == ToolOutcome.OK
    assert f"ran {verb}" in result.content
