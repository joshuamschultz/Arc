"""arcgateway — long-running daemon that makes ArcAgents reachable from any chat platform.

Public API surface for T1.4 skeleton:

    GatewayRunner   — supervises adapters + routes messages
    SessionRouter   — per-(user, agent) session management with race-condition guard
    InboundEvent    — normalised envelope from any surface, carrying an ordered
                      list of parts plus their flattened text projection
    Delta           — streamed response chunk from executor
    DeliveryTarget  — parsed platform:chat_id[:thread_id] address
    AsyncioExecutor — in-process executor (personal/enterprise tier)
    TextPart/MediaPart/Part — the part vocabulary (SPEC-065 COMP-001)
    MediaStore      — workspace custody + audit for inbound/outbound artefacts

Platform adapters (T1.7), SubprocessExecutor (T1.6), and NATSExecutor are
registered separately and not exported here yet.
"""

from arcgateway.delivery import DeliveryTarget
from arcgateway.executor import AsyncioExecutor, Delta, Executor, InboundEvent
from arcgateway.media_store import MediaStore, MediaTooLargeError, StoredMedia
from arcgateway.parts import MediaPart, Part, TextPart, flatten_text
from arcgateway.runner import GatewayRunner
from arcgateway.session import SessionRouter, build_session_key

__all__ = [
    "AsyncioExecutor",
    "DeliveryTarget",
    "Delta",
    "Executor",
    "GatewayRunner",
    "InboundEvent",
    "MediaPart",
    "MediaStore",
    "MediaTooLargeError",
    "Part",
    "SessionRouter",
    "StoredMedia",
    "TextPart",
    "build_session_key",
    "flatten_text",
]

__version__ = "0.3.0"
