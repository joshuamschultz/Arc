"""Task #35 — `arc <multi-word command> [args]` dispatch, unquoted.

Live bug: `arc gateway pair approve CODE` errored "unknown command
'gateway'" — only the quoted single-token form (`arc "gateway pair
approve" CODE`) worked, and docs/cli.md teaches the unquoted one.
``_dispatch_oneshot`` only ever looked up ``argv[0]`` against the registry,
which never matches a multi-word ``CommandDef.name`` like
"gateway pair approve".

These tests drive ``_dispatch_oneshot`` itself (the real one-shot entry
point ``arc <command> [args...]`` uses) with a controlled, temporary
registry — proving the FULL dispatch path resolves and invokes the correct
handler with the correct remaining args, not just the lookup helper.
"""

from __future__ import annotations

import pytest

import arccli.commands.registry as registry_mod
import arccli.main as main_mod
from arccli.commands.registry import CommandDef


def _stub(calls: list[list[str]]) -> object:
    def _handler(args: list[str]) -> None:
        calls.append(args)

    return _handler


class TestDispatchOneshotMultiWord:
    def test_three_word_command_unquoted_reaches_handler(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The literal live bug, reproduced through the real dispatcher."""
        calls: list[list[str]] = []
        fake_cmd = CommandDef(
            name="gateway pair approve",
            description="stub",
            category="Configuration",
            handler=_stub(calls),  # type: ignore[arg-type]
        )
        monkeypatch.setattr(registry_mod, "COMMAND_REGISTRY", [fake_cmd])

        main_mod._dispatch_oneshot(["gateway", "pair", "approve", "ABCD1234"])

        assert calls == [["ABCD1234"]]

    def test_single_word_command_unaffected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls: list[list[str]] = []
        fake_cmd = CommandDef(
            name="agent",
            description="stub",
            category="Session",
            handler=_stub(calls),  # type: ignore[arg-type]
        )
        monkeypatch.setattr(registry_mod, "COMMAND_REGISTRY", [fake_cmd])

        main_mod._dispatch_oneshot(["agent", "status", "."])

        assert calls == [["status", "."]]

    def test_still_works_with_the_previously_required_quoting(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Backward compat: the workaround that used to be mandatory still works."""
        calls: list[list[str]] = []
        fake_cmd = CommandDef(
            name="gateway pair approve",
            description="stub",
            category="Configuration",
            handler=_stub(calls),  # type: ignore[arg-type]
        )
        monkeypatch.setattr(registry_mod, "COMMAND_REGISTRY", [fake_cmd])

        main_mod._dispatch_oneshot(["gateway pair approve", "ABCD1234"])

        assert calls == [["ABCD1234"]]

    def test_unknown_command_still_errors_cleanly(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setattr(registry_mod, "COMMAND_REGISTRY", [])

        with pytest.raises(SystemExit) as exc:
            main_mod._dispatch_oneshot(["totally", "bogus", "thing"])

        assert exc.value.code == 1
        assert "unknown command" in capsys.readouterr().err

    def test_real_registry_gateway_pair_approve_resolves(self) -> None:
        """Against the REAL registry (no monkeypatch): confirms the actual
        production command is reachable unquoted, not just a synthetic one.
        """
        from arccli.commands.registry import resolve_command_and_args

        cmd, args = resolve_command_and_args(["gateway", "pair", "approve", "ABCD1234"])
        assert cmd is not None
        assert cmd.name == "gateway pair approve"
        assert cmd.handler is not None
        assert args == ["ABCD1234"]
