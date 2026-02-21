"""Integration tests — Memory Module end-to-end with Module Bus.

Tests real component interactions: Module Bus → MarkdownMemoryModule
with NoteManager, ContextGuard, IdentityAuditor wired together.
External dependencies (ArcLLM, ArcRun) are stubbed.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest


def _mock_model(
    *, return_value: Any = None, side_effect: Exception | None = None
) -> MagicMock:
    """Create a mock LLM model with invoke() returning LLMResponse-like object."""
    model = MagicMock()
    if side_effect is not None:
        model.invoke = AsyncMock(side_effect=side_effect)
    else:
        model.invoke = AsyncMock(return_value=MagicMock(content=return_value))
    return model

from arcagent.core.agent import ArcAgent
from arcagent.core.config import (
    AgentConfig,
    ArcAgentConfig,
    EvalConfig,
    IdentityConfig,
    LLMConfig,
    ModuleEntry,
    TelemetryConfig,
)
from arcagent.core.module_bus import ModuleBus, ModuleContext
from arcagent.core.telemetry import AgentTelemetry
from arcagent.modules.memory.config import MemoryConfig
from arcagent.modules.memory.markdown_memory import MarkdownMemoryModule


def _make_telemetry() -> AgentTelemetry:
    t = MagicMock(spec=AgentTelemetry)
    t.audit_event = MagicMock()
    t.session_span = MagicMock(return_value=AsyncMock().__aenter__())
    return t


def _make_bus(config: ArcAgentConfig | None = None) -> ModuleBus:
    if config is None:
        config = ArcAgentConfig(
            agent=AgentConfig(name="test", workspace="./test-workspace"),
            llm=LLMConfig(model="test/model"),
        )
    telemetry = _make_telemetry()
    return ModuleBus()


def _make_module_ctx(bus: ModuleBus, workspace: Path) -> ModuleContext:
    config = ArcAgentConfig(
        agent=AgentConfig(name="test", workspace=str(workspace)),
        llm=LLMConfig(model="test/model"),
    )
    return ModuleContext(
        bus=bus,
        tool_registry=MagicMock(),
        config=config,
        telemetry=_make_telemetry(),
        workspace=workspace,
        llm_config=config.llm,
    )


@pytest.fixture()
def workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "workspace"
    ws.mkdir()
    return ws


class TestMemoryModuleStartupShutdown:
    """T5.1: Module loads via Module Bus and subscribes to events."""

    async def test_module_registers_handlers(self, workspace: Path) -> None:
        bus = _make_bus()
        module = MarkdownMemoryModule(
            config={},
            eval_config=EvalConfig(),
            telemetry=_make_telemetry(),
            workspace=workspace,
        )

        await module.startup(_make_module_ctx(bus, workspace))

        assert bus.handler_count("agent:pre_tool") >= 1
        assert bus.handler_count("agent:post_tool") >= 1
        assert bus.handler_count("agent:assemble_prompt") >= 1
        assert bus.handler_count("agent:post_respond") >= 1

        await module.shutdown()

    async def test_module_via_bus_lifecycle(self, workspace: Path) -> None:
        bus = _make_bus()
        module = MarkdownMemoryModule(
            config={},
            eval_config=EvalConfig(),
            telemetry=_make_telemetry(),
            workspace=workspace,
        )

        bus.register_module(module)
        await bus.startup(_make_module_ctx(bus, workspace))

        # Verify handlers registered
        assert bus.handler_count("agent:pre_tool") >= 1

        await bus.shutdown()


class TestNotesAppendOnlyEndToEnd:
    """T5.1.3: Notes append-only enforcement via Module Bus event flow."""

    async def test_write_to_notes_vetoed(self, workspace: Path) -> None:
        bus = _make_bus()
        module = MarkdownMemoryModule(
            config={},
            eval_config=EvalConfig(),
            telemetry=_make_telemetry(),
            workspace=workspace,
        )
        await module.startup(_make_module_ctx(bus, workspace))

        notes_dir = workspace / "notes"
        notes_dir.mkdir()
        note_path = notes_dir / "2026-02-15.md"
        note_path.write_text("existing content")

        ctx = await bus.emit(
            "agent:pre_tool",
            {
                "tool": "write",
                "args": {"path": str(note_path), "content": "overwrite"},
            },
        )

        assert ctx.is_vetoed
        assert "append-only" in ctx.veto_reason.lower()

        await module.shutdown()

    async def test_edit_to_notes_allowed(self, workspace: Path) -> None:
        bus = _make_bus()
        module = MarkdownMemoryModule(
            config={},
            eval_config=EvalConfig(),
            telemetry=_make_telemetry(),
            workspace=workspace,
        )
        await module.startup(_make_module_ctx(bus, workspace))

        notes_dir = workspace / "notes"
        notes_dir.mkdir()
        note_path = notes_dir / "2026-02-15.md"
        note_path.write_text("existing content")

        ctx = await bus.emit(
            "agent:pre_tool",
            {
                "tool": "edit",
                "args": {"path": str(note_path), "content": "append more"},
            },
        )

        assert not ctx.is_vetoed
        await module.shutdown()

    async def test_bash_to_notes_vetoed(self, workspace: Path) -> None:
        bus = _make_bus()
        module = MarkdownMemoryModule(
            config={},
            eval_config=EvalConfig(),
            telemetry=_make_telemetry(),
            workspace=workspace,
        )
        await module.startup(_make_module_ctx(bus, workspace))

        notes_dir = workspace / "notes"
        notes_dir.mkdir()
        note_path = notes_dir / "2026-02-15.md"
        note_path.write_text("existing content")

        ctx = await bus.emit(
            "agent:pre_tool",
            {
                "tool": "bash",
                "args": {"command": f"echo 'bad' > {note_path}"},
            },
        )

        assert ctx.is_vetoed
        await module.shutdown()


class TestIdentityAuditEndToEnd:
    """T5.1.2: Identity self-editing produces audit trail via bus events."""

    async def test_identity_edit_creates_audit(self, workspace: Path) -> None:
        telemetry = _make_telemetry()
        bus = _make_bus()
        module = MarkdownMemoryModule(
            config={},
            eval_config=EvalConfig(),
            telemetry=telemetry,
            workspace=workspace,
        )
        await module.startup(_make_module_ctx(bus, workspace))

        identity_path = workspace / "identity.md"
        identity_path.write_text("Agent: original")

        # Pre-tool: capture before state
        await bus.emit(
            "agent:pre_tool",
            {
                "tool": "write",
                "args": {"path": str(identity_path), "content": "Agent: modified"},
            },
        )

        # Simulate the write happening
        identity_path.write_text("Agent: modified")

        # Post-tool: capture after state
        await bus.emit(
            "agent:post_tool",
            {
                "tool": "write",
                "args": {"path": str(identity_path)},
            },
        )

        # Audit trail should exist
        audit_file = workspace / "audit" / "identity-changes.jsonl"
        assert audit_file.exists()
        entry = json.loads(audit_file.read_text().strip())
        assert entry["before"] == "Agent: original"
        assert entry["after"] == "Agent: modified"

        # Telemetry audit event emitted
        telemetry.audit_event.assert_called()

        await module.shutdown()


class TestContextBudgetEndToEnd:
    """T5.1.8: Context.md budget enforcement via bus events."""

    async def test_over_budget_truncates(self, workspace: Path) -> None:
        bus = _make_bus()
        module = MarkdownMemoryModule(
            config={"context_budget_tokens": 10},
            eval_config=EvalConfig(),
            telemetry=_make_telemetry(),
            workspace=workspace,
        )
        await module.startup(_make_module_ctx(bus, workspace))

        context_path = workspace / "context.md"
        # 10 tokens * 4 chars = 40 chars max
        big_content = "x" * 200

        args: dict[str, Any] = {
            "path": str(context_path),
            "content": big_content,
        }
        await bus.emit(
            "agent:pre_tool",
            {
                "tool": "write",
                "args": args,
            },
        )

        # Content should be truncated
        assert len(args["content"]) < 200

        await module.shutdown()


class TestAssemblePromptWithNotes:
    """T5.1.1: Notes injected into system prompt via assemble_prompt event."""

    async def test_notes_injected(self, workspace: Path) -> None:
        from datetime import date

        bus = _make_bus()
        module = MarkdownMemoryModule(
            config={},
            eval_config=EvalConfig(),
            telemetry=_make_telemetry(),
            workspace=workspace,
        )
        await module.startup(_make_module_ctx(bus, workspace))

        # Create today's notes
        notes_dir = workspace / "notes"
        notes_dir.mkdir()
        today = date.today().isoformat()
        (notes_dir / f"{today}.md").write_text("Today's important notes")

        sections: dict[str, str] = {}
        await bus.emit("agent:assemble_prompt", {"sections": sections})

        assert "notes" in sections
        assert "important notes" in sections["notes"].lower()

        await module.shutdown()


class TestEntityExtractionIntegration:
    """T5.1.4: Entity extraction triggered by post_respond event."""

    async def test_entity_extraction_path(self, workspace: Path) -> None:
        """Verify the entity extractor can be invoked through the module."""
        from arcagent.modules.memory.entity_extractor import EntityExtractor

        telemetry = _make_telemetry()
        extractor = EntityExtractor(
            eval_config=EvalConfig(),
            workspace=workspace,
            telemetry=telemetry,
        )

        model = _mock_model(
            return_value=json.dumps(
                {
                    "entities": [
                        {
                            "name": "ArcAgent",
                            "type": "project",
                            "aliases": ["Arc"],
                            "facts": [
                                {"predicate": "type", "value": "AI framework", "confidence": 0.9}
                            ],
                        }
                    ]
                }
            )
        )

        messages = [
            {"role": "user", "content": "ArcAgent is an AI framework for building agents"},
            {"role": "assistant", "content": "That's right, ArcAgent handles the orchestration"},
        ]
        await extractor.extract(messages, model)

        entity_path = workspace / "entities" / "arcagent.md"
        assert entity_path.exists()

        content = entity_path.read_text(encoding="utf-8")
        assert "name: ArcAgent" in content
        assert "type: project" in content
        assert "AI framework" in content


class TestHybridSearchIntegration:
    """T5.1.5: Hybrid search across notes and entities."""

    async def test_search_finds_notes_content(self, workspace: Path) -> None:
        from arcagent.modules.memory.hybrid_search import HybridSearch

        search = HybridSearch(workspace=workspace, config=MemoryConfig())

        # Create searchable content
        notes_dir = workspace / "notes"
        notes_dir.mkdir()
        (notes_dir / "2026-02-15.md").write_text("Meeting about ArcAgent architecture decisions")

        await search.reindex_if_needed()
        results = await search.search("ArcAgent architecture", top_k=5)

        assert len(results) >= 1
        assert any("arcagent" in r.content.lower() for r in results)
        await search.close()


class TestPolicyEvaluationIntegration:
    """T5.1.6: ACE policy evaluation cycle."""

    async def test_policy_evaluation_creates_bullets(self, workspace: Path) -> None:
        from arcagent.modules.policy.config import PolicyConfig
        from arcagent.modules.policy.policy_engine import PolicyEngine

        engine = PolicyEngine(
            config=PolicyConfig(),
            workspace=workspace,
            telemetry=_make_telemetry(),
        )

        model = _mock_model(
            return_value=json.dumps(
                {
                    "additions": ["Always verify test results before claiming success"],
                    "updates": [],
                    "rewrites": [],
                }
            )
        )

        messages = [
            {"role": "user", "content": "Run the tests"},
            {"role": "assistant", "content": "Tests pass. All 50 green."},
        ]
        await engine.evaluate(messages, model)

        policy_path = workspace / "policy.md"
        assert policy_path.exists()
        content = policy_path.read_text()
        assert "verify" in content.lower()
        assert "score:5" in content


class TestNonMemoryPathIgnored:
    """Verify events for non-memory paths are not intercepted."""

    async def test_write_to_random_file_not_vetoed(self, workspace: Path) -> None:
        bus = _make_bus()
        module = MarkdownMemoryModule(
            config={},
            eval_config=EvalConfig(),
            telemetry=_make_telemetry(),
            workspace=workspace,
        )
        await module.startup(_make_module_ctx(bus, workspace))

        random_file = workspace / "some_code.py"
        ctx = await bus.emit(
            "agent:pre_tool",
            {
                "tool": "write",
                "args": {"path": str(random_file), "content": "print('hello')"},
            },
        )

        assert not ctx.is_vetoed
        await module.shutdown()


class TestAgentWithMemoryModule:
    """T5.2: Module registration in agent.py startup."""

    async def test_agent_startup_with_memory_enabled(
        self, tmp_path: Path, workspace: Path
    ) -> None:
        config = ArcAgentConfig(
            agent=AgentConfig(
                name="memory-agent",
                org="testorg",
                type="executor",
                workspace=str(workspace),
            ),
            llm=LLMConfig(model="test/model"),
            identity=IdentityConfig(key_dir=str(tmp_path / "keys")),
            telemetry=TelemetryConfig(enabled=True),
            modules={"memory": ModuleEntry(enabled=True)},
            eval=EvalConfig(),
        )

        agent = ArcAgent(config=config)
        await agent.startup()

        # Memory module should be registered
        assert agent._bus is not None
        assert len(agent._bus._modules) >= 1
        assert any(m.name == "memory" for m in agent._bus._modules)

        # Handlers should be registered
        assert agent._bus.handler_count("agent:pre_tool") >= 1
        assert agent._bus.handler_count("agent:assemble_prompt") >= 1

        await agent.shutdown()

    async def test_agent_startup_with_memory_disabled(
        self, tmp_path: Path, workspace: Path
    ) -> None:
        config = ArcAgentConfig(
            agent=AgentConfig(
                name="no-memory-agent",
                org="testorg",
                type="executor",
                workspace=str(workspace),
            ),
            llm=LLMConfig(model="test/model"),
            identity=IdentityConfig(key_dir=str(tmp_path / "keys")),
            telemetry=TelemetryConfig(enabled=True),
            modules={"memory": ModuleEntry(enabled=False)},
        )

        agent = ArcAgent(config=config)
        await agent.startup()

        # Memory module should NOT be registered
        assert agent._bus is not None
        assert not any(m.name == "memory" for m in agent._bus._modules)

        await agent.shutdown()

    async def test_agent_startup_without_memory_config(
        self, tmp_path: Path, workspace: Path
    ) -> None:
        config = ArcAgentConfig(
            agent=AgentConfig(
                name="plain-agent",
                org="testorg",
                type="executor",
                workspace=str(workspace),
            ),
            llm=LLMConfig(model="test/model"),
            identity=IdentityConfig(key_dir=str(tmp_path / "keys")),
            telemetry=TelemetryConfig(enabled=True),
        )

        agent = ArcAgent(config=config)
        await agent.startup()

        # No memory module when not in config
        assert agent._bus is not None
        assert not any(m.name == "memory" for m in agent._bus._modules)

        await agent.shutdown()
