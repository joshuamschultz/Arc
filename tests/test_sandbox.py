"""Tests for sandbox permission enforcement."""
import pytest

from arcrun.events import EventBus
from arcrun.types import SandboxConfig


class TestSandbox:
    def _make_bus(self) -> EventBus:
        return EventBus(run_id="test")

    @pytest.mark.asyncio
    async def test_no_config_allows_all(self):
        from arcrun.sandbox import Sandbox

        sandbox = Sandbox(config=None, event_bus=self._make_bus())
        allowed, reason = await sandbox.check("anything", {})
        assert allowed is True
        assert reason == ""

    @pytest.mark.asyncio
    async def test_allowlist_permits_listed_tool(self):
        from arcrun.sandbox import Sandbox

        cfg = SandboxConfig(allowed_tools=["search", "calc"])
        sandbox = Sandbox(config=cfg, event_bus=self._make_bus())
        allowed, reason = await sandbox.check("search", {})
        assert allowed is True

    @pytest.mark.asyncio
    async def test_allowlist_denies_unlisted_tool(self):
        from arcrun.sandbox import Sandbox

        cfg = SandboxConfig(allowed_tools=["search"])
        sandbox = Sandbox(config=cfg, event_bus=self._make_bus())
        allowed, reason = await sandbox.check("delete_files", {})
        assert allowed is False
        assert "not in allowed tools" in reason

    @pytest.mark.asyncio
    async def test_check_callback_allows(self):
        from arcrun.sandbox import Sandbox

        async def allow_all(name: str, params: dict) -> tuple[bool, str]:
            return True, ""

        cfg = SandboxConfig(allowed_tools=["tool1"], check=allow_all)
        sandbox = Sandbox(config=cfg, event_bus=self._make_bus())
        allowed, reason = await sandbox.check("tool1", {"x": 1})
        assert allowed is True

    @pytest.mark.asyncio
    async def test_check_callback_denies(self):
        from arcrun.sandbox import Sandbox

        async def deny_all(name: str, params: dict) -> tuple[bool, str]:
            return False, "policy violation"

        cfg = SandboxConfig(allowed_tools=["tool1"], check=deny_all)
        sandbox = Sandbox(config=cfg, event_bus=self._make_bus())
        allowed, reason = await sandbox.check("tool1", {})
        assert allowed is False
        assert reason == "policy violation"

    @pytest.mark.asyncio
    async def test_check_callback_exception_is_denial(self):
        from arcrun.sandbox import Sandbox

        async def bad_checker(name: str, params: dict) -> tuple[bool, str]:
            raise RuntimeError("checker crashed")

        cfg = SandboxConfig(allowed_tools=["tool1"], check=bad_checker)
        sandbox = Sandbox(config=cfg, event_bus=self._make_bus())
        allowed, reason = await sandbox.check("tool1", {})
        assert allowed is False
        assert reason == "check callback error"

    @pytest.mark.asyncio
    async def test_denied_emits_tool_denied_event(self):
        from arcrun.sandbox import Sandbox

        bus = self._make_bus()
        cfg = SandboxConfig(allowed_tools=["search"])
        sandbox = Sandbox(config=cfg, event_bus=bus)
        await sandbox.check("evil_tool", {"a": 1})
        denied_events = [e for e in bus.events if e.type == "tool.denied"]
        assert len(denied_events) == 1
        assert denied_events[0].data["name"] == "evil_tool"
        assert "arguments" not in denied_events[0].data

    @pytest.mark.asyncio
    async def test_allowed_does_not_emit_denied_event(self):
        from arcrun.sandbox import Sandbox

        bus = self._make_bus()
        cfg = SandboxConfig(allowed_tools=["search"])
        sandbox = Sandbox(config=cfg, event_bus=bus)
        await sandbox.check("search", {})
        denied_events = [e for e in bus.events if e.type == "tool.denied"]
        assert len(denied_events) == 0
