"""``arc runtime`` — the operator's hands on the ``current`` symlink.

``arctrust.paths.activate_runtime`` has always been able to flip ``current``
atomically, and nothing on a real box ever called it: ``runtime/current`` shipped
as a plain directory holding only ``modules/``, with no versioned siblings. So
"an update is an atomic flip and a rollback is flipping it back" was a docstring
claim with no operator surface behind it — there was no way to roll back at all.

These assertions are that surface: list what is installed, say which one is
live, and put a named version back — while refusing anything that would point
``current`` outside the runtime root.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from arctrust import paths

from arccli.commands import runtime as runtime_cmd


@pytest.fixture
def arc_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """An isolated Arc home — nothing here may reach the developer's own ~/.arc."""
    root = tmp_path / "arc-home"
    monkeypatch.setenv("ARC_CONFIG_DIR", str(root))
    for version in ("0.2.0-aaaaaaa", "0.2.0-bbbbbbb"):
        (root / "runtime" / version / ".venv" / "bin").mkdir(parents=True)
    return root


def test_activate_makes_current_a_real_symlink(
    arc_home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A plain directory named ``current`` is the shape that has no rollback."""
    runtime_cmd.runtime_handler(["activate", "0.2.0-aaaaaaa"])

    current = paths.arc_runtime()
    assert current.is_symlink(), "current must be a symlink, not a copied tree"
    assert current.resolve().name == "0.2.0-aaaaaaa"
    assert "0.2.0-aaaaaaa" in capsys.readouterr().out


def test_activate_rolls_back_to_the_previous_version(arc_home: Path) -> None:
    """Rollback is the same verb — that is the whole point of the flip."""
    runtime_cmd.runtime_handler(["activate", "0.2.0-aaaaaaa"])
    runtime_cmd.runtime_handler(["activate", "0.2.0-bbbbbbb"])
    assert paths.arc_runtime().resolve().name == "0.2.0-bbbbbbb"

    runtime_cmd.runtime_handler(["activate", "0.2.0-aaaaaaa"])
    assert paths.arc_runtime().resolve().name == "0.2.0-aaaaaaa"


def test_list_marks_the_active_version(arc_home: Path, capsys: pytest.CaptureFixture[str]) -> None:
    runtime_cmd.runtime_handler(["activate", "0.2.0-bbbbbbb"])
    capsys.readouterr()

    runtime_cmd.runtime_handler(["list"])

    out = capsys.readouterr().out
    assert "0.2.0-aaaaaaa" in out
    assert "0.2.0-bbbbbbb" in out
    active_line = next(line for line in out.splitlines() if "0.2.0-bbbbbbb" in line)
    assert "active" in active_line.lower()


def test_activating_a_version_that_is_not_installed_fails_loudly(arc_home: Path) -> None:
    """Exit non-zero rather than leave ``current`` dangling at a missing tree."""
    with pytest.raises(SystemExit) as exc:
        runtime_cmd.runtime_handler(["activate", "9.9.9"])
    assert exc.value.code == 1
    assert not paths.arc_runtime().exists()


def test_a_traversing_version_name_is_refused(arc_home: Path) -> None:
    """A version names a sibling directory. Accepting a path would let ``current``
    be pointed at ``state/`` — the operator key's own root."""
    with pytest.raises(SystemExit) as exc:
        runtime_cmd.runtime_handler(["activate", "../../state"])
    assert exc.value.code == 1
    assert not paths.arc_runtime().is_symlink()


def test_the_set_aside_pre_symlink_install_is_not_offered_as_a_version(
    arc_home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A DGX-shaped box has a real ``current/`` directory that the first flip moves aside.

    ``activate_runtime`` keeps that copy rather than deleting it — its modules are
    the only record of what the box had. But it is a rescued artifact, not an
    installed runtime, and offering it as one invites an operator to activate a
    tree with no venv in it.
    """
    (arc_home / "runtime" / "current").mkdir(parents=True)
    runtime_cmd.runtime_handler(["activate", "0.2.0-aaaaaaa"])
    capsys.readouterr()

    runtime_cmd.runtime_handler(["list"])

    out = capsys.readouterr().out
    assert "pre-symlink" not in out
    assert "0.2.0-aaaaaaa" in out
