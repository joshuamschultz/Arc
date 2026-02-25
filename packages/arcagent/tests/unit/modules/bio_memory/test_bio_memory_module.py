"""Tests for BioMemoryModule — facade, bus subscriptions, tool registration, mutual exclusivity."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from arcagent.core.config import AgentConfig, ArcAgentConfig, LLMConfig
from arcagent.core.errors import ConfigError
from arcagent.core.module_bus import EventContext, ModuleBus, ModuleContext
from arcagent.modules.bio_memory.bio_memory_module import BioMemoryModule


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    return tmp_path / "workspace"


@pytest.fixture
def telemetry() -> MagicMock:
    t = MagicMock()
    t.audit_event = MagicMock()
    return t


@pytest.fixture
def bus() -> ModuleBus:
    return ModuleBus()


@pytest.fixture
def tool_registry() -> MagicMock:
    return MagicMock()


@pytest.fixture
def module_ctx(
    bus: ModuleBus,
    tool_registry: MagicMock,
    workspace: Path,
    telemetry: MagicMock,
) -> ModuleContext:
    return ModuleContext(
        bus=bus,
        tool_registry=tool_registry,
        config=ArcAgentConfig(
            agent=AgentConfig(name="test-agent"),
            llm=LLMConfig(model="test/model"),
        ),
        telemetry=telemetry,
        workspace=workspace,
        llm_config=LLMConfig(model="test/model"),
    )


@pytest.fixture
def module(workspace: Path, telemetry: MagicMock) -> BioMemoryModule:
    return BioMemoryModule(
        workspace=workspace,
        telemetry=telemetry,
    )


class TestModuleProtocol:
    """BioMemoryModule satisfies the Module protocol."""

    def test_name(self, module: BioMemoryModule) -> None:
        assert module.name == "bio_memory"

    @pytest.mark.asyncio
    async def test_startup_registers_handlers(
        self, module: BioMemoryModule, module_ctx: ModuleContext, bus: ModuleBus,
    ) -> None:
        await module.startup(module_ctx)
        # Should have registered handlers for these events
        assert bus.handler_count("agent:assemble_prompt") >= 1
        assert bus.handler_count("agent:post_respond") >= 1
        assert bus.handler_count("agent:pre_tool") >= 1
        assert bus.handler_count("agent:post_tool") >= 1
        assert bus.handler_count("agent:shutdown") >= 1

    @pytest.mark.asyncio
    async def test_startup_registers_tools(
        self, module: BioMemoryModule, module_ctx: ModuleContext,
        tool_registry: MagicMock,
    ) -> None:
        await module.startup(module_ctx)
        # Should register 5 tools
        assert tool_registry.register.call_count == 5
        tool_names = [
            call.args[0].name for call in tool_registry.register.call_args_list
        ]
        assert "memory_search" in tool_names
        assert "memory_note" in tool_names
        assert "memory_recall" in tool_names
        assert "memory_reflect" in tool_names
        assert "memory_consolidate_deep" in tool_names

    @pytest.mark.asyncio
    async def test_shutdown_no_error(self, module: BioMemoryModule) -> None:
        await module.shutdown()


class TestMutualExclusivity:
    """Bio-memory and markdown-memory cannot both be active."""

    @pytest.mark.asyncio
    async def test_raises_if_memory_module_present(
        self, module: BioMemoryModule, module_ctx: ModuleContext,
        bus: ModuleBus,
    ) -> None:
        # Register a fake "memory" module
        fake_memory = MagicMock()
        fake_memory.name = "memory"
        bus.register_module(fake_memory)

        with pytest.raises(ConfigError, match="mutually exclusive"):
            await module.startup(module_ctx)


class TestBashVeto:
    """Pre-tool handler vetoes bash commands targeting memory paths."""

    @pytest.mark.asyncio
    async def test_bash_targeting_memory_vetoed(
        self, module: BioMemoryModule, module_ctx: ModuleContext,
        bus: ModuleBus, workspace: Path,
    ) -> None:
        await module.startup(module_ctx)

        ctx = EventContext(
            event="agent:pre_tool",
            data={
                "tool": "bash",
                "args": {"command": f"rm {workspace}/memory/working.md"},
            },
            agent_did="test",
            trace_id="t1",
        )
        await bus.emit("agent:pre_tool", ctx.data)
        # The emitted context should be vetoed
        result = await bus.emit(
            "agent:pre_tool",
            {
                "tool": "bash",
                "args": {"command": f"cat {workspace}/memory/working.md"},
            },
        )
        assert result.is_vetoed

    @pytest.mark.asyncio
    async def test_bash_not_targeting_memory_allowed(
        self, module: BioMemoryModule, module_ctx: ModuleContext,
        bus: ModuleBus,
    ) -> None:
        await module.startup(module_ctx)

        result = await bus.emit(
            "agent:pre_tool",
            {
                "tool": "bash",
                "args": {"command": "ls /tmp"},
            },
        )
        assert not result.is_vetoed


class TestBashVetoSecurity:
    """Bash veto uses shlex parsing and handles edge cases."""

    @pytest.mark.asyncio
    async def test_bash_sed_targeting_memory_vetoed(
        self, module: BioMemoryModule, module_ctx: ModuleContext,
        bus: ModuleBus, workspace: Path,
    ) -> None:
        """sed with memory subpath detected by dangerous command check."""
        await module.startup(module_ctx)
        result = await bus.emit(
            "agent:pre_tool",
            {
                "tool": "bash",
                "args": {"command": f"sed -i 's/old/new/' {workspace}/memory/working.md"},
            },
        )
        assert result.is_vetoed

    @pytest.mark.asyncio
    async def test_bash_malformed_shell_fallback(
        self, module: BioMemoryModule, module_ctx: ModuleContext,
        bus: ModuleBus,
    ) -> None:
        """Malformed shell with memory path still vetoed (fallback to substring)."""
        await module.startup(module_ctx)
        result = await bus.emit(
            "agent:pre_tool",
            {
                "tool": "bash",
                "args": {"command": "echo 'unclosed memory/ quote"},
            },
        )
        assert result.is_vetoed

    @pytest.mark.asyncio
    async def test_non_bash_tool_not_vetoed(
        self, module: BioMemoryModule, module_ctx: ModuleContext,
        bus: ModuleBus,
    ) -> None:
        await module.startup(module_ctx)
        result = await bus.emit(
            "agent:pre_tool",
            {
                "tool": "read",
                "args": {"file_path": "memory/working.md"},
            },
        )
        assert not result.is_vetoed


class TestToolHandlers:
    """Tool handlers emit audit events, sanitize input, handle all targets."""

    @pytest.mark.asyncio
    async def test_memory_note_episode_target(
        self, module: BioMemoryModule, telemetry: MagicMock,
    ) -> None:
        """memory_note with target='episode' creates episode file (T-2 PRD)."""
        # Create the memory/episodes directory
        memory_dir = module._memory_dir
        memory_dir.mkdir(parents=True, exist_ok=True)

        result = await module._handle_memory_note(
            content="Important discovery about deployment",
            target="episode",
        )
        assert "Episode created" in result
        episodes_dir = memory_dir / "episodes"
        assert episodes_dir.exists()
        episode_files = list(episodes_dir.glob("*.md"))
        assert len(episode_files) == 1

    @pytest.mark.asyncio
    async def test_memory_note_sanitizes_content(
        self, module: BioMemoryModule, telemetry: MagicMock,
    ) -> None:
        """memory_note strips zero-width characters (ASI-06)."""
        memory_dir = module._memory_dir
        memory_dir.mkdir(parents=True, exist_ok=True)

        # Content with zero-width characters
        poisoned = "Normal text\u200bwith\u200finvisible\ufeffchars"
        result = await module._handle_memory_note(content=poisoned, target="working")
        assert "Note recorded" in result

        text = (memory_dir / "working.md").read_text(encoding="utf-8")
        assert "\u200b" not in text
        assert "\u200f" not in text
        assert "\ufeff" not in text

    @pytest.mark.asyncio
    async def test_memory_search_emits_audit(
        self, module: BioMemoryModule, telemetry: MagicMock,
    ) -> None:
        """memory_search emits audit event (NIST AU-2)."""
        await module._handle_memory_search(query="test")
        telemetry.audit_event.assert_called()
        call_args = telemetry.audit_event.call_args
        assert "memory.searched" in str(call_args)

    @pytest.mark.asyncio
    async def test_memory_recall_emits_audit(
        self, module: BioMemoryModule, telemetry: MagicMock,
    ) -> None:
        """memory_recall emits audit event (NIST AU-2)."""
        await module._handle_memory_recall(name="test-entity")
        telemetry.audit_event.assert_called()
        call_args = telemetry.audit_event.call_args
        assert "memory.recalled" in str(call_args)

    @pytest.mark.asyncio
    async def test_memory_note_emits_audit(
        self, module: BioMemoryModule, telemetry: MagicMock,
    ) -> None:
        """memory_note emits audit event for working target (NIST AU-2)."""
        memory_dir = module._memory_dir
        memory_dir.mkdir(parents=True, exist_ok=True)

        await module._handle_memory_note(content="Test note")
        telemetry.audit_event.assert_called()
        call_args = telemetry.audit_event.call_args
        assert "memory.note_written" in str(call_args)


class TestPostToolAudit:
    """_on_post_tool audits write/edit operations targeting memory files."""

    @pytest.mark.asyncio
    async def test_post_tool_audits_write_to_memory(
        self, module: BioMemoryModule, module_ctx: ModuleContext,
        bus: ModuleBus, workspace: Path, telemetry: MagicMock,
    ) -> None:
        """Write tool targeting memory path emits audit event."""
        await module.startup(module_ctx)
        # Create memory dir so _is_memory_path can resolve
        memory_dir = workspace / "memory"
        memory_dir.mkdir(parents=True, exist_ok=True)

        await bus.emit(
            "agent:post_tool",
            {
                "tool": "write",
                "args": {"file_path": str(memory_dir / "working.md")},
            },
        )
        # Check audit was called with memory.file_modified_by_tool
        calls = [str(c) for c in telemetry.audit_event.call_args_list]
        assert any("memory.file_modified_by_tool" in c for c in calls)

    @pytest.mark.asyncio
    async def test_post_tool_ignores_non_memory_write(
        self, module: BioMemoryModule, module_ctx: ModuleContext,
        bus: ModuleBus, telemetry: MagicMock,
    ) -> None:
        """Write tool NOT targeting memory does not emit audit."""
        await module.startup(module_ctx)
        telemetry.audit_event.reset_mock()

        await bus.emit(
            "agent:post_tool",
            {
                "tool": "write",
                "args": {"file_path": "/tmp/not-memory.txt"},
            },
        )
        calls = [str(c) for c in telemetry.audit_event.call_args_list]
        assert not any("memory.file_modified_by_tool" in c for c in calls)

    @pytest.mark.asyncio
    async def test_post_tool_ignores_non_write_tools(
        self, module: BioMemoryModule, module_ctx: ModuleContext,
        bus: ModuleBus, telemetry: MagicMock,
    ) -> None:
        """Non-write/edit tools don't trigger audit."""
        await module.startup(module_ctx)
        telemetry.audit_event.reset_mock()

        await bus.emit(
            "agent:post_tool",
            {
                "tool": "read",
                "args": {"file_path": "memory/working.md"},
            },
        )
        calls = [str(c) for c in telemetry.audit_event.call_args_list]
        assert not any("memory.file_modified_by_tool" in c for c in calls)


