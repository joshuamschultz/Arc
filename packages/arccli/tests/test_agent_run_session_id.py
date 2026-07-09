"""`arc agent run` session-id scheme — dated/rolling, --session override.

A fixed ``cli:run`` id piled every task into one unbounded transcript. The
default is now a dated ``cli:run:<YYYY-MM-DD>`` (bounded per day, resumable
within the day); ``--session`` pins an explicit id.
"""

from __future__ import annotations

from datetime import date

from arccli.commands.agent._dispatch import _build_parser
from arccli.commands.agent.run import _default_session_id


def test_default_session_id_is_dated() -> None:
    assert _default_session_id() == f"cli:run:{date.today().isoformat()}"


def test_default_session_id_not_fixed_cli_run() -> None:
    # Regression: must not collapse to the old shared "cli:run".
    assert _default_session_id() != "cli:run"


def test_run_parser_session_defaults_none() -> None:
    parser = _build_parser()
    args = parser.parse_args(["run", ".", "do a thing"])
    assert args.session is None


def test_run_parser_accepts_explicit_session() -> None:
    parser = _build_parser()
    args = parser.parse_args(["run", ".", "do a thing", "--session", "proj-x"])
    assert args.session == "proj-x"
