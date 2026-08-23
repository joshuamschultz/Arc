"""Optional connected-data synchronization module."""

from arcagent.modules.connected_data.coordinator import ConnectedDataCoordinator
from arcagent.modules.connected_data.ingest import (
    ArcMemoryIngestAdapter,
    ArcStoreObjectState,
    ConnectedDataUnavailableError,
)
from arcagent.modules.connected_data.service import (
    ConnectedDataService,
    MappingProposalStatus,
    SourceOperationResult,
    SourceRuntimeStatus,
    SourceUnreachableError,
)

__all__ = [
    "ArcMemoryIngestAdapter",
    "ArcStoreObjectState",
    "ConnectedDataCoordinator",
    "ConnectedDataService",
    "ConnectedDataUnavailableError",
    "MappingProposalStatus",
    "SourceOperationResult",
    "SourceRuntimeStatus",
    "SourceUnreachableError",
]
