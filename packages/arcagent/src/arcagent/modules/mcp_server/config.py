"""MCP server module configuration.

DEFAULT OFF: the door is an optional, security-sensitive surface, so a discovered
module only serves when an operator enables ``[modules.mcp_server]`` explicitly.
"""

from __future__ import annotations

from pydantic import Field

from arcagent.core.module_config import ModuleConfig


class McpServerConfig(ModuleConfig):
    """Configuration for the MCP server door.

    Covers what the operator controls: whether the door is on, which tool verbs it
    exposes, and which external caller DIDs are enrolled to reach it.
    """

    enabled: bool = False
    server_name: str = "arc"
    page_size: int = Field(default=100, ge=1, le=1000)
    #: Tool verbs this door may expose. Tier is stringency (COMP-005 / REQ-415):
    #: ``["*"]`` is permitted at personal but refused at enterprise/federal, where
    #: an empty list is refused too — the door must name the tools it opens.
    expose: list[str] = Field(default_factory=list)
    #: Operator-enrolled external caller DIDs (REQ-412 / COMP-002). At
    #: enterprise/federal a verified caller absent from this roster is refused
    #: (``require_enrolled``); an empty roster admits nobody there (fail-closed).
    #: Personal tier ignores it — enrollment is optional there.
    enrolled: list[str] = Field(default_factory=list)
