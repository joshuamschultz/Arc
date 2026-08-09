"""``arc connector`` finishes a placed-credential connector, with no CLI change at all.

The brief for the credential seam asked that a non-technical person be able to
finish every bundle from **both** arcui and arccli. The web half is proved in
``arcui/tests/test_connectors_placement.py``; this is the terminal half, and it
exists to hold one claim: ``arc connector add`` needed no edit.

That is not an accident, it is the shape of the seam. A placement rides on
``[[secrets]]``, which the CLI already prompts for through ``getpass`` and hands
to :mod:`arcagent.connections`. What changed is what Arc *does* with the value —
and that is a fact of the bundle, not of the surface. So these tests drive the
real handler against a real bundle whose real binary is this interpreter, and the
binary answers only when the placed variable reached it.

Deliberately no attachment stub. The suite next door installs a
:class:`_FakeAttachment` so that its subject — prompting, rollback, verb coverage
— is not hostage to a binary being present. Here the subject IS the delivery, and
a stub would make every assertion below pass without a credential going anywhere.
"""

from __future__ import annotations

import json
import shlex
import sys
from collections.abc import Callable
from pathlib import Path

import pytest

from arccli.commands.connector import connector_handler

_EXTENSION = "acme_placed"
_INSTANCE = "work"

#: Distinctive enough that finding it in captured output is proof, not coincidence.
_SENTINEL = "zzz-cli-placed-sentinel-6620"

#: The variable this bundle's binary reads. The CLI never sees this name.
_VARIABLE = "ACME_PLACED_TOKEN"

#: Prints the JSON a CLI command must produce, and only with the credential present.
_PROBE_SCRIPT = (
    f"import os,sys; sys.stdout.write('{{}}') if os.environ.get({_VARIABLE!r}) else sys.exit(1)"
)

#: The second question, asked of the same binary: is this account connected?
_VERIFY = (
    f"{sys.executable} -c "
    f"{shlex.quote(f'import os,sys; sys.exit(0 if os.environ.get({_VARIABLE!r}) else 2)')}"
)


def _manifest(*, placed: bool = True) -> str:
    placement = f"\n[secrets.placement]\nvariable = {json.dumps(_VARIABLE)}\n" if placed else "\n"
    return f"""
[extension]
name = "{_EXTENSION}"
version = "1.0.0"
attachment = "cli"
description = "Acme through a binary that reads its token from the environment."

[[host_requires]]
name = {json.dumps(sys.executable)}
authorize_command = "acme login"
verify_command = {json.dumps(_VERIFY)}
instruction = "Install the acme CLI on this host, then paste a token into Arc."

[[secrets]]
name = "access_token"
prompt = "Acme access token, from the Acme console under Settings then API tokens."
{placement}
[tools]
allow = ["acme_ping"]

[[tools.declared]]
name = "acme_ping"
description = "Report which account this connection is authorised as."
classification = "read_only"

[approval]
default = "outbound"

[config.cli]
binary = {json.dumps(sys.executable)}
probe_argv = ["-c", {json.dumps(_PROBE_SCRIPT)}]
install_instruction = "See [[host_requires]]."

[[config.cli.commands]]
tool = "acme_ping"
argv = ["-c", {json.dumps(_PROBE_SCRIPT)}]
description = "Report which account this connection is authorised as."
classification = "read_only"
"""


@pytest.fixture
def arc_dir(tmp_path: Path) -> Path:
    """A minimal deployment root holding the placed bundle."""
    root = tmp_path / "arc"
    bundle = root / "extensions" / _EXTENSION
    bundle.mkdir(parents=True)
    (bundle / "extension.toml").write_text(_manifest(), encoding="utf-8")
    return root


@pytest.fixture
def run(arc_dir: Path, tmp_path: Path) -> Callable[..., None]:
    """Invoke the real handler with every path pinned inside this test's tmp dir."""

    def _run(*args: str) -> None:
        connector_handler([*args, "--arc-dir", str(arc_dir), "--data-dir", str(tmp_path / "data")])

    return _run


@pytest.fixture
def paste(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Answer the hidden prompt, and record what the operator was asked.

    ``getpass`` rather than ``input`` is the point: the value is never echoed to
    the terminal it was typed into.
    """
    asked: list[str] = []

    def _getpass(prompt: str = "") -> str:
        asked.append(prompt)
        return _SENTINEL

    monkeypatch.setattr("arccli.commands.connector.getpass.getpass", _getpass)
    return asked


def test_add_prompts_in_the_bundles_own_words_and_delivers_the_value(
    run: Callable[..., None], paste: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    """The install probes by running the binary, which fails without the credential.

    So this passing means the pasted value was stored, read back out of the store,
    put in a real child's environment, and answered from there. The prompt shown is
    the manifest's own sentence — the CLI supplies no wording of its own, which is
    what keeps a new connector's instructions inside its own bundle.
    """
    run("add", _EXTENSION, "--name", _INSTANCE)

    assert any("Acme console" in prompt for prompt in paste), paste
    captured = capsys.readouterr()
    assert "acme_ping" in captured.out
    assert _SENTINEL not in captured.out + captured.err


def test_doctor_reports_the_account_as_signed_in(
    run: Callable[..., None], paste: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    """The sign-in row runs the manifest's check WITH the placement, or it lies.

    ``verify_command`` here exits 2 without the variable — the same shape as a
    logged-out ``dbxcli account``. A ``sign-in signed_in`` row therefore cannot
    come from the probe wearing the badge's name.
    """
    run("add", _EXTENSION, "--name", _INSTANCE)
    capsys.readouterr()

    run("doctor", _INSTANCE)

    out = capsys.readouterr().out
    assert "sign-in" in out
    assert "signed_in" in out
    assert _SENTINEL not in out


def test_a_bundle_that_places_nothing_is_refused_by_name(
    arc_dir: Path,
    run: Callable[..., None],
    paste: list[str],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Fails closed at the terminal too, and says which field has nowhere to go."""
    (arc_dir / "extensions" / _EXTENSION / "extension.toml").write_text(
        _manifest(placed=False), encoding="utf-8"
    )

    with pytest.raises(SystemExit) as raised:
        run("add", _EXTENSION, "--name", _INSTANCE)

    assert raised.value.code != 0
    captured = capsys.readouterr()
    assert "access_token" in captured.out + captured.err
    assert _SENTINEL not in captured.out + captured.err
