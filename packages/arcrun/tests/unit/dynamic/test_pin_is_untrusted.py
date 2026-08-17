"""A pinned script is convenience, never trust.

The pin lives at ``<agent workspace>/runs/dynamic/<run_id>/script.py``, which is
exactly where the agent's own ``write``/``bash``/``edit`` tools operate. Pinning
was added so a resume does not re-author and diverge its journal; it must not
become a way to hand a workspace write straight to the interpreter with the
validation gate skipped.
"""

from __future__ import annotations

from pathlib import Path

from arcrun.strategies.dynamic import (
    _discard_script,
    _run_home,
    _store_script,
    _stored_script,
)

VALID = 'phase("work")\ncomplete({"done": True})\n'


class _State:
    def __init__(self, work_dir: Path | None, run_id: str) -> None:
        self.work_dir = work_dir
        self.run_id = run_id


def test_a_tampered_pin_is_discarded_rather_than_left_to_fail_forever(
    tmp_path: Path,
) -> None:
    """One bad write must not permanently kill a run id."""
    home = _run_home(_State(tmp_path, "run-1"))
    assert home is not None
    _store_script(home, VALID, None)

    _discard_script(home)

    assert _stored_script(home, None) == ""


def test_discarding_a_pin_that_is_already_gone_is_not_an_error(tmp_path: Path) -> None:
    home = _run_home(_State(tmp_path, "run-1"))
    assert home is not None

    _discard_script(home)
    _discard_script(None)

    assert _stored_script(home, None) == ""


def test_a_pin_with_one_bad_byte_degrades_to_authoring(tmp_path: Path) -> None:
    """UnicodeDecodeError subclasses ValueError, not OSError — it escaped once."""
    home = _run_home(_State(tmp_path, "run-1"))
    assert home is not None
    home.mkdir(parents=True)
    (home / "script.py").write_bytes(b"complete(1)\n\xff\xfe invalid utf-8")

    assert _stored_script(home, None) == ""
