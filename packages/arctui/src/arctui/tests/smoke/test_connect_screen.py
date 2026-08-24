"""SPEC-064 T-030 — ``/connect``: the TUI makes a connection without ``getpass``.

``arc connector add`` collects credentials with ``getpass``, which reads the
controlling terminal — the one Textual has taken over. Routing ``/connect`` to
the arccli registry handler would therefore block on a prompt nobody can see, so
the TUI owns this verb and drives
:mod:`arcagent.modules.connectors.install` directly (D-586).

Four properties are pinned here, and each of them is a defect this file exists to
catch rather than a restatement of the happy path:

* ``/connect`` never reaches ``resolve_command``. A regression that deletes the
  built-in branch still "works" in every other test — the registry handler is
  found, a worker thread starts, and the TUI simply hangs. Recording that the
  registry was never consulted is the only assertion that sees it.
* The install goes through the real :func:`install_connector` against a real
  bundle, and the instance lands in the agent's ``arcagent.toml``. A test that
  substitutes the install proves the screen can call a substitute.
* The credential the operator typed is absent from the *rendered* transcript.
  Asserting on a dict the test built proves nothing; the leak would be in what a
  person can read on screen.
* An unmet host prerequisite ends the flow with the instruction and installs
  nothing — the TUI directs, it never installs a host dependency (REQ-262).
"""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any

import pytest
from arcstore.backends.memory import FakeBackend
from arctrust.paths import config_file
from textual.widgets import Button, Input, Label, OptionList

from arctui.app import ArcTUI
from arctui.input_composer import InputComposer
from arctui.transcript import TranscriptView

#: A credential value distinctive enough that finding it anywhere is proof of a leak.
_SENTINEL = "zzz-tui-secret-sentinel-4711"

_EXTENSION = "acme_tickets"
_INSTANCE = "work"
_DID = "did:arc:example:org:agent:acme"

_MANIFEST = """
[extension]
name = "acme_tickets"
version = "2.1.0"
attachment = "native"
description = "Open and read Acme tickets."

[config.native]
entrypoint = "acme_tui_attachment"

[[secrets]]
name = "api_token"
prompt = "Paste the Acme API token"

# Configuration, not a credential — declared here so the modal's masking branch is
# exercised in BOTH directions. A suite where every field is sensitive passes
# against a screen that masks unconditionally, which is the shipped defect.
[[secrets]]
name = "base_url"
prompt = "Your Acme web address, as it appears in the browser bar"
sensitive = false

[tools]
allow = ["ping"]

[[tools.declared]]
name = "ping"
description = "Report the Acme client version."
classification = "read_only"

[approval]
default = "outbound"
"""

#: The bundle's own implementation, written beside its manifest as a real bundle
#: does. A ``native`` bundle rather than a ``cli`` one because the credential the
#: operator types must actually reach the connector, and a ``cli`` attachment
#: reaches its service by spawning a binary — it has no way to receive one, which
#: ``build_attachment`` refuses by name. Probing reachable therefore means the
#: screen delivered the credential, not merely that it stored one.
#:
#: The entrypoint name is unique to this suite: a native entrypoint is imported by
#: bare module name and ``sys.modules`` caches it for the whole session, so two
#: fixtures sharing one name would silently serve each other's implementation.
_ADAPTER = '''
"""The acme fixture's own implementation, outside every Arc package."""

from __future__ import annotations

from typing import Any

from arcagent.extension.attachment import ProbeResult, ToolResult, ToolSpec


class AcmeAttachment:
    """Reachable exactly when Arc handed it the credential the manifest declares."""

    def __init__(self, context: dict[str, Any]) -> None:
        self._token = str(context.get("api_token") or "")

    def requirements(self) -> list[Any]:
        return []

    async def probe(self) -> ProbeResult:
        if not self._token:
            return ProbeResult(reachable=False, detail="acme has no credential for api_token")
        return ProbeResult(
            reachable=True, tools=await self.describe_tools(), detail="acme is authenticated"
        )

    async def describe_tools(self) -> list[ToolSpec]:
        return [
            ToolSpec(
                name="ping",
                description="Report the Acme client version.",
                input_schema={"type": "object", "properties": {}, "additionalProperties": False},
                classification="read_only",
            )
        ]

    async def invoke(self, tool: str, args: dict[str, Any]) -> ToolResult:
        return ToolResult(tool=tool, content="acme 2.1.0")


def build_native_attachment(context: dict[str, Any]) -> AcmeAttachment:
    return AcmeAttachment(context)
'''