class TestConstructor:
    """Constructor handles various parameter combinations."""

    def test_default_construction(self, workspace: Path) -> None:
        m = BioMemoryModule(workspace=workspace)
        assert m.name == "bio_memory"

    def test_custom_config(self, workspace: Path) -> None:
        m = BioMemoryModule(
            config={"total_per_turn": 8000, "identity_budget": 1000},
            workspace=workspace,
        )
        assert m.name == "bio_memory"


class TestAssemblePrompt:
    """_on_assemble_prompt injects identity, working memory, and entity hint."""

    @pytest.mark.asyncio
    async def test_injects_identity_and_working(
        self, module: BioMemoryModule, module_ctx: ModuleContext, bus: ModuleBus,
    ) -> None:
        await module.startup(module_ctx)
        memory_dir = module._memory_dir
        memory_dir.mkdir(parents=True, exist_ok=True)

        # Create identity and working memory files
        (memory_dir / "how-i-work.md").write_text(
            "---\ntitle: identity\n---\n\nI help with code.\n", encoding="utf-8",
        )
        (memory_dir / "working.md").write_text(
            "---\ntype: note\n---\n\nCurrent task notes.\n", encoding="utf-8",
        )

        result = await bus.emit("agent:assemble_prompt", {})
        ctx_data = result.data
        mem_ctx = ctx_data.get("memory_context", "")
        assert "<agent-identity>" in mem_ctx
        assert "<working-memory>" in mem_ctx

    @pytest.mark.asyncio
    async def test_injects_entity_hint_when_dir_exists(
        self, module: BioMemoryModule, module_ctx: ModuleContext,
        bus: ModuleBus, workspace: Path,
    ) -> None:
        await module.startup(module_ctx)
        module._memory_dir.mkdir(parents=True, exist_ok=True)

        # Create entities dir
        entities_dir = workspace / "entities"
        entities_dir.mkdir(parents=True, exist_ok=True)

        result = await bus.emit("agent:assemble_prompt", {})
        mem_ctx = result.data.get("memory_context", "")
        assert "<memory-hint>" in mem_ctx

    @pytest.mark.asyncio
    async def test_no_context_when_empty(
        self, module: BioMemoryModule, module_ctx: ModuleContext, bus: ModuleBus,
    ) -> None:
        await module.startup(module_ctx)
        result = await bus.emit("agent:assemble_prompt", {})
        # No memory files exist, so memory_context should not be set
        assert "memory_context" not in result.data


