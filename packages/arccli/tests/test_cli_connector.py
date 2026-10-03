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

import asyncio
import json
import shlex
import sys
import tomllib
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from arcagent.extension.attachment import (
    ProbeResult,
    Requirement,
    ToolOutcome,
    ToolResult,
    ToolSpec,
)
from arctrust.paths import config_file

from arccli.commands.connector import _SUBCOMMAND_MAP, connector_handler

_EXTENSION = "acme_tickets"
_INSTANCE = "sales"

#: The agent a grant names — its DIRECTORY name under ``<arc-dir>/team``.
_AGENT = "sales_agent"
_TOKEN = "sup3r-s3cret-token"

_MANIFEST = """
[extension]
name = "acme_tickets"
version = "1.0.0"
attachment = "cli"

[[secrets]]
name = "api_token"
prompt = "Paste your Acme API token"

[health]
probe = "tool:acme_whoami"

[tools]
allow = ["create_issue", "acme_whoami"]

[[tools.declared]]
name = "acme_whoami"
description = "Say who is signed in."
classification = "read_only"

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


def _connect_hosted(run: Callable[..., None], arc_dir: Path, *, token_login: str = "") -> None:
    """Connect the hosted bundle, first re-declaring how its binary signs in."""
    host = sys.executable if token_login else "sh"
    (arc_dir / "extensions" / _HOSTED_EXTENSION / "extension.toml").write_text(
        _hosted_manifest(host=host, token_login=token_login), encoding="utf-8"
    )
    run("add", _HOSTED_EXTENSION, "--name", _HOSTED_INSTANCE)


_DECLARED_VERBS = (
    "add",
    "grant",
    "revoke",
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

    async def invoke(self, tool: str, args: dict[str, Any]) -> ToolResult:
        return ToolResult(tool=tool, outcome=ToolOutcome.ERROR, content="acme: command not found")


@pytest.fixture
def arc_dir(tmp_path: Path) -> Path:
    """A deployment root with one agent and two resolvable extension bundles."""
    root = tmp_path / "arc"
    for name in (_AGENT, "marketer"):
        agent = root / "team" / name
        agent.mkdir(parents=True)
        (agent / "arcagent.toml").write_text(
            "[agent]\n"
            f'name = "{name}"\n\n'
            "[llm]\n"
            'model = "none"\n\n'
            "[identity]\n"
            'did = "did:arc:local:executor/7e3e"\n\n'
            "[security]\n"
            'tier = "personal"\n',
            encoding="utf-8",
        )
    bundle = root / "extensions" / _EXTENSION
    bundle.mkdir(parents=True)
    (bundle / "extension.toml").write_text(_MANIFEST, encoding="utf-8")
    hosted = root / "extensions" / _HOSTED_EXTENSION
    hosted.mkdir(parents=True)
    (hosted / "extension.toml").write_text(_HOSTED_MANIFEST, encoding="utf-8")
    return root


@pytest.fixture(autouse=True)
def _reachable(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default every test to a reachable attachment; a test may override it."""
    monkeypatch.setattr(
        "arccli.commands.connector._attachment_factory",
        lambda: lambda _manifest, _bundle, _secrets, credential=None: _FakeAttachment(),
    )


