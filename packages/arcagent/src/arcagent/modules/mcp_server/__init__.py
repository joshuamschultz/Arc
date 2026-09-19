"""MCP server module — the optional door that lets external MCP clients reach an
agent's tools (SPEC-082, COMP-001).

Discovered by folder presence (``capabilities.py`` + ``_runtime.py``) and
activated only by an enabled ``[modules.mcp_server]`` entry, like every module.
The nucleus has zero knowledge of it, so it is fully removable.

The full door is built here: ``server`` (the read-only ``tools/list`` catalog),
``identity`` (signed-envelope verify), ``enrollment`` (fleet-member roster),
``allowlist`` (per-tier tool exposure), ``dispatch``/``door`` (the
verify → enroll → allowlist → dispatch → audit pipeline), ``audit``, and
``sdk_server`` — the official ``mcp`` SDK server that the
``http_transport``/``stdio_transport`` wire transports serve. The module's own
``capabilities.py`` builds the read-only listing surface in-process;
``serving.build_door_from_agent`` assembles the full serving door that
``arc mcp serve`` (or the arcui ``/mcp`` mount) exposes (arcagent stays headless).
"""

from __future__ import annotations
