"""``arc connector`` — the operator's complete connector management surface.

SPEC-062 T-914, serving REQ-260, REQ-261, and REQ-293. Eight verbs — ``add``,
``auth``, ``list``, ``tools``, ``probe``, ``doctor``, ``approve``, ``remove`` —
and the requirement is that they are COMPLETE: nothing an operator needs may
require the web interface (D-561), so ``test_every_declared_verb_is_reachable``
is a real assertion, not a formality.

Two properties get most of the attention here:

* **The secret is never echoed and never leaves the store.** ``add`` collects it
  through ``getpass``, and the test asserts the value is absent from stdout,
  stderr, and the written config — the constraint ``gateway_connect.py`` states
  in its module docstring, made checkable.
* **A failure names its step and leaves nothing behind.** The install path's own
  rollback is tested in ``arcagent``; what is tested here is that the CLI
  reports the failing step to the operator and exits non-zero rather than
  claiming success.
"""

from __future__ import annotations

import json
import shlex
import sys
import tomllib
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from arcagent.extension.attachment import ProbeResult, Requirement, ToolResult, ToolSpec

from arccli.commands.connector import _SUBCOMMAND_MAP, connector_handler

_EXTENSION = "acme_tickets"
_INSTANCE = "sales"
_TOKEN = "sup3r-s3cret-token"

_MANIFEST = """
[extension]
name = "acme_tickets"
version = "1.0.0"
attachment = "cli"

[[secrets]]
name = "api_token"
prompt = "Paste your Acme API token"

[tools]
allow = ["create_issue"]

[approval]
default = "outbound"

[config.cli]
binary = "acme"
"""

#: A bundle in the shape half the shipped ones actually have: a ``cli`` connector
#: with NO ``[[secrets]]``, because its binary keeps its own token in the host
#: keyring. ``arc connector auth`` used to answer these with "declares no
#: credentials; nothing to supply", which is true and leaves the operator stuck.
_HOSTED_EXTENSION = "acme_hosted"
_AUTHORIZE_COMMAND = "sh -c 'acme auth login --scopes read'"

_HOSTED_MANIFEST = f"""
[extension]
name = "{_HOSTED_EXTENSION}"
version = "1.0.0"
attachment = "cli"

[[host_requires]]
name = "sh"
authorize_command = "{_AUTHORIZE_COMMAND}"
instruction = "Authorise the acme CLI on this host."

[tools]
allow = ["create_issue"]

[approval]
default = "outbound"

[config.cli]
binary = "acme"
"""

_HOSTED_INSTANCE = "hosted"


def _hosted_manifest(*, host: str = "sh", token_login: str = "") -> str:
    """The hosted bundle, re-declared with or without a login Arc can finish."""
    token_clause = f"token_command = {json.dumps(token_login)}\n" if token_login else ""
    return f"""
[extension]
name = "{_HOSTED_EXTENSION}"
version = "1.0.0"

attachment = "cli"

[[host_requires]]
name = {json.dumps(host)}
authorize_command = {json.dumps(_AUTHORIZE_COMMAND)}
{token_clause}instruction = "Authorise the acme CLI on this host."

[tools]
allow = ["create_issue"]

[approval]
default = "outbound"

[config.cli]
binary = "acme"
"""


def _token_login_command(marker: Path) -> str:
    """A real non-interactive login: reads the token on stdin, then signs in.

    A real subprocess rather than a substitute, because what is under test is
    that the token reached stdin at all.
    """
    script = (
        "import sys, pathlib;"
        " token = sys.stdin.read().strip();"
        f" pathlib.Path({str(marker)!r}).write_text('ok') if token else None;"
        " sys.exit(0 if token else 1)"
    )
    return f"{sys.executable} -c {shlex.quote(script)}"


