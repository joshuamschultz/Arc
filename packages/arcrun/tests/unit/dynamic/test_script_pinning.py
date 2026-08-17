"""A resumed run must re-execute the exact script it recorded against.

The journal is keyed on the calls a script issues, so re-authoring on resume is
not merely wasteful — a model that words one prompt differently diverges the
journal at the first host call and strands a run that was otherwise recoverable.
"""

from __future__ import annotations

from pathlib import Path

from arcrun.strategies.dynamic import _run_home, _store_script, _stored_script

SCRIPT = 'phase("work")\ncomplete({"done": True})\n'


class _State:
    """Just the two fields the run-home resolver reads."""

    def __init__(self, work_dir: Path | None, run_id: str) -> None:
        self.work_dir = work_dir
        self.run_id = run_id


def test_a_validated_script_is_pinned_and_read_back_verbatim(tmp_path: Path) -> None:
    home = _run_home(_State(tmp_path, "run-1"))
    assert home is not None

    _store_script(home, SCRIPT, None)

    assert _stored_script(home, None) == SCRIPT


def test_the_pin_is_keyed_on_the_run_so_two_runs_never_share_a_script(
    tmp_path: Path,
) -> None:
    """A pinned run id is what makes a re-run find its own work, not another's."""
    first = _run_home(_State(tmp_path, "run-1"))
    second = _run_home(_State(tmp_path, "run-2"))
    assert first is not None and second is not None

    _store_script(first, SCRIPT, None)

    assert _stored_script(second, None) == ""


def test_a_run_with_nowhere_to_write_reports_no_pinned_script(tmp_path: Path) -> None:
    """Without a durable home the strategy must author, not resume from nothing."""
    assert _run_home(_State(None, "run-1")) is None
    assert _stored_script(None, None) == ""


def test_pinning_never_raises_when_the_home_cannot_be_written(tmp_path: Path) -> None:
    """Losing the pin costs a re-author; it must never fail a ready run."""
    blocker = tmp_path / "blocked"
    blocker.write_text("not a directory")

    _store_script(blocker / "run-1", SCRIPT, None)

    assert _stored_script(blocker / "run-1", None) == ""


def test_a_corrupt_pin_reads_as_absent_rather_than_raising(tmp_path: Path) -> None:
    """An unreadable pin degrades to authoring, which always terminates."""
    home = _run_home(_State(tmp_path, "run-1"))
    assert home is not None
    home.mkdir(parents=True)
    (home / "script.py").mkdir()

    assert _stored_script(home, None) == ""


def test_pinned_bytes_survive_content_that_needs_encoding(tmp_path: Path) -> None:
    """Prompts carry real prose, so the pin must be byte-exact, not ASCII-only."""
    home = _run_home(_State(tmp_path, "run-1"))
    assert home is not None
    script = 'log("café — naïve ✓")\ncomplete(1)\n'

    _store_script(home, script, None)

    assert _stored_script(home, None) == script


def test_pinning_is_idempotent_so_a_third_attempt_still_replays(tmp_path: Path) -> None:
    home = _run_home(_State(tmp_path, "run-1"))
    assert home is not None

    _store_script(home, SCRIPT, None)
    _store_script(home, SCRIPT, None)

    assert _stored_script(home, None) == SCRIPT
