"""`arc prompt` — view + edit/overwrite system prompts across all Arc packages.

CLI parity with the arcui Prompts surface (arcui.routes.agent_detail.prompts):
list / show / diff author-and-sign an operator overlay under
``<agent_root>/context/<package>/<name>.md`` + a detached ``.arcsig`` sidecar.

The signing + monkeypatch pattern mirrors
``packages/arcagent/tests/integration/test_prompt_overlay_wiring.py``: a temp
deployment operator key is generated and pinned as the default so the CLI signs
overlays with it and the agent's resolver verifies against it.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import arctrust
import pytest
from arcprompt import parse_prompt
from arctrust.operator import OperatorKey
from arctrust.policy import OperatorApprovalAuthority

from arccli.commands.prompt import prompt_handler

_PACKAGE = "arcagent"
_NAME = "spawn_guidance"


def _pin_operator(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    """Generate a deployment operator key at a temp path and pin it as default."""
    key_path = tmp_path / "operator" / "operator.key"
    key_path.parent.mkdir(parents=True)
    op = OperatorKey.generate()
    op.save(key_path)
    monkeypatch.setattr(arctrust, "default_operator_key_path", lambda: key_path)
    return OperatorApprovalAuthority(op.into_signer()).did


def _agent_root(tmp_path: Path, tier: str = "personal") -> Path:
    """Create a minimal agent root with an arcagent.toml declaring ``tier``."""
    root = tmp_path / "team" / "an_agent"
    (root / "workspace").mkdir(parents=True)
    (root / "arcagent.toml").write_text(f'[security]\ntier = "{tier}"\n', encoding="utf-8")
    return root


def _overlay(root: Path, package: str = _PACKAGE, name: str = _NAME) -> Path:
    return root / "context" / package / f"{name}.md"


def test_list_marks_stock_before_any_edit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    _pin_operator(tmp_path, monkeypatch)
    root = _agent_root(tmp_path)

    prompt_handler(["list", "--agent", str(root)])
    out = capsys.readouterr().out
    assert _PACKAGE in out
    assert _NAME in out
    # Every prompt is stock before any edit.
    assert "overridden" not in out


def test_edit_authors_signed_overlay_then_show_effective_reflects_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    did = _pin_operator(tmp_path, monkeypatch)
    root = _agent_root(tmp_path)
    body = "OVERRIDDEN spawn guidance body"
    monkeypatch.setattr("sys.stdin", _FakeStdin(body))

    prompt_handler(["edit", _PACKAGE, _NAME, "--agent", str(root), "--stdin"])
    out = capsys.readouterr().out
    assert did in out  # signer DID printed
    assert "sha256" in out

    overlay = _overlay(root)
    assert overlay.is_file()
    assert (overlay.parent / f"{_NAME}.md.arcsig").is_file()
    # The overlay is a faithful round-trip of the authored body.
    assert parse_prompt(overlay.read_bytes(), source="overlay").body == body

    # show --effective resolves the signed overlay (verifies against the pinned key).
    prompt_handler(["show", _PACKAGE, _NAME, "--agent", str(root), "--effective"])
    assert body in capsys.readouterr().out


def test_list_shows_overridden_after_edit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    _pin_operator(tmp_path, monkeypatch)
    root = _agent_root(tmp_path)
    monkeypatch.setattr("sys.stdin", _FakeStdin("new body"))
    prompt_handler(["edit", _PACKAGE, _NAME, "--agent", str(root), "--stdin"])
    capsys.readouterr()

    prompt_handler(["list", "--agent", str(root)])
    lines = [ln for ln in capsys.readouterr().out.splitlines() if _NAME in ln]
    assert any("overridden" in ln for ln in lines)


def test_show_stock_reads_packaged_body_without_agent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    _pin_operator(tmp_path, monkeypatch)
    prompt_handler(["show", _PACKAGE, _NAME, "--stock"])
    out = capsys.readouterr().out
    assert out.strip()  # non-empty stock body


def test_diff_stock_vs_effective(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    _pin_operator(tmp_path, monkeypatch)
    root = _agent_root(tmp_path)

    # No overlay -> no diff.
    prompt_handler(["diff", _PACKAGE, _NAME, "--agent", str(root)])
    assert "No override" in capsys.readouterr().out

    monkeypatch.setattr("sys.stdin", _FakeStdin("TOTALLY DIFFERENT BODY"))
    prompt_handler(["edit", _PACKAGE, _NAME, "--agent", str(root), "--stdin"])
    capsys.readouterr()

    prompt_handler(["diff", _PACKAGE, _NAME, "--agent", str(root)])
    diff = capsys.readouterr().out
    assert "TOTALLY DIFFERENT BODY" in diff
    assert "+++" in diff and "---" in diff


def test_edit_from_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: Any) -> None:
    _pin_operator(tmp_path, monkeypatch)
    root = _agent_root(tmp_path)
    src = tmp_path / "body.txt"
    src.write_text("body authored in a file", encoding="utf-8")

    prompt_handler(["edit", _PACKAGE, _NAME, "--agent", str(root), "--file", str(src)])
    capsys.readouterr()
    assert parse_prompt(_overlay(root).read_bytes(), source="overlay").body == (
        "body authored in a file"
    )


def test_edit_refuses_secret_content(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    _pin_operator(tmp_path, monkeypatch)
    root = _agent_root(tmp_path)
    monkeypatch.setattr("sys.stdin", _FakeStdin('api_key = "' + "A" * 24 + '"'))

    with pytest.raises(SystemExit) as exc:
        prompt_handler(["edit", _PACKAGE, _NAME, "--agent", str(root), "--stdin"])
    assert exc.value.code != 0
    assert not _overlay(root).exists()
    assert "credential" in capsys.readouterr().err.lower()


def test_reset_removes_overlay_and_sidecar(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    _pin_operator(tmp_path, monkeypatch)
    root = _agent_root(tmp_path)
    monkeypatch.setattr("sys.stdin", _FakeStdin("temp override"))
    prompt_handler(["edit", _PACKAGE, _NAME, "--agent", str(root), "--stdin"])
    capsys.readouterr()
    assert _overlay(root).is_file()

    prompt_handler(["reset", _PACKAGE, _NAME, "--agent", str(root)])
    assert "removed" in capsys.readouterr().out.lower()
    assert not _overlay(root).exists()
    assert not (_overlay(root).parent / f"{_NAME}.md.arcsig").exists()


def test_reset_without_overlay_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    _pin_operator(tmp_path, monkeypatch)
    root = _agent_root(tmp_path)
    with pytest.raises(SystemExit) as exc:
        prompt_handler(["reset", _PACKAGE, _NAME, "--agent", str(root)])
    assert exc.value.code != 0
    assert "no override" in capsys.readouterr().err.lower()


def test_list_requires_agent(capsys: Any) -> None:
    with pytest.raises(SystemExit) as exc:
        prompt_handler(["list"])
    assert exc.value.code != 0
    assert "--agent" in capsys.readouterr().err


def test_edit_rejects_path_traversal_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    _pin_operator(tmp_path, monkeypatch)
    root = _agent_root(tmp_path)
    monkeypatch.setattr("sys.stdin", _FakeStdin("x"))
    with pytest.raises(SystemExit) as exc:
        prompt_handler(["edit", _PACKAGE, "../../escape", "--agent", str(root), "--stdin"])
    assert exc.value.code != 0


@pytest.mark.parametrize("sub", ["show", "diff"])
def test_read_commands_reject_path_traversal_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: Any, sub: str
) -> None:
    """SEC-04: `show --effective` / `diff` refuse a traversal name, never read outside roots."""
    _pin_operator(tmp_path, monkeypatch)
    root = _agent_root(tmp_path)
    with pytest.raises(SystemExit) as exc:
        prompt_handler([sub, _PACKAGE, "../../../../etc/passwd", "--agent", str(root)])
    assert exc.value.code != 0


def test_show_stock_rejects_path_traversal_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    _pin_operator(tmp_path, monkeypatch)
    with pytest.raises(SystemExit) as exc:
        prompt_handler(["show", _PACKAGE, "../../../../etc/passwd", "--stock"])
    assert exc.value.code != 0


class _FakeStdin:
    """Minimal stdin stand-in exposing ``.read()`` for --stdin edits."""

    def __init__(self, data: str) -> None:
        self._data = data

    def read(self) -> str:
        return self._data
