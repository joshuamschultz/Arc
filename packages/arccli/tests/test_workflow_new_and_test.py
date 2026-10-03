"""``arc workflow templates | new --from | test`` (J3 F6, G5, G8)."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from arctrust.paths import workflows_dir

from arccli.commands import workflow as wf_cmd
from arccli.commands.workflow import workflow_handler

_GATE_WORKFLOW = """
[workflow]
id = "release"
version = 1
owner = "@sales"

[[node]]
id = "ok"
kind = "gate"
gate = "human:approve_release"
"""


@pytest.fixture(autouse=True)
def _isolated_arc(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ARC_CONFIG_DIR", str(tmp_path))
    monkeypatch.setenv("ARCSTORE_DATA_DIR", str(tmp_path / "data"))
    from arcstore.backends.memory import FakeBackend

    backend = FakeBackend()
    monkeypatch.setattr(wf_cmd, "_backend_factory", lambda: backend)

    class _Owners:
        async def get(self, handle: str) -> Any:
            return SimpleNamespace(did="did:arc:local:agent/sales01")

    async def _bindings(arc_dir: Path) -> tuple[Any, Any]:
        return _Owners(), None

    monkeypatch.setattr(wf_cmd, "_team_bindings", _bindings)


@pytest.fixture
def arc_dir(tmp_path: Path) -> Path:
    from arccli.commands.operator import load_operator_key

    load_operator_key(tmp_path)
    return tmp_path


def test_templates_lists_the_four_starters(
    arc_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    workflow_handler(["templates", "--dir", str(arc_dir)])

    out = capsys.readouterr().out
    for template in (
        "intake_specialist",
        "fanout_synthesize",
        "maker_checker",
        "scheduled_watcher",
    ):
        assert template in out


def test_new_copies_a_template_as_an_unsigned_draft_and_names_the_sign_step(
    arc_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    workflow_handler(
        [
            "new",
            "weekly-brief",
            "--from",
            "fanout_synthesize",
            "--owner",
            "@sales",
            "--dir",
            str(arc_dir),
        ]
    )

    out = capsys.readouterr().out
    assert "arc workflow sign weekly-brief" in out
    bundle = workflows_dir(arc_dir) / "weekly-brief"
    assert (bundle / "workflow.toml").is_file()
    assert (bundle / "prompts" / "synthesize.md").is_file()
    assert not (bundle / "workflow.toml.arcsig").exists()

    workflow_handler(["sign", "weekly-brief", "--dir", str(arc_dir)])
    workflow_handler(["verify", "weekly-brief", "--dir", str(arc_dir)])
    assert "VALID" in capsys.readouterr().out


def test_new_refuses_an_unknown_template(
    arc_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as exc:
        workflow_handler(["new", "x", "--from", "nope", "--dir", str(arc_dir)])

    assert exc.value.code == 1
    assert "no template" in capsys.readouterr().err


def test_new_will_not_overwrite_an_existing_workflow(arc_dir: Path) -> None:
    workflow_handler(["new", "dup", "--from", "maker_checker", "--dir", str(arc_dir)])

    with pytest.raises(SystemExit):
        workflow_handler(["new", "dup", "--from", "maker_checker", "--dir", str(arc_dir)])


def test_test_run_starts_flagged_and_is_cancelled_when_it_waits_too_long(
    arc_dir: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    source = tmp_path / "src" / "release"
    source.mkdir(parents=True)
    (source / "workflow.toml").write_text(_GATE_WORKFLOW, encoding="utf-8")
    workflow_handler(["create", str(source), "--dir", str(arc_dir)])
    capsys.readouterr()

    with pytest.raises(SystemExit) as exc:
        workflow_handler(["test", "release", "--timeout", "1", "--dir", str(arc_dir)])

    assert exc.value.code == 1
    captured = capsys.readouterr()
    assert "TEST run test-" in captured.out
    assert "Cancelled" in captured.err


def test_sign_refuses_a_workflow_still_owned_by_the_template_placeholder(
    arc_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    workflow_handler(["new", "ph", "--from", "maker_checker", "--dir", str(arc_dir)])
    capsys.readouterr()

    with pytest.raises(SystemExit) as exc:
        workflow_handler(["sign", "ph", "--dir", str(arc_dir)])

    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert "placeholder" in err and "maker" in err and "--owner" in err
    assert not (workflows_dir(arc_dir) / "ph" / "workflow.toml.arcsig").exists()


@pytest.mark.parametrize("command", [["test", "ph"], ["run", "ph"]])
def test_run_and_test_refuse_a_placeholder_owner_up_front(
    arc_dir: Path, capsys: pytest.CaptureFixture[str], command: list[str]
) -> None:
    workflow_handler(["new", "ph", "--from", "maker_checker", "--dir", str(arc_dir)])
    capsys.readouterr()

    with pytest.raises(SystemExit) as exc:
        workflow_handler([*command, "--dir", str(arc_dir)])

    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert "placeholder" in err and "maker" in err and "--owner" in err


def test_edit_owner_alone_fixes_a_placeholder_draft_so_it_signs(
    arc_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    workflow_handler(["new", "ph", "--from", "maker_checker", "--dir", str(arc_dir)])
    capsys.readouterr()

    workflow_handler(["edit", "ph", "--owner", "@sales", "--dir", str(arc_dir)])
    assert "Edited ph -> v2" in capsys.readouterr().out

    workflow_handler(["sign", "ph", "--dir", str(arc_dir)])
    assert "Signed" in capsys.readouterr().out


def test_edit_owner_for_one_node_leaves_the_others_blocked(
    arc_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    workflow_handler(["new", "ph", "--from", "maker_checker", "--dir", str(arc_dir)])
    workflow_handler(["edit", "ph", "--node", "maker", "--owner", "@sales", "--dir", str(arc_dir)])
    capsys.readouterr()

    with pytest.raises(SystemExit):
        workflow_handler(["sign", "ph", "--dir", str(arc_dir)])

    err = capsys.readouterr().err
    assert "checker" in err and "publish" in err
    assert "node(s) maker" not in err


def test_the_placeholder_error_points_at_the_single_edit_command(
    arc_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    workflow_handler(["new", "ph", "--from", "maker_checker", "--dir", str(arc_dir)])
    capsys.readouterr()

    with pytest.raises(SystemExit):
        workflow_handler(["sign", "ph", "--dir", str(arc_dir)])

    assert "arc workflow edit ph --owner @<agent>" in capsys.readouterr().err


def test_edit_needs_a_document_or_an_owner(arc_dir: Path) -> None:
    workflow_handler(["new", "ph", "--from", "maker_checker", "--dir", str(arc_dir)])

    with pytest.raises(SystemExit) as exc:
        workflow_handler(["edit", "ph", "--dir", str(arc_dir)])

    assert exc.value.code != 0
