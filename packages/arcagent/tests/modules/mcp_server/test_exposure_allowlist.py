"""SPEC-082 T-1083 (RED) — the door exposes only an explicit, tier-aware allowlist.

REQ-415 / COMP-005. The door must expose only tools an operator named. ``tools/list``
returns the allowlist ∩ catalog; a ``tools/call`` on a verb outside the allowlist is
denied at the allowlist layer, before any dispatch can run. Tier is stringency: at
enterprise/federal an unbounded (``*``) or empty allowlist is refused outright; at
personal it is permitted (D-548, ASI02/LLM06).

This test drives ``arcagent.modules.mcp_server.allowlist`` (``ExposureAllowlist`` +
``AllowlistRefused``), sourced from ``McpServerConfig`` extended with an ``expose``
list. Neither the module nor the ``expose`` field exists yet. The RED is the import:
``No module named 'arcagent.modules.mcp_server.allowlist'``. It goes GREEN when
T-1084 adds the module and the config field.
"""

from __future__ import annotations

import pytest

from arcagent.modules.mcp_server.allowlist import AllowlistRefused, ExposureAllowlist
from arcagent.modules.mcp_server.config import McpServerConfig


def test_tools_list_returns_only_allowlisted_verbs() -> None:
    """The exposed catalog is the allowlist intersected with the real catalog."""
    allowlist = ExposureAllowlist.from_config(
        McpServerConfig(enabled=True, expose=["read_file", "list_dir"]), tier="personal"
    )

    exposed = allowlist.filter(["read_file", "list_dir", "delete_everything"])

    assert set(exposed) == {"read_file", "list_dir"}
    assert "delete_everything" not in exposed


def test_non_allowlisted_call_is_denied_before_dispatch() -> None:
    """The allowlist gate refuses an unexposed verb on its own — no provider is consulted.

    ``check_call`` is a pure gate that raises before any dispatch object exists, so a
    denial here is structurally *before* dispatch (REQ-415).
    """
    allowlist = ExposureAllowlist.from_config(
        McpServerConfig(enabled=True, expose=["read_file"]), tier="personal"
    )

    allowlist.check_call("read_file")  # allowlisted → no raise
    with pytest.raises(AllowlistRefused):
        allowlist.check_call("delete_everything")


@pytest.mark.parametrize("tier", ["enterprise", "federal"])
def test_unbounded_allowlist_is_refused_at_enterprise_and_federal(tier: str) -> None:
    """A ``*`` exposure is refused outside personal (REQ-415)."""
    with pytest.raises(AllowlistRefused):
        ExposureAllowlist.from_config(
            McpServerConfig(enabled=True, expose=["*"]), tier=tier
        )


@pytest.mark.parametrize("tier", ["enterprise", "federal"])
def test_empty_allowlist_is_refused_at_enterprise_and_federal(tier: str) -> None:
    """An empty allowlist is refused outside personal — the door must name its tools."""
    with pytest.raises(AllowlistRefused):
        ExposureAllowlist.from_config(McpServerConfig(enabled=True, expose=[]), tier=tier)


def test_unbounded_allowlist_is_permitted_at_personal() -> None:
    """Personal tier permits ``*`` — tier is stringency, not a gate."""
    allowlist = ExposureAllowlist.from_config(
        McpServerConfig(enabled=True, expose=["*"]), tier="personal"
    )

    assert allowlist.is_allowed("anything_at_all") is True
    allowlist.check_call("anything_at_all")  # does not raise