def _connect_hosted(
    run: Callable[..., None], agent_dir: Path, *, token_login: str = ""
) -> None:
    """Install the hosted bundle, first re-declaring how its binary signs in."""
    host = sys.executable if token_login else "sh"
    (agent_dir / "extensions" / _HOSTED_EXTENSION / "extension.toml").write_text(
        _hosted_manifest(host=host, token_login=token_login), encoding="utf-8"
    )
    run("add", _HOSTED_EXTENSION, "--instance", _HOSTED_INSTANCE)


_DECLARED_VERBS = (
    "add",
    "auth",
    "list",
    "tools",
    "probe",
    "doctor",
    "approve",
    "remove",
)


class _FakeAttachment:
    """A reachable attachment, so no test depends on a binary being installed."""

    def requirements(self) -> list[Requirement]:
        return []

    async def probe(self) -> ProbeResult:
        return ProbeResult(
            reachable=True,
            tools=[ToolSpec(name="create_issue", description="Open a ticket.")],
            detail="acme 1.0.0",
        )

    async def describe_tools(self) -> list[ToolSpec]:
        return [ToolSpec(name="create_issue", description="Open a ticket.")]

    async def invoke(self, tool: str, args: dict[str, Any]) -> ToolResult:
        return ToolResult(tool=tool)


class _UnreachableAttachment(_FakeAttachment):
    async def probe(self) -> ProbeResult:
        return ProbeResult(reachable=False, detail="acme: command not found")


@pytest.fixture
def agent_dir(tmp_path: Path) -> Path:
    """A minimal agent home with one resolvable extension bundle."""
    agent = tmp_path / "sales_agent"
    agent.mkdir()
    (agent / "arcagent.toml").write_text(
        "[agent]\n"
        'name = "sales_agent"\n\n'
        "[llm]\n"
        'model = "none"\n\n'
        "[identity]\n"
        'did = "did:arc:local:executor/7e3e"\n\n'
        "[security]\n"
        'tier = "personal"\n',
        encoding="utf-8",
    )
    bundle = agent / "extensions" / _EXTENSION
    bundle.mkdir(parents=True)
    (bundle / "extension.toml").write_text(_MANIFEST, encoding="utf-8")
    hosted = agent / "extensions" / _HOSTED_EXTENSION
    hosted.mkdir(parents=True)
    (hosted / "extension.toml").write_text(_HOSTED_MANIFEST, encoding="utf-8")
    return agent