class TestPostRespond:
    """_on_post_respond captures messages."""

    @pytest.mark.asyncio
    async def test_captures_messages(
        self, module: BioMemoryModule, module_ctx: ModuleContext, bus: ModuleBus,
    ) -> None:
        await module.startup(module_ctx)
        messages = [{"role": "user", "content": "hello"}]
        await bus.emit("agent:post_respond", {"messages": messages})
        assert module._messages == messages


class TestOnShutdown:
    """_on_shutdown triggers light consolidation."""

    @pytest.mark.asyncio
    async def test_skips_when_no_messages(
        self, module: BioMemoryModule, module_ctx: ModuleContext, bus: ModuleBus,
    ) -> None:
        """No messages means no consolidation."""
        await module.startup(module_ctx)
        # No messages set - should return without spawning
        await bus.emit("agent:shutdown", {})
        # No error = success

    @pytest.mark.asyncio
    async def test_skips_when_light_disabled(
        self, workspace: Path, telemetry: MagicMock,
        module_ctx: ModuleContext, bus: ModuleBus,
    ) -> None:
        """light_on_shutdown=False skips consolidation."""
        m = BioMemoryModule(
            config={"light_on_shutdown": False},
            workspace=workspace,
            telemetry=telemetry,
        )
        await m.startup(module_ctx)
        m._messages = [{"role": "user", "content": "test"}]
        await bus.emit("agent:shutdown", {})

    @pytest.mark.asyncio
    async def test_warns_when_no_eval_model(
        self, module: BioMemoryModule, module_ctx: ModuleContext,
        bus: ModuleBus,
    ) -> None:
        """No eval model logs warning."""
        await module.startup(module_ctx)
        module._messages = [{"role": "user", "content": "test"}]
        # No eval_config set, so _get_eval_model returns None
        await bus.emit("agent:shutdown", {})


