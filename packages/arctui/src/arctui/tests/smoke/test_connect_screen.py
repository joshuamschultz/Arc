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
attachment = "cli"
description = "Open and read Acme tickets."

[[secrets]]
name = "api_token"
prompt = "Paste the Acme API token"

[tools]
allow = ["ping"]

[approval]
default = "outbound"

[config.cli]
binary = "python3"
probe_argv = ["--version"]

[[config.cli.commands]]
tool = "ping"
argv = ["--version"]
description = "Report the Acme client version."
classification = "read_only"
"""

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
    """An agent directory with one bundle, and an Arc world entirely inside tmp_path.

    ``ARC_CONFIG_DIR`` and ``ARCSTORE_DATA_DIR`` are redirected so the operator
    key this test mints and the WORM chain it writes never touch the real
    ``~/.arc``. ``ARC_EXTENSIONS_ROOT`` is cleared so a value in the developer's
    environment cannot add a bundle the assertions do not expect.
    """
    monkeypatch.setenv("ARC_CONFIG_DIR", str(tmp_path / "arc"))
    monkeypatch.setenv("ARCSTORE_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.delenv("ARC_EXTENSIONS_ROOT", raising=False)

    agent = tmp_path / "acme_agent"
    (agent / "extensions").mkdir(parents=True)
    (agent / "arcagent.toml").write_text(
        f'[agent]\nname = "acme_agent"\n\n[identity]\ndid = "{_DID}"\n\n'
        '[security]\ntier = "personal"\n',
        encoding="utf-8",
    )
    return agent


def _write_bundle(agent_dir: Path, manifest: str = _MANIFEST) -> None:
    bundle = agent_dir / "extensions" / _EXTENSION
    bundle.mkdir(parents=True, exist_ok=True)
    (bundle / "extension.toml").write_text(manifest, encoding="utf-8")


def _rendered(transcript: TranscriptView) -> str:
    """Everything a person can read in the transcript right now."""
    return "\n".join(transcript._format_message(msg) for msg in transcript._messages)


def _instance_blocks(agent_dir: Path) -> dict[str, Any]:
    raw = tomllib.loads((agent_dir / "arcagent.toml").read_text(encoding="utf-8"))
    blocks = raw.get("extensions", {})
    assert isinstance(blocks, dict)
    return blocks


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


async def test_the_flow_installs_through_the_real_install_path(agent_dir: Path) -> None:
    """The modal reaches ``install_connector`` and the instance lands in the config."""
    _write_bundle(agent_dir)

    app = ArcTUI(transport=None, agent_label="acme_agent", agent_dir=agent_dir)
    async with app.run_test() as pilot:
        screen = await _open_connect(pilot)
        await _fill_and_install(pilot, screen, plan_check=True)

    blocks = _instance_blocks(agent_dir)
    assert blocks[_INSTANCE]["extension"] == _EXTENSION
    assert blocks[_INSTANCE]["approval"] == "outbound"
    env = (agent_dir / "connectors.env").read_text(encoding="utf-8")
    assert _SENTINEL in env, "the credential belongs in the owner-only env file"


async def test_the_credential_never_reaches_the_transcript(agent_dir: Path) -> None:
    """A value typed into the masked field is absent from what the operator can read."""
    _write_bundle(agent_dir)

    app = ArcTUI(transport=None, agent_label="acme_agent", agent_dir=agent_dir)
    async with app.run_test() as pilot:
        transcript = pilot.app.query_one("#transcript", TranscriptView)
        screen = await _open_connect(pilot)
        await _fill_and_install(pilot, screen)
        text = _rendered(transcript)

    # The value really did flow — so its absence above is a redaction, not a no-op.
    assert _SENTINEL in (agent_dir / "connectors.env").read_text(encoding="utf-8")
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
    assert _instance_blocks(agent_dir) == {}
    assert not (agent_dir / "connectors.env").exists()


async def test_connections_lists_what_the_agent_already_has(agent_dir: Path) -> None:
    """``/connections`` shows installed instances.

    The operator needs to see state, not only create it.
    """
    _write_bundle(agent_dir)

    app = ArcTUI(transport=None, agent_label="acme_agent", agent_dir=agent_dir)
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
        assert _INSTANCE in str(rows.get_option_at_index(0).prompt)

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
    broken = agent_dir / "extensions" / "brokenbundle"
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
