"""SPEC-082 T-1073 (RED) — build_attachment must wire HttpTransport for a hosted MCP door.

REQ-420 / COMP-004. Today ``build_attachment`` (``modules/connectors/attachments.py``,
the ``kind == "mcp"`` branch) constructs only a ``StdioTransport``, and ``_McpConfig``
permits ``transport = "stdio"`` alone. A connector manifest that declares a *hosted*
MCP endpoint — ``transport = "http"`` with a ``url`` (this is how Composio, T-1100,
is meant to attach) — therefore has no path to the agent.

``HttpTransport`` already exists (``extension/mcp_attachment.py:162``); swapping stdio
for it "changes nothing else about ``McpAttachment``", per that class's own docstring.
This test proves the missing wiring: given an ``http`` manifest, ``build_attachment``
must return an ``McpAttachment`` whose transport is that ``HttpTransport``, bound to
the declared ``url``.

RED reason: ``_McpConfig.model_validate`` rejects ``transport = "http"`` (and the
``url`` key, under ``extra = "forbid"``), so the call raises before any attachment is
built. It goes GREEN only when T-1074 adds the stdio|http discriminator and the
``HttpTransport`` construction. The transport-type assertion is deliberately strict so
a builder that accepts the http config but still hands back a ``StdioTransport`` also
fails here.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from arcagent.core.tier import Tier
from arcagent.extension.manifest import load_manifest
from arcagent.extension.mcp_attachment import SdkMcpClient
from arcagent.extension.secrets import Secret
from arcagent.modules.connectors.attachments import build_attachment

#: A hosted-MCP connector manifest: no local binary to spawn, a pinned HTTP endpoint.
#: Mirrors the shape of the working stdio manifest in ``tests/unit/modules/connectors/
#: test_install.py`` (``_MCP_MANIFEST``), differing only in the ``[config.mcp]`` block.
_HTTP_MCP_MANIFEST = """
[extension]
name = "hosted_mcp"
version = "1.0.0"
attachment = "mcp"

[tools]
allow = ["remote_read"]

[[tools.declared]]
name = "remote_read"
description = "Read a remote record."
classification = "read_only"

[config.mcp]
transport = "http"
url = "https://mcp.example.com/endpoint"
client_name = "arc-test"

[config.mcp.tools.remote_read]
classification = "read_only"
"""


def test_http_mcp_manifest_builds_an_attachment_over_http_transport(tmp_path: Path) -> None:
    """An ``attachment = "mcp"`` bundle with ``transport = "http"`` attaches over HTTP.

    SPEC-084 re-bases the wire on the ``mcp`` SDK, so the hosted-MCP manifest now
    reaches the agent as an :class:`SdkMcpClient` (its HTTP session factory is built
    from the declared endpoint). The endpoint binding is closed over inside that
    factory, so what the build contract can assert at the SDK-client level is (1) an
    ``SdkMcpClient`` comes back and (2) it carries the manifest's ``client_name``.
    """
    manifest = load_manifest(_HTTP_MCP_MANIFEST, tier=Tier.PERSONAL)

    attachment = build_attachment(manifest, tmp_path, {})

    assert isinstance(attachment, SdkMcpClient)
    assert attachment._client_name == "arc-test"


# --- SPEC-084 /review SEC-26 + SEC-01: the operator-minted-URL trust boundary ---------
#
# When a manifest lets the operator paste the endpoint URL (``url_secret_field``), the
# connector's credential is attached as a header and sent to whatever host was pasted.
# The host allow-list (``url_origin``) is therefore load-bearing, and the guard must be
# structural — a string prefix match trusts a look-alike host on a manifest whose
# ``url_origin`` lacks a trailing slash.

#: An operator-suppliable manifest that FORGETS the host guard: ``url_secret_field`` set,
#: no ``url_origin``. Building it must fail closed (SEC-26) rather than send the
#: credential to any pasted host.
_URL_SECRET_NO_ORIGIN_MANIFEST = """
[extension]
name = "hosted_mcp_no_origin"
version = "1.0.0"
attachment = "mcp"

[tools]
allow = ["remote_read"]

[[tools.declared]]
name = "remote_read"
description = "Read a remote record."
classification = "read_only"

[config.mcp]
transport = "http"
url = "https://placeholder.example.com/mcp"
credential_field = "api_key"
url_secret_field = "mcp_url"
"""

#: A manifest whose ``url_origin`` omits the trailing slash. A prefix match would trust
#: ``https://vendor.example.com.evil.com/...`` here; a structural host check must not.
_URL_ORIGIN_NO_SLASH_MANIFEST = """
[extension]
name = "hosted_mcp_lookalike"
version = "1.0.0"
attachment = "mcp"

[tools]
allow = ["remote_read"]

[[tools.declared]]
name = "remote_read"
description = "Read a remote record."
classification = "read_only"

[config.mcp]
transport = "http"
url = "https://vendor.example.com/mcp"
credential_field = "api_key"
url_secret_field = "mcp_url"
url_origin = "https://vendor.example.com"
"""


def test_operator_suppliable_url_without_origin_guard_is_refused(tmp_path: Path) -> None:
    """SEC-26: a manifest that lets the operator paste the URL MUST pin a host origin.

    Without ``url_origin`` there is no allow-list, so the connector's credential would
    ride to whatever host the operator pasted (SSRF-with-credential). Building such a
    manifest must fail closed rather than attach.
    """
    manifest = load_manifest(_URL_SECRET_NO_ORIGIN_MANIFEST, tier=Tier.PERSONAL)
    with pytest.raises(Exception, match="url_origin"):
        build_attachment(
            manifest, tmp_path, {"mcp_url": Secret("https://x.example.com/mcp"), "api_key": Secret("k")}
        )


def test_operator_pasted_lookalike_host_is_refused_even_without_trailing_slash(
    tmp_path: Path,
) -> None:
    """SEC-01: the origin guard is structural (scheme + host), not a string prefix.

    ``https://vendor.example.com.evil.com/mcp`` shares the ``url_origin`` prefix
    ``https://vendor.example.com`` but is a different host; it must be refused.
    """
    manifest = load_manifest(_URL_ORIGIN_NO_SLASH_MANIFEST, tier=Tier.PERSONAL)
    lookalike = Secret("https://vendor.example.com.evil.com/mcp")

    with pytest.raises(Exception, match="not on the required origin"):
        build_attachment(manifest, tmp_path, {"mcp_url": lookalike, "api_key": Secret("k")})
