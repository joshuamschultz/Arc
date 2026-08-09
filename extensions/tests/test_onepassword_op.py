"""The onepassword bundle as `op` actually receives it.

This connector resolves credentials, so it gets its own file for two reasons the
shared bundle rules cannot express.

**Its output is credential material.** `onepassword_resolve_secret` returns a live
secret by definition. What must never happen is that value reaching a log line, an
audit event, or a refusal an operator reads in a browser — so the asserted property
is not "the tool works" but "the value went to exactly one place".

**Its blast radius is a manifest value, not a model argument.** Which vault this
connection may read is the operator's decision. `--vault={vault_id}` is filled from
the bundle's own configured field, and no argument any verb declares can change it.

Every argv below was run against op 2.35.0 on the deployment before it was written
here. The credential there is a bogus service-account token, which is enough: op
parses flags and arguments before it authenticates, so a shape it accepts is a
shape it accepts.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

import pytest
from arcagent.core.tier import Tier
from arcagent.extension.host_login import authorization_verdict
from arcagent.extension.manifest import ExtensionManifest, load_manifest
from arcagent.extension.secrets import Secret
from arcagent.modules.connectors.install import build_attachment, visible_values

BUNDLE = Path(__file__).resolve().parents[1] / "onepassword"

#: The vault the operator configured. Distinctive so finding it in an argv proves
#: it came from configuration rather than from a default.
_VAULT = "Engineering-Ops"

#: Stands in for the service-account token. Finding it anywhere but the child's
#: environment is a leak.
_TOKEN = "ops_sentinel_Rk9SQklEREVO_000"

#: What a resolve returns. Finding THIS in a log or a refusal is the failure this
#: file exists to prevent.
_SECRET = "correct-horse-battery-staple-9931"


def _manifest() -> ExtensionManifest:
    return load_manifest(
        (BUNDLE / "extension.toml").read_text(encoding="utf-8"), tier=Tier.PERSONAL
    )


def _secrets() -> dict[str, Secret]:
    return {"service_account_token": Secret(_TOKEN), "vault_id": Secret(_VAULT)}


class _Recorder:
    """Stands in for ``create_subprocess_exec`` and records argv and the child env."""

    def __init__(self, stdout: bytes = b"{}", stderr: bytes = b"", returncode: int = 0) -> None:
        self.argv: list[str] = []
        self.env: dict[str, str] = {}
        self._stdout = stdout
        self._stderr = stderr
        self.returncode = returncode

    async def __call__(self, program: str, *args: str, **kwargs: Any) -> _Recorder:
        self.argv = [program, *args]
        self.env = dict(kwargs.get("env") or {})
        return self

    async def communicate(self, input: bytes | None = None) -> tuple[bytes, bytes]:  # noqa: A002
        return self._stdout, self._stderr

    async def wait(self) -> int:
        return self.returncode

    def kill(self) -> None:
        return None


def _attach(recorder: _Recorder, monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setattr(asyncio, "create_subprocess_exec", recorder)
    return build_attachment(_manifest(), BUNDLE, _secrets())


# --- the vault is the operator's, not the model's ------------------------------


async def test_listing_is_pinned_to_the_configured_vault(monkeypatch: pytest.MonkeyPatch) -> None:
    recorder = _Recorder(stdout=b"[]")

    await _attach(recorder, monkeypatch).invoke("onepassword_list_items", {})

    assert recorder.argv == ["op", "item", "list", "--format=json", f"--vault={_VAULT}"]


async def test_reading_an_item_is_pinned_to_the_same_vault(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorder = _Recorder()

    await _attach(recorder, monkeypatch).invoke("onepassword_get_item", {"item": "Deploy key"})

    assert recorder.argv == [
        "op",
        "item",
        "get",
        "--format=json",
        f"--vault={_VAULT}",
        "--",
        "Deploy key",
    ]


async def test_no_verb_offers_the_vault_as_an_argument() -> None:
    """A model that could name the vault could read every vault the token reaches."""
    for command in _manifest().config["cli"]["commands"]:
        names = {argument["name"] for argument in command.get("arguments", [])}
        assert "vault" not in names and "vault_id" not in names, command["tool"]


async def test_an_item_name_that_looks_like_a_flag_cannot_become_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``--vault`` in an item name would otherwise repoint the read."""
    recorder = _Recorder()

    await _attach(recorder, monkeypatch).invoke(
        "onepassword_get_item", {"item": "--vault=Everything"}
    )

    assert recorder.argv[-2:] == ["--", "--vault=Everything"]
    assert recorder.argv.count(f"--vault={_VAULT}") == 1


# --- reading one secret ---------------------------------------------------------


async def test_a_resolve_returns_the_value_op_printed(monkeypatch: pytest.MonkeyPatch) -> None:
    """``op read`` has no ``--format``; its whole answer IS the secret."""
    recorder = _Recorder(stdout=f"{_SECRET}\n".encode())

    result = await _attach(recorder, monkeypatch).invoke(
        "onepassword_resolve_secret", {"reference": f"op://{_VAULT}/Deploy key/password"}
    )

    assert recorder.argv == ["op", "read", "--", f"op://{_VAULT}/Deploy key/password"]
    assert result.content == _SECRET


