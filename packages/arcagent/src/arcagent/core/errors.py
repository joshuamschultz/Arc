"""ArcAgent error hierarchy.

All errors carry a machine-readable code, component name, and optional details.
Every error is an audit event — callers emit to telemetry and Module Bus.

Subclasses set ``_component`` at the class level. The base __init__
uses it as the default, so subclasses only need a custom __init__
when they override parameter defaults (e.g. fixed code or message).
"""

from __future__ import annotations

from typing import Any


class ArcAgentError(Exception):
    """Base error for all ArcAgent failures.

    Carries structured context for audit trails and telemetry.
    """

    _component: str = "unknown"

    def __init__(
        self,
        code: str,
        message: str,
        component: str = "",
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.component = component or self._component
        self.details = details or {}

    def __str__(self) -> str:
        return f"[{self.code}] {self.component}: {self.message}"


class ConfigError(ArcAgentError):
    """TOML parse failure or Pydantic validation error."""

    _component = "config"


class IdentityError(ArcAgentError):
    """Key generation, signing, verification, or DID creation failure."""

    _component = "identity"


class IdentityRequired(IdentityError):  # noqa: N818 — domain convention; peers use non-Error suffix
    """Raised when ArcAgent is started without a DID configured.

    Run ``arc agent init`` to generate a DID and keypair, then set the
    resulting DID in ``arcagent.toml`` under ``[identity] did``.
    """

    def __init__(self, details: dict[str, Any] | None = None) -> None:
        hint = "Run 'arc agent init' to generate a DID and keypair."
        super().__init__(
            code="IDENTITY_REQUIRED",
            message="Agent DID is required. " + hint,
            details={**(details or {}), "hint": "arc agent init"},
        )


class ToolError(ArcAgentError):
    """Tool execution failure, timeout, or transport error."""

    _component = "tool_registry"


class ToolVetoedError(ToolError):
    """Tool execution was vetoed by a pre_tool handler."""

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(code="TOOL_VETOED", message=message, details=details)


class ContextError(ArcAgentError):
    """Token budget exceeded, compaction failure, or prompt assembly error."""

    _component = "context_manager"


class ModuleBusError(ArcAgentError):
    """Handler failure, timeout, or module lifecycle error."""

    _component = "module_bus"


class SessionError(ArcAgentError):
    """Session creation, load, or compaction failure."""

    _component = "session_manager"


class SkillError(ArcAgentError):
    """Skill discovery, parse, or format error."""

    _component = "skill_registry"


class ExtensionError(ArcAgentError):
    """Extension load, sandbox violation, or factory error."""

    _component = "extensions"


class SettingsError(ArcAgentError):
    """Settings validation, persistence, or access error."""

    _component = "settings_manager"
