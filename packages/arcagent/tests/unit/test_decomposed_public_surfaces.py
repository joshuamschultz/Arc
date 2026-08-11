"""Decomposed domains keep their established import surfaces."""

from __future__ import annotations

import arcagent.connection_catalog as connection_catalog
import arcagent.connections as connections
import arcagent.extension.mcp_attachment as mcp_attachment
import arcagent.extension.mcp_policy as mcp_policy
import arcagent.modules.connectors.attachments as attachments
import arcagent.modules.connectors.credential_placement as credential_placement
import arcagent.modules.connectors.install as install
import arcagent.modules.connectors.models as models


def test_connection_catalog_types_remain_available_from_connections() -> None:
    assert connections.AuditChain is connection_catalog.AuditChain
    assert connections.CatalogEntry is connection_catalog.CatalogEntry
    assert connections.catalog is connection_catalog.catalog


def test_mcp_policy_types_remain_available_from_attachment_module() -> None:
    assert mcp_attachment.McpResilience is mcp_policy.McpResilience
    assert mcp_attachment.McpToolPolicy is mcp_policy.McpToolPolicy


def test_connector_install_models_and_builder_keep_their_import_surface() -> None:
    assert install.ConnectorPlan is models.ConnectorPlan
    assert install.InstallReport is models.InstallReport
    assert install.RemovalReport is models.RemovalReport
    assert install.build_attachment is attachments.build_attachment


def test_credential_placement_has_one_owner() -> None:
    assert install.placement_environment is credential_placement.placement_environment
    assert install.visible_values is credential_placement.visible_values
    assert attachments.placement_environment is credential_placement.placement_environment
    assert attachments.visible_values is credential_placement.visible_values