class TestShutdownWithTasks:
    """shutdown() awaits background tasks."""

    @pytest.mark.asyncio
    async def test_awaits_background_tasks(
        self, module: BioMemoryModule,
    ) -> None:
        completed = False

        async def background_work() -> None:
            nonlocal completed
            completed = True

        task = asyncio.create_task(background_work())
        module._background_tasks.add(task)
        await module.shutdown()
        assert completed


class TestMemoryNoteUnknownTarget:
    """memory_note with unknown target returns error message."""

    @pytest.mark.asyncio
    async def test_unknown_target(
        self, module: BioMemoryModule,
    ) -> None:
        result = await module._handle_memory_note(
            content="test", target="invalid",
        )
        assert "Unknown target" in result


class TestMemorySearchWithResults:
    """memory_search returns formatted results when matches found."""

    @pytest.mark.asyncio
    async def test_returns_formatted_results(
        self, module: BioMemoryModule, telemetry: MagicMock,
    ) -> None:
        memory_dir = module._memory_dir
        episodes_dir = memory_dir / "episodes"
        episodes_dir.mkdir(parents=True, exist_ok=True)

        # Create an episode with matching content
        (episodes_dir / "2026-01-01-deploy-notes.md").write_text(
            "---\ntitle: deploy-notes\ndate: 2026-01-01\ntags: []\n---\n\n"
            "Deployed the new API to production.\n",
            encoding="utf-8",
        )

        result = await module._handle_memory_search(query="deploy")
        assert "<memory-result" in result
        assert "deploy" in result.lower()


