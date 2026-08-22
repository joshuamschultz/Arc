"""`[llm.routes]` reaches arcllm as the router's route table.

The declared list is the permission boundary: an agent can be routed only to
models it declared, never to one merely configured on the machine.
"""

import pytest
from pydantic import ValidationError

from arcagent.core.config import ArcAgentConfig, LLMConfig
from arcagent.core.model_manager import _arcllm_modules


@pytest.fixture(autouse=True)
def _provider_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """Route construction is local, but adapters still validate their key."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-anthropic-key")


def _config(**llm: object) -> ArcAgentConfig:
    return ArcAgentConfig(
        agent={"name": "olivia"},
        llm=LLMConfig(model="anthropic/claude-opus-5", **llm),
    )


class TestRouteDeclaration:
    def test_no_routes_still_sends_an_empty_table(self):
        """Silence must mean "no alternates", not "inherit the machine's".

        A deployment-wide route table in ~/.arc/arcllm.toml would otherwise
        apply to an agent that declared nothing, which defeats the boundary.
        """
        assert _arcllm_modules(_config()) == {"routing": {"routes": {}}}

    def test_undeclared_agent_cannot_inherit_a_machine_wide_route(self, monkeypatch, tmp_path):
        """End to end: a route in ~/.arc/arcllm.toml is unreachable to this agent."""
        from arcllm.modules.routing import RoutingModule
        from arcllm.registry import clear_cache

        (tmp_path / "arcllm.toml").write_text(
            '[modules.routing.routes.sneaky]\nmodel = "ollama/llama"\n'
        )
        monkeypatch.setenv("ARC_CONFIG_DIR", str(tmp_path))
        clear_cache()
        try:
            from arcagent.utils import load_eval_model

            model = load_eval_model(
                "anthropic/claude-opus-5", arcllm_modules=_arcllm_modules(_config())
            )
            router = model
            while not isinstance(router, RoutingModule):
                router = router._inner
            assert router.routes == ("default",)
        finally:
            clear_cache()

    def test_routes_become_the_routing_module_table(self):
        config = _config(
            routes={
                "local": {
                    "model": "litellm/qwen3-coder",
                    "phrases": ["run this locally", "keep this on my machine"],
                }
            }
        )

        modules = _arcllm_modules(config)

        assert modules == {
            "routing": {
                "routes": {
                    "local": {
                        "model": "litellm/qwen3-coder",
                        "phrases": ["run this locally", "keep this on my machine"],
                    }
                }
            }
        }

    def test_routes_merge_with_other_module_overrides(self):
        config = _config(
            modules={"queue": {"call_timeout": 600}},
            routes={"local": {"model": "ollama/llama"}},
        )

        modules = _arcllm_modules(config)

        assert modules["queue"] == {"call_timeout": 600}
        assert modules["routing"]["routes"]["local"]["model"] == "ollama/llama"

    def test_routes_do_not_clobber_routing_tuning(self):
        """An agent may tune the threshold and still declare its own routes."""
        config = _config(
            modules={"routing": {"threshold": 0.6}},
            routes={"local": {"model": "ollama/llama"}},
        )

        routing = _arcllm_modules(config)["routing"]

        assert routing["threshold"] == 0.6
        assert "local" in routing["routes"]

    def test_route_typo_fails_loudly(self):
        """A misspelled key must not vanish into an ignored extra."""
        with pytest.raises(ValidationError):
            LLMConfig(
                model="anthropic/claude-opus-5",
                routes={"local": {"model": "ollama/llama", "phrasez": ["x"]}},
            )

    def test_route_without_model_rejected(self):
        with pytest.raises(ValidationError):
            LLMConfig(model="anthropic/claude-opus-5", routes={"local": {"phrases": ["x"]}})


class TestRoutesReachTheRealModelPath:
    """Through `ensure_model`, the function the agent actually calls.

    Asserting on `_arcllm_modules` alone would pass just as happily if nothing
    ever called it.
    """

    def test_ensure_model_builds_the_declared_routes(self, tmp_path):
        from arcllm.modules.routing import RoutingModule

        from arcagent.core.model_manager import ensure_model

        workspace = tmp_path / "agent" / "workspace"
        workspace.mkdir(parents=True)
        config = _config(
            routes={"cheap": {"model": "anthropic/claude-haiku-4-5", "phrases": ["quick"]}}
        )

        model, _trace_store = ensure_model(config=config, workspace=workspace, bus=None)

        router = model
        while not isinstance(router, RoutingModule):
            router = router._inner
        assert router.routes == ("default", "cheap")
        assert router._routes["cheap"].phrases == ("quick",)

    def test_ensure_model_without_routes_is_a_pass_through(self, tmp_path):
        from arcllm.modules.routing import RoutingModule

        from arcagent.core.model_manager import ensure_model

        workspace = tmp_path / "agent" / "workspace"
        workspace.mkdir(parents=True)

        model, _trace_store = ensure_model(config=_config(), workspace=workspace, bus=None)

        router = model
        while not isinstance(router, RoutingModule):
            router = router._inner
        assert router.routes == ("default",)


class TestRoutesReachLoadModel:
    def test_declared_routes_build_a_multi_route_router(self):
        """End to end through the real seam: agent config -> arcllm router."""
        from arcllm.modules.routing import RoutingModule

        from arcagent.utils import load_eval_model

        config = _config(
            routes={"cheap": {"model": "anthropic/claude-haiku-4-5", "phrases": ["quick"]}}
        )
        model = load_eval_model(
            config.llm.model,
            arcllm_modules=_arcllm_modules(config),
        )

        router = model
        while not isinstance(router, RoutingModule):
            router = router._inner

        assert router.routes == ("default", "cheap")
