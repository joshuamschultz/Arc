"""Folder-scanned foreign harness types (H-040 §2.3).

Each subfolder exporting ``AGENT_TYPE = AgentType(...)`` is a foreign harness
kind, discovered by :func:`arcteam.harness.agent_type.discover_agent_types`.
Deleting a subfolder deletes the type with no core change — the seam is
deletable (§11). ``hermes`` is the Slice-1 reference proving the seam end to end.
"""