class TestMemoryRecallWithResult:
    """memory_recall returns content when episode/entity found."""

    @pytest.mark.asyncio
    async def test_returns_content_when_found(
        self, module: BioMemoryModule, telemetry: MagicMock,
    ) -> None:
        memory_dir = module._memory_dir
        episodes_dir = memory_dir / "episodes"
        episodes_dir.mkdir(parents=True, exist_ok=True)

        (episodes_dir / "2026-01-01-my-session.md").write_text(
            "---\ntitle: my-session\ndate: 2026-01-01\n---\n\nSession content.\n",
            encoding="utf-8",
        )

        result = await module._handle_memory_recall(name="2026-01-01-my-session")
        assert "<memory-result" in result
        assert "Session content" in result


class TestMemoryReflect:
    """memory_reflect handler triggers identity evaluation."""

    @pytest.mark.asyncio
    async def test_reflect_no_model(
        self, module: BioMemoryModule,
    ) -> None:
        """Reflect without eval model returns unavailable message."""
        result = await module._handle_memory_reflect()
        assert "unavailable" in result.lower()

    @pytest.mark.asyncio
    async def test_reflect_no_messages(
        self, module: BioMemoryModule,
    ) -> None:
        """Reflect with model but no messages returns no changes."""
        mock_model = MagicMock()
        module._eval_model = mock_model
        result = await module._handle_memory_reflect()
        assert "No significant changes" in result

    @pytest.mark.asyncio
    async def test_reflect_updates_identity(
        self, module: BioMemoryModule, telemetry: MagicMock,
    ) -> None:
        """Reflect with model + messages triggers identity update."""
        memory_dir = module._memory_dir
        memory_dir.mkdir(parents=True, exist_ok=True)
        (memory_dir / "how-i-work.md").write_text(
            "---\ntitle: identity\n---\n\nOld identity.\n", encoding="utf-8",
        )

        mock_model = MagicMock()
        module._eval_model = mock_model
        module._messages = [{"role": "user", "content": "test msg"}]

        # Mock the consolidator's evaluate_identity to return new content
        module._consolidator.evaluate_identity = AsyncMock(
            return_value="Updated identity content.",
        )

        result = await module._handle_memory_reflect(focus="communication")
        assert "Identity updated" in result
        telemetry.audit_event.assert_called()


class TestDeepConsolidation:
    """memory_consolidate_deep handler."""

    @pytest.mark.asyncio
    async def test_deep_no_model(
        self, module: BioMemoryModule,
    ) -> None:
        """Deep consolidation without eval model returns unavailable."""
        result = await module._handle_deep_consolidation()
        assert "unavailable" in result.lower()

    @pytest.mark.asyncio
    async def test_deep_skipped_result(
        self, module: BioMemoryModule, telemetry: MagicMock,
    ) -> None:
        """Deep consolidation skipped formats correctly."""
        mock_model = MagicMock()
        module._eval_model = mock_model
        module._memory_dir.mkdir(parents=True, exist_ok=True)

        # No episodes → skip
        result = await module._handle_deep_consolidation()
        assert "skipped" in result.lower()
        telemetry.audit_event.assert_called()

    @pytest.mark.asyncio
    async def test_deep_with_results(
        self, module: BioMemoryModule, telemetry: MagicMock,
    ) -> None:
        """Deep consolidation with recent activity formats entity/graph/stale stats."""
        mock_model = MagicMock()
        module._eval_model = mock_model
        module._memory_dir.mkdir(parents=True, exist_ok=True)

        # Mock DeepConsolidator.consolidate to return full result
        mock_result = {
            "intensity": "full",
            "entity_pass": {"entities_rewritten": 2, "skipped_unchanged": 1},
            "graph_pass": {"links_added": 3},
            "stale": {"flagged": 1, "archived": 0},
            "identity_refreshed": True,
        }

        with patch(
            "arcagent.modules.bio_memory.deep_consolidator.DeepConsolidator",
        ) as mock_cls:
            instance = mock_cls.return_value
            instance.consolidate = AsyncMock(return_value=mock_result)

            result = await module._handle_deep_consolidation()

        assert "complete" in result.lower()
        assert "Entities rewritten: 2" in result
        assert "Graph links added: 3" in result
        assert "Identity refreshed" in result


