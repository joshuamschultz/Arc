"""Item 20 — what ``arc connector`` causes is attributed to the CLI operator.

``Connections`` records the bound causal initiator as the actor of every
credential read and change (it used to record the operator KEY's DID). The CLI
binds the person at the terminal; the key stays the signer. Bound per command,
not in ``main``: a long-lived ``arc ui``/``arc agent`` daemon must not inherit
an operator identity for everything it later does.
"""

from __future__ import annotations

import getpass

import pytest
from arctrust import causal

from arccli.commands import connector


def test_a_connector_command_runs_as_the_cli_operator(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[causal.CausalContext | None] = []

    def fake_dispatch(parser: object, table: object, argv: list[str]) -> None:
        seen.append(causal.current())

    monkeypatch.setattr(connector, "dispatch", fake_dispatch)
    connector.connector_handler(["list"])

    (ctx,) = seen
    assert ctx is not None
    assert ctx.initiator == "operator"
    assert ctx.initiator_id == f"did:arc:cli:{getpass.getuser()}"
    assert causal.current() is None
