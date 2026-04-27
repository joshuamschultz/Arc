"""arcgateway — long-running daemon that makes ArcAgents reachable from any chat platform.

Public API surface for T1.4 skeleton:

    GatewayRunner   — supervises adapters + routes messages
    SessionRouter   — per-(user, agent) session management with race-condition guard
    InboundEvent    — normalised event from any platform adapter
    Delta           — streamed response chunk from executor
    DeliveryTarget  — parsed platform:chat_id[:thread_id] address
    AsyncioExecutor — in-process executor (personal/enterprise tier)

Platform adapters (T1.7), SubprocessExecutor (T1.6), and NATSExecutor are
registered separately and not exported here yet.
"""

from arcgateway.delivery import DeliveryTarget
from arcgateway.executor import AsyncioExecutor, Delta, Executor, InboundEvent
from arcgateway.runner import GatewayRunner
from arcgateway.session import SessionRouter, build_session_key

__all__ = [
    "AsyncioExecutor",
    "DeliveryTarget",
    "Delta",
    "Executor",
    "GatewayRunner",
    "InboundEvent",
    "SessionRouter",
    "build_session_key",
]

__version__ = "0.2.0"
