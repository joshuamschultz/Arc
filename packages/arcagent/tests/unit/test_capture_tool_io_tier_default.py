"""RED — SPEC-073 Phase A (A3): tier-driven ``capture_tool_io`` default.

``TelemetryConfig.capture_tool_io`` today hardcodes ``True`` (see
``arcagent/core/config.py``) with no awareness of ``security.tier`` at all.
SPEC-073 A3 makes it tier-driven: personal defaults to capturing tool I/O
(the current effective behavior for personal, kept), federal/enterprise
default to NOT capturing (bodies may carry sensitive data), and an explicit
value in either tier's TOML is honored unchanged.

The federal-tier assertion below is the one guaranteed to fail against
today's code (the hardcoded default is always ``True`` regardless of tier)
-- that failure is the RED proof the tier-resolution validator does not
exist yet. The personal-tier and explicit-override assertions currently
happen to already hold (the hardcoded default already reads ``True``, and
an explicit value was always honored) -- they are kept here as the
regression pins the post-implementation contract requires, not as
RED-proving assertions in isolation.
"""

from __future__ import annotations

from arcagent.core.config import (
    AgentConfig,
    ArcAgentConfig,
    LLMConfig,
    SecurityConfig,
    TelemetryConfig,
)


def _config(**overrides: object) -> ArcAgentConfig:
    return ArcAgentConfig(
        agent=AgentConfig(name="tier-default-agent"),
        llm=LLMConfig(model="test/model"),
        **overrides,
    )


def test_capture_tool_io_unset_resolves_true_for_personal_tier() -> None:
    cfg = _config(security=SecurityConfig(tier="personal"))

    assert cfg.telemetry.capture_tool_io is True


def test_capture_tool_io_unset_resolves_false_for_federal_tier() -> None:
    cfg = _config(security=SecurityConfig(tier="federal"))

    assert cfg.telemetry.capture_tool_io is False


def test_capture_tool_io_unset_resolves_false_for_enterprise_tier() -> None:
    cfg = _config(security=SecurityConfig(tier="enterprise"))

    assert cfg.telemetry.capture_tool_io is False


def test_explicit_capture_tool_io_false_on_personal_tier_stays_false() -> None:
    cfg = _config(
        security=SecurityConfig(tier="personal"),
        telemetry=TelemetryConfig(capture_tool_io=False),
    )

    assert cfg.telemetry.capture_tool_io is False


def test_explicit_capture_tool_io_true_on_federal_tier_stays_true() -> None:
    """An operator who explicitly opts a federal agent into raw capture is honored."""
    cfg = _config(
        security=SecurityConfig(tier="federal"),
        telemetry=TelemetryConfig(capture_tool_io=True),
    )

    assert cfg.telemetry.capture_tool_io is True
