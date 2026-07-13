"""Adversarial security: memory-tool writes are sign->authorize->audit, fail-closed.

An UNSIGNED or policy-DENIED state-modifying call MUST NOT mutate the store and
MUST still audit. A signed+authorized call succeeds and audits. A raising pipeline
fails closed. Read tools audit even without a write authorization.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from arctrust.audit import AuditEvent
from arctrust.identity import AgentIdentity
from arctrust.policy import PolicyContext, ToolCall, build_pipeline

from arcmemory.config import MemoryConfig
from arcmemory.db import MemoryDB
from arcmemory.index.graph import WeightedGraph
from arcmemory.stores.semantic import SemanticStore
from arcmemory.tools import build_memory_tools

_CALLER = "did:arc:default:memory/deadbeef"


class _RecordingSink:
    def __init__(self) -> None:
        self.events: list[AuditEvent] = []

    def write(self, event: AuditEvent) -> None:
        self.events.append(event)


def _semantic(workspace: Path, db: MemoryDB) -> SemanticStore:
    graph = WeightedGraph(db, MemoryConfig())
    return SemanticStore(workspace, graph, scope=_CALLER)


def _tool(tools: list[Any], name: str) -> Any:
    return next(t for t in tools if t.name == name)


async def test_signed_authorized_write_succeeds_and_audits(
    workspace: Path, db: MemoryDB
) -> None:
    identity = AgentIdentity.generate(org="default", agent_type="memory")
    caller = identity.did
    sink = _RecordingSink()
    pipeline = build_pipeline(tier="personal")
    tools = build_memory_tools(
        workspace=workspace,
        db=db,
        config=MemoryConfig(),
        caller_did=caller,
        identity=identity,
        policy_pipeline=pipeline,
        audit_sink=sink,
    )
    result = await _tool(tools, "write_fact").execute(
        {"slug": "brad-baker", "predicate": "role", "value": "cto"}
    )
    assert "wrote" in result
    entity = SemanticStore(
        workspace, WeightedGraph(db, MemoryConfig()), scope=caller
    ).read("brad-baker")
    assert entity is not None and entity.facts  # the store WAS mutated
    assert any(e.action == "memory.tool.write_fact" and e.outcome == "allow" for e in sink.events)


async def test_unsigned_write_is_blocked_and_does_not_mutate(
    workspace: Path, db: MemoryDB
) -> None:
    sink = _RecordingSink()
    pipeline = build_pipeline(tier="personal")
    tools = build_memory_tools(
        workspace=workspace,
        db=db,
        config=MemoryConfig(),
        caller_did=_CALLER,
        identity=None,  # no signer -> cannot sign -> fail closed
        policy_pipeline=pipeline,
        audit_sink=sink,
    )
    result = await _tool(tools, "write_fact").execute(
        {"slug": "mallory", "predicate": "role", "value": "attacker"}
    )
    assert result.startswith("denied")
    assert _semantic(workspace, db).read("mallory") is None  # NO mutation
    assert any(e.action == "memory.tool.write_fact" and e.outcome == "deny" for e in sink.events)


async def test_policy_denied_write_is_blocked_and_does_not_mutate(
    workspace: Path, db: MemoryDB
) -> None:
    identity = AgentIdentity.generate(org="default", agent_type="memory")
    sink = _RecordingSink()
    pipeline = build_pipeline(
        tier="personal", global_deny_rules={"write_fact": "writes denied by policy"}
    )
    tools = build_memory_tools(
        workspace=workspace,
        db=db,
        config=MemoryConfig(),
        caller_did=identity.did,
        identity=identity,
        policy_pipeline=pipeline,
        audit_sink=sink,
    )
    result = await _tool(tools, "write_fact").execute(
        {"slug": "widget", "predicate": "kind", "value": "thing"}
    )
    assert result.startswith("denied")
    assert _semantic(workspace, db).read("widget") is None  # NO mutation
    assert any(e.outcome == "deny" for e in sink.events)


class _RaisingPipeline:
    """A pipeline whose evaluate raises — the wrapper must fail closed."""

    async def evaluate(self, call: ToolCall, ctx: PolicyContext) -> Any:
        raise RuntimeError("policy backend unavailable")


async def test_raising_pipeline_fails_closed(workspace: Path, db: MemoryDB) -> None:
    identity = AgentIdentity.generate(org="default", agent_type="memory")
    sink = _RecordingSink()
    tools = build_memory_tools(
        workspace=workspace,
        db=db,
        config=MemoryConfig(),
        caller_did=identity.did,
        identity=identity,
        # reason: a deliberately-raising stand-in for PolicyPipeline to prove fail-closed
        policy_pipeline=_RaisingPipeline(),  # type: ignore[arg-type]
        audit_sink=sink,
    )
    result = await _tool(tools, "write_fact").execute(
        {"slug": "sneaky", "predicate": "x", "value": "y"}
    )
    assert result.startswith("denied")
    assert "policy-error" in result
    assert _semantic(workspace, db).read("sneaky") is None  # NO mutation


async def test_read_tool_audits_even_without_authz(workspace: Path, db: MemoryDB) -> None:
    sink = _RecordingSink()
    pipeline = build_pipeline(tier="personal")
    tools = build_memory_tools(
        workspace=workspace,
        db=db,
        config=MemoryConfig(),
        caller_did=_CALLER,
        identity=None,  # no signer
        policy_pipeline=pipeline,
        audit_sink=sink,
    )
    out = await _tool(tools, "list_recent_episodes").execute({"limit": 5})
    assert "episode" in out  # ran (no episodes yet)
    assert any(e.action == "memory.tool.list_recent_episodes" for e in sink.events)
