"""``LLMProvider.max_output_tokens`` — the output ceiling a caller may ask for.

arcrun's pinned ``oneshot`` strategy holds a *run* token budget, not an output
cap. It needs the model's real output limit to clamp the one call it makes, and
only arcllm knows that limit (per-model ``max_output_tokens`` metadata, else the
operator's ``[defaults].max_tokens``). The ceiling is read through every layer a
caller actually holds: the adapter, any module wrapping it, and the router.
"""

from __future__ import annotations

from typing import Any

import pytest

from arcllm.adapters.base import BaseAdapter, _default_max_output_tokens
from arcllm.config import ModelMetadata, ProviderConfig, ProviderSettings
from arcllm.modules.base import BaseModule
from arcllm.modules.routing import Route, RoutingModule
from arcllm.types import LLMResponse, Message, Tool

_MODEL = "declared-model"

_SETTINGS = ProviderSettings(
    api_format="anthropic-messages",
    base_url="https://example.invalid",
    api_key_env="ARCLLM_CEILING_TEST_KEY",
    default_model=_MODEL,
    default_temperature=0.7,
)

_META = ModelMetadata(
    context_window=200_000,
    max_output_tokens=4096,
    supports_tools=True,
    supports_vision=False,
    supports_thinking=False,
    input_modalities=["text"],
    cost_input_per_1m=1.0,
    cost_output_per_1m=1.0,
    cost_cache_read_per_1m=0.1,
    cost_cache_write_per_1m=1.0,
)

_CONFIG = ProviderConfig(provider=_SETTINGS, models={_MODEL: _META})


class _Adapter(BaseAdapter):
    async def invoke(
        self, messages: list[Message], tools: list[Tool] | None = None, **kwargs: Any
    ) -> LLMResponse:
        raise NotImplementedError


@pytest.fixture(autouse=True)
def _api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ARCLLM_CEILING_TEST_KEY", "k")


def test_a_declared_model_reports_its_metadata_output_limit() -> None:
    assert _Adapter(_CONFIG, _MODEL).max_output_tokens == 4096


def test_an_undeclared_model_reports_the_operator_default() -> None:
    assert _Adapter(_CONFIG, "undeclared").max_output_tokens == _default_max_output_tokens()


def test_a_module_reports_the_ceiling_of_the_provider_it_wraps() -> None:
    assert BaseModule({}, _Adapter(_CONFIG, _MODEL)).max_output_tokens == 4096


def test_the_router_reports_the_default_routes_ceiling() -> None:
    adapters = {"main": _Adapter(_CONFIG, _MODEL), "other": _Adapter(_CONFIG, "undeclared")}
    routes = [
        Route(name="main", provider="anthropic", model=_MODEL),
        Route(name="other", provider="anthropic", model="undeclared"),
    ]
    router = RoutingModule({"default_route": "main"}, routes, lambda r: adapters[r.name])

    assert router.max_output_tokens == 4096
