"""Tests for SPEC-017 Phase 2: Tool Policy Pipeline.

Coverage targets (from PLAN §Phase 2):
- Pydantic model frozenness and round-trip
- Pipeline happy path (empty → ALLOW)
- First-DENY-wins short-circuit
- Fail-closed on layer exception
- Structured deny reasons (layer, rule_id, input values)
- Per-tier layer composition
- Decision cache hit/miss
- Shadow mode (log-only)
- Air-gapped restricted mode (stale bundle → deny all except safe set)
- Telemetry emission per evaluation
"""

from __future__ import annotations

import time
from typing import Any
from unittest.mock import MagicMock


class TestPolicyModels:
    """Phase 2 Task 2.1: Decision / ToolCall / PolicyContext Pydantic models."""

    def test_decision_allow(self) -> None:
        from arcagent.core.tool_policy import Decision

        d = Decision.allow(input_hash="h", evaluated_at_us=1)
        assert d.outcome == "allow"
        assert d.layer is None
        assert d.rule_id is None

    def test_decision_deny_carries_3_questions(self) -> None:
        """Deny reason must answer: which layer, which rule, what inputs."""
        from arcagent.core.tool_policy import Decision

        d = Decision.deny(
            layer="agent",
            rule_id="allowlist.v1",
            reason="Tool 'bash' not in agent allowlist; agent has [read, grep]",
            input_hash="h",
            evaluated_at_us=2,
        )
        assert d.outcome == "deny"
        assert d.layer == "agent"
        assert d.rule_id == "allowlist.v1"
        assert "bash" in d.reason
        assert "agent" in d.reason

    def test_decision_is_immutable(self) -> None:
        """Decisions must be frozen — no post-hoc mutation."""
        from pydantic import ValidationError

        from arcagent.core.tool_policy import Decision

        d = Decision.allow(input_hash="h", evaluated_at_us=1)
        with __import__("pytest").raises(ValidationError):
            d.outcome = "deny"  # type: ignore[misc]

    def test_tool_call_required_fields(self) -> None:
        from arcagent.core.tool_policy import ToolCall

        call = ToolCall(
            tool_name="read",
            arguments={"path": "/tmp/x"},
            agent_did="did:arc:test",
            session_id="sess_1",
            classification="unclassified",
        )
        assert call.tool_name == "read"
        assert call.parent_call_id is None

    def test_policy_context_fields(self) -> None:
        from arcagent.core.tool_policy import PolicyContext

        ctx = PolicyContext(
            tier="federal",
            policy_version="v1.0.0",
            bundle_age_seconds=0.0,
        )
        assert ctx.tier == "federal"


class TestPipelineEmpty:
    """Phase 2 Task 2.3-2.4: Empty pipeline returns ALLOW."""

    async def test_empty_pipeline_allows(self) -> None:
        from arcagent.core.tool_policy import (
            PolicyContext,
            ToolCall,
            PolicyPipeline,
        )

        pipeline = PolicyPipeline(layers=[])
        call = ToolCall(
            tool_name="read",
            arguments={},
            agent_did="did:arc:a",
            session_id="s",
            classification="unclassified",
        )
        ctx = PolicyContext(tier="personal", policy_version="v1", bundle_age_seconds=0)

        decision = await pipeline.evaluate(call, ctx)
        assert decision.outcome == "allow"


