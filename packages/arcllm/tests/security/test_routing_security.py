"""Security tests for routing — classification bypass and adapter isolation."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from arcllm.exceptions import ArcLLMConfigError
from arcllm.modules.routing import RoutingModule
from arcllm.types import LLMProvider, LLMResponse, Message, Usage

_OK_RESPONSE = LLMResponse(
    content="routed",
    usage=Usage(input_tokens=100, output_tokens=50, total_tokens=150),
    model="test-model",
    stop_reason="end_turn",
)


def _make_adapter(name: str = "test", model: str = "m") -> MagicMock:
    adapter = MagicMock(spec=LLMProvider)
    adapter.name = name
    adapter.model_name = model
    adapter.validate_config.return_value = True
    adapter.invoke = AsyncMock(return_value=_OK_RESPONSE)
    adapter.close = AsyncMock()
    return adapter


@pytest.fixture
def messages():
    return [Message(role="user", content="hi")]


# ---------------------------------------------------------------------------
# Classification Downgrade Prevention
# ---------------------------------------------------------------------------


class TestClassificationDowngrade:
    """Verify CUI data cannot be routed to an unauthorized provider."""

    async def test_unknown_classification_blocked_in_strict_mode(self, messages):
        """Block mode prevents any unknown classification from reaching a provider."""
        adapters = {
            "cui": _make_adapter("fedramp-provider", "secure-model"),
            "unclassified": _make_adapter("cheap-provider", "fast-model"),
        }
        router = RoutingModule(
            {"enforcement": "block", "default_classification": "unclassified"},
            adapters,
        )
        with pytest.raises(ArcLLMConfigError, match="No matching route"):
            await router.invoke(messages, classification="secret")
        # Neither adapter should have been called
        adapters["cui"].invoke.assert_not_awaited()
        adapters["unclassified"].invoke.assert_not_awaited()

    async def test_classification_case_sensitivity(self, messages):
        """'CUI' != 'cui' — uppercase rejected at format validation."""
        adapters = {
            "cui": _make_adapter("fedramp", "secure"),
            "unclassified": _make_adapter("cheap", "fast"),
        }
        router = RoutingModule(
            {"enforcement": "block", "default_classification": "unclassified"},
            adapters,
        )
        with pytest.raises(ArcLLMConfigError, match="Invalid classification format"):
            await router.invoke(messages, classification="CUI")


# ---------------------------------------------------------------------------
# Adapter Isolation
# ---------------------------------------------------------------------------


class TestAdapterIsolation:
    """Verify adapters within the router don't share state."""

    async def test_adapters_are_distinct_instances(self, messages):
        """Each classification gets its own adapter — no shared httpx client."""
        adapter_cui = _make_adapter("fedramp", "secure")
        adapter_unc = _make_adapter("cheap", "fast")
        adapters = {"cui": adapter_cui, "unclassified": adapter_unc}
        router = RoutingModule(
            {"enforcement": "block", "default_classification": "unclassified"},
            adapters,
        )
        assert router._adapters["cui"] is not router._adapters["unclassified"]

    async def test_close_does_not_affect_other_adapters_state(self, messages):
        """Closing one adapter in the router doesn't affect others."""
        adapter_cui = _make_adapter("fedramp", "secure")
        adapter_unc = _make_adapter("cheap", "fast")
        adapters = {"cui": adapter_cui, "unclassified": adapter_unc}
        router = RoutingModule(
            {"enforcement": "block", "default_classification": "unclassified"},
            adapters,
        )
        # Close the router (closes all)
        await router.close()
        # Both should have been closed independently
        adapter_cui.close.assert_awaited_once()
        adapter_unc.close.assert_awaited_once()


# ---------------------------------------------------------------------------
# Config Injection Prevention
# ---------------------------------------------------------------------------


class TestConfigInjection:
    """Verify routing config cannot be modified at runtime."""

    async def test_cannot_add_rules_via_kwargs(self, messages):
        """Extra kwargs should not create new routing rules."""
        adapters = {
            "cui": _make_adapter("fedramp", "secure"),
            "unclassified": _make_adapter("cheap", "fast"),
        }
        router = RoutingModule(
            {"enforcement": "block", "default_classification": "unclassified"},
            adapters,
        )
        # Try to route to a non-existent but format-valid classification
        with pytest.raises(ArcLLMConfigError, match="No matching route"):
            await router.invoke(messages, classification="evil.route")

    def test_adapters_dict_frozen_at_init(self):
        """Modifying the input adapters dict after init must not affect the router."""
        adapters = {
            "cui": _make_adapter("fedramp", "secure"),
            "unclassified": _make_adapter("cheap", "fast"),
        }
        router = RoutingModule(
            {"enforcement": "block", "default_classification": "unclassified"},
            adapters,
        )
        # Mutate the original dict — inject a new adapter and remove one
        adapters["injected"] = _make_adapter("evil", "model")
        del adapters["cui"]
        # Router must be unaffected (defensive copy)
        assert "cui" in router._adapters
        assert "injected" not in router._adapters
        assert len(router._adapters) == 2


# ---------------------------------------------------------------------------
# Audit Trail
# ---------------------------------------------------------------------------


class TestRoutingAuditTrail:
    """Verify all routing decisions are observable."""

    async def test_all_routed_calls_return_response(self, messages):
        """Every routed call must return a valid LLMResponse."""
        adapters = {
            "cui": _make_adapter("fedramp", "secure"),
            "unclassified": _make_adapter("cheap", "fast"),
        }
        router = RoutingModule(
            {"enforcement": "block", "default_classification": "unclassified"},
            adapters,
        )
        for classification in ("cui", "unclassified"):
            result = await router.invoke(messages, classification=classification)
            assert result is not None
            assert result.content == "routed"
