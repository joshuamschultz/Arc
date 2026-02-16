"""Tests for ArcAgent error hierarchy."""

from arcagent.core.errors import (
    ArcAgentError,
    ConfigError,
    ContextError,
    IdentityError,
    ModuleBusError,
    SessionError,
    ToolError,
    ToolVetoedError,
)


class TestArcAgentError:
    def test_base_error_str_format(self) -> None:
        err = ArcAgentError(code="TEST_001", message="something broke", component="test")
        assert str(err) == "[TEST_001] test: something broke"

    def test_base_error_is_exception(self) -> None:
        err = ArcAgentError(code="TEST_001", message="msg", component="test")
        assert isinstance(err, Exception)

    def test_base_error_with_details(self) -> None:
        err = ArcAgentError(
            code="TEST_001",
            message="msg",
            component="test",
            details={"key": "value"},
        )
        assert err.details == {"key": "value"}

    def test_base_error_details_default_empty(self) -> None:
        err = ArcAgentError(code="TEST_001", message="msg", component="test")
        assert err.details == {}

    def test_raises_as_exception(self) -> None:
        with __import__("pytest").raises(ArcAgentError) as exc_info:
            raise ArcAgentError(code="TEST_001", message="msg", component="test")
        assert exc_info.value.code == "TEST_001"


class TestConfigError:
    def test_default_component(self) -> None:
        err = ConfigError(code="CONFIG_PARSE", message="bad toml")
        assert err.component == "config"

    def test_inherits_arcagent_error(self) -> None:
        err = ConfigError(code="CONFIG_PARSE", message="bad toml")
        assert isinstance(err, ArcAgentError)
        assert isinstance(err, Exception)

    def test_with_line_details(self) -> None:
        err = ConfigError(
            code="CONFIG_SYNTAX",
            message="unexpected char",
            details={"line": 5, "column": 12},
        )
        assert err.details["line"] == 5


class TestIdentityError:
    def test_default_component(self) -> None:
        err = IdentityError(code="IDENTITY_KEYGEN", message="failed")
        assert err.component == "identity"

    def test_inherits_arcagent_error(self) -> None:
        err = IdentityError(code="IDENTITY_KEYGEN", message="failed")
        assert isinstance(err, ArcAgentError)


class TestToolError:
    def test_default_component(self) -> None:
        err = ToolError(code="TOOL_TIMEOUT", message="30s exceeded")
        assert err.component == "tool_registry"

    def test_inherits_arcagent_error(self) -> None:
        err = ToolError(code="TOOL_TIMEOUT", message="30s exceeded")
        assert isinstance(err, ArcAgentError)


class TestToolVetoedError:
    def test_default_code(self) -> None:
        err = ToolVetoedError(message="policy blocked")
        assert err.code == "TOOL_VETOED"

    def test_inherits_tool_error(self) -> None:
        err = ToolVetoedError(message="policy blocked")
        assert isinstance(err, ToolError)
        assert isinstance(err, ArcAgentError)

    def test_default_component_from_parent(self) -> None:
        err = ToolVetoedError(message="policy blocked")
        assert err.component == "tool_registry"


class TestContextError:
    def test_default_component(self) -> None:
        err = ContextError(code="CONTEXT_OVERFLOW", message="token budget exceeded")
        assert err.component == "context_manager"


class TestModuleBusError:
    def test_default_component(self) -> None:
        err = ModuleBusError(code="MODULE_TIMEOUT", message="handler timed out")
        assert err.component == "module_bus"


class TestSessionError:
    def test_default_component(self) -> None:
        err = SessionError(code="SESSION_CREATE", message="failed to create session")
        assert err.component == "session_manager"

    def test_inherits_arcagent_error(self) -> None:
        err = SessionError(code="SESSION_CREATE", message="failed")
        assert isinstance(err, ArcAgentError)

    def test_with_details(self) -> None:
        err = SessionError(
            code="SESSION_LOAD",
            message="malformed JSONL",
            details={"session_id": "abc-123", "line": 42},
        )
        assert err.details["session_id"] == "abc-123"
