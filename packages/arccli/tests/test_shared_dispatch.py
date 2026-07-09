"""Unit tests for the shared argparse dispatch tail (arccli.commands._shared).

Every `arc <group>` handler now routes through ``dispatch``; these lock the
behavior that used to be copy-pasted six times: empty args -> help + exit 0,
bare group -> help + exit 0, unknown subcommand -> stderr + exit 1 with the
group name taken from ``parser.prog``.
"""

from __future__ import annotations

import argparse

import pytest

from arccli.commands._shared import dispatch


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="arc demo")
    subs = parser.add_subparsers(dest="subcmd", metavar="<subcommand>")
    subs.add_parser("go")
    return parser


def test_empty_args_prints_help_and_exits_zero(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        dispatch(_parser(), {}, [])
    assert exc.value.code == 0
    assert "usage" in capsys.readouterr().out.lower()


def test_unknown_subcommand_uses_prog_and_exits_one(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as exc:
        dispatch(_parser(), {}, ["go"])
    assert exc.value.code == 1
    assert "arc demo: unknown subcommand 'go'" in capsys.readouterr().err


def test_known_subcommand_is_routed() -> None:
    called: list[argparse.Namespace] = []
    dispatch(_parser(), {"go": called.append}, ["go"])
    assert len(called) == 1
    assert called[0].subcmd == "go"
