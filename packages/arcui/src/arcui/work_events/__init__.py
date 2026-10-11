"""Work-event callbacks (SPEC-088 COMP-013): the callback allowlist + SSRF policy.

Optional: when this package is physically absent arcui still starts and intake
reports notifications as unavailable.
"""

from arcui.work_events.policy import CallbackPolicy, CallbackRefusedError

__all__ = ["CallbackPolicy", "CallbackRefusedError"]
