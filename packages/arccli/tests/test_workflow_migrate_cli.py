"""``arc workflow migrate | check`` and an honest ``list`` for legacy bundles.

The alpha-2 regression: every saved bundle carried ``join = "all"``, which the
new schema forbids, so every workflow silently vanished. These tests drive the
real CLI against a legacy bundle on disk.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from arctrust.paths import workflows_dir

from arccli.commands.workflow import workflow_handler

_LEGACY = """[workflow]
schema_version = "1.0"
id = "morning"
version = 1
owner = "@olivia"

[trigger]
type = "cron"
expression = "0 7 * * *"

[[node]]
id = "a"
kind = "agent"
agent = "@olivia"
join = "all"
"""


@pytest.fixture(autouse=True)
def _isolated_arc(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ARC_CONFIG_DIR", str(tmp_path))
    monkeypatch.setenv("ARCSTORE_DATA_DIR", str(tmp_path / "data"))


@pytest.fixture
def legacy(tmp_path: Path) -> Path:
    from arccli.commands.operator import load_operator_key

    load_operator_key(tmp_path)
    bundle = workflows_dir(tmp_path) / "morning"
    bundle.mkdir(parents=True)
    (bundle / "workflow.toml").write_text(_LEGACY, encoding="utf-8")
    return bundle


def _run(tmp_path: Path, *argv: str) -> int:
    try:
        workflow_handler([*argv, "--dir", str(tmp_path)])
    except SystemExit as exc:
        return int(exc.code or 0)
    return 0


def test_list_shows_a_legacy_bundle_as_unreadable_with_the_fix(
    legacy: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _run(tmp_path, "list") == 0
    out = capsys.readouterr().out
    assert "morning" in out
    assert "UNREADABLE" in out
    assert "arc workflow migrate" in out


def test_check_exits_nonzero_listing_the_failure(
    legacy: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _run(tmp_path, "check") == 1
    out = capsys.readouterr().out
    assert "morning" in out
    assert "unreadable" in out


def test_migrate_dry_run_shows_the_change_and_writes_nothing(
    legacy: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    before = (legacy / "workflow.toml").read_text()
    assert _run(tmp_path, "migrate", "--dry-run") == 0
    out = capsys.readouterr().out
    assert "morning" in out
    assert "would_rewrite" in out
    assert (legacy / "workflow.toml").read_text() == before


def test_migrate_resign_then_check_is_clean_and_list_shows_it_signed(
    legacy: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from arctrust import sign_artifact_with_signer

    from arccli.commands.operator import resolve_operator_signer

    # the signature the old runtime wrote, over bytes that no longer match
    signer = resolve_operator_signer(tmp_path)
    stale = sign_artifact_with_signer(
        b"old-canonical-form", signer_did="operator:x", signer=signer
    )
    (legacy / "workflow.toml.arcsig").write_text(stale.to_json())

    assert _run(tmp_path, "migrate", "--resign") == 0
    assert (legacy / "workflow.toml").read_text().count("join") == 0
    assert _run(tmp_path, "check") == 0
    capsys.readouterr()
    assert _run(tmp_path, "list") == 0
    assert "signed" in capsys.readouterr().out
    assert _run(tmp_path, "migrate", "--resign") == 0  # idempotent
    assert "unchanged" in capsys.readouterr().out


def test_migrate_refuses_join_any_and_exits_nonzero(
    legacy: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    text = _LEGACY.replace('join = "all"', 'join = "any"')
    (legacy / "workflow.toml").write_text(text)
    assert _run(tmp_path, "migrate", "--resign") == 1
    out = capsys.readouterr().out
    assert "refused" in out
    assert "join" in out
    assert (legacy / "workflow.toml").read_text() == text
