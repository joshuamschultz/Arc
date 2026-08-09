"""The credential seam: a bundle says where its tool reads a credential, Arc puts it there.

Five of the eight shipped bundles could be finished by pasting a credential.
Three could not, and they are the three that matter most — dropbox,
google_workspace, microsoft365 — because their tools hold their own credential
and ``build_attachment`` refused any ``cli`` bundle that declared one at all:

    "no manifest table says which of that binary's inputs a credential would
    become. Accepting the declaration would store a credential and deliver it
    nowhere."

``[secrets.placement]`` is that table. It carries one field, the name of the
environment variable the bundle's own tool reads its credential from, and Arc
exports the stored value into every process that attachment starts — the tool's
verbs, its probe, and its sign-in check. Arc learns nothing: that ``dbxcli``
reads ``DBXCLI_ACCESS_TOKEN`` is a fact that lives in the dropbox bundle and
moves when Dropbox moves it.

What these tests pin, and why each one is here rather than left to review:

* **A placement that did not happen must not report success.** A ``cli`` bundle
  declaring a credential with no placement is still refused, by name — that is
  the original defect, and the seam must close it rather than widen it.
* **The variable may not steer the process.** ``PATH`` would let a pasted value
  choose which binary runs; ``LD_PRELOAD`` and ``NODE_OPTIONS`` would load code
  into it. The launcher already scrubs the second family, so a manifest naming
  one would be silently dropped — a placement an operator believes is in force
  and which delivers nothing. Both are refused where the manifest is parsed.
* **The value reaches the child and nothing else.** A real subprocess reads the
  variable back, and a sentinel is asserted absent from every rendered string
  around it: the refusal, the audit event, the log, and ``repr``.
* **The sign-in check runs with the placement.** ``dbxcli account`` answers
  "signed out" without the token in its environment, so a check taken outside
  the placed environment would report a freshly connected account as not
  connected — the same lie as the defect, told the other way round.
"""

from __future__ import annotations

import hashlib
import logging
import sys
from pathlib import Path

import pytest
from arctrust.audit import AuditEvent
from pydantic import ValidationError

from arcagent.core.errors import ExtensionError
from arcagent.core.tier import Tier
from arcagent.extension.host_login import run_authorization_check
from arcagent.extension.manifest import ExtensionManifest, HostRequirement, load_manifest
from arcagent.extension.secrets import Secret
from arcagent.modules.connectors.install import build_attachment, placement_environment

#: A value no other string in this suite could produce, so an assertion that it
#: is absent is an assertion about this credential and not about phrasing.
_SENTINEL = "sentinel-credential-9d41f0e7"

_CALLER = "did:arc:test-caller"

#: What the fake binary prints: the digest of what it found in ``ACME_TOKEN``, as the
#: JSON a CLI command must produce. A digest rather than the value, because the
#: attachment redacts any placed credential out of everything it renders — so a child
#: that echoed the value would prove delivery and then have the proof taken away
#: again. Comparing digests proves the exact credential arrived without any assertion
#: here holding it.
_WITNESS = (
    "import hashlib,json,os; "
    "print(json.dumps({'digest': "
    "hashlib.sha256(os.environ.get('ACME_TOKEN','').encode()).hexdigest()}))"
)


