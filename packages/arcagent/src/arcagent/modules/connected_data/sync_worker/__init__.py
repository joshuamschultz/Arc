"""Connected-data store writes in their own supervised process.

The main process (dashboard, gateway, agents) reads document stores and sends
every write to one child process, the sync worker. See
``docs/concepts/sync-worker-process.md``.
"""

from arcagent.modules.connected_data.sync_worker.remote_port import (
    RemoteIngestPort,
    WriterChannel,
)
from arcagent.modules.connected_data.sync_worker.specs import (
    SealKey,
    StoreSpec,
    SyncWorkerRefusedError,
    SyncWorkerUnavailableError,
    SyncWorkerWriteError,
)
from arcagent.modules.connected_data.sync_worker.supervisor import (
    SyncWorkerStatus,
    SyncWorkerSupervisor,
    process_supervisor,
    shutdown_process_supervisor,
)

__all__ = [
    "RemoteIngestPort",
    "SealKey",
    "StoreSpec",
    "SyncWorkerRefusedError",
    "SyncWorkerStatus",
    "SyncWorkerSupervisor",
    "SyncWorkerUnavailableError",
    "SyncWorkerWriteError",
    "WriterChannel",
    "process_supervisor",
    "shutdown_process_supervisor",
]
