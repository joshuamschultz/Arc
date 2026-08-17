"""Which execution strategies a tier permits.

Strategy choice is a security decision, not a preference: `code` and `dynamic`
both let a model author its own control flow. Federal must run only the strategy
whose sequence of work is code an operator can read, and that floor has to hold
against config, because a floor config can widen is not a floor.
"""

from __future__ import annotations

import pytest

from arcagent.tools.approval_policy import resolve_allowed_strategies


@pytest.mark.parametrize("tier", ["personal", "enterprise"])
def test_an_open_tier_leaves_the_set_open_so_each_run_picks_its_own_shape(
    tier: str,
) -> None:
    """None reaches arcrun meaning every registered strategy."""
    assert resolve_allowed_strategies(None, tier) is None


@pytest.mark.parametrize("tier", ["personal", "enterprise"])
def test_an_open_tier_still_honours_a_deliberate_operator_narrowing(tier: str) -> None:
    """Open by default is not the same as ignoring an operator who narrowed it."""
    assert resolve_allowed_strategies(["react"], tier) == ["react"]


def test_federal_narrows_to_react_even_when_nothing_was_configured() -> None:
    assert resolve_allowed_strategies(None, "federal") == ["react"]


def test_federal_cannot_be_widened_by_configuration() -> None:
    """The whole point of a floor: asking for more does not get you more."""
    assert resolve_allowed_strategies(["react", "dynamic", "code"], "federal") == ["react"]


def test_federal_refuses_a_config_that_names_only_a_model_authored_strategy() -> None:
    """Substituting react is the safe reading; silently running dynamic is not."""
    assert resolve_allowed_strategies(["dynamic"], "federal") == ["react"]


def test_federal_says_out_loud_what_it_ignored(caplog: pytest.LogCaptureFixture) -> None:
    """An operator whose config was overridden must be able to find out why."""
    with caplog.at_level("WARNING"):
        resolve_allowed_strategies(["react", "dynamic"], "federal")

    assert "federal" in caplog.text
    assert "dynamic" in caplog.text


def test_federal_stays_quiet_when_the_config_already_agreed(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A warning that fires on correct config trains operators to ignore it."""
    with caplog.at_level("WARNING", logger="arcagent.tools.approval_policy"):
        resolve_allowed_strategies(["react"], "federal")

    assert caplog.text == ""