_MANIFEST_NEEDS_HOST = (
    _MANIFEST
    + """
[[host_requires]]
name = "definitely_not_installed_xyz"
instruction = "brew install definitely-not-installed-xyz"
"""
)


@pytest.fixture
def agent_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """An agent inside a deployment whose whole world lives in ``tmp_path``.

    ``ARC_CONFIG_DIR`` and ``ARCSTORE_DATA_DIR`` are redirected so the operator
    key this test mints, the connections it defines, and the WORM chain it writes
    never touch the real ``~/.arc``. ``ARC_EXTENSIONS_ROOT`` is cleared so a value
    in the developer's environment cannot add a bundle the assertions do not
    expect.

    The agent lives under ``<arc_dir>/team/<name>`` because that is where the
    grant model looks for its tier: the directory name is the grant coordinate,
    and the config beside it is the tier the connection is served at.
    """
    monkeypatch.setenv("ARC_CONFIG_DIR", str(tmp_path / "arc"))
    monkeypatch.setenv("ARCSTORE_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.delenv("ARC_EXTENSIONS_ROOT", raising=False)

    agent = tmp_path / "arc" / "team" / "acme_agent"
    agent.mkdir(parents=True)
    (agent / "arcagent.toml").write_text(
        f'[agent]\nname = "acme_agent"\n\n[identity]\ndid = "{_DID}"\n\n'
        '[security]\ntier = "personal"\n',
        encoding="utf-8",
    )
    return agent


@pytest.fixture
def state_backend() -> FakeBackend:
    """One backend per test, shared by every connector verb in that test."""
    return FakeBackend()


async def _open_state_backend(backend: FakeBackend) -> FakeBackend:
    """Return the test's captured state backend without using configured storage."""
    return backend


def _app(agent_dir: Path, backend: FakeBackend) -> ArcTUI:
    """Build a TUI whose connector verbs share this test's state backend."""
    return ArcTUI(
        transport=None,
        agent_label="acme_agent",
        agent_dir=agent_dir,
        state_opener=lambda: _open_state_backend(backend),
    )


def _arc_dir(agent_dir: Path) -> Path:
    """The deployment root — where connections, credentials and bundles live."""
    return agent_dir.parent.parent


def _write_bundle(agent_dir: Path, manifest: str = _MANIFEST) -> Path:
    """Put a bundle on the DEPLOYMENT's search path.

    There is deliberately no agent-local extensions root: a connection is the
    deployment's, so its bundle has to be resolvable by every agent granted it.
    """
    bundle = _arc_dir(agent_dir) / "extensions" / _EXTENSION
    bundle.mkdir(parents=True, exist_ok=True)
    (bundle / "extension.toml").write_text(manifest, encoding="utf-8")
    (bundle / "acme_tui_attachment.py").write_text(_ADAPTER, encoding="utf-8")
    return bundle


def _rendered(transcript: TranscriptView) -> str:
    """Everything a person can read in the transcript right now."""
    return "\n".join(transcript._format_message(msg) for msg in transcript._messages)


def _connections(agent_dir: Path) -> dict[str, Any]:
    """The deployment's connections, read back off disk exactly as the runtime does."""
    path = config_file("connections.toml", _arc_dir(agent_dir))
    if not path.is_file():
        return {}
    table = tomllib.loads(path.read_text(encoding="utf-8")).get("connections", {})
    assert isinstance(table, dict)
    return table


def _env_file(agent_dir: Path) -> Path:
    """Where a connector credential is written — one owner-only file per deployment."""
    return config_file("connections.env", _arc_dir(agent_dir))


async def _open_connect(pilot: Any) -> Any:
    """Submit ``/connect`` from the composer and return the screen it opened."""
    composer = pilot.app.query_one("#composer", InputComposer)
    composer.post_message(InputComposer.SubmitMessage("/connect"))
    await pilot.pause()
    await pilot.app.workers.wait_for_complete()
    await pilot.pause()
    return pilot.app.screen


async def _fill_and_install(
    pilot: Any, screen: Any, *, token: str = _SENTINEL, plan_check: bool = False
) -> None:
    """Name the instance, review the plan, type the credential, and install."""
    screen.query_one("#connect-instance", Input).value = _INSTANCE
    screen.query_one("#connect-review", Button).press()
    await pilot.pause()
    await pilot.app.workers.wait_for_complete()
    await pilot.pause()

    if plan_check:
        # What will happen is shown BEFORE the first field appears: the credential
        # it will ask for, and the approval mode the connection will run under.
        shown = str(screen.query_one("#connect-plan", Label).renderable)
        assert "api_token" in shown
        assert "outbound" in shown

    field = screen.query_one("#connect-secret-api_token", Input)
    assert field.password is True, "a credential must be collected into a masked field"
    field.value = token

    # The other direction: a base URL typed behind password dots is a typo nobody
    # can see until the probe fails, and there is no secret there to protect.
    configuration = screen.query_one("#connect-secret-base_url", Input)
    assert configuration.password is False, "configuration must be visible as it is typed"
    configuration.value = "https://acme.example.net"

    screen.query_one("#connect-install", Button).press()
    await pilot.pause()
    await pilot.app.workers.wait_for_complete()
    await pilot.pause()


async def test_connect_is_handled_inside_the_tui(
    agent_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``/connect`` opens the modal and never consults the arccli registry.

    The registry handler prompts with ``getpass`` against a terminal Textual owns,
    so reaching it is the failure this whole task exists to remove.
    """
    _write_bundle(agent_dir)
    import arccli.commands.registry as registry

    consulted: list[str] = []
    real_resolve = registry.resolve_command

    def _recording_resolve(name: str) -> Any:
        consulted.append(name)
        return real_resolve(name)

    monkeypatch.setattr(registry, "resolve_command", _recording_resolve)

    app = ArcTUI(transport=None, agent_label="acme_agent", agent_dir=agent_dir)
    async with app.run_test() as pilot:
        screen = await _open_connect(pilot)
        assert type(screen).__name__ == "ConnectScreen"
        assert consulted == []


async def test_the_flow_installs_through_the_real_install_path(
    agent_dir: Path, state_backend: FakeBackend
) -> None:
    """The modal reaches ``install_connector``, and the connection lands granted.

    The grant is the assertion that matters. A connection defined but handed to
    nobody is a working account no agent can see, so a flow that installed and
    forgot to grant would look identical to a working one until the operator
    asked their agent to use it.
    """
    _write_bundle(agent_dir)

    app = _app(agent_dir, state_backend)
    async with app.run_test() as pilot:
        screen = await _open_connect(pilot)
        await _fill_and_install(pilot, screen, plan_check=True)

    defined = _connections(agent_dir)
    assert defined[_INSTANCE]["extension"] == _EXTENSION
    assert defined[_INSTANCE]["approval"] == "outbound"
    assert defined[_INSTANCE]["agents"] == ["acme_agent"]
    assert not (agent_dir / "connections.toml").exists(), "nothing is written into the agent"
    env = _env_file(agent_dir).read_text(encoding="utf-8")
    assert _SENTINEL in env, "the credential belongs in the owner-only env file"


async def test_the_credential_never_reaches_the_transcript(
    agent_dir: Path, state_backend: FakeBackend
) -> None:
    """A value typed into the masked field is absent from what the operator can read."""
    _write_bundle(agent_dir)

    app = _app(agent_dir, state_backend)
    async with app.run_test() as pilot:
        transcript = pilot.app.query_one("#transcript", TranscriptView)
        screen = await _open_connect(pilot)
        await _fill_and_install(pilot, screen)
        text = _rendered(transcript)

    # The value really did flow — so its absence above is a redaction, not a no-op.
    assert _SENTINEL in _env_file(agent_dir).read_text(encoding="utf-8")
    assert _SENTINEL not in text
    assert _INSTANCE in text, "the report itself must still reach the transcript"


async def test_an_unmet_host_prerequisite_ends_the_flow_and_installs_nothing(
    agent_dir: Path,
) -> None:
    """The instruction is shown, no credential is asked for, and nothing is written."""
    _write_bundle(agent_dir, manifest=_MANIFEST_NEEDS_HOST)

    app = ArcTUI(transport=None, agent_label="acme_agent", agent_dir=agent_dir)
    async with app.run_test() as pilot:
        transcript = pilot.app.query_one("#transcript", TranscriptView)
        screen = await _open_connect(pilot)
        screen.query_one("#connect-instance", Input).value = _INSTANCE
        screen.query_one("#connect-review", Button).press()
        await pilot.pause()
        await pilot.app.workers.wait_for_complete()
        await pilot.pause()

        text = _rendered(transcript)

    assert "brew install definitely-not-installed-xyz" in text
    assert _connections(agent_dir) == {}
    assert not _env_file(agent_dir).exists()


async def test_connections_lists_what_the_agent_already_has(
    agent_dir: Path, state_backend: FakeBackend
) -> None:
    """``/connections`` shows the deployment's accounts and who holds each one.

    The operator needs to see state, not only create it — and under deny-by-default
    the state that decides everything is the grant, so the row has to carry it.
    """
    _write_bundle(agent_dir)

    app = _app(agent_dir, state_backend)
    async with app.run_test() as pilot:
        screen = await _open_connect(pilot)
        await _fill_and_install(pilot, screen)

        composer = pilot.app.query_one("#composer", InputComposer)
        composer.post_message(InputComposer.SubmitMessage("/connections"))
        await pilot.pause()
        await pilot.app.workers.wait_for_complete()
        await pilot.pause()

        listing = pilot.app.screen
        assert type(listing).__name__ == "ConnectionsScreen"
        rows = listing.query_one("#connections-list", OptionList)
        assert rows.option_count == 1
        row = str(rows.get_option_at_index(0).prompt)
        assert _INSTANCE in row
        assert "granted to acme_agent" in row

        # Probing is the only honest answer to "does this connection work" — it opens
        # the real attachment rather than reading the config back.
        rows.highlighted = 0
        listing.query_one("#connections-probe", Button).press()
        await pilot.pause()
        await pilot.app.workers.wait_for_complete()
        await pilot.pause()
        verdict = str(listing.query_one("#connections-status", Label).renderable)

    assert "is reachable" in verdict
    assert "ping" in verdict


async def test_a_bundle_that_will_not_parse_is_named_not_dropped(agent_dir: Path) -> None:
    """A broken bundle is reported and the picker still offers the good one.

    Silently omitting it would be indistinguishable from a bundle that was never
    installed, and 500-ing the whole listing would hide the working one too.
    """
    _write_bundle(agent_dir)
    broken = _arc_dir(agent_dir) / "extensions" / "brokenbundle"
    broken.mkdir()
    (broken / "extension.toml").write_text(
        '[extension]\nname = "brokenbundle"\n', encoding="utf-8"
    )

    app = ArcTUI(transport=None, agent_label="acme_agent", agent_dir=agent_dir)
    async with app.run_test() as pilot:
        screen = await _open_connect(pilot)
        options = screen.query_one("#connect-bundles", OptionList)
        hint = str(screen.query_one("#connect-hint", Label).renderable)

    assert options.option_count == 1
    assert _EXTENSION in str(options.get_option_at_index(0).prompt)
    assert "brokenbundle" in hint
    assert "\n" not in hint.split("unreadable: ", 1)[1], "a reason must not wrap the hint"


async def test_connect_without_an_agent_says_so(tmp_path: Path) -> None:
    """No attached agent means no agent directory to connect anything to."""
    app = ArcTUI(transport=None)
    async with app.run_test() as pilot:
        transcript = pilot.app.query_one("#transcript", TranscriptView)
        composer = pilot.app.query_one("#composer", InputComposer)
        composer.post_message(InputComposer.SubmitMessage("/connect"))
        await pilot.pause()
        text = _rendered(transcript)

    assert "no agent" in text.lower()


async def test_a_name_that_would_break_the_agent_config_is_refused_here_too(
    agent_dir: Path,
) -> None:
    """Every surface refuses it, because every surface plans before it installs.

    A space is not a legal bare TOML key: written into
    ``[connections.<instance>]`` it stops ``connections.toml`` parsing, which is
    now the whole deployment's grant list — every agent loses every connection,
    not just this one.
    """
    _write_bundle(agent_dir)

    app = ArcTUI(transport=None, agent_label="acme_agent", agent_dir=agent_dir)
    async with app.run_test() as pilot:
        transcript = pilot.app.query_one("#transcript", TranscriptView)
        screen = await _open_connect(pilot)
        screen.query_one("#connect-instance", Input).value = "blackarc industrial email"
        screen.query_one("#connect-review", Button).press()
        await pilot.pause()
        await pilot.app.workers.wait_for_complete()
        await pilot.pause()

        text = _rendered(transcript)

    assert "blackarc_industrial_email" in text
    assert _connections(agent_dir) == {}
