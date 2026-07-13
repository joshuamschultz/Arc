"""Generated arcllm.toml files expose the COMPLETE, documented option surface.

The per-agent scaffold and the fleet ``arc init`` file must both list every
arcllm module so an operator can discover and edit any knob without knowing it
exists in advance. The surface is derived from arcllm's packaged ``config.toml``
(single source of truth); the rot-guard test below fails the moment arcllm adds
a module that the generators would otherwise silently omit.
"""

from __future__ import annotations

import re
import tomllib

import pytest

from arccli.commands._arcllm_surface import (
    _packaged_config_text,
    commented_module_surface,
)
from arccli.commands.agent._common import _DEFAULT_ARCLLM_CONFIG
from arccli.commands.init import _generate_arcllm_toml


def _packaged_module_names() -> set[str]:
    """Top-level ``[modules.<name>]`` names in arcllm's packaged config.toml."""
    return set(re.findall(r"^\[modules\.([a-z_]+)", _packaged_config_text(), re.MULTILINE))


def _rendered_module_names(text: str, prefix: str = "") -> set[str]:
    pat = rf"#?\s*\[{re.escape(prefix)}modules\.([a-z_]+)"
    return set(re.findall(pat, text))


class TestRotGuard:
    def test_surface_covers_every_packaged_module(self) -> None:
        # If arcllm adds a module, this fails until the surface picks it up —
        # which, being derived, it does automatically. This asserts the wiring.
        assert _rendered_module_names(commented_module_surface()) == _packaged_module_names()

    def test_packaged_set_is_the_known_thirteen(self) -> None:
        # A human tripwire: names change -> read the diff, don't rubber-stamp.
        assert _packaged_module_names() == {
            "routing", "telemetry", "audit", "retry", "fallback", "rate_limit",
            "circuit_breaker", "load_balance", "queue", "otel", "security",
            "injection", "guardrails",
        }


class TestPerAgentScaffold:
    def test_all_modules_present_and_commented(self) -> None:
        # Every module is discoverable, but none is an ACTIVE override (that
        # would silently shadow the fleet/packaged layer with a default).
        assert _rendered_module_names(_DEFAULT_ARCLLM_CONFIG, "llm.") == _packaged_module_names()
        parsed = tomllib.loads(_DEFAULT_ARCLLM_CONFIG)
        assert parsed["llm"].get("modules") in (None, {})

    def test_eval_and_budget_complete(self) -> None:
        assert "background_task_timeout" in _DEFAULT_ARCLLM_CONFIG
        assert "max_input_tokens" in _DEFAULT_ARCLLM_CONFIG
        assert "[budget]" in _DEFAULT_ARCLLM_CONFIG

    def test_uncommenting_a_module_is_a_valid_override(self) -> None:
        # Prove the commented surface is real config, not decoration: flip one
        # line on and it parses as an active per-agent module override.
        edited = _DEFAULT_ARCLLM_CONFIG.replace(
            "# [llm.modules.audit]\n# enabled = false",
            "[llm.modules.audit]\nenabled = true",
        )
        parsed = tomllib.loads(edited)
        assert parsed["llm"]["modules"]["audit"]["enabled"] is True


class TestFleetInit:
    @pytest.mark.parametrize("tier", ["personal", "enterprise", "federal"])
    def test_all_modules_present_and_parses(self, tier: str) -> None:
        text = _generate_arcllm_toml(tier)
        tomllib.loads(text)  # active (preset) config must parse
        assert _rendered_module_names(text) == _packaged_module_names()
        assert "background_task_timeout" in text

    def test_preset_modules_are_active_rest_commented(self) -> None:
        # personal preset activates telemetry; circuit_breaker is not in any
        # preset, so it must appear only as a commented block.
        text = _generate_arcllm_toml("personal")
        parsed = tomllib.loads(text)
        assert parsed["modules"]["telemetry"]["enabled"] is True
        assert "circuit_breaker" not in parsed.get("modules", {})
        assert "# [modules.circuit_breaker]" in text
