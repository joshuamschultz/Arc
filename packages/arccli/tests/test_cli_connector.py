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
    return agent


@pytest.fixture(autouse=True)
def _reachable(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default every test to a reachable attachment; a test may override it."""
    monkeypatch.setattr(
        "arccli.commands.connector._attachment_factory",
        lambda: lambda _manifest, _bundle: _FakeAttachment(),
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
            lambda: lambda _manifest, _bundle: _UnreachableAttachment(),
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
            lambda: lambda _manifest, _bundle: _UnreachableAttachment(),
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