@pytest.fixture(autouse=True)
def _arcstore_backend(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Keep every command in one test on one fresh ArcStore fake."""
    from arcstore.backends.memory import FakeBackend

    backend = FakeBackend()

    async def _open() -> FakeBackend:
        return backend

    monkeypatch.setattr("arccli.commands.connector._arcstore_opener", lambda: _open)
    return backend


def _custody(backend: Any, arc_dir: Path, connection: str) -> tuple[Any, Any]:
    """The sealed custody row for a connection, and the store that opens it."""
    import asyncio

    from arcagent.core.tier import Tier
    from arcagent.extension.custody import CredentialRowStore
    from arcagent.extension.custody_select import deployment_cipher

    store = CredentialRowStore(backend, deployment_cipher(arc_dir, tier=Tier.PERSONAL))
    return asyncio.run(store.read(connection)), store


def _raw_rows(backend: Any) -> str:
    """Every custody row exactly as stored, as one string."""
    import asyncio

    from arcagent.extension.custody import CREDENTIAL_COLLECTION

    rows = asyncio.run(backend.mutable_query(CREDENTIAL_COLLECTION))
    return json.dumps(rows, default=str)


@pytest.fixture
def run(arc_dir: Path, tmp_path: Path) -> Callable[..., None]:
    """Invoke the handler with every path pinned inside the test's own tmp dir.

    ``--arc-dir`` and ``--data-dir`` are real operator flags, not test scaffolding:
    without them the operator key and the connection store would be bootstrapped in
    the developer's own home the first time this suite ran.
    """

    def _run(*args: str) -> None:
        connector_handler([*args, "--arc-dir", str(arc_dir), "--data-dir", str(tmp_path / "data")])

    return _run


def _answer_prompts(monkeypatch: pytest.MonkeyPatch, value: str = _TOKEN) -> list[str]:
    """Capture what getpass was asked, and answer it. Nothing is ever echoed."""
    asked: list[str] = []

    def _getpass(prompt: str = "") -> str:
        asked.append(prompt)
        return value

    monkeypatch.setattr("arccli.commands.connector.getpass.getpass", _getpass)
    return asked


def _connections(arc_dir: Path) -> dict[str, Any]:
    """The deployment's connections, as they are actually written to disk."""
    path = config_file("connections.toml", arc_dir)
    if not path.is_file():
        return {}
    parsed = tomllib.loads(path.read_text(encoding="utf-8")).get("connections", {})
    assert isinstance(parsed, dict)
    return parsed


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
        arc_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        asked = _answer_prompts(monkeypatch)
        run("add", _EXTENSION, "--name", _INSTANCE, "--agents", _AGENT)

        assert len(asked) == 1
        assert "Acme API token" in asked[0]
        captured = capsys.readouterr()
        assert _TOKEN not in captured.out
        assert _TOKEN not in captured.err

    def test_persists_the_instance_block_but_never_the_secret(
        self, run: Callable[..., None], arc_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _answer_prompts(monkeypatch)
        run("add", _EXTENSION, "--name", _INSTANCE, "--agents", _AGENT)

        block = _connections(arc_dir)[_INSTANCE]
        assert block["extension"] == _EXTENSION
        assert block["approval"] == "outbound"
        assert _TOKEN not in config_file("connections.toml", arc_dir).read_text(encoding="utf-8")

    def test_reports_the_tools_the_probe_found(
        self,
        run: Callable[..., None],
        arc_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        _answer_prompts(monkeypatch)
        run("add", _EXTENSION, "--name", _INSTANCE, "--agents", _AGENT)
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
        run("add", _EXTENSION, "--name", _INSTANCE, "--agents", _AGENT)

        chain = tmp_path / "data" / "worm" / WORM_ACTIVE_FILENAME
        assert chain.exists(), "connector verdicts must reach the operator-signed chain"
        written = chain.read_text(encoding="utf-8")
        assert "secret.write" in written
        assert _TOKEN not in written

    def test_a_failed_probe_names_the_step_and_persists_nothing(
        self,
        run: Callable[..., None],
        arc_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        monkeypatch.setattr(
            "arccli.commands.connector._attachment_factory",
            lambda: lambda _manifest, _bundle, _secrets, credential=None: _UnreachableAttachment(),
        )
        _answer_prompts(monkeypatch)

        with pytest.raises(SystemExit) as exited:
            run("add", _EXTENSION, "--name", _INSTANCE, "--agents", _AGENT)

        assert exited.value.code == 1
        assert "probe" in capsys.readouterr().err
        assert _connections(arc_dir) == {}

    def test_an_unknown_extension_names_the_resolve_step(
        self,
        run: Callable[..., None],
        arc_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        _answer_prompts(monkeypatch)
        with pytest.raises(SystemExit):
            run("add", "no_such_bundle", "--name", _INSTANCE)
        assert "resolve" in capsys.readouterr().err

    def test_an_instance_name_that_is_not_a_legal_config_key_is_refused(
        self,
        run: Callable[..., None],
        arc_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """Every surface refuses it, and the terminal says what to type instead.

        The bundle is the credential-less one on purpose: that is the shape whose
        name nothing used to check.
        """
        _answer_prompts(monkeypatch)

        with pytest.raises(SystemExit) as exited:
            run("add", _HOSTED_EXTENSION, "--name", "blackarc industrial email")

        assert exited.value.code == 1
        assert "blackarc_industrial_email" in capsys.readouterr().err
        assert _connections(arc_dir) == {}

    def test_a_secret_given_on_the_command_line_is_refused(
        self, run: Callable[..., None], arc_dir: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # There is deliberately no --token flag: a credential on argv lands in the
        # shell history and in the process table, which is exactly what the hidden
        # prompt exists to avoid.
        with pytest.raises(SystemExit):
            run("add", _EXTENSION, "--name", _INSTANCE, "--api-token", _TOKEN)
        assert _TOKEN not in capsys.readouterr().out

    def test_add_refuses_an_agent_missing_from_the_deployment_roster(
        self,
        run: Callable[..., None],
        arc_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        asked = _answer_prompts(monkeypatch)

        with pytest.raises(SystemExit) as exited:
            run("add", _EXTENSION, "--name", _INSTANCE, "--agents", "missing_agent")

        assert exited.value.code == 1
        assert asked == []
        assert _connections(arc_dir) == {}
        assert "no agent named missing_agent" in capsys.readouterr().err


class TestReadVerbs:
    """The verbs an operator uses to see what is connected and whether it works."""

    def test_list_shows_an_installed_instance(
        self,
        run: Callable[..., None],
        arc_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        _answer_prompts(monkeypatch)
        run("add", _EXTENSION, "--name", _INSTANCE, "--agents", _AGENT)
        capsys.readouterr()

        run("list")
        out = capsys.readouterr().out
        assert _INSTANCE in out
        assert _EXTENSION in out

    def test_list_on_a_fresh_deployment_says_so_without_failing(
        self, run: Callable[..., None], arc_dir: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        run("list")
        assert "No connections" in capsys.readouterr().out

    def test_tools_lists_the_verbs_the_instance_offers(
        self,
        run: Callable[..., None],
        arc_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        _answer_prompts(monkeypatch)
        run("add", _EXTENSION, "--name", _INSTANCE, "--agents", _AGENT)
        capsys.readouterr()

        run("tools", _INSTANCE)
        assert "create_issue" in capsys.readouterr().out

    def test_probe_reports_a_live_connection(
        self,
        run: Callable[..., None],
        arc_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        _answer_prompts(monkeypatch)
        run("add", _EXTENSION, "--name", _INSTANCE, "--agents", _AGENT)
        capsys.readouterr()

        run("probe", _INSTANCE)
        out = capsys.readouterr().out
        assert "'sales' is healthy" in out
        assert "create_issue" in out

    def test_probe_exits_non_zero_when_unreachable(
        self, run: Callable[..., None], arc_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _answer_prompts(monkeypatch)
        run("add", _EXTENSION, "--name", _INSTANCE, "--agents", _AGENT)
        monkeypatch.setattr(
            "arccli.commands.connector._attachment_factory",
            lambda: lambda _manifest, _bundle, _secrets, credential=None: _UnreachableAttachment(),
        )
        with pytest.raises(SystemExit) as exited:
            run("probe", _INSTANCE)
        assert exited.value.code == 1

    def test_doctor_reports_credential_presence_without_the_value(
        self,
        run: Callable[..., None],
        arc_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        _answer_prompts(monkeypatch)
        run("add", _EXTENSION, "--name", _INSTANCE, "--agents", _AGENT)
        capsys.readouterr()

        run("doctor", _INSTANCE)
        out = capsys.readouterr().out
        assert "api_token" in out
        assert _TOKEN not in out

    def test_doctor_names_a_missing_credential(
        self,
        _arcstore_backend: Any,
        run: Callable[..., None],
        arc_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        _answer_prompts(monkeypatch)
        run("add", _EXTENSION, "--name", _INSTANCE, "--agents", _AGENT)
        capsys.readouterr()
        # The credential is gone but the connection is still configured — the
        # shape an operator hits after a store is rotated or restored without it.
        import asyncio

        from arcagent.extension.custody import CREDENTIAL_COLLECTION

        asyncio.run(
            _arcstore_backend.mutable_delete(
                CREDENTIAL_COLLECTION, _INSTANCE, actor_did="did:arc:test:operator"
            )
        )

        run("doctor", _INSTANCE)
        assert "missing" in capsys.readouterr().out.lower()


class TestAuthAndApprove:
    """Re-supplying a credential, and re-approving a changed tool contract."""

    def test_auth_replaces_the_credential_through_a_hidden_prompt(
        self,
        _arcstore_backend: Any,
        run: Callable[..., None],
        arc_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        _answer_prompts(monkeypatch)
        run("add", _EXTENSION, "--name", _INSTANCE, "--agents", _AGENT)
        capsys.readouterr()

        asked = _answer_prompts(monkeypatch, "rotated-value")
        run("auth", _INSTANCE)

        assert asked, "auth must prompt, not read a flag"
        out = capsys.readouterr().out
        assert "rotated-value" not in out
        row, store = _custody(_arcstore_backend, arc_dir, _INSTANCE)
        opened = asyncio.run(store.open_field(row, "api_token"))
        assert opened is not None
        assert opened.reveal() == "rotated-value"
        raw = _raw_rows(_arcstore_backend)
        assert "rotated-value" not in raw
        assert _TOKEN not in raw

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
        run("add", _HOSTED_EXTENSION, "--name", "hosted")
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
        arc_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        _answer_prompts(monkeypatch)
        run("add", _EXTENSION, "--name", _INSTANCE, "--agents", _AGENT)
        capsys.readouterr()

        run("approve", _INSTANCE)
        assert "create_issue" in capsys.readouterr().out


class TestRemove:
    """Nothing is left behind — the same standard as a failed install."""

    def test_remove_drops_the_block_and_the_credential(
        self,
        _arcstore_backend: Any,
        run: Callable[..., None],
        arc_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        _answer_prompts(monkeypatch)
        run("add", _EXTENSION, "--name", _INSTANCE, "--agents", _AGENT)
        capsys.readouterr()

        run("remove", _INSTANCE)

        assert _connections(arc_dir) == {}
        row, _store = _custody(_arcstore_backend, arc_dir, _INSTANCE)
        assert row is None
        assert _TOKEN not in _raw_rows(_arcstore_backend)

    def test_removing_an_unknown_instance_is_reported_not_crashed(
        self, run: Callable[..., None], arc_dir: Path, capsys: pytest.CaptureFixture[str]
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
        arc_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """No ``token_command`` means no button and no prompt — just the command."""
        _connect_hosted(run, arc_dir)
        prompted = _answer_prompts(monkeypatch, "unused")

        run("authorize", _HOSTED_INSTANCE)

        out = capsys.readouterr().out
        assert _AUTHORIZE_COMMAND in out
        assert prompted == [], "an interactive-only login must not ask for a token"

    def test_authorize_prompts_for_the_token_when_arc_can_finish_the_login(
        self,
        run: Callable[..., None],
        arc_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """Hidden prompt, never a ``--token`` flag: argv lands in shell history
        and in the process table, which is the exposure getpass exists to remove."""
        marker = arc_dir / "signed-in"
        _connect_hosted(run, arc_dir, token_login=_token_login_command(marker))
        _answer_prompts(monkeypatch, _TOKEN)

        run("authorize", _HOSTED_INSTANCE)

        assert marker.exists(), "the token never reached the binary"
        assert _TOKEN not in capsys.readouterr().out

    def test_host_setup_reports_a_refusal_and_the_manual_steps(
        self,
        run: Callable[..., None],
        arc_dir: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """A bundle pinning no build cannot be installed, and says so with the
        steps a person runs instead rather than a bare failure."""
        (arc_dir / "extensions" / _HOSTED_EXTENSION / "extension.toml").write_text(
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
        arc_dir: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        run("host-setup", _HOSTED_EXTENSION)

        assert "has what it needs here" in capsys.readouterr().out


class TestGrantAndRevoke:
    """Access control at the terminal: who holds this account, and who stops holding it.

    Driven through the real handler and read back off the real deployment file, not
    from a return value: the whole point of the verb is what the running agent will
    later read, and a grant that prints and does not persist is the shape this
    feature has already shipped.
    """

    def test_add_grants_in_the_same_pass(
        self, run: Callable[..., None], arc_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """One command connects the account AND hands it over — no separate step."""
        _answer_prompts(monkeypatch)

        run("add", _EXTENSION, "--name", _INSTANCE, "--agents", _AGENT)

        assert _connections(arc_dir)[_INSTANCE]["agents"] == [_AGENT]

    def test_add_without_agents_connects_an_account_nobody_holds(
        self, run: Callable[..., None], arc_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Deny by default: an account proven to work still reaches nobody untold."""
        _answer_prompts(monkeypatch)

        run("add", _EXTENSION, "--name", _INSTANCE)

        assert _connections(arc_dir)[_INSTANCE]["agents"] == []

    def test_grant_adds_an_agent_and_keeps_the_ones_already_holding_it(
        self, run: Callable[..., None], arc_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _answer_prompts(monkeypatch)
        run("add", _EXTENSION, "--name", _INSTANCE, "--agents", _AGENT)

        run("grant", _INSTANCE, "--agents", "marketer")

        assert _connections(arc_dir)[_INSTANCE]["agents"] == [_AGENT, "marketer"]

    def test_grant_refuses_an_agent_missing_from_the_deployment_roster(
        self,
        run: Callable[..., None],
        arc_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        _answer_prompts(monkeypatch)
        run("add", _EXTENSION, "--name", _INSTANCE, "--agents", _AGENT)

        with pytest.raises(SystemExit) as exited:
            run("grant", _INSTANCE, "--agents", "missing_agent")

        assert exited.value.code == 1
        assert _connections(arc_dir)[_INSTANCE]["agents"] == [_AGENT]
        assert "no agent named missing_agent" in capsys.readouterr().err

    def test_revoke_removes_one_and_leaves_the_rest(
        self, run: Callable[..., None], arc_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _answer_prompts(monkeypatch)
        run("add", _EXTENSION, "--name", _INSTANCE, "--agents", f"{_AGENT},marketer")

        run("revoke", _INSTANCE, "--agents", "marketer")

        assert _connections(arc_dir)[_INSTANCE]["agents"] == [_AGENT]

    def test_granting_a_connection_that_does_not_exist_exits_non_zero(
        self, run: Callable[..., None], capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A clear refusal, not a silent no-op that writes a row nobody reads."""
        with pytest.raises(SystemExit) as exited:
            run("grant", "no_such_account", "--agents", _AGENT)

        assert exited.value.code == 1
        assert "no_such_account" in capsys.readouterr().err

    def test_list_shows_who_holds_each_connection(
        self,
        run: Callable[..., None],
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """ "Who can read my mail" answered in one table, for the whole deployment."""
        _answer_prompts(monkeypatch)
        run("add", _EXTENSION, "--name", _INSTANCE, "--agents", _AGENT)
        capsys.readouterr()

        run("list")

        out = capsys.readouterr().out
        assert _INSTANCE in out
        assert _AGENT in out

    def test_list_says_plainly_when_a_connection_reaches_nobody(
        self,
        run: Callable[..., None],
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """An empty column is something an operator has to interpret. This is not."""
        _answer_prompts(monkeypatch)
        run("add", _EXTENSION, "--name", _INSTANCE)
        capsys.readouterr()

        run("list")

        assert "(nobody)" in capsys.readouterr().out


# --- native OAuth authorize flow (SPEC-062 connect) ---------------------------


def _oauth_auth(instance: str) -> Any:
    import arcagent

    return arcagent.Authorization(
        instance=instance,
        extension="dropbox",
        credentials=(),
        hosts=(),
        reachable=False,
        detail="unauthenticated",
        oauth=True,
    )


class _OAuthConnections:
    """Stands in for ``Connections``: records what the terminal flow hands it."""

    def __init__(self, *, redirect_mode: str = "callback") -> None:
        self.redirect_mode = redirect_mode
        self.began: list[str] = []
        self.completed: dict[str, str] = {}

    async def authorization(self, instance: str) -> Any:
        return _oauth_auth(instance)

    async def begin_oauth(self, instance: str, *, session_id: str) -> Any:
        from types import SimpleNamespace

        self.began.append(session_id)
        return SimpleNamespace(
            authorize_url="https://www.dropbox.com/oauth2/authorize?client_id=ak-123&state=s1",
            state="s1",
            redirect_mode=self.redirect_mode,
        )

    async def complete_oauth(self, *, session_id: str, **found: str) -> Any:
        from types import SimpleNamespace

        assert session_id == self.began[0], "begin and complete share one session"
        self.completed = found
        return SimpleNamespace(status="healthy", reason_text=None)


def _run_authorize(monkeypatch: pytest.MonkeyPatch, connections: Any, pasted: str) -> None:
    import argparse

    from arccli.commands.connector import _authorize

    monkeypatch.setattr("arccli.commands.connector._connections", lambda _args: connections)
    monkeypatch.setattr("arccli.commands.connector.getpass.getpass", lambda _prompt="": pasted)
    _authorize(argparse.Namespace(instance="personal_dropbox"))


def test_authorize_oauth_shows_the_link_takes_the_landed_address_and_completes(
    monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    """An OAuth connector prints its link, reads the pasted address, completes — no token field."""
    connections = _OAuthConnections()
    landed = "http://127.0.0.1:8420/oauth/callback?code=c1&state=s1"

    _run_authorize(monkeypatch, connections, f"  {landed}  ")

    assert connections.completed == {"redirect_url": landed}, "the pasted address is trimmed"
    out = capsys.readouterr().out
    assert "dropbox.com/oauth2/authorize" in out, "the operator is shown the consent link"
    assert "Connected" in out
    assert landed not in out, "a pasted code is never echoed back"


def test_authorize_oauth_for_a_code_showing_provider_sends_state_and_code(
    monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    connections = _OAuthConnections(redirect_mode="none")

    _run_authorize(monkeypatch, connections, "the-one-time-code")

    assert connections.completed == {"state": "s1", "code": "the-one-time-code"}
    assert "copy the code" in capsys.readouterr().out


def test_authorize_oauth_with_nothing_pasted_completes_nothing(
    monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    connections = _OAuthConnections()

    with pytest.raises(SystemExit):
        _run_authorize(monkeypatch, connections, "   ")

    assert connections.completed == {}
    assert "nothing pasted" in capsys.readouterr().err


def test_authorize_oauth_without_the_provider_app_says_set_it_up_first(
    monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    """No app slot yet means no link to open: the refusal names what to register."""
    import arcagent

    class _NoApp(_OAuthConnections):
        async def begin_oauth(self, instance: str, *, session_id: str) -> Any:
            raise arcagent.ExtensionError(
                code="OAUTH_APP_MISSING",
                message="set up the dropbox sign-in app first (arc connector oauth-app dropbox)",
            )

    with pytest.raises(SystemExit):
        _run_authorize(monkeypatch, _NoApp(), "unused")

    assert "oauth-app dropbox" in capsys.readouterr().err


def test_oauth_app_takes_tenant_cloud_and_a_piped_secret_never_from_argv(
    monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    """A deploy pipes a freshly minted secret in; tenant and cloud come as flags."""
    import argparse
    import io

    from arccli.commands.connector import _oauth_app

    class _Apps:
        oauth_redirect_uri = "http://127.0.0.1:8420/oauth/callback"

        def __init__(self) -> None:
            self.saved: dict[str, Any] = {}

        async def set_oauth_app(self, provider: str, **values: str) -> None:
            self.saved = {"provider": provider, **values}

    apps = _Apps()
    monkeypatch.setattr("arccli.commands.connector._connections", lambda _args: apps)
    monkeypatch.setattr("sys.stdin", io.StringIO("minted-secret-value\n"))
    _oauth_app(
        argparse.Namespace(
            provider="microsoft",
            client_id="aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
            tenant_id="11111111-2222-3333-4444-555555555555",
            cloud="global",
            client_secret_stdin=True,
        )
    )

    assert apps.saved == {
        "provider": "microsoft",
        "client_id": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
        "client_secret": "minted-secret-value",
        "tenant_id": "11111111-2222-3333-4444-555555555555",
        "cloud": "global",
    }
    assert "minted-secret-value" not in capsys.readouterr().out


class TestSemanticLayer:
    """The surface an operator uses to say what their data MEANS."""

    def test_it_prints_only_the_path_for_an_editor(
        self, arc_dir: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """`$EDITOR "$(arc connector semantic shop --path)"` must work, so this
        prints the path and nothing else — no heading, no advice."""
        connector_handler(["semantic", "shop", "--path", "--arc-dir", str(arc_dir)])

        printed = capsys.readouterr().out.strip()
        assert printed.endswith("semantic/shop.toml")
        assert "\n" not in printed

    def test_an_absent_layer_says_how_one_appears(
        self, arc_dir: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """ "Not found" with no next step is how an operator concludes a feature
        does not exist."""
        connector_handler(["semantic", "shop", "--arc-dir", str(arc_dir)])

        printed = capsys.readouterr().out
        assert "No semantic layer yet" in printed
        assert "first time the datastore's tables are read" in printed

    def test_it_names_the_tables_still_missing_a_description(
        self, arc_dir: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """The point of the readout: which tables an agent still sees only a name
        for."""
        from arcmemory.semantic_layer import layer_path

        path = layer_path("shop", arc_dir)
        assert path is not None
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            '[table.inv_hdr]\nentity = "invoice"\ndescription = "Billed jobs."\n'
            "[table.audit_log]\n",
            encoding="utf-8",
        )

        connector_handler(["semantic", "shop", "--arc-dir", str(arc_dir)])

        printed = capsys.readouterr().out
        assert "Billed jobs." in printed
        assert "no description" in printed

    def test_an_unparseable_layer_is_reported_rather_than_shown_as_empty(
        self, arc_dir: Path
    ) -> None:
        """It is hand-edited, so a stray quote is an ordinary Tuesday — and an
        operator whose edits stopped taking effect needs to be told why."""
        from arcmemory.semantic_layer import layer_path

        path = layer_path("shop", arc_dir)
        assert path is not None
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('[table.inv_hdr]\nentity = "unclosed\n', encoding="utf-8")

        with pytest.raises(SystemExit):
            connector_handler(["semantic", "shop", "--arc-dir", str(arc_dir)])
