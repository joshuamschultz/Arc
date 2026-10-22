"""Contract-level exceptions — vendor-free, no Arc imports."""


class ModelStreamProtocolError(Exception):
    """Raised when provider stream frames cannot form a valid response.

    The message is intentionally provider-neutral and contains no model output,
    tool arguments, or raw wire payload. Callers can safely surface it as a
    stable failure class without leaking streamed content.
    """

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(f"Invalid streamed tool call: {reason}")


__all__ = ["ModelStreamProtocolError"]
