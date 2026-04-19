"""arcrun.backends — pluggable execution backend system.

Public surface
--------------
ExecutorBackend    — @runtime_checkable Protocol; implement to create a new backend
BackendCapabilities — Pydantic model declaring what a backend can do
ExecHandle         — opaque handle returned by ExecutorBackend.run()
TRUNCATION_MARKER  — sentinel bytes appended when stdout is hard-truncated
LocalBackend       — subprocess-based local execution (no container)
DockerBackend      — docker exec into a long-lived container per agent
load_backend       — 3-tier federal-aware backend discovery
FederalBackendPolicyError — raised when entry_points attempted at federal tier
BackendSignatureError     — raised when backend not in federal allowed_backends manifest
"""

from arcrun.backends.base import (
    TRUNCATION_MARKER,
    BackendCapabilities,
    ExecHandle,
    ExecutorBackend,
    _ThreadedProcessHandle,
)
from arcrun.backends.docker import DockerBackend
from arcrun.backends.loader import (
    BackendSignatureError,
    FederalBackendPolicyError,
    load_backend,
)
from arcrun.backends.local import LocalBackend

__all__ = [
    "TRUNCATION_MARKER",
    "BackendCapabilities",
    "BackendSignatureError",
    "DockerBackend",
    "ExecHandle",
    "ExecutorBackend",
    "FederalBackendPolicyError",
    "LocalBackend",
    "_ThreadedProcessHandle",
    "load_backend",
]
