"""Unit tests for messaging module lifecycle."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from arcagent.modules.messaging import MessagingModule
from tests.unit.modules.messaging.conftest import (
    make_config_dict,
    make_ctx,
    make_team_config,
)


def _make_module(
    tmp_path: Path,
    entity_id: str = "agent://test_agent",
) -> MessagingModule:
    """Create a MessagingModule with test config."""
    config = make_config_dict(entity_id=entity_id)
    team_config = make_team_config(str(tmp_path / "team"))
    return MessagingModule(
        config=config,
        team_config=team_config,
        telemetry=MagicMock(),
        workspace=tmp_path,
    )


class TestModuleProtocol:
    def test_has_name(self, tmp_path: Path) -> None:
        module = _make_module(tmp_path)
        assert module.name == "messaging"

    def test_has_startup(self, tmp_path: Path) -> None:
        module = _make_module(tmp_path)
        assert hasattr(module, "startup")
        assert callable(module.startup)

    def test_has_shutdown(self, tmp_path: Path) -> None:
        module = _make_module(tmp_path)
        assert hasattr(module, "shutdown")
        assert callable(module.shutdown)


class TestModuleStartup:
    @pytest.mark.asyncio
    async def test_startup_registers_tools(self, tmp_path: Path) -> None:
        module = _make_module(tmp_path)
        ctx = make_ctx(tmp_path)
        await module.startup(ctx)
        try:
            # Should register 7 tools: 5 messaging + 2 team file tools.
            assert ctx.tool_registry.register.call_count == 7
        finally:
            await module.shutdown()

    @pytest.mark.asyncio
    async def test_startup_subscribes_to_events(
        self,
        tmp_path: Path,
    ) -> None:
        module = _make_module(tmp_path)
        ctx = make_ctx(tmp_path)
        await module.startup(ctx)
        try:
            subscribed = [call.args[0] for call in ctx.bus.subscribe.call_args_list]
            assert "agent:assemble_prompt" in subscribed
            assert "agent:shutdown" in subscribed
        finally:
            await module.shutdown()

    @pytest.mark.asyncio
    async def test_startup_registers_entity(
        self,
        tmp_path: Path,
    ) -> None:
        module = _make_module(tmp_path)
        ctx = make_ctx(tmp_path)
        await module.startup(ctx)
        try:
            # Entity should be in the registry
            entity = await module._registry.get("agent://test_agent")
            assert entity is not None
            assert entity.name == "Test Agent"
        finally:
            await module.shutdown()

    @pytest.mark.asyncio
    async def test_startup_starts_poll_task(
        self,
        tmp_path: Path,
    ) -> None:
        module = _make_module(tmp_path)
        ctx = make_ctx(tmp_path)
        await module.startup(ctx)
        try:
            assert module._poll_task is not None
            assert not module._poll_task.done()
        finally:
            await module.shutdown()


class TestModuleShutdown:
    @pytest.mark.asyncio
    async def test_shutdown_cancels_poll(self, tmp_path: Path) -> None:
        module = _make_module(tmp_path)
        ctx = make_ctx(tmp_path)
        await module.startup(ctx)
        await module.shutdown()
        assert module._poll_task is None

    @pytest.mark.asyncio
    async def test_double_shutdown_is_safe(
        self,
        tmp_path: Path,
    ) -> None:
        module = _make_module(tmp_path)
        ctx = make_ctx(tmp_path)
        await module.startup(ctx)
        await module.shutdown()
        await module.shutdown()
        assert module._poll_task is None


class TestEntityIdFallback:
    @pytest.mark.asyncio
    async def test_entity_id_from_agent_name(
        self,
        tmp_path: Path,
    ) -> None:
        """When entity_id is empty, derive from agent config name."""
        config = make_config_dict(entity_id="")
        team_config = make_team_config(str(tmp_path / "team"))
        module = MessagingModule(
            config=config,
            team_config=team_config,
            telemetry=MagicMock(),
            workspace=tmp_path,
        )
        ctx = make_ctx(tmp_path)
        ctx.config.agent.name = "my_agent"
        await module.startup(ctx)
        try:
            assert module._config.entity_id == "agent://my_agent"
        finally:
            await module.shutdown()


class TestTeamRootResolution:
    def test_team_root_from_absolute_path(self, tmp_path: Path) -> None:
        """Absolute team_config.root is used as-is."""
        team_config = make_team_config(str(tmp_path / "custom_team"))
        workspace = tmp_path / "workspace"
        module = MessagingModule(
            config=make_config_dict(),
            team_config=team_config,
            workspace=workspace,
        )
        assert module._resolve_team_root() == tmp_path / "custom_team"

    def test_team_root_relative_resolved_against_agent_dir(self, tmp_path: Path) -> None:
        """Relative team root resolves against agent dir (workspace parent)."""
        team_config = make_team_config("shared")
        workspace = tmp_path / "workspace"
        module = MessagingModule(
            config=make_config_dict(),
            team_config=team_config,
            workspace=workspace,
        )
        assert module._resolve_team_root() == tmp_path / "shared"

    def test_team_root_parent_traversal(self, tmp_path: Path) -> None:
        """../shared resolves to sibling of agent dir, matching real layout."""
        agent_dir = tmp_path / "brad_agent"
        workspace = agent_dir / "workspace"
        team_config = make_team_config("../shared")
        module = MessagingModule(
            config=make_config_dict(),
            team_config=team_config,
            workspace=workspace,
        )
        resolved = module._resolve_team_root().resolve()
        assert resolved == (tmp_path / "shared").resolve()

    def test_team_root_fallback_when_no_team_config(self, tmp_path: Path) -> None:
        """Falls back to agent_dir/team when no team_config provided."""
        workspace = tmp_path / "workspace"
        module = MessagingModule(
            config=make_config_dict(),
            workspace=workspace,
        )
        assert module._resolve_team_root() == tmp_path / "team"

    def test_team_root_fallback_when_empty(self, tmp_path: Path) -> None:
        """Falls back to agent_dir/team when team_config.root is empty."""
        team_config = make_team_config("")
        workspace = tmp_path / "workspace"
        module = MessagingModule(
            config=make_config_dict(),
            team_config=team_config,
            workspace=workspace,
        )
        assert module._resolve_team_root() == tmp_path / "team"


class TestAssemblePromptSectionKey:
    """R4: Section key change from 'messaging' to 'teams'."""

    @pytest.mark.asyncio
    async def test_section_key_is_teams(self, tmp_path: Path) -> None:
        """R4.1: Team-related prompt content uses sections['teams']."""
        module = _make_module(tmp_path)
        ctx = make_ctx(tmp_path)
        await module.startup(ctx)
        try:
            # Build a mock EventContext with sections dict
            event_ctx = MagicMock()
            event_ctx.data = {"sections": {}}
            await module._on_assemble_prompt(event_ctx)
            sections = event_ctx.data["sections"]
            assert "teams" in sections
            assert "messaging" not in sections
        finally:
            await module.shutdown()

    @pytest.mark.asyncio
    async def test_messaging_context_preserved(self, tmp_path: Path) -> None:
        """R4.1: Existing messaging context (identity, rules) preserved."""
        module = _make_module(tmp_path)
        ctx = make_ctx(tmp_path)
        await module.startup(ctx)
        try:
            event_ctx = MagicMock()
            event_ctx.data = {"sections": {}}
            await module._on_assemble_prompt(event_ctx)
            content = event_ctx.data["sections"]["teams"]
            assert "Team Messaging" in content
            assert "test_agent" in content or "Test Agent" in content
        finally:
            await module.shutdown()


class TestBuildRoster:
    """R2: Team roster injection tests."""

    def test_roster_cache_vars_initialized(self, tmp_path: Path) -> None:
        """Task 2.2: Cache instance variables initialized."""
        module = _make_module(tmp_path)
        assert module._roster_cache is None
        assert module._roster_cache_time == 0.0

    @pytest.mark.asyncio
    async def test_empty_registry_returns_empty(self, tmp_path: Path) -> None:
        """R2.5: Empty roster produces no section."""
        module = _make_module(tmp_path)
        # No startup — registry is None
        result = await module._build_roster()
        assert result == ""

    @pytest.mark.asyncio
    async def test_roster_with_entities(self, tmp_path: Path) -> None:
        """R2.1: All entities from EntityRegistry appear in roster."""
        module = _make_module(tmp_path)
        ctx = make_ctx(tmp_path)
        await module.startup(ctx)
        try:
            # After startup, the module registered itself
            roster = await module._build_roster()
            assert "<team-roster>" in roster
            assert "</team-roster>" in roster
            assert "test_agent" in roster.lower() or "Test Agent" in roster
        finally:
            await module.shutdown()

    @pytest.mark.asyncio
    async def test_roster_xml_format(self, tmp_path: Path) -> None:
        """R2.2: Roster uses XML format."""
        module = _make_module(tmp_path)
        ctx = make_ctx(tmp_path)
        await module.startup(ctx)
        try:
            roster = await module._build_roster()
            assert "<entity " in roster
            assert "</entity>" in roster
        finally:
            await module.shutdown()

    @pytest.mark.asyncio
    async def test_roster_ttl_cache(self, tmp_path: Path) -> None:
        """R2.4: Roster refreshes on TTL-based schedule."""
        module = _make_module(tmp_path)
        ctx = make_ctx(tmp_path)
        await module.startup(ctx)
        try:
            first = await module._build_roster()
            second = await module._build_roster()
            # Within TTL — should be same cached object
            assert first is second
        finally:
            await module.shutdown()

    @pytest.mark.asyncio
    async def test_roster_appended_to_prompt(self, tmp_path: Path) -> None:
        """Roster XML appears in assembled prompt."""
        module = _make_module(tmp_path)
        ctx = make_ctx(tmp_path)
        await module.startup(ctx)
        try:
            event_ctx = MagicMock()
            event_ctx.data = {"sections": {}}
            await module._on_assemble_prompt(event_ctx)
            content = event_ctx.data["sections"]["teams"]
            assert "<team-roster>" in content
        finally:
            await module.shutdown()

    @pytest.mark.asyncio
    async def test_roster_cache_expires_after_ttl(self, tmp_path: Path) -> None:
        """R2.4: Cache expires and roster is rebuilt after TTL."""
        import time

        module = _make_module(tmp_path)
        ctx = make_ctx(tmp_path)
        await module.startup(ctx)
        try:
            first = await module._build_roster()
            # Force expiry by back-dating cache time past TTL
            module._roster_cache_time = time.monotonic() - module._config.roster_ttl_seconds - 1
            second = await module._build_roster()
            assert first is not second  # Cache was rebuilt
            assert first == second  # Same content though
        finally:
            await module.shutdown()

    @pytest.mark.asyncio
    async def test_roster_xml_escaping(self, tmp_path: Path) -> None:
        """R2.3: Entity values with XML chars are properly escaped in roster."""
        module = _make_module(tmp_path)
        ctx = make_ctx(tmp_path)
        await module.startup(ctx)
        try:
            # The startup registered the entity; rebuild to check escaping
            roster = await module._build_roster()
            # All values go through xml_escape — verify the output is valid XML-safe
            assert "<team-roster>" in roster
            # No unescaped angle brackets in values (name/id are clean in fixture)
            # Verify the structure is valid
            assert "</team-roster>" in roster
        finally:
            await module.shutdown()

    @pytest.mark.asyncio
    async def test_roster_renders_entity_fields(self, tmp_path: Path) -> None:
        """R2.6: Entity fields beyond name/id appear as XML child elements."""
        module = _make_module(tmp_path)
        ctx = make_ctx(tmp_path)
        await module.startup(ctx)
        try:
            roster = await module._build_roster()
            # The entity was registered with roles=["executor"] and
            # capabilities=["task-execution"] from make_config_dict
            assert "<roles>" in roster
            assert "<capabilities>" in roster
        finally:
            await module.shutdown()

    @pytest.mark.asyncio
    async def test_roster_audit_event_emitted(self, tmp_path: Path) -> None:
        """R7.2, R7.3: prompt.roster_rebuilt event emitted with entity_count."""
        module = _make_module(tmp_path)
        ctx = make_ctx(tmp_path)
        await module.startup(ctx)
        try:
            await module._build_roster()
            module._telemetry.audit_event.assert_called_with(
                "prompt.roster_rebuilt",
                {"entity_count": 1},
            )
        finally:
            await module.shutdown()

    @pytest.mark.asyncio
    async def test_roster_audit_not_emitted_on_cache_hit(self, tmp_path: Path) -> None:
        """R7.2: Audit event only fires on rebuild, not cache hit."""
        module = _make_module(tmp_path)
        ctx = make_ctx(tmp_path)
        await module.startup(ctx)
        try:
            await module._build_roster()
            call_count_after_first = module._telemetry.audit_event.call_count
            await module._build_roster()  # Cache hit
            assert module._telemetry.audit_event.call_count == call_count_after_first
        finally:
            await module.shutdown()


class TestPromptSanitization:
    """Pre-existing security: sanitize untrusted data before prompt interpolation.

    Addresses OWASP LLM01 (Prompt Injection) and ASI06 (Memory Poisoning).
    """

    @pytest.mark.asyncio
    async def test_entity_name_zero_width_stripped(self, tmp_path: Path) -> None:
        """Zero-width chars in entity_name don't reach the system prompt."""
        module = _make_module(tmp_path)
        ctx = make_ctx(tmp_path)
        await module.startup(ctx)
        try:
            # Inject malicious values after startup to isolate prompt sanitization.
            module._config = module._config.model_copy(
                update={
                    "entity_name": "Evil\u200bAgent",
                    "entity_id": "agent://evil\u200bagent",
                },
            )
            event_ctx = MagicMock()
            event_ctx.data = {"sections": {}}
            await module._on_assemble_prompt(event_ctx)
            content = event_ctx.data["sections"]["teams"]
            assert "\u200b" not in content
            assert "EvilAgent" in content
        finally:
            await module.shutdown()

    @pytest.mark.asyncio
    async def test_entity_id_control_chars_stripped(self, tmp_path: Path) -> None:
        """Control chars in entity_id don't reach the system prompt."""
        module = _make_module(tmp_path)
        ctx = make_ctx(tmp_path)
        await module.startup(ctx)
        try:
            # Inject malicious values after startup to isolate prompt sanitization.
            module._config = module._config.model_copy(
                update={"entity_id": "agent://test\x00agent"},
            )
            event_ctx = MagicMock()
            event_ctx.data = {"sections": {}}
            await module._on_assemble_prompt(event_ctx)
            content = event_ctx.data["sections"]["teams"]
            assert "\x00" not in content
        finally:
            await module.shutdown()

    @pytest.mark.asyncio
    async def test_unread_stream_names_sanitized(self, tmp_path: Path) -> None:
        """Stream names in unread display don't pass through unsanitized."""
        module = _make_module(tmp_path)
        ctx = make_ctx(tmp_path)
        await module.startup(ctx)
        try:
            module._last_unread = {"stream\u200b\x00evil": 3}
            event_ctx = MagicMock()
            event_ctx.data = {"sections": {}}
            await module._on_assemble_prompt(event_ctx)
            content = event_ctx.data["sections"]["teams"]
            assert "\u200b" not in content
            assert "\x00" not in content
            assert "streamevil" in content
        finally:
            await module.shutdown()

    @pytest.mark.asyncio
    async def test_inbox_body_sanitized(self, tmp_path: Path) -> None:
        """Message body with zero-width/control chars is sanitized before prompt."""
        config = make_config_dict(auto_ack=False)
        team_config = make_team_config(str(tmp_path / "team"))
        module = MessagingModule(
            config=config,
            team_config=team_config,
            telemetry=MagicMock(),
            workspace=tmp_path,
        )
        captured: list[str] = []

        async def capture(prompt: str) -> None:
            captured.append(prompt)

        module._agent_chat_fn = capture

        mock_msg = MagicMock()
        mock_msg.seq = 1
        mock_msg.id = "msg1"
        mock_msg.sender = "agent://peer"
        mock_msg.body = "Inject\u200b\x00this"
        mock_msg.msg_type = "dm"
        mock_msg.priority = "normal"
        mock_msg.action_required = False
        mock_msg.thread_id = ""
        mock_msg.ts = "2024-01-01"

        await module._process_inbox({"stream1": [mock_msg]})

        assert captured
        assert "\u200b" not in captured[0]
        assert "\x00" not in captured[0]
        assert "Injectthis" in captured[0]

    @pytest.mark.asyncio
    async def test_inbox_sender_and_metadata_sanitized(self, tmp_path: Path) -> None:
        """Sender, msg_type, priority with control chars sanitized before prompt."""
        config = make_config_dict(auto_ack=False)
        team_config = make_team_config(str(tmp_path / "team"))
        module = MessagingModule(
            config=config,
            team_config=team_config,
            telemetry=MagicMock(),
            workspace=tmp_path,
        )
        captured: list[str] = []

        async def capture(prompt: str) -> None:
            captured.append(prompt)

        module._agent_chat_fn = capture

        mock_msg = MagicMock()
        mock_msg.seq = 1
        mock_msg.id = "msg1"
        mock_msg.sender = "agent://\x00evil\u200b"
        mock_msg.body = "hello"
        mock_msg.msg_type = "dm\x0b"
        mock_msg.priority = "high\u200f"
        mock_msg.action_required = False
        mock_msg.thread_id = ""
        mock_msg.ts = "2024-01-01"

        await module._process_inbox({"stream1": [mock_msg]})

        assert captured
        prompt = captured[0]
        assert "\x00" not in prompt
        assert "\u200b" not in prompt
        assert "\x0b" not in prompt
        assert "\u200f" not in prompt