class TestFirstDenyWins:
    """Phase 2 Task 2.5, 2.8: first DENY short-circuits downstream layers."""

    async def test_first_deny_wins(self) -> None:
        from arcagent.core.tool_policy import (
            Decision,
            PolicyContext,
            ToolCall,
            PolicyPipeline,
        )

        layer1 = MagicMock()
        layer1.name = "global"
        layer1.evaluate = _async_return(
            Decision.deny(
                layer="global",
                rule_id="r1",
                reason="denied by global",
                input_hash="h",
                evaluated_at_us=10,
            )
        )

        layer2 = MagicMock()
        layer2.name = "agent"
        layer2.evaluate = _async_return(
            Decision.allow(input_hash="h", evaluated_at_us=20)
        )

        pipeline = PolicyPipeline(layers=[layer1, layer2])
        call = ToolCall(
            tool_name="bash",
            arguments={},
            agent_did="did:arc:a",
            session_id="s",
            classification="unclassified",
        )
        ctx = PolicyContext(tier="federal", policy_version="v1", bundle_age_seconds=0)

        decision = await pipeline.evaluate(call, ctx)
        assert decision.outcome == "deny"
        assert decision.layer == "global"
        # Downstream layer must NOT have been consulted
        layer2.evaluate.assert_not_called()


class TestFailClosed:
    """Phase 2 Task 2.6, 2.8: exception in layer → DENY (never ALLOW)."""

    async def test_exception_in_layer_is_deny(self) -> None:
        from arcagent.core.tool_policy import (
            PolicyContext,
            ToolCall,
            PolicyPipeline,
        )

        class BrokenLayer:
            name = "broken"

            async def evaluate(self, call: Any, ctx: Any) -> Any:
                raise RuntimeError("boom")

        pipeline = PolicyPipeline(layers=[BrokenLayer()])
        call = ToolCall(
            tool_name="read",
            arguments={},
            agent_did="did:arc:a",
            session_id="s",
            classification="unclassified",
        )
        ctx = PolicyContext(tier="personal", policy_version="v1", bundle_age_seconds=0)

        decision = await pipeline.evaluate(call, ctx)
        assert decision.outcome == "deny"
        assert decision.layer == "broken"
        assert decision.rule_id == "layer_error"

    async def test_subsequent_layers_not_called_after_exception(self) -> None:
        from arcagent.core.tool_policy import (
            Decision,
            PolicyContext,
            ToolCall,
            PolicyPipeline,
        )

        class BrokenLayer:
            name = "broken"

            async def evaluate(self, call: Any, ctx: Any) -> Any:
                raise RuntimeError("boom")

        layer2 = MagicMock()
        layer2.name = "downstream"
        layer2.evaluate = _async_return(
            Decision.allow(input_hash="h", evaluated_at_us=1)
        )

        pipeline = PolicyPipeline(layers=[BrokenLayer(), layer2])
        call = ToolCall(
            tool_name="read",
            arguments={},
            agent_did="did:arc:a",
            session_id="s",
            classification="unclassified",
        )
        ctx = PolicyContext(tier="personal", policy_version="v1", bundle_age_seconds=0)

        await pipeline.evaluate(call, ctx)
        layer2.evaluate.assert_not_called()


class TestPolicyDeniedException:
    """PolicyDenied error carries the full Decision payload."""

    def test_policy_denied_carries_decision(self) -> None:
        from arcagent.core.tool_policy import Decision, PolicyDenied

        d = Decision.deny(
            layer="agent",
            rule_id="allowlist",
            reason="not in allowlist",
            input_hash="h",
            evaluated_at_us=1,
        )
        err = PolicyDenied(d)
        assert err.decision is d
        assert err.code == "POLICY_DENIED"
        assert "agent" in str(err)


class TestGlobalLayer:
    """Phase 2 Task 2.10: GlobalLayer with indexed rules + forbidden compositions."""

    async def test_global_layer_allows_when_no_rules(self) -> None:
        from arcagent.core.tool_policy import (
            GlobalLayer,
            PolicyContext,
            ToolCall,
        )

        layer = GlobalLayer(deny_rules={}, forbidden_compositions=[])
        call = ToolCall(
            tool_name="read",
            arguments={},
            agent_did="did:arc:a",
            session_id="s",
            classification="unclassified",
        )
        ctx = PolicyContext(tier="personal", policy_version="v1", bundle_age_seconds=0)

        decision = await layer.evaluate(call, ctx)
        assert decision.outcome == "allow"

    async def test_global_layer_denies_when_tool_in_denylist(self) -> None:
        from arcagent.core.tool_policy import (
            GlobalLayer,
            PolicyContext,
            ToolCall,
        )

        layer = GlobalLayer(
            deny_rules={"bash": "global.denylist: bash is privileged"},
            forbidden_compositions=[],
        )
        call = ToolCall(
            tool_name="bash",
            arguments={},
            agent_did="did:arc:a",
            session_id="s",
            classification="unclassified",
        )
        ctx = PolicyContext(tier="federal", policy_version="v1", bundle_age_seconds=0)

        decision = await layer.evaluate(call, ctx)
        assert decision.outcome == "deny"
        assert decision.layer == "global"
        assert "bash" in decision.reason


