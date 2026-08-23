"""Deterministic, non-live reliability profile for ArcRun seams."""

from __future__ import annotations

import math

import pytest

from arcrun.ledger import tool_invocation_key


def _wilson_lower_bound(successes: int, total: int, z: float = 1.96) -> float:
    proportion = successes / total
    denominator = 1 + z**2 / total
    centre = proportion + z**2 / (2 * total)
    margin = z * math.sqrt(proportion * (1 - proportion) / total + z**2 / (4 * total**2))
    return (centre - margin) / denominator


def _run_profile(count: int) -> tuple[int, set[str]]:
    keys = {tool_invocation_key("run", str(index), "write", {"index": index}) for index in range(count)}
    return len(keys), keys


def test_reliability_profile_has_unique_invocations() -> None:
    successes, keys = _run_profile(100)
    assert successes == len(keys) == 100
    assert _wilson_lower_bound(successes, 100) < 0.99


@pytest.mark.reliability_nightly
def test_10k_non_live_slo_profile() -> None:
    successes, keys = _run_profile(10_000)
    assert successes == len(keys) == 10_000
    assert _wilson_lower_bound(successes, 10_000) >= 0.99
