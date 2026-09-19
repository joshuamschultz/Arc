"""CON-15 e2e (SPEC-084 T-1149) — the Microsoft 365 connector over a REAL stdio server.

REQ-454 / REQ-455 / COMP-004 / COMP-005. SPEC-084 re-bases Arc's MCP client on the
official ``mcp`` SDK, which negotiates the real Legacy ``initialize`` handshake the
ecosystem runs. The Microsoft connector attaches over **stdio**: ``build_attachment``
spawns ``ms-365-mcp-server`` and speaks MCP over its pipes. CON-15 requires proving
probe → list → call end-to-end against a *spec-conformant* server on the real wire —
not the hand-rolled :class:`mcp.ClientSession` stub ``test_microsoft365_source.py``
uses, which never starts a process.

This is a **characterization / conformance proof, not a strict RED**: T-1144 already
rewired both transports onto the SDK, so the stdio path works today — inventing a
failing assertion would only lie about that. What was genuinely missing is the proof
itself: no test in the suite spawned a real MCP stdio subprocess. That is what this
file adds, along two paths that share the same real ``FastMCP`` stdio fixture
(``_fake_ms365_server``, spawned as a real subprocess):

1. **The client directly** — :meth:`SdkMcpClient.for_stdio` against the fixture, to
   pin the raw hook (``probe`` / ``describe_tools`` / ``invoke``) over the real wire.
2. **The shipped bundle** — the real ``extensions/microsoft365/extension.toml`` through
   :func:`build_attachment`, so the proof exercises the shipped manifest, the
   scrubbed-environment spawn (REQ-273) and the sandbox-wrapped argv, not just a bare
   client. The manifest's ``argv[0]`` is ``ms-365-mcp-server``; a PATH shim resolves
   that shipped name to the Python fixture, so the shipped argv runs verbatim.

The SDK stdio-server run call verified against ``mcp==1.29.0`` is
``FastMCP(name=...).run(transport="stdio")`` (see ``_fake_ms365_server``).
"""

from __future__ import annotations

import os
import stat
import sys
from pathlib import Path

import pytest

from arcagent.core.tier import Tier
from arcagent.extension.attachment import (
    ExtensionAttachment,
    ProbeResult,
    ToolOutcome,
    ToolResult,
    ToolSpec,
)
from arcagent.extension.manifest import load_manifest
from arcagent.extension.mcp_attachment import SdkMcpClient
from arcagent.extension.source import SyncSource
from arcagent.modules.connectors.attachments import build_attachment

_HERE = Path(__file__).resolve().parent
_FAKE_SERVER = str(_HERE / "_fake_ms365_server.py")

_EXTENSIONS_ROOT = Path(__file__).resolve().parents[4] / "extensions"
_MS365 = _EXTENSIONS_ROOT / "microsoft365"

_CONNECTION = "ms365-test"
_MAIL_VERB = "list-mail-messages"

#: The PATH shim is a POSIX ``sh`` script; the direct-client path below is
#: cross-platform, but the shipped-bundle path resolves the manifest's binary name
#: through it, so it is POSIX-only.
_posix_only = pytest.mark.skipif(sys.platform == "win32", reason="PATH shim is a POSIX sh script")


def _direct_client() -> SdkMcpClient:
    """An SDK client bound to a real ``ms-365-mcp-server``-shaped stdio subprocess."""
    return SdkMcpClient.for_stdio(command=sys.executable, args=[_FAKE_SERVER])


# --- Path 1: the client directly, over the real wire -------------------------


async def test_probe_reaches_the_real_stdio_server_and_lists_its_verb() -> None:
    """``probe`` completes the real SDK handshake and reports the server's live tool."""
    probe = await _direct_client().probe()

    assert isinstance(probe, ProbeResult)
    assert probe.reachable is True
    assert _MAIL_VERB in {tool.name for tool in probe.tools}


async def test_describe_tools_surfaces_the_verb_with_its_input_schema() -> None:
    """``describe_tools`` returns the verb and the SDK-generated input schema."""
    specs = await _direct_client().describe_tools()

    assert all(isinstance(spec, ToolSpec) for spec in specs)
    mail = next(spec for spec in specs if spec.name == _MAIL_VERB)
    assert "folder" in mail.input_schema.get("properties", {})


async def test_invoke_returns_the_servers_scripted_result_over_the_real_protocol() -> None:
    """``invoke`` calls the tool over the real protocol and returns its scripted page."""
    result = await _direct_client().invoke(_MAIL_VERB, {"folder": "inbox"})

    assert isinstance(result, ToolResult)
    assert result.outcome is ToolOutcome.OK
    assert "m1" in result.content


# --- Path 2: the shipped bundle, spawned for real via build_attachment -------


@pytest.fixture
def spawns_shipped_binary(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Resolve the manifest's ``ms-365-mcp-server`` to the Python fixture via a PATH shim.

    The shipped manifest launches ``ms-365-mcp-server``; a real host installs that Node
    binary. Here a tiny ``sh`` shim of the same name execs the ``FastMCP`` fixture, so
    ``build_attachment`` spawns a real subprocess running the shipped argv verbatim —
    the scrubbed-environment spawn and the sandbox-wrapped argv are exercised, not
    bypassed. Prepending to ``PATH`` on ``os.environ`` covers both the command lookup
    and the child environment ``scrubbed_environment`` inherits.
    """
    shim = tmp_path / "ms-365-mcp-server"
    shim.write_text(f"#!/bin/sh\nexec {sys.executable!r} {_FAKE_SERVER!r}\n")
    shim.chmod(shim.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ['PATH']}")


def _shipped_attachment() -> ExtensionAttachment:
    """Build the connector from the REAL shipped manifest; no secrets are needed to attach."""
    manifest = load_manifest(
        (_MS365 / "extension.toml").read_text(encoding="utf-8"), tier=Tier.PERSONAL
    )
    return build_attachment(manifest, _MS365, {}, connection_id=_CONNECTION)


@_posix_only
async def test_shipped_bundle_probes_lists_and_invokes_end_to_end(
    spawns_shipped_binary: None,
) -> None:
    """The shipped ``extension.toml`` attaches, spawns the server, and completes the round trip."""
    attachment = _shipped_attachment()

    probe = await attachment.probe()
    assert probe.reachable is True
    assert _MAIL_VERB in {tool.name for tool in probe.tools}

    mail = next(spec for spec in await attachment.describe_tools() if spec.name == _MAIL_VERB)
    assert "folder" in mail.input_schema.get("properties", {})

    result = await attachment.invoke(_MAIL_VERB, {"folder": "inbox"})
    assert result.outcome is ToolOutcome.OK
    assert "m1" in result.content


@_posix_only
async def test_shipped_bundle_source_adapter_syncs_mail_over_the_real_wire(
    spawns_shipped_binary: None,
) -> None:
    """The real Outlook source adapter syncs mail end-to-end over the spawned server.

    ``build_attachment`` wires the manifest's ``[config.source]`` adapters onto the SDK
    client, so this proves the *connector* — not just the raw client — completes a sync
    over the real stdio wire.
    """
    attachment = _shipped_attachment()

    # source_adapters() lives on _MultiSourceEnabledAttachment, beyond the base hook protocol.
    outlook = attachment.source_adapters()["outlook"]  # type: ignore[attr-defined]
    page = await outlook.sync_source(SyncSource(connection_id=_CONNECTION))

    assert {obj.object_id for obj in page.objects} == {"m1"}


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-v"]))