class TestAgentLayer:
    """Per-agent allowlists."""

    async def test_agent_layer_denies_tool_not_in_allowlist(self) -> None:
        from arcagent.core.tool_policy import (
            AgentLayer,
            PolicyContext,
            ToolCall,
        )

        layer = AgentLayer(allowlist_by_agent={"did:arc:a": {"read", "grep"}})
        call = ToolCall(
            tool_name="bash",
            arguments={},
            agent_did="did:arc:a",
            session_id="s",
            classification="unclassified",
        )
        ctx = PolicyContext(tier="personal", policy_version="v1", bundle_age_seconds=0)

        decision = await layer.evaluate(call, ctx)
        assert decision.outcome == "deny"
        assert decision.layer == "agent"

    async def test_agent_layer_allows_tool_in_allowlist(self) -> None:
        from arcagent.core.tool_policy import (
            AgentLayer,
            PolicyContext,
            ToolCall,
        )

        layer = AgentLayer(allowlist_by_agent={"did:arc:a": {"read", "grep"}})
        call = ToolCall(
            tool_name="read",
            arguments={},
            agent_did="did:arc:a",
            session_id="s",
            classification="unclassified",
        )
        ctx = PolicyContext(tier="personal", policy_version="v1", bundle_age_seconds=0)

        decision = await layer.evaluate(call, ctx)
        assert decision.outcome == "allow"

    async def test_agent_layer_allows_when_no_allowlist_defined(self) -> None:
        """Agents without explicit allowlist have unrestricted access (default-allow per-agent).

        Denylist is the global layer's job; agent layer only constrains
        agents that have opt-in allowlists.
        """
        from arcagent.core.tool_policy import (
            AgentLayer,
            PolicyContext,
            ToolCall,
        )

        layer = AgentLayer(allowlist_by_agent={})
        call = ToolCall(
            tool_name="bash",
            arguments={},
            agent_did="did:arc:unrestricted",
            session_id="s",
            classification="unclassified",
        )
        ctx = PolicyContext(tier="personal", policy_version="v1", bundle_age_seconds=0)

        decision = await layer.evaluate(call, ctx)
        assert decision.outcome == "allow"


class TestTierFactory:
    """Phase 2 Task 2.15: per-tier layer composition."""

    def test_federal_uses_five_layers(self) -> None:
        from arcagent.core.tool_policy import build_pipeline

        pipeline = build_pipeline(tier="federal")
        names = [layer.name for layer in pipeline.layers]
        # All 5 layers present for federal
        assert names == ["global", "provider", "agent", "team", "sandbox"]

    def test_enterprise_drops_team_layer(self) -> None:
        from arcagent.core.tool_policy import build_pipeline

        pipeline = build_pipeline(tier="enterprise")
        names = [layer.name for layer in pipeline.layers]
        assert "team" not in names
        assert names == ["global", "provider", "agent", "sandbox"]

    def test_personal_uses_only_global(self) -> None:
        from arcagent.core.tool_policy import build_pipeline

        pipeline = build_pipeline(tier="personal")
        names = [layer.name for layer in pipeline.layers]
        assert names == ["global"]


