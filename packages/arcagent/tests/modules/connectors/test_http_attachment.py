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

from arcagent.core.tier import Tier
from arcagent.extension.manifest import load_manifest
from arcagent.extension.mcp_attachment import HttpTransport, McpAttachment
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

    The wire class is unchanged: the same ``McpAttachment`` reaches the agent, only its
    transport differs. So the contract asserted is (1) an ``McpAttachment`` comes back,
    (2) its transport is the ``HttpTransport`` (not stdio), and (3) it is bound to the
    endpoint the manifest names.
    """
    manifest = load_manifest(_HTTP_MCP_MANIFEST, tier=Tier.PERSONAL)

    attachment = build_attachment(manifest, tmp_path, {})

    assert isinstance(attachment, McpAttachment)
    assert isinstance(attachment._transport, HttpTransport)
    assert attachment._transport._url == "https://mcp.example.com/endpoint"
    assert attachment._client_name == "arc-test"
