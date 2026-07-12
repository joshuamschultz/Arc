"""WorkpadConfig validation."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from arcagent.modules.workpad.config import WorkpadConfig


def test_defaults() -> None:
    cfg = WorkpadConfig()
    assert cfg.every_n_runs == 20
    assert cfg.max_transcript_chars == 24000
    assert cfg.max_context_chars == 8000


def test_every_n_runs_override() -> None:
    assert WorkpadConfig(every_n_runs=5).every_n_runs == 5


def test_every_n_runs_rejects_zero() -> None:
    """ge=1 guards the run_count % every_n_runs trigger against modulo-by-zero."""
    with pytest.raises(ValidationError):
        WorkpadConfig(every_n_runs=0)


def test_rejects_unknown_key() -> None:
    """ModuleConfig forbids extras so a toml typo fails loudly."""
    with pytest.raises(ValidationError):
        WorkpadConfig(evrey_n_runs=20)  # type: ignore[call-arg]