class TestDecisionCache:
    """Phase 2 Task 2.16-2.17: LRU cache with monotonic TTL."""

    async def test_cache_hit_skips_evaluation(self) -> None:
        from arcagent.core.tool_policy import (
            Decision,
            PolicyContext,
            ToolCall,
            PolicyPipeline,
        )

        layer = MagicMock()
        layer.name = "counted"
        layer.evaluate = _counting_evaluate(
            Decision.allow(input_hash="h", evaluated_at_us=1)
        )

        pipeline = PolicyPipeline(layers=[layer], cache_ttl_seconds=30.0)
        call = ToolCall(
            tool_name="read",
            arguments={"path": "/tmp/x"},
            agent_did="did:arc:a",
            session_id="s",
            classification="unclassified",
        )
        ctx = PolicyContext(tier="personal", policy_version="v1", bundle_age_seconds=0)

        await pipeline.evaluate(call, ctx)
        await pipeline.evaluate(call, ctx)
        # Second call should hit the cache — layer only evaluated once
        assert layer.evaluate.call_count == 1  # type: ignore[attr-defined]

    async def test_cache_expires_after_ttl(self) -> None:
        from arcagent.core.tool_policy import (
            Decision,
            PolicyContext,
            ToolCall,
            PolicyPipeline,
        )

        layer = MagicMock()
        layer.name = "counted"
        layer.evaluate = _counting_evaluate(
            Decision.allow(input_hash="h", evaluated_at_us=1)
        )

        fake_clock = [0.0]

        def clock() -> float:
            return fake_clock[0]

        pipeline = PolicyPipeline(
            layers=[layer], cache_ttl_seconds=10.0, monotonic=clock
        )
        call = ToolCall(
            tool_name="read",
            arguments={},
            agent_did="did:arc:a",
            session_id="s",
            classification="unclassified",
        )
        ctx = PolicyContext(tier="personal", policy_version="v1", bundle_age_seconds=0)

        await pipeline.evaluate(call, ctx)
        fake_clock[0] = 11.0  # advance past TTL
        await pipeline.evaluate(call, ctx)
        assert layer.evaluate.call_count == 2  # type: ignore[attr-defined]


class TestShadowMode:
    """Phase 2 Task 2.19-2.20: shadow mode logs DENY but returns ALLOW."""

    async def test_shadow_mode_returns_allow_even_when_layer_denies(self) -> None:
        from arcagent.core.tool_policy import (
            Decision,
            PolicyContext,
            ToolCall,
            PolicyPipeline,
        )

        layer = MagicMock()
        layer.name = "global"
        layer.evaluate = _async_return(
            Decision.deny(
                layer="global",
                rule_id="denylist",
                reason="denied",
                input_hash="h",
                evaluated_at_us=1,
            )
        )

        pipeline = PolicyPipeline(layers=[layer], shadow=True)
        call = ToolCall(
            tool_name="bash",
            arguments={},
            agent_did="did:arc:a",
            session_id="s",
            classification="unclassified",
        )
        ctx = PolicyContext(tier="personal", policy_version="v1", bundle_age_seconds=0)

        decision = await pipeline.evaluate(call, ctx)
        assert decision.outcome == "allow"