def _digest_of(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


class _RecordingSink:
    def __init__(self) -> None:
        self.events: list[AuditEvent] = []

    def write(self, event: AuditEvent) -> None:
        self.events.append(event)


def _manifest(placement: str = 'variable = "ACME_TOKEN"') -> str:
    """A ``cli`` bundle declaring one credential, placed unless told otherwise."""
    block = f"\n[secrets.placement]\n{placement}\n" if placement else "\n"
    return f"""
[extension]
name = "acme"
version = "1.0.0"
description = "Acme from the command line."
attachment = "cli"

[[secrets]]
name = "api_token"
prompt = "Acme API token"
{block}
[config.cli]
binary = "{sys.executable}"
probe_argv = ["-c", "print('{{}}')"]

[[config.cli.commands]]
tool = "acme_whoami"
argv = ["-c", "{_WITNESS}"]
classification = "read_only"

[tools]
allow = ["acme_whoami"]

[[tools.declared]]
name = "acme_whoami"
classification = "read_only"
"""


def _parsed(placement: str = 'variable = "ACME_TOKEN"') -> ExtensionManifest:
    return load_manifest(_manifest(placement), tier=Tier.PERSONAL)


# --- what a bundle may declare ------------------------------------------------


def test_a_bundle_declares_the_variable_its_tool_reads_the_credential_from() -> None:
    """The whole seam: one field, owned by the bundle, meaningless to Arc."""
    declared = _parsed().secrets[0]
    assert declared.placement is not None
    assert declared.placement.variable == "ACME_TOKEN"


def test_a_credential_with_no_placement_is_not_an_error() -> None:
    """jira, confluence and onepassword take values straight into their own adapter."""
    assert _parsed(placement="").secrets[0].placement is None


@pytest.mark.parametrize("name", ["path", "1TOKEN", "ACME-TOKEN", "ACME TOKEN", ""])
def test_a_variable_that_is_not_an_environment_name_is_refused(name: str) -> None:
    """It becomes an env key in a spawned child; anything else silently vanishes."""
    with pytest.raises(ValidationError):
        _parsed(placement=f'variable = "{name}"')


@pytest.mark.parametrize("name", ["PATH", "LD_PRELOAD", "DYLD_INSERT_LIBRARIES", "NODE_OPTIONS"])
def test_a_variable_that_would_steer_the_process_is_refused(name: str) -> None:
    """A placement must deliver a credential, never choose what runs or what loads.

    ``PATH`` would let a pasted value pick the binary. The loader and interpreter
    families are scrubbed by :func:`scrubbed_environment` after the merge, so a
    manifest naming one would parse, place nothing, and report success — which is
    precisely the shape of failure this seam exists to end.
    """
    with pytest.raises(ValidationError) as raised:
        _parsed(placement=f'variable = "{name}"')
    assert name in str(raised.value)


# --- what Arc does with it ----------------------------------------------------


def test_the_placement_maps_declared_fields_to_their_stored_credentials() -> None:
    """The one function that turns declarations plus credentials into a child env.

    Still wrapped on the way out: the mapping is held on an attachment for the life
    of the connection, so a value here would render in every traceback that walked
    over it.
    """
    placed = placement_environment(_parsed(), {"api_token": Secret(_SENTINEL)})
    assert set(placed) == {"ACME_TOKEN"}
    assert placed["ACME_TOKEN"].reveal() == _SENTINEL
    assert _SENTINEL not in repr(placed)


def test_a_credential_with_no_placement_contributes_no_variable() -> None:
    assert placement_environment(_parsed(placement=""), {"api_token": Secret(_SENTINEL)}) == {}


def test_a_cli_bundle_whose_credential_is_placed_builds(tmp_path: Path) -> None:
    """The refusal this seam replaces: a ``cli`` bundle could declare no credential."""
    attachment = build_attachment(_parsed(), tmp_path, {"api_token": Secret(_SENTINEL)})
    assert attachment is not None


def test_a_cli_bundle_whose_credential_is_not_placed_is_still_refused(tmp_path: Path) -> None:
    """Fails closed and names the field: storing a value delivered nowhere is the bug."""
    with pytest.raises(ExtensionError) as raised:
        build_attachment(_parsed(placement=""), tmp_path, {"api_token": Secret(_SENTINEL)})
    assert "api_token" in raised.value.message
    assert _SENTINEL not in raised.value.message
    assert _SENTINEL not in str(raised.value.details)


async def test_the_placed_credential_really_reaches_the_child_process(tmp_path: Path) -> None:
    """A real subprocess reads the variable back — the only proof of delivery.

    Everything above is a declaration. This is the assertion that the declaration
    became an environment entry in a process that actually ran, which is the one
    thing a unit test of the mapping cannot show.
    """
    attachment = build_attachment(_parsed(), tmp_path, {"api_token": Secret(_SENTINEL)})
    result = await attachment.invoke("acme_whoami", {})
    assert _digest_of(_SENTINEL) in result.content


async def test_a_credential_placed_under_another_name_stays_out_of_the_child(
    tmp_path: Path,
) -> None:
    """A tool reads the variable its own bundle named, and no other.

    The negative half of the test above: without it, a child that happened to
    inherit the value from the ambient environment would look like delivery.
    """
    attachment = build_attachment(
        _parsed(placement='variable = "OTHER_TOKEN"'), tmp_path, {"api_token": Secret(_SENTINEL)}
    )
    result = await attachment.invoke("acme_whoami", {})
    assert _digest_of("") in result.content


# --- the seam is not something the agent can call -----------------------------


async def test_placing_a_credential_is_not_a_verb_the_agent_can_reach(tmp_path: Path) -> None:
    """The placement runs from the operator-gated install path, by direct call only.

    ``describe_tools`` is the entire surface the ``CapabilityBridge`` registers, so a
    name absent from it cannot become a capability, cannot appear in a tool list, and
    cannot be produced by a model. The second assertion closes the other door: a name
    the attachment does not declare is refused rather than dispatched, so guessing at
    one reaches nothing either.
    """
    attachment = build_attachment(_parsed(), tmp_path, {"api_token": Secret(_SENTINEL)})
    assert [spec.name for spec in await attachment.describe_tools()] == ["acme_whoami"]

    with pytest.raises(ExtensionError) as raised:
        await attachment.invoke("placement_environment", {"variable": "ACME_TOKEN"})
    assert "undeclared tool" in raised.value.message


# --- the value never leaks ----------------------------------------------------


def test_the_attachment_renders_no_credential(tmp_path: Path) -> None:
    """``repr`` of a built attachment is what lands in a traceback and a log line."""
    attachment = build_attachment(_parsed(), tmp_path, {"api_token": Secret(_SENTINEL)})
    assert _SENTINEL not in repr(attachment)
    assert _SENTINEL not in str(vars(attachment))


async def test_a_binary_that_echoes_the_credential_has_it_taken_back_out(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Several CLIs print what they were handed when they fail, and Arc renders that.

    The failure output goes three places at once: a log record, an operator's
    browser, and — as the tool result — the model's context (LLM02). One of those is
    enough; a binary Arc does not control writing the token is the one surface a
    wrapper type cannot protect, so the value is removed from the text instead.
    """
    manifest = load_manifest(
        _manifest().replace(
            _WITNESS,
            "import os,sys; sys.stderr.write(os.environ.get('ACME_TOKEN','')); sys.exit(3)",
        ),
        tier=Tier.PERSONAL,
    )
    attachment = build_attachment(manifest, tmp_path, {"api_token": Secret(_SENTINEL)})
    with caplog.at_level(logging.DEBUG):
        result = await attachment.invoke("acme_whoami", {})
    assert result.outcome == "error"
    assert _SENTINEL not in result.content
    assert _SENTINEL not in "".join(record.getMessage() for record in caplog.records)


# --- the sign-in check runs where the credential was placed -------------------


def _check_requirement() -> HostRequirement:
    """A "binary" that reports signed in only when the placed variable reached it.

    Both branches exit 0, which is the shape ``gog auth list`` really has and the
    reason ``verify_pattern`` exists: a check read on its exit code alone would call
    the signed-out branch signed in.
    """
    script = "import os; print('signed in as a@b.test' if os.environ.get('ACME_TOKEN') else 'no accounts stored')"
    return HostRequirement(
        name=sys.executable,
        verify_command=f"{sys.executable} -c {script!r}",
        verify_pattern="signed in as",
        authorize_command="acme login",
    )


async def test_the_sign_in_check_sees_the_placed_credential() -> None:
    """Otherwise a freshly connected account reports itself as not connected.

    ``dbxcli account`` answers from its environment. A check taken outside the
    placed environment reports "signed out" for an account that was just
    connected, which sends an operator to redo work that is already done — the
    mirror image of the green tick over an empty account.
    """
    sink = _RecordingSink()
    result = await run_authorization_check(
        _check_requirement(),
        caller_did=_CALLER,
        audit_sink=sink,
        tier=Tier.PERSONAL,
        env={"ACME_TOKEN": Secret(_SENTINEL)},
    )
    assert result.known and result.authorized


async def test_the_sign_in_check_without_the_credential_reports_signed_out() -> None:
    """The negative half: the check has to be able to fail, or it proves nothing."""
    sink = _RecordingSink()
    result = await run_authorization_check(
        _check_requirement(), caller_did=_CALLER, audit_sink=sink, tier=Tier.PERSONAL, env={}
    )
    assert result.known and not result.authorized


async def test_the_sign_in_check_audits_the_coordinate_and_never_the_value() -> None:
    """Pillar 4 records that a check happened, never what was placed to make it pass."""
    sink = _RecordingSink()
    await run_authorization_check(
        _check_requirement(),
        caller_did=_CALLER,
        audit_sink=sink,
        tier=Tier.PERSONAL,
        env={"ACME_TOKEN": Secret(_SENTINEL)},
    )
    assert sink.events
    for event in sink.events:
        assert _SENTINEL not in str(event.model_dump())
