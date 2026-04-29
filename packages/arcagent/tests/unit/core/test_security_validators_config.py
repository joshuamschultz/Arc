"""SPEC-021 Task 1.9 — ``[security.validators]`` TOML schema.

The TOFU policy layer reads approved validator hashes from
``arcagent.toml`` under ``[security.validators]``. This test pins the
schema:

  * ``auto_run_agent_code`` — bool, default False (federal-safe default).
  * ``approved`` — list of ``ValidatorEntry(name, hash, approver, timestamp)``.

Trust file lives at agent-root, never inside workspace (R-043). The
loader treats ``arcagent.toml`` as authoritative; only the human user
appends entries via ``arc trust approve``.
"""

from __future__ import annotations

import pytest


class TestValidatorEntry:
    def test_required_fields(self) -> None:
        from arcagent.core.config import ValidatorEntry

        entry = ValidatorEntry(
            name="create-skill",
            hash="sha256:abc123",
            approver="alice@example.com",
            timestamp="2026-04-28T14:30:00Z",
        )
        assert entry.name == "create-skill"
        assert entry.hash == "sha256:abc123"

    def test_missing_field_rejected(self) -> None:
        from arcagent.core.config import ValidatorEntry

        with pytest.raises(Exception):  # pydantic ValidationError
            ValidatorEntry(name="x", hash="sha256:y", approver="a")  # type: ignore[call-arg]


class TestValidatorsConfigDefaults:
    def test_defaults_safe(self) -> None:
        from arcagent.core.config import ValidatorsConfig

        v = ValidatorsConfig()
        assert v.auto_run_agent_code is False
        assert v.approved == ()

    def test_round_trip_with_entries(self) -> None:
        from arcagent.core.config import ValidatorEntry, ValidatorsConfig

        v = ValidatorsConfig(
            auto_run_agent_code=True,
            approved=[
                ValidatorEntry(
                    name="x",
                    hash="sha256:y",
                    approver="a@b",
                    timestamp="2026-04-28T00:00:00Z",
                )
            ],
        )
        assert v.auto_run_agent_code is True
        assert len(v.approved) == 1
        assert v.approved[0].name == "x"


class TestSecurityConfigEmbedsValidators:
    def test_validators_field_present(self) -> None:
        from arcagent.core.config import SecurityConfig, ValidatorsConfig

        s = SecurityConfig()
        assert isinstance(s.validators, ValidatorsConfig)
        assert s.validators.auto_run_agent_code is False

    def test_toml_round_trip(self, tmp_path: object) -> None:
        """End-to-end: TOML body produces the expected nested schema."""
        from pathlib import Path

        from arcagent.core.config import load_config

        toml = """
[agent]
name = "test"
description = "x"

[identity]
did = "did:example:123"
keypair_path = "keypair.json"

[llm]
model = "test"

[security]
tier = "enterprise"

[security.validators]
auto_run_agent_code = true

[[security.validators.approved]]
name = "create-skill"
hash = "sha256:abc"
approver = "alice@example.com"
timestamp = "2026-04-28T14:30:00Z"
"""
        path = Path(tmp_path) / "arcagent.toml"  # type: ignore[arg-type]
        path.write_text(toml)
        cfg = load_config(path)
        assert cfg.security.tier == "enterprise"
        assert cfg.security.validators.auto_run_agent_code is True
        assert len(cfg.security.validators.approved) == 1
        approved = cfg.security.validators.approved[0]
        assert approved.name == "create-skill"
        assert approved.hash == "sha256:abc"
