"""SPEC-021 — the ``[security.validators]`` TOFU approval store (arctrust).

The models (``ValidatorEntry`` / ``ValidatorsConfig``), the canonical source
hash, the pure ``approve_source`` mutation, and the file-level ``approve`` /
``disapprove`` that persist a pin into an ``arcagent.toml`` all live in arctrust
because approval is a trust-store concern. These tests pin the schema and the
tomlkit round-trip; the end-to-end "pin flips a load verdict" behaviour is an
arcagent integration test (it needs the capability inventory).
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest
from pydantic import ValidationError

from arctrust import (
    ValidatorEntry,
    ValidatorsConfig,
    approve,
    approve_source,
    disapprove,
    hash_source,
    load_validators,
    persist_validators,
)

_SOURCE = "async def fn(): return 42\n"


def _write_config(tmp_path: Path, *, extra: str = "") -> Path:
    """Write a minimal ``arcagent.toml`` and return its path."""
    path = tmp_path / "arcagent.toml"
    path.write_text(
        "[agent]\n"
        'name = "fixture"\n'
        "\n[llm]\n"
        'model = "test/model"\n'
        "\n[security]\n"
        'tier = "enterprise"\n' + extra,
        encoding="utf-8",
    )
    return path


class TestValidatorEntry:
    def test_required_fields(self) -> None:
        entry = ValidatorEntry(
            name="create-skill",
            hash="sha256:abc123",
            approver="alice@example.com",
            timestamp="2026-04-28T14:30:00Z",
        )
        assert entry.name == "create-skill"
        assert entry.hash == "sha256:abc123"

    def test_missing_field_rejected(self) -> None:
        with pytest.raises(ValidationError):
            ValidatorEntry(name="x", hash="sha256:y", approver="a")  # type: ignore[call-arg]


class TestValidatorsConfigDefaults:
    def test_defaults_safe(self) -> None:
        v = ValidatorsConfig()
        assert v.auto_run_agent_code is False
        assert v.approved == ()


class TestHashSourceAndApproveSource:
    def test_hash_source_is_sha256_prefixed(self) -> None:
        digest = hash_source(_SOURCE)
        assert digest.startswith("sha256:")
        assert len(digest) == len("sha256:") + 64

    def test_approve_source_pins_and_supersedes(self) -> None:
        v = ValidatorsConfig()
        pinned = approve_source(
            v, name="tool", source=_SOURCE, approver="op", timestamp="t1"
        )
        assert len(pinned.approved) == 1
        assert pinned.approved[0].hash == hash_source(_SOURCE)

        # Re-approving the same name after drift supersedes the stale pin.
        superseded = approve_source(
            pinned, name="tool", source=_SOURCE + "x", approver="op", timestamp="t2"
        )
        assert len(superseded.approved) == 1
        assert superseded.approved[0].hash == hash_source(_SOURCE + "x")
        assert superseded.approved[0].timestamp == "t2"


class TestLoadValidators:
    def test_missing_file_is_default(self, tmp_path: Path) -> None:
        assert load_validators(tmp_path / "nope.toml") == ValidatorsConfig()

    def test_reads_block_from_config(self, tmp_path: Path) -> None:
        path = _write_config(
            tmp_path,
            extra=(
                "\n[security.validators]\n"
                "auto_run_agent_code = true\n"
                "\n[[security.validators.approved]]\n"
                'name = "known"\n'
                'hash = "sha256:abc"\n'
                'approver = "alice@example.com"\n'
                'timestamp = "2026-04-28T00:00:00Z"\n'
            ),
        )
        validators = load_validators(path)
        assert validators.auto_run_agent_code is True
        assert len(validators.approved) == 1
        assert validators.approved[0].name == "known"


class TestApproveDisapprovePersistence:
    def test_approve_writes_pin_and_round_trips(self, tmp_path: Path) -> None:
        path = _write_config(tmp_path)
        updated = approve(
            path, name="reporter", source=_SOURCE, approver="did:arc:ui:operator", timestamp="ts"
        )
        assert updated.approved[0].hash == hash_source(_SOURCE)

        # The pin is on disk under [[security.validators.approved]].
        reloaded = load_validators(path)
        assert len(reloaded.approved) == 1
        entry = reloaded.approved[0]
        assert entry.name == "reporter"
        assert entry.hash == hash_source(_SOURCE)
        assert entry.approver == "did:arc:ui:operator"
        assert entry.timestamp == "ts"

        # The rest of the file round-trips untouched (tier still enterprise).
        doc = tomllib.loads(path.read_text(encoding="utf-8"))
        assert doc["security"]["tier"] == "enterprise"

    def test_disapprove_removes_pin(self, tmp_path: Path) -> None:
        path = _write_config(tmp_path)
        approve(path, name="reporter", source=_SOURCE, approver="op", timestamp="ts")
        assert disapprove(path, name="reporter") is True
        assert load_validators(path).approved == ()

    def test_disapprove_unknown_is_false(self, tmp_path: Path) -> None:
        path = _write_config(tmp_path)
        assert disapprove(path, name="ghost") is False

    def test_persist_creates_missing_security_block(self, tmp_path: Path) -> None:
        path = tmp_path / "arcagent.toml"
        path.write_text('[agent]\nname = "x"\n\n[llm]\nmodel = "m"\n', encoding="utf-8")
        persist_validators(path, ValidatorsConfig(auto_run_agent_code=True))
        reloaded = load_validators(path)
        assert reloaded.auto_run_agent_code is True