class TestGetTeamService:
    """_get_team_service lazy-initializes TeamMemoryService."""

    def test_returns_none_when_no_team_config(
        self, module: BioMemoryModule,
    ) -> None:
        assert module._get_team_service() is None

    def test_returns_none_when_arcteam_import_fails(
        self, workspace: Path, telemetry: MagicMock,
    ) -> None:
        m = BioMemoryModule(
            workspace=workspace,
            telemetry=telemetry,
            team_config={"root_path": "/tmp/team"},
        )
        # Simulate arcteam not installed by patching the import
        with patch.dict(
            "sys.modules",
            {"arcteam.memory.config": None, "arcteam.memory.service": None},
        ):
            result = m._get_team_service()
        assert result is None

    def test_returns_cached_service(
        self, module: BioMemoryModule,
    ) -> None:
        fake_service = MagicMock()
        module._team_service = fake_service
        assert module._get_team_service() is fake_service


class TestGetTeamEntitiesDir:
    """_get_team_entities_dir resolves team entity path."""

    def test_returns_none_when_no_team_config(
        self, module: BioMemoryModule,
    ) -> None:
        assert module._get_team_entities_dir() is None

    def test_returns_none_when_no_root(
        self, workspace: Path, telemetry: MagicMock,
    ) -> None:
        m = BioMemoryModule(
            workspace=workspace,
            telemetry=telemetry,
            team_config={"other": "value"},
        )
        assert m._get_team_entities_dir() is None

    def test_returns_path_when_exists(
        self, workspace: Path, telemetry: MagicMock, tmp_path: Path,
    ) -> None:
        team_root = tmp_path / "team"
        team_entities = team_root / "entities"
        team_entities.mkdir(parents=True)

        m = BioMemoryModule(
            workspace=workspace,
            telemetry=telemetry,
            team_config={"root_path": str(team_root)},
        )
        assert m._get_team_entities_dir() == team_entities

    def test_returns_none_when_dir_missing(
        self, workspace: Path, telemetry: MagicMock, tmp_path: Path,
    ) -> None:
        m = BioMemoryModule(
            workspace=workspace,
            telemetry=telemetry,
            team_config={"root_path": str(tmp_path / "nonexistent")},
        )
        assert m._get_team_entities_dir() is None


class TestGetEvalModel:
    """_get_eval_model caches and returns model."""

    def test_returns_none_without_config(
        self, module: BioMemoryModule,
    ) -> None:
        result = module._get_eval_model()
        assert result is None

    def test_caches_model(
        self, module: BioMemoryModule,
    ) -> None:
        """After first call sets _eval_model, second call returns cached."""
        fake_model = MagicMock()
        with patch(
            "arcagent.modules.bio_memory.bio_memory_module.get_eval_model",
            return_value=fake_model,
        ):
            result = module._get_eval_model()
        assert result is fake_model
        assert module._eval_model is fake_model


class TestIsMemoryPathEntities:
    """_is_memory_path covers entities directory branch."""

    def test_entities_path_detected(
        self, module: BioMemoryModule, workspace: Path,
    ) -> None:
        entities_dir = workspace / "entities"
        entities_dir.mkdir(parents=True, exist_ok=True)
        entity_path = entities_dir / "test.md"
        entity_path.touch()
        assert module._is_memory_path(entity_path.resolve()) is True

    def test_unrelated_path_not_detected(
        self, module: BioMemoryModule, tmp_path: Path,
    ) -> None:
        unrelated = tmp_path / "other" / "file.md"
        unrelated.parent.mkdir(parents=True, exist_ok=True)
        unrelated.touch()
        assert module._is_memory_path(unrelated.resolve()) is False