@pytest.fixture(autouse=True)
def _reachable(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default every test to a reachable attachment; a test may override it."""
    monkeypatch.setattr(
        "arccli.commands.connector._attachment_factory",
        lambda: lambda _manifest, _bundle, _secrets: _FakeAttachment(),
    )


@pytest.fixture
def run(agent_dir: Path, tmp_path: Path) -> Callable[..., None]:
    """Invoke the handler with every path pinned inside the test's own tmp dir.

    ``--arc-dir`` and ``--data-dir`` are real operator flags, not test scaffolding:
    without them the operator key and the connection store would be bootstrapped in
    the developer's own home the first time this suite ran.
    """

    def _run(*args: str) -> None:
        connector_handler(
            [
                *args,
                "--agent",
                str(agent_dir),
                "--arc-dir",
                str(tmp_path / "arc"),
                "--data-dir",
                str(tmp_path / "data"),
            ]
        )

    return _run


def _answer_prompts(monkeypatch: pytest.MonkeyPatch, value: str = _TOKEN) -> list[str]:
    """Capture what getpass was asked, and answer it. Nothing is ever echoed."""
    asked: list[str] = []

    def _getpass(prompt: str = "") -> str:
        asked.append(prompt)
        return value

    monkeypatch.setattr("arccli.commands.connector.getpass.getpass", _getpass)
    return asked


def _config(agent_dir: Path) -> dict[str, Any]:
    return tomllib.loads((agent_dir / "arcagent.toml").read_text(encoding="utf-8"))


class TestSurfaceCompleteness:
    """REQ-293 — every management action exists at the command line."""

    def test_every_declared_verb_is_reachable(self) -> None:
        assert set(_DECLARED_VERBS) <= set(_SUBCOMMAND_MAP)

    def test_the_command_is_registered(self) -> None:
        from arccli.commands.registry import COMMAND_REGISTRY

        assert any(command.name == "connector" for command in COMMAND_REGISTRY)


class TestAdd:
    """REQ-260 — prompt hidden, probe, then persist. In that order."""

    def test_prompts_for_each_declared_secret_without_echoing_it(
        self,
        run: Callable[..., None],
        agent_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        asked = _answer_prompts(monkeypatch)
        run("add", _EXTENSION, "--instance", _INSTANCE)

        assert len(asked) == 1
        assert "Acme API token" in asked[0]
        captured = capsys.readouterr()
        assert _TOKEN not in captured.out
        assert _TOKEN not in captured.err

    def test_persists_the_instance_block_but_never_the_secret(
        self, run: Callable[..., None], agent_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _answer_prompts(monkeypatch)
        run("add", _EXTENSION, "--instance", _INSTANCE)

        block = _config(agent_dir)["extensions"][_INSTANCE]
        assert block["extension"] == _EXTENSION
        assert block["approval"] == "outbound"
        assert _TOKEN not in (agent_dir / "arcagent.toml").read_text(encoding="utf-8")

    def test_reports_the_tools_the_probe_found(
        self,
        run: Callable[..., None],
        agent_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        _answer_prompts(monkeypatch)
        run("add", _EXTENSION, "--instance", _INSTANCE)
        assert "create_issue" in capsys.readouterr().out

    def test_the_install_is_audited_without_recording_the_credential(
        self,
        run: Callable[..., None],
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Two things at once, and both matter: the audit chain is really wired
        # (a NullSink would leave no file at all), and the credential write is
        # recorded by its coordinates and never by its value (REQ-277).
        from arcstore.ingest import WORM_ACTIVE_FILENAME

        _answer_prompts(monkeypatch)
        run("add", _EXTENSION, "--instance", _INSTANCE)

        chain = tmp_path / "data" / "worm" / WORM_ACTIVE_FILENAME
        assert chain.exists(), "connector verdicts must reach the operator-signed chain"
        written = chain.read_text(encoding="utf-8")
        assert "secret.write" in written
        assert _TOKEN not in written

    def test_a_failed_probe_names_the_step_and_persists_nothing(
        self,
        run: Callable[..., None],
        agent_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        monkeypatch.setattr(
            "arccli.commands.connector._attachment_factory",
            lambda: lambda _manifest, _bundle, _secrets: _UnreachableAttachment(),
        )
        _answer_prompts(monkeypatch)

        with pytest.raises(SystemExit) as exited:
            run("add", _EXTENSION, "--instance", _INSTANCE)

        assert exited.value.code == 1
        assert "probe" in capsys.readouterr().err
        assert "extensions" not in _config(agent_dir)

    def test_an_unknown_extension_names_the_resolve_step(
        self,
        run: Callable[..., None],
        agent_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        _answer_prompts(monkeypatch)
        with pytest.raises(SystemExit):
            run("add", "no_such_bundle", "--instance", _INSTANCE)
        assert "resolve" in capsys.readouterr().err

    def test_a_secret_given_on_the_command_line_is_refused(
        self, run: Callable[..., None], agent_dir: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # There is deliberately no --token flag: a credential on argv lands in the
        # shell history and in the process table, which is exactly what the hidden
        # prompt exists to avoid.
        with pytest.raises(SystemExit):
            run("add", _EXTENSION, "--instance", _INSTANCE, "--api-token", _TOKEN)
        assert _TOKEN not in capsys.readouterr().out


class TestReadVerbs:
    """The verbs an operator uses to see what is connected and whether it works."""

    def test_list_shows_an_installed_instance(
        self,
        run: Callable[..., None],
        agent_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        _answer_prompts(monkeypatch)
        run("add", _EXTENSION, "--instance", _INSTANCE)
        capsys.readouterr()

        run("list")
        out = capsys.readouterr().out
        assert _INSTANCE in out
        assert _EXTENSION in out

    def test_list_on_a_fresh_agent_says_so_without_failing(
        self, run: Callable[..., None], agent_dir: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        run("list")
        assert "No connector" in capsys.readouterr().out

    def test_tools_lists_the_verbs_the_instance_offers(
        self,
        run: Callable[..., None],
        agent_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        _answer_prompts(monkeypatch)
        run("add", _EXTENSION, "--instance", _INSTANCE)
        capsys.readouterr()

        run("tools", _INSTANCE)
        assert "create_issue" in capsys.readouterr().out

    def test_probe_reports_a_live_connection(
        self,
        run: Callable[..., None],
        agent_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        _answer_prompts(monkeypatch)
        run("add", _EXTENSION, "--instance", _INSTANCE)
        capsys.readouterr()

        run("probe", _INSTANCE)
        assert "acme 1.0.0" in capsys.readouterr().out

    def test_probe_exits_non_zero_when_unreachable(
        self, run: Callable[..., None], agent_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _answer_prompts(monkeypatch)
        run("add", _EXTENSION, "--instance", _INSTANCE)
        monkeypatch.setattr(
            "arccli.commands.connector._attachment_factory",
            lambda: lambda _manifest, _bundle, _secrets: _UnreachableAttachment(),
        )
        with pytest.raises(SystemExit) as exited:
            run("probe", _INSTANCE)
        assert exited.value.code == 1

    def test_doctor_reports_credential_presence_without_the_value(
        self,
        run: Callable[..., None],
        agent_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        _answer_prompts(monkeypatch)
        run("add", _EXTENSION, "--instance", _INSTANCE)
        capsys.readouterr()

        run("doctor", _INSTANCE)
        out = capsys.readouterr().out
        assert "api_token" in out
        assert _TOKEN not in out

    def test_doctor_names_a_missing_credential(
        self,
        run: Callable[..., None],
        agent_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        _answer_prompts(monkeypatch)
        run("add", _EXTENSION, "--instance", _INSTANCE)
        capsys.readouterr()
        # The credential is gone but the connection is still configured — the
        # shape an operator hits after a store is rotated or restored without it.
        (agent_dir / "connectors.env").unlink()

        run("doctor", _INSTANCE)
        assert "missing" in capsys.readouterr().out.lower()


class TestAuthAndApprove:
    """Re-supplying a credential, and re-approving a changed tool contract."""

    def test_auth_replaces_the_credential_through_a_hidden_prompt(
        self,
        run: Callable[..., None],
        agent_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        _answer_prompts(monkeypatch)
        run("add", _EXTENSION, "--instance", _INSTANCE)
        capsys.readouterr()

        asked = _answer_prompts(monkeypatch, "rotated-value")
        run("auth", _INSTANCE)

        assert asked, "auth must prompt, not read a flag"
        out = capsys.readouterr().out
        assert "rotated-value" not in out
        env = (agent_dir / "connectors.env").read_text(encoding="utf-8")
        assert "rotated-value" in env
        assert _TOKEN not in env

    def test_auth_on_a_credential_less_connector_names_the_host_command(
        self,
        run: Callable[..., None],
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """The operator must finish this command knowing exactly what to type next.

        Four of the eight shipped bundles declare no ``[[secrets]]`` — ``gh``,
        ``gog``, ``dbxcli`` and ``readwise`` each keep their own token — and the
        old answer, "declares no credentials; nothing to supply", left a
        non-technical operator with a connector that could not be authorised at
        all. The manifest's ``authorize_command`` is the whole answer, so it has
        to reach stdout verbatim.
        """
        run("add", _HOSTED_EXTENSION, "--instance", "hosted")
        capsys.readouterr()

        run("auth", "hosted")

        out = capsys.readouterr().out
        assert _AUTHORIZE_COMMAND in out, (
            "a credential-less connector's auth verb told the operator nothing to run"
        )
        assert "nothing to supply" not in out

    def test_approve_records_the_current_tool_contract(
        self,
        run: Callable[..., None],
        agent_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        _answer_prompts(monkeypatch)
        run("add", _EXTENSION, "--instance", _INSTANCE)
        capsys.readouterr()

        run("approve", _INSTANCE)
        assert "create_issue" in capsys.readouterr().out


class TestRemove:
    """Nothing is left behind — the same standard as a failed install."""

    def test_remove_drops_the_block_and_the_credential(
        self,
        run: Callable[..., None],
        agent_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        _answer_prompts(monkeypatch)
        run("add", _EXTENSION, "--instance", _INSTANCE)
        capsys.readouterr()

        run("remove", _INSTANCE)

        assert "extensions" not in _config(agent_dir)
        assert _TOKEN not in (agent_dir / "connectors.env").read_text(encoding="utf-8")

    def test_removing_an_unknown_instance_is_reported_not_crashed(
        self, run: Callable[..., None], agent_dir: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        run("remove", "never_installed")
        assert "never_installed" in capsys.readouterr().out


class TestAuthorizeAndHostSetup:
    """REQ-293 — the two host-facing actions exist at the terminal too.

    The requirement is "through arccli OR arcui", so a button the web has and the
    terminal does not is a surface an operator on a headless box cannot reach.
    Both verbs keep the same honesty the web keeps: a login only a person can
    finish is printed, never attempted.
    """

    def test_both_verbs_are_reachable(self) -> None:
        assert {"authorize", "host-setup"} <= set(_SUBCOMMAND_MAP)

    def test_authorize_prints_the_command_for_an_interactive_only_connector(
        self,
        run: Callable[..., None],
        agent_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """No ``token_command`` means no button and no prompt — just the command."""
        _connect_hosted(run, agent_dir)
        prompted = _answer_prompts(monkeypatch, "unused")

        run("authorize", _HOSTED_INSTANCE)

        out = capsys.readouterr().out
        assert _AUTHORIZE_COMMAND in out
        assert prompted == [], "an interactive-only login must not ask for a token"

    def test_authorize_prompts_for_the_token_when_arc_can_finish_the_login(
        self,
        run: Callable[..., None],
        agent_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """Hidden prompt, never a ``--token`` flag: argv lands in shell history
        and in the process table, which is the exposure getpass exists to remove."""
        marker = agent_dir / "signed-in"
        _connect_hosted(run, agent_dir, token_login=_token_login_command(marker))
        _answer_prompts(monkeypatch, _TOKEN)

        run("authorize", _HOSTED_INSTANCE)

        assert marker.exists(), "the token never reached the binary"
        assert _TOKEN not in capsys.readouterr().out

    def test_host_setup_reports_a_refusal_and_the_manual_steps(
        self,
        run: Callable[..., None],
        agent_dir: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """A bundle pinning no build cannot be installed, and says so with the
        steps a person runs instead rather than a bare failure."""
        (agent_dir / "extensions" / _HOSTED_EXTENSION / "extension.toml").write_text(
            _hosted_manifest(host="definitely_not_installed_xyz"), encoding="utf-8"
        )

        with pytest.raises(SystemExit):
            run("host-setup", _HOSTED_EXTENSION)

        out = capsys.readouterr().out
        assert "pins no downloadable build" in out
        assert "Authorise the acme CLI on this host." in out

    def test_host_setup_says_so_when_the_host_already_has_what_it_needs(
        self,
        run: Callable[..., None],
        agent_dir: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        run("host-setup", _HOSTED_EXTENSION)

        assert "has what it needs here" in capsys.readouterr().out