class TestRestrictedMode:
    """Phase 2 Task 2.21-2.22: stale bundle triggers restricted mode (safe-set only)."""

    async def test_stale_bundle_denies_unsafe_tools(self) -> None:
        from arcagent.core.tool_policy import (
            PolicyContext,
            ToolCall,
            PolicyPipeline,
        )

        pipeline = PolicyPipeline(
            layers=[],
            max_bundle_age_seconds=60.0,
            safe_set={"read"},
        )
        call = ToolCall(
            tool_name="bash",
            arguments={},
            agent_did="did:arc:a",
            session_id="s",
            classification="unclassified",
        )
        stale_ctx = PolicyContext(
            tier="federal", policy_version="v1", bundle_age_seconds=300.0
        )

        decision = await pipeline.evaluate(call, stale_ctx)
        assert decision.outcome == "deny"
        assert decision.rule_id == "restricted_mode"

    async def test_stale_bundle_allows_safe_set_tool(self) -> None:
        from arcagent.core.tool_policy import (
            PolicyContext,
            ToolCall,
            PolicyPipeline,
        )

        pipeline = PolicyPipeline(
            layers=[],
            max_bundle_age_seconds=60.0,
            safe_set={"read", "grep"},
        )
        call = ToolCall(
            tool_name="read",
            arguments={},
            agent_did="did:arc:a",
            session_id="s",
            classification="unclassified",
        )
        stale_ctx = PolicyContext(
            tier="federal", policy_version="v1", bundle_age_seconds=300.0
        )

        decision = await pipeline.evaluate(call, stale_ctx)
        assert decision.outcome == "allow"


class TestTelemetry:
    """Phase 2 Task 2.23-2.24: OTel spans + audit events per evaluation."""

    async def test_audit_event_emitted_per_evaluation(self) -> None:
        from arcagent.core.tool_policy import (
            Decision,
            PolicyContext,
            ToolCall,
            PolicyPipeline,
        )

        events: list[tuple[str, dict[str, Any]]] = []

        def emit(event_type: str, payload: dict[str, Any]) -> None:
            events.append((event_type, payload))

        layer = MagicMock()
        layer.name = "global"
        layer.evaluate = _async_return(
            Decision.allow(input_hash="h", evaluated_at_us=1)
        )

        pipeline = PolicyPipeline(layers=[layer], audit_sink=emit)
        call = ToolCall(
            tool_name="read",
            arguments={},
            agent_did="did:arc:a",
            session_id="s",
            classification="unclassified",
        )
        ctx = PolicyContext(tier="personal", policy_version="v1", bundle_age_seconds=0)

        await pipeline.evaluate(call, ctx)

        assert len(events) == 1
        ev_type, payload = events[0]
        assert ev_type == "policy.evaluate"
        assert payload["decision"] == "allow"
        assert payload["tool_name"] == "read"
        assert payload["agent_did"] == "did:arc:a"
        assert "evaluation_time_us" in payload


# --- helpers ---------------------------------------------------------------


def _async_return(value: Any) -> Any:
    """Build an AsyncMock returning ``value``.

    Using a plain ``async def`` closure keeps the mock call count queryable.
    """
    calls = {"count": 0}

    async def _fn(*_args: Any, **_kw: Any) -> Any:
        calls["count"] += 1
        return value

    _fn.assert_not_called = lambda: _assert_count(_fn, 0)  # type: ignore[attr-defined]
    return _wrap(_fn)


def _counting_evaluate(value: Any) -> Any:
    """Async callable that counts calls via ``.call_count``."""
    return _wrap(
        _make_counting(value),
    )


def _make_counting(value: Any) -> Any:
    state = {"count": 0}

    async def _fn(*_args: Any, **_kw: Any) -> Any:
        state["count"] += 1
        return value

    _fn._state = state  # type: ignore[attr-defined]
    return _fn


def _wrap(fn: Any) -> Any:
    """Return ``fn`` unchanged; placeholder for future wrapping."""

    class _Wrapper:
        def __init__(self, inner: Any) -> None:
            self._inner = inner
            self._calls = 0

        async def __call__(self, *args: Any, **kw: Any) -> Any:
            self._calls += 1
            return await self._inner(*args, **kw)

        @property
        def call_count(self) -> int:
            return self._calls

        def assert_not_called(self) -> None:
            assert self._calls == 0, f"Expected no calls, got {self._calls}"

    return _Wrapper(fn)


def _assert_count(fn: Any, expected: int) -> None:
    actual = getattr(fn, "call_count", 0)
    assert actual == expected, f"Expected {expected} calls, got {actual}"


# Smoke-test the helper harness itself so it is covered
_ = time  # silence unused import
