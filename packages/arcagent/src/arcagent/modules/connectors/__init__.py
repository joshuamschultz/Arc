"""Connector module — hosts SPEC-062 extension attachments as an optional module.

Disabled by default: an agent with no ``[modules.connectors]`` entry loads nothing
here and gains no new behaviour (REQ-286). The live surface lands in
:mod:`.capabilities` (loaded by the capability loader) backed by per-agent runtime
state in :mod:`._runtime`.
"""

from __future__ import annotations
