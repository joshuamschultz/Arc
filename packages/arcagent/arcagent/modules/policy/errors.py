"""Error hierarchy for the policy module.

Extends ArcAgentError from core, keeping the structured error
contract while living alongside the module.
"""

from __future__ import annotations

from typing import Any

from arcagent.core.errors import ArcAgentError


class PolicyEvalError(ArcAgentError):
    """Policy evaluation or merge failure."""

    _component = "policy"

    def __init__(
        self,
        code: str = "POLICY_EVAL",
        message: str = "Policy evaluation failed",
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(code=code, message=message, details=details)
