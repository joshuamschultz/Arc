"""``arc connector`` hides credentials and shows configuration — the terminal half.

The same defect the web form had: every declared field was collected with
``getpass``, so an operator typing ``https://yourcompany.atlassian.net`` typed it
blind. A hidden prompt protects a token; on a URL it only guarantees that the
typo is invisible until the probe fails.

So the manifest's ``sensitive`` flag drives the prompt: hidden for a credential,
echoing for configuration. The credential's treatment is unchanged and is
asserted here alongside, because "we made the prompts nicer" is exactly the kind
of change that quietly relaxes the one that mattered.
"""

from __future__ import annotations

import tomllib
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from arctrust.paths import config_file

from arccli.commands.connector import connector_handler

_EXTENSION = "acme_fields"
_INSTANCE = "work"

_TOKEN = "zzz-cli-sensitive-4402"
_URL = "https://yourcompany.example.net"

_MANIFEST = f"""
[extension]
name = "{_EXTENSION}"
version = "1.0.0"
attachment = "native"
description = "Acme, configured with a URL and authorised with a token."

[config.native]
entrypoint = "acme_fields_attachment"

[[secrets]]
name = "api_token"
prompt = "Acme API token, from the Acme console under Settings then API tokens."

[[secrets]]
name = "base_url"
prompt = "Your Acme web address, exactly as it appears in the browser bar."
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

_ADAPTER = '''
"""The acme fixture's own implementation, outside every Arc package."""

from __future__ import annotations

from typing import Any

from arcagent.extension.attachment import ProbeResult, ToolResult, ToolSpec


class AcmeFieldsAttachment:
    def __init__(self, context: dict[str, Any]) -> None:
        self._token = str(context.get("api_token") or "")

    def requirements(self) -> list[Any]:
        return []

    async def probe(self) -> ProbeResult:
        if not self._token:
            return ProbeResult(reachable=False, detail="acme has no credential for api_token")
        return ProbeResult(reachable=True, tools=await self.describe_tools(), detail="ok")

    async def describe_tools(self) -> list[ToolSpec]:
        return [ToolSpec(name="ping", description="Report the Acme client version.")]

    async def invoke(self, tool: str, args: dict[str, Any]) -> ToolResult:
        return ToolResult(tool=tool, content="acme 1.0.0")


def build_native_attachment(context: dict[str, Any]) -> AcmeFieldsAttachment:
    return AcmeFieldsAttachment(context)
'''

_ANSWERS = {"api_token": _TOKEN, "base_url": _URL}


@pytest.fixture
def arc_dir(tmp_path: Path) -> Path:
    root = tmp_path / "arc"
    bundle = root / "extensions" / _EXTENSION
    bundle.mkdir(parents=True)
    (bundle / "extension.toml").write_text(_MANIFEST, encoding="utf-8")
    (bundle / "acme_fields_attachment.py").write_text(_ADAPTER, encoding="utf-8")
    return root


@pytest.fixture
def run(arc_dir: Path, tmp_path: Path) -> Callable[..., None]:
    def _run(*args: str) -> None:
        connector_handler([*args, "--arc-dir", str(arc_dir), "--data-dir", str(tmp_path / "data")])

    return _run


@pytest.fixture
def prompts(monkeypatch: pytest.MonkeyPatch) -> dict[str, list[str]]:
    """Answer both prompt kinds, recording which channel each field arrived on.

    Two separate recorders rather than one: the assertion that matters is not
    "the operator was asked" but "the token went down the hidden channel and the
    URL did not", and a single log could not tell those apart.
    """
    seen: dict[str, list[str]] = {"hidden": [], "visible": []}

    def _answer(channel: str) -> Callable[[str], str]:
        def _ask(prompt: str = "") -> str:
            seen[channel].append(prompt)
            field = next((name for name in _ANSWERS if name in prompt), None)
            return _ANSWERS.get(field or "", "")

        return _ask

    monkeypatch.setattr("arccli.commands.connector.getpass.getpass", _answer("hidden"))
    monkeypatch.setattr("builtins.input", _answer("visible"))
    return seen


def _prompt_text(prompts: dict[str, list[str]], channel: str) -> str:
    return " | ".join(prompts[channel])


def test_a_credential_is_asked_for_hidden_and_configuration_is_not(
    run: Callable[..., None], prompts: dict[str, list[str]]
) -> None:
    """The whole change: which channel each field is asked on.

    ``api_token`` must never reach the echoing prompt, and ``base_url`` must never
    reach the hidden one — an operator who cannot see the URL they typed is the
    person this was reported by.
    """
    run("add", _EXTENSION, "--name", _INSTANCE)

    assert "api_token" in _prompt_text(prompts, "hidden")
    assert "base_url" not in _prompt_text(prompts, "hidden")
    assert "base_url" in _prompt_text(prompts, "visible")
    assert "api_token" not in _prompt_text(prompts, "visible")


def test_the_prompts_are_the_bundles_own_sentences(
    run: Callable[..., None], prompts: dict[str, list[str]]
) -> None:
    """Whichever channel a field goes down, the words come from the manifest."""
    run("add", _EXTENSION, "--name", _INSTANCE)

    assert "Acme console" in _prompt_text(prompts, "hidden")
    assert "browser bar" in _prompt_text(prompts, "visible")


def test_neither_value_is_echoed_into_the_config_or_the_terminal(
    arc_dir: Path,
    run: Callable[..., None],
    prompts: dict[str, list[str]],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Showing a URL on the way IN must not mean writing either value on the way out.

    A visible prompt changes how the operator types the value, and nothing else:
    the config file still records only which bundle and which approval mode, and
    the terminal still reports field names.
    """
    run("add", _EXTENSION, "--name", _INSTANCE)

    captured = capsys.readouterr()
    config: dict[str, Any] = tomllib.loads(
        config_file("connections.toml", arc_dir).read_text(encoding="utf-8")
    )
    assert config["connections"][_INSTANCE]["extension"] == _EXTENSION
    for rendered in (captured.out + captured.err, str(config)):
        assert _TOKEN not in rendered
        assert _URL not in rendered
