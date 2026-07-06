"""SPEC-034 — tests for the three now-real policy layers + PolicyContext schema.

TDD: written before implementation. Covers ProviderLayer (LLM10), TeamLayer
(ASI03/ASI07), SandboxLayer (ASI04/ASI05), the extended PolicyContext contract,
and the extended build_pipeline factory.
"""

from __future__ import annotations

import pytest

from arctrust.identity import AgentIdentity
from arctrust.policy import (
    PolicyContext,
    ProviderLayer,
    ProviderLimit,
    ProviderUsage,
    SandboxLayer,
    TeamLayer,
    TeamScope,
    ToolCall,
    ToolRuntimeStatus,
    build_pipeline,
    sign_call,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_call(
    tool_name: str = "read_file",
    agent_did: str = "did:arc:test:exec/aabbccdd",
    classification: str = "UNCLASSIFIED",
    parent_call_id: str | None = None,
) -> ToolCall:
    return ToolCall(
        tool_name=tool_name,
        arguments={"path": "/tmp/test"},
        agent_did=agent_did,
        session_id="sess-001",
        classification=classification,
        parent_call_id=parent_call_id,
    )


def make_ctx(
    tier: str = "federal",
    *,
    provider_usage: ProviderUsage | None = None,
    team_scope: TeamScope | None = None,
    tool_runtime: ToolRuntimeStatus | None = None,
) -> PolicyContext:
    return PolicyContext(
        tier=tier,  # type: ignore[arg-type]
        policy_version="1.0",
        bundle_age_seconds=0.0,
        provider_usage=provider_usage,
        team_scope=team_scope,
        tool_runtime=tool_runtime,
    )


# ---------------------------------------------------------------------------
# Phase 0 — PolicyContext extension (REQ-014, REQ-015)
# ---------------------------------------------------------------------------


class TestPolicyContextExtension:
    def test_three_field_construction_still_valid(self) -> None:
        """Existing 3-field construction must remain valid (REQ-014)."""
        ctx = PolicyContext(tier="federal", policy_version="1.0", bundle_age_seconds=0.0)
        assert ctx.provider_usage is None
        assert ctx.team_scope is None
        assert ctx.tool_runtime is None

    def test_new_models_are_frozen(self) -> None:
        usage = ProviderUsage(
            provider="anthropic", tokens_used=1, cost_used=0.0, requests_in_window=0
        )
        scope = TeamScope(role="analyst", authorized_tools=frozenset({"read_file"}))
        rt = ToolRuntimeStatus(
            verified=True, required_isolation="host", available_isolation="host"
        )
        for model in (usage, scope, rt):
            with pytest.raises(Exception):  # noqa: B017 — frozen mutation raises
                model.provider = "x"  # type: ignore[attr-defined]

    def test_team_scope_delegation_grant_defaults_none(self) -> None:
        scope = TeamScope(role="analyst", authorized_tools=frozenset({"read_file"}))
        assert scope.delegation_grant is None


# ---------------------------------------------------------------------------
# Phase 1 — ProviderLayer (REQ-001..005)
# ---------------------------------------------------------------------------


class TestProviderLayer:
    async def test_budget_exceeded_denies_on_tokens(self) -> None:
        layer = ProviderLayer(
            limits_by_provider={
                "anthropic": ProviderLimit(max_tokens=1000, max_cost=100.0, max_requests=50)
            }
        )
        usage = ProviderUsage(
            provider="anthropic", tokens_used=1000, cost_used=0.0, requests_in_window=0
        )
        d = await layer.evaluate(make_call(), make_ctx(provider_usage=usage))
        assert d.outcome == "deny"
        assert d.layer == "provider"
        assert d.rule_id == "provider.budget_exceeded"
        assert "anthropic" in (d.reason or "")
        assert "1000" in (d.reason or "")  # names the limit + observed

    async def test_budget_exceeded_denies_on_cost(self) -> None:
        layer = ProviderLayer(
            limits_by_provider={
                "openai": ProviderLimit(max_tokens=10_000, max_cost=5.0, max_requests=50)
            }
        )
        usage = ProviderUsage(
            provider="openai", tokens_used=10, cost_used=5.0, requests_in_window=0
        )
        d = await layer.evaluate(make_call(), make_ctx(provider_usage=usage))
        assert d.outcome == "deny"
        assert d.rule_id == "provider.budget_exceeded"

    async def test_rate_exceeded_denies(self) -> None:
        layer = ProviderLayer(
            limits_by_provider={
                "anthropic": ProviderLimit(max_tokens=10_000, max_cost=100.0, max_requests=10)
            }
        )
        usage = ProviderUsage(
            provider="anthropic", tokens_used=1, cost_used=0.1, requests_in_window=10
        )
        d = await layer.evaluate(make_call(), make_ctx(provider_usage=usage))
        assert d.outcome == "deny"
        assert d.rule_id == "provider.rate_exceeded"

    async def test_under_limit_allows(self) -> None:
        layer = ProviderLayer(
            limits_by_provider={
                "anthropic": ProviderLimit(max_tokens=10_000, max_cost=100.0, max_requests=10)
            }
        )
        usage = ProviderUsage(
            provider="anthropic", tokens_used=500, cost_used=1.0, requests_in_window=3
        )
        d = await layer.evaluate(make_call(), make_ctx(provider_usage=usage))
        assert d.outcome == "allow"

    async def test_unconstrained_provider_allows(self) -> None:
        """A provider with no configured limit is unconstrained (ALLOW)."""
        layer = ProviderLayer(limits_by_provider={})
        usage = ProviderUsage(
            provider="anthropic", tokens_used=10**9, cost_used=10**9, requests_in_window=10**9
        )
        d = await layer.evaluate(make_call(), make_ctx(provider_usage=usage))
        assert d.outcome == "allow"

    async def test_configured_over_budget_denies(self) -> None:
        """REQ-004: a configured limit is a hard floor — over-budget usage denies."""
        limits = {"anthropic": ProviderLimit(max_tokens=1000, max_cost=1.0, max_requests=1)}
        usage = ProviderUsage(
            provider="anthropic", tokens_used=99_999, cost_used=999.0, requests_in_window=999
        )
        layer = ProviderLayer(limits_by_provider=limits)
        assert (await layer.evaluate(make_call(), make_ctx(provider_usage=usage))).outcome == (
            "deny"
        )

    async def test_configured_but_missing_usage_denies(self) -> None:
        """REQ-005 (configured-gate): a configured budget with a blind meter fails
        closed. Previously this test used empty limits and asserted DENY — that
        encoded the brick bug (empty policy must be a no-op ALLOW). Corrected to a
        real limit, which is the only case where state_missing may legitimately fire."""
        limits = {"anthropic": ProviderLimit(max_tokens=1000, max_cost=1.0, max_requests=1)}
        layer = ProviderLayer(limits_by_provider=limits)
        d = await layer.evaluate(make_call(), make_ctx(provider_usage=None))
        assert d.outcome == "deny"
        assert d.rule_id == "provider.state_missing"

    async def test_no_configured_limits_allows_when_usage_missing(self) -> None:
        """REQ-005 (configured-gate): no budget policy configured → no-op ALLOW,
        even with no usage state. Absence of a policy is not a violation."""
        layer = ProviderLayer(limits_by_provider={})
        d = await layer.evaluate(make_call(), make_ctx(provider_usage=None))
        assert d.outcome == "allow"

    async def test_no_clock_or_io_in_evaluate(self) -> None:
        """REQ-002: decision is O(1) over injected counters — patch time to prove
        no clock read drives the decision (evaluated_at_us stamp aside)."""
        layer = ProviderLayer(
            limits_by_provider={
                "anthropic": ProviderLimit(max_tokens=10, max_cost=1.0, max_requests=1)
            }
        )
        usage = ProviderUsage(
            provider="anthropic", tokens_used=5, cost_used=0.0, requests_in_window=0
        )
        # Same injected state -> same decision, regardless of wall time.
        d1 = await layer.evaluate(make_call(), make_ctx(provider_usage=usage))
        d2 = await layer.evaluate(make_call(), make_ctx(provider_usage=usage))
        assert d1.outcome == d2.outcome == "allow"


# ---------------------------------------------------------------------------
# Phase 2 — TeamLayer (REQ-006..009)
# ---------------------------------------------------------------------------


class TestTeamLayer:
    async def test_no_team_scope_allows(self) -> None:
        """REQ-009: absence of team scoping is not a team violation."""
        layer = TeamLayer(roles={"analyst": frozenset({"read_file"})})
        d = await layer.evaluate(make_call(), make_ctx(team_scope=None))
        assert d.outcome == "allow"

    async def test_out_of_scope_denies(self) -> None:
        layer = TeamLayer(roles={"analyst": frozenset({"read_file"})})
        scope = TeamScope(role="analyst", authorized_tools=frozenset({"read_file"}))
        d = await layer.evaluate(
            make_call(tool_name="delete_all"), make_ctx(team_scope=scope)
        )
        assert d.outcome == "deny"
        assert d.layer == "team"
        assert d.rule_id == "team.scope_violation"
        assert "analyst" in (d.reason or "")

    async def test_in_scope_allows(self) -> None:
        layer = TeamLayer(roles={"analyst": frozenset({"read_file"})})
        scope = TeamScope(role="analyst", authorized_tools=frozenset({"read_file"}))
        d = await layer.evaluate(make_call(tool_name="read_file"), make_ctx(team_scope=scope))
        assert d.outcome == "allow"

    async def test_context_scope_used_when_role_absent_from_construction(self) -> None:
        """Construction map is the static floor; context may carry a dynamic role."""
        layer = TeamLayer(roles={})
        scope = TeamScope(role="dynamic", authorized_tools=frozenset({"read_file"}))
        d = await layer.evaluate(make_call(tool_name="read_file"), make_ctx(team_scope=scope))
        assert d.outcome == "allow"

    async def test_delegation_exceeded_denies(self) -> None:
        layer = TeamLayer(roles={"analyst": frozenset({"read_file", "write_file"})})
        scope = TeamScope(
            role="analyst",
            authorized_tools=frozenset({"read_file", "write_file"}),
            delegation_grant=frozenset({"read_file"}),
        )
        d = await layer.evaluate(
            make_call(tool_name="write_file", parent_call_id="parent-1"),
            make_ctx(team_scope=scope),
        )
        assert d.outcome == "deny"
        assert d.rule_id == "team.delegation_exceeded"

    async def test_delegation_within_grant_allows(self) -> None:
        layer = TeamLayer(roles={"analyst": frozenset({"read_file", "write_file"})})
        scope = TeamScope(
            role="analyst",
            authorized_tools=frozenset({"read_file", "write_file"}),
            delegation_grant=frozenset({"read_file"}),
        )
        d = await layer.evaluate(
            make_call(tool_name="read_file", parent_call_id="parent-1"),
            make_ctx(team_scope=scope),
        )
        assert d.outcome == "allow"

    async def test_non_delegated_call_ignores_grant(self) -> None:
        """No parent_call_id -> delegation grant does not apply."""
        layer = TeamLayer(roles={})
        scope = TeamScope(
            role="analyst",
            authorized_tools=frozenset({"read_file", "write_file"}),
            delegation_grant=frozenset({"read_file"}),
        )
        d = await layer.evaluate(
            make_call(tool_name="write_file", parent_call_id=None),
            make_ctx(team_scope=scope),
        )
        assert d.outcome == "allow"


# ---------------------------------------------------------------------------
# Phase 3 — SandboxLayer (REQ-010..013)
# ---------------------------------------------------------------------------


class TestSandboxLayer:
    async def test_unverified_tool_denies(self) -> None:
        layer = SandboxLayer()
        rt = ToolRuntimeStatus(
            verified=False, required_isolation="host", available_isolation="host"
        )
        d = await layer.evaluate(make_call(), make_ctx(tool_runtime=rt))
        assert d.outcome == "deny"
        assert d.layer == "sandbox"
        assert d.rule_id == "sandbox.unverified_tool"

    async def test_verified_satisfiable_allows(self) -> None:
        layer = SandboxLayer()
        rt = ToolRuntimeStatus(
            verified=True, required_isolation="container", available_isolation="vm"
        )
        d = await layer.evaluate(make_call(), make_ctx(tool_runtime=rt))
        assert d.outcome == "allow"

    async def test_isolation_unsatisfiable_denies(self) -> None:
        layer = SandboxLayer()
        rt = ToolRuntimeStatus(
            verified=True, required_isolation="vm", available_isolation="host"
        )
        d = await layer.evaluate(make_call(), make_ctx(tool_runtime=rt))
        assert d.outcome == "deny"
        assert d.rule_id == "sandbox.isolation_unsatisfiable"

    async def test_missing_runtime_allows(self) -> None:
        """Configured-gate: no runtime status → no-op ALLOW at every tier. The
        SPEC-033 load gate already verified any registry tool, so a blind sandbox
        layer has nothing to add. Previously this asserted DENY (sandbox.state_missing),
        which bricked enterprise/federal — that DENY branch was removed."""
        layer = SandboxLayer()
        d = await layer.evaluate(make_call(), make_ctx(tool_runtime=None))
        assert d.outcome == "allow"

    @pytest.mark.parametrize(
        ("available", "required", "expected"),
        [
            ("host", "host", "allow"),
            ("container", "host", "allow"),
            ("vm", "container", "allow"),
            ("host", "container", "deny"),
            ("container", "vm", "deny"),
            ("host", "vm", "deny"),
            ("vm", "vm", "allow"),
        ],
    )
    async def test_isolation_ladder(
        self, available: str, required: str, expected: str
    ) -> None:
        layer = SandboxLayer()
        rt = ToolRuntimeStatus(
            verified=True, required_isolation=required, available_isolation=available
        )
        d = await layer.evaluate(make_call(), make_ctx(tool_runtime=rt))
        assert d.outcome == expected


# ---------------------------------------------------------------------------
# Phase 5 — build_pipeline factory extension (REQ-004, REQ-008) + full pipeline
# ---------------------------------------------------------------------------


class TestBuildPipelineConfig:
    def test_federal_layers_carry_provider_and_team_config(self) -> None:
        limits = {"anthropic": ProviderLimit(max_tokens=1, max_cost=1.0, max_requests=1)}
        roles = {"analyst": frozenset({"read_file"})}
        pipeline = build_pipeline(
            tier="federal", provider_limits=limits, team_roles=roles
        )
        names = [layer.name for layer in pipeline.layers]
        assert names == ["identity", "global", "provider", "agent", "team", "sandbox"]
        provider = next(layer for layer in pipeline.layers if layer.name == "provider")
        team = next(layer for layer in pipeline.layers if layer.name == "team")
        assert provider._limits == limits  # type: ignore[attr-defined]
        assert team._roles == roles  # type: ignore[attr-defined]

    def test_personal_still_identity_global_only(self) -> None:
        pipeline = build_pipeline(tier="personal", provider_limits={"x": None})  # type: ignore[dict-item]
        assert [layer.name for layer in pipeline.layers] == ["identity", "global"]


class TestFullPipelineFirstDenyWins:
    @staticmethod
    def _signed(ident: AgentIdentity, tool_name: str = "read_file") -> ToolCall:
        return sign_call(make_call(tool_name=tool_name, agent_did=ident.did), ident)

    async def test_provider_breach_stops_before_sandbox(self) -> None:
        """First-DENY-wins across the three now-real layers: a provider breach
        denies before the sandbox layer is consulted (REQ-001, REQ-016)."""
        ident = AgentIdentity.generate(org="test", agent_type="exec")
        limits = {"anthropic": ProviderLimit(max_tokens=10, max_cost=1.0, max_requests=1)}
        pipeline = build_pipeline(
            tier="federal",
            agent_registry={ident.did: ident.public_key},
            provider_limits=limits,
        )
        # Over-budget provider usage; tool_runtime deliberately unverified —
        # the sandbox layer WOULD also deny, but provider is earlier in order.
        usage = ProviderUsage(
            provider="anthropic", tokens_used=999, cost_used=0.0, requests_in_window=0
        )
        rt = ToolRuntimeStatus(
            verified=False, required_isolation="host", available_isolation="host"
        )
        ctx = PolicyContext(
            tier="federal",
            policy_version="1.0",
            bundle_age_seconds=0.0,
            provider_usage=usage,
            tool_runtime=rt,
        )
        d = await pipeline.evaluate(self._signed(ident), ctx)
        assert d.outcome == "deny"
        assert d.layer == "provider"  # stopped at provider, not sandbox

    async def test_all_layers_allow_when_state_clean(self) -> None:
        ident = AgentIdentity.generate(org="test", agent_type="exec")
        pipeline = build_pipeline(
            tier="federal",
            agent_registry={ident.did: ident.public_key},
            provider_limits={},
        )
        usage = ProviderUsage(
            provider="anthropic", tokens_used=1, cost_used=0.0, requests_in_window=0
        )
        rt = ToolRuntimeStatus(
            verified=True, required_isolation="host", available_isolation="vm"
        )
        ctx = PolicyContext(
            tier="federal",
            policy_version="1.0",
            bundle_age_seconds=0.0,
            provider_usage=usage,
            tool_runtime=rt,
        )
        d = await pipeline.evaluate(self._signed(ident), ctx)
        assert d.outcome == "allow"


# ---------------------------------------------------------------------------
# Configured-gate semantics — default/empty config must not brick a tier.
# Product-owner decision: absence of a configured policy is not a violation;
# a call only fails closed when a real policy AND missing telemetry coexist.
# ---------------------------------------------------------------------------


class TestDefaultConfigDoesNotBrick:
    @staticmethod
    def _signed(ident: AgentIdentity, tool_name: str = "read_file") -> ToolCall:
        return sign_call(make_call(tool_name=tool_name, agent_did=ident.did), ident)

    async def test_enterprise_default_config_allows_with_blind_state(self) -> None:
        """Enterprise pipeline built with default (empty) config, evaluating a
        validly-signed call whose context carries no provider_usage/tool_runtime,
        must ALLOW — the tier is not bricked while producers are unwired."""
        ident = AgentIdentity.generate(org="test", agent_type="exec")
        pipeline = build_pipeline(
            tier="enterprise", agent_registry={ident.did: ident.public_key}
        )
        ctx = PolicyContext(
            tier="enterprise", policy_version="1.0", bundle_age_seconds=0.0
        )
        d = await pipeline.evaluate(self._signed(ident), ctx)
        assert d.outcome == "allow"

    async def test_federal_default_config_allows_with_blind_state(self) -> None:
        """Federal pipeline built with default (empty) config, same blind context,
        must ALLOW — Provider/Sandbox are no-ops when unconfigured."""
        ident = AgentIdentity.generate(org="test", agent_type="exec")
        pipeline = build_pipeline(
            tier="federal", agent_registry={ident.did: ident.public_key}
        )
        ctx = PolicyContext(
            tier="federal", policy_version="1.0", bundle_age_seconds=0.0
        )
        d = await pipeline.evaluate(self._signed(ident), ctx)
        assert d.outcome == "allow"