async def test_only_the_resolve_verb_is_exempt_from_the_json_check() -> None:
    """Every other command asks op for JSON, and still gets the check."""
    commands = {c["tool"]: c for c in _manifest().config["cli"]["commands"]}

    assert commands["onepassword_resolve_secret"]["output"] == "text"
    for tool, command in commands.items():
        if tool == "onepassword_resolve_secret":
            continue
        assert command.get("output", "json") == "json"
        assert "--format=json" in command["argv"], f"{tool} would return an unparseable table"


async def test_a_resolve_that_printed_nothing_is_not_reported_as_a_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An empty answer must never read as "the secret is empty"."""
    recorder = _Recorder(stdout=b"")

    result = await _attach(recorder, monkeypatch).invoke(
        "onepassword_resolve_secret", {"reference": "op://v/i/password"}
    )

    assert result.outcome.value == "error"


# --- the credential goes to exactly one place -----------------------------------


async def test_the_service_account_token_reaches_the_child_environment_and_not_argv(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """This is the whole placement seam, asserted at the boundary op sees."""
    recorder = _Recorder(stdout=b"[]")

    await _attach(recorder, monkeypatch).invoke("onepassword_list_items", {})

    assert recorder.env["OP_SERVICE_ACCOUNT_TOKEN"] == _TOKEN
    assert _TOKEN not in " ".join(recorder.argv)


async def test_the_token_is_taken_back_out_of_anything_op_echoes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Several CLIs print what they were handed, and that output is logged."""
    recorder = _Recorder(stdout=b"", stderr=f"[ERROR] bad token {_TOKEN}".encode(), returncode=1)

    result = await _attach(recorder, monkeypatch).invoke("onepassword_list_items", {})

    assert result.outcome.value == "error"
    assert _TOKEN not in result.content


async def test_a_resolved_secret_never_reaches_the_log(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The value is the answer to the caller and to nothing else.

    ``CliAttachment`` logs stderr, never stdout, and the secret arrives on stdout —
    this asserts that split holds rather than assuming it, because a future change
    that logged the result would be invisible in review and catastrophic here.
    """
    recorder = _Recorder(stdout=f"{_SECRET}\n".encode(), stderr=b"[warn] cache miss")

    with caplog.at_level(logging.DEBUG):
        result = await _attach(recorder, monkeypatch).invoke(
            "onepassword_resolve_secret", {"reference": "op://v/i/password"}
        )

    assert result.content == _SECRET
    assert _SECRET not in caplog.text


def test_the_configured_vault_is_not_a_credential_and_the_token_is() -> None:
    """``visible_values`` feeds argv, so only non-sensitive fields may be in it."""
    manifest = _manifest()

    values = visible_values(manifest, _secrets())

    assert values == {"vault_id": _VAULT}
    assert _TOKEN not in str(values)


# --- signing in -----------------------------------------------------------------

#: Measured on the deployment. op separates the two states by exit code as well,
#: but the pattern is what makes a future op that exits 0 still read correctly.
_NO_SESSION = (1, "[ERROR] 2026/08/09 14:36:55 no active session found for account joshuaschultz")
_BAD_TOKEN = (9, "[ERROR] 2026/08/09 14:36:55 failed to DecodeSACredentials: illegal base64 data")
_SERVICE_ACCOUNT = (
    0,
    '{"URL":"https://my.1password.com","IntegrationID":"6P4HHXFVGVCE7",'
    '"UserType":"SERVICE_ACCOUNT"}',
)
_USER_ACCOUNT = (0, '{"url":"https://my.1password.com","user_uuid":"ABCDEF","email":"a@b.test"}')


@pytest.mark.parametrize("signed_out", [_NO_SESSION, _BAD_TOKEN])
def test_neither_refusal_can_be_read_as_signed_in(signed_out: tuple[int, str]) -> None:
    requirement = _manifest().host_requires[0]

    assert not authorization_verdict(requirement, *signed_out)
    assert not authorization_verdict(requirement, 0, signed_out[1]), "not even at exit 0"


@pytest.mark.parametrize("signed_in", [_SERVICE_ACCOUNT, _USER_ACCOUNT])
def test_either_account_shape_is_read_as_signed_in(signed_in: tuple[int, str]) -> None:
    requirement = _manifest().host_requires[0]

    assert authorization_verdict(requirement, *signed_in)


def test_the_sign_in_check_runs_the_binary_this_bundle_declares() -> None:
    requirement = _manifest().host_requires[0]

    assert requirement.name == "op"
    assert requirement.verify_command.split()[0] == "op"


def test_arc_never_offers_a_terminal_command_that_does_not_exist() -> None:
    """A service account is authorised by pasting a token, not by running anything."""
    requirement = _manifest().host_requires[0]

    assert requirement.authorize_command == ""
    assert requirement.token_command == ""
    assert {field.name for field in _manifest().secrets} == {
        "service_account_token",
        "vault_id",
    }


def test_this_bundle_ships_no_transport_code_of_its_own() -> None:
    """It imported a Python SDK nothing installed; now it runs the vendor's binary."""
    assert sorted(path.name for path in BUNDLE.iterdir()) == ["extension.toml", "skills"]
    assert not list(BUNDLE.rglob("*.py"))


def test_no_write_verb_is_reachable() -> None:
    """`op` has item create/edit/delete; read-only here means they are not declared."""
    for command in _manifest().config["cli"]["commands"]:
        assert not {"create", "edit", "delete", "template"}.intersection(command["argv"])
        assert command["classification"] == "read_only"
