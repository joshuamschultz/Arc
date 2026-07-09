"""Unit tests for the shared exponential_backoff helper."""

from __future__ import annotations

from arcgateway.adapters._backoff import exponential_backoff


def test_first_attempt_returns_base() -> None:
    assert exponential_backoff(1, base=30.0, factor=2.0, cap=300.0) == 30.0


def test_grows_by_factor_each_attempt() -> None:
    assert exponential_backoff(2, base=30.0, factor=2.0, cap=300.0) == 60.0
    assert exponential_backoff(3, base=30.0, factor=2.0, cap=300.0) == 120.0


def test_caps_at_ceiling() -> None:
    assert exponential_backoff(20, base=30.0, factor=2.0, cap=300.0) == 300.0


def test_attempt_below_one_floors_to_first() -> None:
    assert exponential_backoff(0, base=30.0, factor=2.0, cap=300.0) == 30.0


def test_returns_float() -> None:
    result = exponential_backoff(1, base=2, factor=2, cap=60)
    assert isinstance(result, float)
