"""Tests for RoutingModule — classification-based provider routing."""

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


def _make_adapter(name: str = "test-provider", model: str = "test-model") -> MagicMock:
    adapter = MagicMock(spec=LLMProvider)
    adapter.name = name
    adapter.model_name = model
    adapter.validate_config.return_value = True
    adapter.invoke = AsyncMock(return_value=_OK_RESPONSE)
    adapter.close = AsyncMock()
    return adapter


def _make_routing_config(
    enforcement: str = "block",
    default_classification: str = "unclassified",
    **extra: object,
) -> dict[str, object]:
    """Build a routing config dict."""
    config: dict[str, object] = {
        "enforcement": enforcement,
        "default_classification": default_classification,
    }
    config.update(extra)
    return config


@pytest.fixture
def messages() -> list[Message]:
    return [Message(role="user", content="hi")]


@pytest.fixture
def adapters() -> dict[str, MagicMock]:
    return {
        "cui": _make_adapter("anthropic", "claude-sonnet-4-6"),
        "unclassified": _make_adapter("openai", "gpt-4o-mini"),
    }


# ---------------------------------------------------------------------------
# TestRoutingSelection
# ---------------------------------------------------------------------------


class TestRoutingSelection:
    async def test_routes_to_correct_adapter(
        self, messages: list[Message], adapters: dict[str, MagicMock]
    ) -> None:
        config = _make_routing_config()
        router = RoutingModule(config, adapters)
        await router.invoke(messages, classification="cui")
        adapters["cui"].invoke.assert_awaited_once()
        adapters["unclassified"].invoke.assert_not_awaited()

    async def test_routes_unclassified_by_default(
        self, messages: list[Message], adapters: dict[str, MagicMock]
    ) -> None:
        config = _make_routing_config()
        router = RoutingModule(config, adapters)
        await router.invoke(messages)  # No classification kwarg
        adapters["unclassified"].invoke.assert_awaited_once()

    async def test_classification_popped_from_kwargs(
        self, messages: list[Message], adapters: dict[str, MagicMock]
    ) -> None:
        """classification kwarg should not reach the adapter."""
        config = _make_routing_config()
        router = RoutingModule(config, adapters)
        await router.invoke(messages, classification="cui", max_tokens=100)
        # Adapter should get max_tokens but NOT classification
        call_kwargs = adapters["cui"].invoke.call_args[1]
        assert "classification" not in call_kwargs
        assert call_kwargs["max_tokens"] == 100


# ---------------------------------------------------------------------------
# TestRoutingUnknown
# ---------------------------------------------------------------------------


class TestRoutingUnknown:
    async def test_unknown_classification_block_raises(
        self, messages: list[Message], adapters: dict[str, MagicMock]
    ) -> None:
        config = _make_routing_config(enforcement="block")
        router = RoutingModule(config, adapters)
        with pytest.raises(ArcLLMConfigError, match="No matching route"):
            await router.invoke(messages, classification="secret.squirrel")

    async def test_unknown_classification_warn_defaults(
        self, messages: list[Message], adapters: dict[str, MagicMock]
    ) -> None:
        config = _make_routing_config(enforcement="warn")
        router = RoutingModule(config, adapters)
        result = await router.invoke(messages, classification="secret.squirrel")
        # Should route to default (unclassified)
        adapters["unclassified"].invoke.assert_awaited_once()
        assert result.content == "routed"


# ---------------------------------------------------------------------------
# TestRoutingAdapterLifecycle
# ---------------------------------------------------------------------------


class TestRoutingAdapterLifecycle:
    async def test_close_closes_all_adapters(
        self, adapters: dict[str, MagicMock]
    ) -> None:
        config = _make_routing_config()
        router = RoutingModule(config, adapters)
        await router.close()
        for adapter in adapters.values():
            adapter.close.assert_awaited_once()

    async def test_close_tolerates_individual_adapter_failure(
        self, adapters: dict[str, MagicMock]
    ) -> None:
        """If one adapter fails to close, others should still be closed."""
        adapters["cui"].close = AsyncMock(side_effect=RuntimeError("conn reset"))
        config = _make_routing_config()
        router = RoutingModule(config, adapters)
        with pytest.raises(ExceptionGroup):
            await router.close()
        # Even though cui failed, unclassified should have been closed
        adapters["unclassified"].close.assert_awaited_once()

    def test_validate_config_checks_all_adapters(
        self, adapters: dict[str, MagicMock]
    ) -> None:
        config = _make_routing_config()
        router = RoutingModule(config, adapters)
        assert router.validate_config() is True

    def test_validate_config_fails_if_any_adapter_invalid(
        self, adapters: dict[str, MagicMock]
    ) -> None:
        adapters["cui"].validate_config.return_value = False
        config = _make_routing_config()
        router = RoutingModule(config, adapters)
        assert router.validate_config() is False

    def test_adapters_dict_is_defensive_copy(self) -> None:
        """Mutating the input dict after init must not affect the router."""
        adapters = {
            "cui": _make_adapter("anthropic", "claude"),
            "unclassified": _make_adapter("openai", "gpt"),
        }
        config = _make_routing_config()
        router = RoutingModule(config, adapters)
        # Mutate the original dict
        adapters["injected"] = _make_adapter("evil", "model")
        del adapters["cui"]
        # Router should be unaffected
        assert "cui" in router._adapters
        assert "injected" not in router._adapters


# ---------------------------------------------------------------------------
# TestRoutingProperties
# ---------------------------------------------------------------------------


class TestRoutingProperties:
    def test_name_from_default_adapter(
        self, adapters: dict[str, MagicMock]
    ) -> None:
        config = _make_routing_config(default_classification="unclassified")
        router = RoutingModule(config, adapters)
        assert router.name == "openai"

    def test_model_name_from_default_adapter(
        self, adapters: dict[str, MagicMock]
    ) -> None:
        config = _make_routing_config(default_classification="unclassified")
        router = RoutingModule(config, adapters)
        assert router.model_name == "gpt-4o-mini"


# ---------------------------------------------------------------------------
# TestRoutingValidation
# ---------------------------------------------------------------------------


class TestRoutingValidation:
    def test_invalid_enforcement_rejected(
        self, adapters: dict[str, MagicMock]
    ) -> None:
        with pytest.raises(ArcLLMConfigError, match="enforcement"):
            RoutingModule(
                _make_routing_config(enforcement="ignore"), adapters
            )

    def test_default_classification_must_exist_in_adapters(self) -> None:
        adapters = {"cui": _make_adapter()}
        with pytest.raises(ArcLLMConfigError, match="default_classification"):
            RoutingModule(
                _make_routing_config(default_classification="nonexistent"),
                adapters,
            )

    def test_empty_adapters_rejected(self) -> None:
        with pytest.raises(ArcLLMConfigError, match="at least one"):
            RoutingModule(_make_routing_config(), {})
