"""The arcllm.toml module surface is derived from arcllm's packaged config.toml.

Every generated ``arcllm.toml`` lists every arcllm module so an operator can find
any knob. The rot guard fails the moment the packaged config adds a module the
renderer would silently omit.
"""

from __future__ import annotations

import re

import arcllm
from arcllm.config_surface import _packaged_config_text


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
        assert (
            _rendered_module_names(arcllm.commented_module_surface()) == _packaged_module_names()
        )

    def test_packaged_set_is_the_known_thirteen(self) -> None:
        # A human tripwire: names change -> read the diff, don't rubber-stamp.
        assert _packaged_module_names() == {
            "routing",
            "telemetry",
            "audit",
            "retry",
            "fallback",
            "rate_limit",
            "circuit_breaker",
            "load_balance",
            "queue",
            "otel",
            "security",
            "injection",
            "guardrails",
        }
