"""The jira bundle as `acli` actually receives it.

``test_connector_bundles`` asks whether every bundle obeys the rules. This asks
the one question those rules cannot: does each declared tool become the command
Atlassian's CLI really answers. Every argv asserted below was run against the
deployment's live Jira before it was written here, so a rename upstream fails this
file rather than failing an operator.

Three properties, and each of them is a defect that shipped somewhere:

* **``--json`` is in every argv.** On a data command ``acli`` prints
  ``Authenticated site: <site>`` to STDOUT ahead of its payload, and a
  ``CliAttachment`` reports stdout that is not the declared JSON as a tool ERROR.
  Drop the flag from one command and that verb fails on success — the exact shape
  that made the ``github`` bundle drop its write verbs.
* **A read-only tool reaches a read-only verb.** The classification is a promise
  made in one table and kept in another; nothing but this compares them against
  what ``acli`` would do with the argv.
* **The token is on stdin and never on argv**, asserted against the argv the
  kernel would have seen, for the real ``token_command`` this bundle ships. argv
  is the process table and every other user on the box can read it.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from arcagent.core.tier import Tier
from arcagent.extension.host_login import authorization_verdict, run_token_login
from arcagent.extension.manifest import ExtensionManifest, load_manifest
from arcagent.modules.connectors.install import build_attachment
from arctrust.audit import AuditEvent

BUNDLE = Path(__file__).resolve().parents[1] / "jira"

_CALLER = "did:arc:testorg:executor/jira"

#: Distinctive enough that finding it in an argv is proof of a leak.
_SENTINEL = "ATATT3xFfGF0-sentinel-Rk9SQklEREVO-000"

_SITE = "ctgfederal.atlassian.net"
_EMAIL = "jschultz@ctgfederal.com"

#: Every ``acli jira`` subcommand that changes something. A read-only tool whose
#: argv contains one of these has been misclassified in a way no tag check sees.
_WRITE_VERBS = frozenset(
    {
        "create",
        "edit",
        "delete",
        "transition",
        "assign",
        "archive",
        "unarchive",
        "clone",
        "comment",
        "update",
        "restore",
        "link",
        "watcher",
        "create-bulk",
    }
)


def _manifest() -> ExtensionManifest:
    return load_manifest(
        (BUNDLE / "extension.toml").read_text(encoding="utf-8"), tier=Tier.PERSONAL
    )


class _Recorder:
    """Stands in for ``create_subprocess_exec`` and keeps the real token list."""

    def __init__(self, stdout: bytes = b"{}") -> None:
        self.argv: list[str] = []
        self.stdin: bytes | None = None
        self._stdout = stdout

    async def __call__(self, program: str, *args: str, **kwargs: Any) -> _Recorder:
        self.argv = [program, *args]
        return self

    async def communicate(self, input: bytes | None = None) -> tuple[bytes, bytes]:  # noqa: A002
        self.stdin = input
        return self._stdout, b""

    @property
    def returncode(self) -> int:
        return 0

    async def wait(self) -> int:
        return 0

    def kill(self) -> None:
        return None


@pytest.fixture
def spawn(monkeypatch: pytest.MonkeyPatch) -> _Recorder:
    recorder = _Recorder()
    monkeypatch.setattr(asyncio, "create_subprocess_exec", recorder)
    return recorder


async def _argv_for(tool: str, args: dict[str, str], spawn: _Recorder) -> list[str]:
    await build_attachment(_manifest(), BUNDLE, {}).invoke(tool, args)
    return spawn.argv


# --- each tool becomes the command acli answers -------------------------------


async def test_search_becomes_a_bounded_jql_query(spawn: _Recorder) -> None:
    argv = await _argv_for("jira_search_issues", {"jql": "project = KAN", "limit": "25"}, spawn)

    assert argv == [
        "acli",
        "jira",
        "workitem",
        "search",
        "--json",
        "--fields=issuetype,key,assignee,priority,status,summary,reporter,labels",
        "--jql=project = KAN",
        "--limit=25",
    ]


async def test_reading_one_issue_puts_the_key_after_the_terminator(spawn: _Recorder) -> None:
    """``acli jira workitem view`` answers ``--key`` with "unknown flag: --key"."""
    argv = await _argv_for("jira_get_issue", {"issue_key": "KAN-68"}, spawn)

    assert argv[:4] == ["acli", "jira", "workitem", "view"]
    assert argv[-2:] == ["--", "KAN-68"]


async def test_an_issue_key_that_looks_like_a_flag_stays_behind_the_terminator(
    spawn: _Recorder,
) -> None:
    """The model chooses the key; it may never choose a flag."""
    argv = await _argv_for("jira_get_issue", {"issue_key": "--web"}, spawn)

    assert argv[-2:] == ["--", "--web"]


@pytest.mark.parametrize("key", ["-KAN-1", "--web", "-", "--json", "-o/etc/passwd"])
async def test_a_key_beginning_with_a_dash_is_read_as_the_key(spawn: _Recorder, key: str) -> None:
    """The `--` is what makes this true, and it is why the terminator is required.

    Without it every one of these occupies a flag position: `--web` would open a
    browser, `--json` would be consumed as the flag, and `-o` families become file
    writes on CLIs that have them.
    """
    argv = await _argv_for("jira_get_issue", {"issue_key": key}, spawn)

    assert argv[-2:] == ["--", key]
    assert argv.count("--") == 1


async def test_listing_projects_takes_only_a_limit(spawn: _Recorder) -> None:
    argv = await _argv_for("jira_list_projects", {"limit": "50"}, spawn)

    assert argv == ["acli", "jira", "project", "list", "--json", "--paginate", "--limit=50"]


async def test_listing_projects_works_when_the_model_passes_no_limit(spawn: _Recorder) -> None:
    """acli refuses this verb without one of [recent limit paginate].

    Measured against the live site: an omitted argument contributes no token, so a
    call that simply does not pass `limit` exited 1 with acli's own flag-group
    error. `--paginate` is pinned in the fixed argv so the verb cannot be invoked
    in a shape acli rejects, whatever the model does or does not supply.
    """
    argv = await _argv_for("jira_list_projects", {}, spawn)

    assert "--paginate" in argv


async def test_creating_an_issue_names_the_project_and_the_type(spawn: _Recorder) -> None:
    argv = await _argv_for(
        "jira_create_issue",
        {"project": "KAN", "type": "Bug", "summary": "Login loops", "description": "Steps: ..."},
        spawn,
    )

    assert argv == [
        "acli",
        "jira",
        "workitem",
        "create",
        "--json",
        "--project=KAN",
        "--type=Bug",
        "--summary=Login loops",
        "--description=Steps: ...",
    ]


async def test_commenting_reaches_the_comment_subcommand(spawn: _Recorder) -> None:
    argv = await _argv_for("jira_add_comment", {"key": "KAN-68", "body": "on it"}, spawn)

    assert argv == [
        "acli",
        "jira",
        "workitem",
        "comment",
        "create",
        "--json",
        "--key=KAN-68",
        "--body=on it",
    ]


async def test_a_transition_never_waits_for_a_person_at_a_terminal(spawn: _Recorder) -> None:
    """Without ``--yes`` acli prompts, and there is nobody at this terminal."""
    argv = await _argv_for(
        "jira_transition_issue", {"key": "KAN-68", "status": "In Progress"}, spawn
    )

    assert argv == [
        "acli",
        "jira",
        "workitem",
        "transition",
        "--json",
        "--yes",
        "--key=KAN-68",
        "--status=In Progress",
    ]


# --- the properties every one of them has to keep ------------------------------


def test_every_command_asks_for_json() -> None:
    """Without it acli prints its site banner to stdout and every verb fails on success."""
    commands = _manifest().config["cli"]["commands"]

    for command in commands:
        assert "--json" in command["argv"], f"{command['tool']} would get the site banner"


def test_no_read_only_tool_reaches_a_verb_that_changes_something() -> None:
    """The classification is asserted against what acli would do, not against a tag."""
    for command in _manifest().config["cli"]["commands"]:
        if command["classification"] != "read_only":
            continue
        reached = _WRITE_VERBS.intersection(command["argv"])
        assert not reached, f"{command['tool']} is read_only but runs acli {sorted(reached)}"


def test_the_two_verbs_that_speak_to_an_audience_are_the_tagged_ones() -> None:
    """The federal refusal fires because these lines exist; it must be these two."""
    tagged = {
        tool.name
        for tool in _manifest().tools.declared
        if "network_egress" in tool.capability_tags
    }

    assert tagged == {"jira_create_issue", "jira_add_comment"}


# --- signing in ----------------------------------------------------------------


class _Sink:
    def __init__(self) -> None:
        self.events: list[AuditEvent] = []

    def write(self, event: AuditEvent) -> None:
        self.events.append(event)


async def test_the_token_reaches_the_login_on_stdin_and_never_on_argv(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The bundle's real token_command, with the site and address really filled in."""
    recorder = _Recorder(stdout=b"")
    monkeypatch.setattr(asyncio, "create_subprocess_exec", recorder)
    requirement = _manifest().host_requires[0]

    await run_token_login(
        requirement,
        token=_SENTINEL,
        caller_did=_CALLER,
        audit_sink=_Sink(),
        tier=Tier.PERSONAL,
        values={"site": _SITE, "email": _EMAIL},
    )

    assert recorder.argv == [
        "acli",
        "jira",
        "auth",
        "login",
        f"--site={_SITE}",
        f"--email={_EMAIL}",
        "--token",
    ]
    assert _SENTINEL not in " ".join(recorder.argv)
    assert recorder.stdin == _SENTINEL.encode()


async def test_a_sign_in_arc_cannot_finish_is_never_attempted_for_this_bundle() -> None:
    """It can, and the manifest says so — a button that hangs is worse than none."""
    assert _manifest().host_requires[0].token_command.startswith("acli jira auth login")


#: Measured on the deployment, in both states, minutes apart. Kept beside the
#: predicate that reads it so the evidence and the rule cannot drift apart.
_SIGNED_OUT = (1, "✗ Error: unauthorized: use 'acli jira auth login' to authenticate")
_SIGNED_IN = (
    0,
    "✓ Authenticated\n  Site: ctgfederal.atlassian.net\n"
    "  Email: jschultz@ctgfederal.com\n  Authentication Type: api_token",
)


def test_the_sign_in_check_tells_the_two_real_states_apart() -> None:
    requirement = _manifest().host_requires[0]

    assert not authorization_verdict(requirement, *_SIGNED_OUT)
    assert authorization_verdict(requirement, *_SIGNED_IN)


def test_the_signed_out_wording_alone_could_never_be_read_as_signed_in() -> None:
    """Defence in depth: neither an exit-code change nor a reworded error suffices.

    The refusal contains "authenticate"; the pattern is "Authentication Type:",
    which only an account can produce. So an acli that started exiting 0 on a
    signed-out check would still be read correctly.
    """
    requirement = _manifest().host_requires[0]

    assert not authorization_verdict(requirement, 0, _SIGNED_OUT[1])


def test_arc_stores_no_copy_of_the_token() -> None:
    """acli owns its credential; a second copy is a second place to leak it from."""
    declared = _manifest().secrets

    assert {field.name for field in declared} == {"site", "email"}
    assert not any(field.sensitive for field in declared)


def test_the_binary_this_bundle_runs_is_the_one_it_pins() -> None:
    """A manifest naming one binary and pinning another installs the wrong thing."""
    manifest = _manifest()
    pin = manifest.artifact

    assert pin is not None
    assert manifest.config["cli"]["binary"] == "acli"
    assert manifest.host_requires[0].name == "acli"
    assert {build.member for build in pin.platforms.values()} == {"acli"}


def test_the_pinned_download_is_the_executable_rather_than_an_archive() -> None:
    """Atlassian publishes no archive and no checksums file; the URL is the binary."""
    pin = _manifest().artifact

    assert pin is not None
    for build in pin.platforms.values():
        assert build.url.endswith("/acli")
        assert not build.url.endswith((".zip", ".tar.gz", ".tgz"))


def test_this_bundle_ships_no_transport_code_of_its_own() -> None:
    """The whole point: Atlassian's client owns the wire, so we own none of it."""
    assert sorted(path.name for path in BUNDLE.iterdir()) == ["extension.toml", "skills"]
    assert not list(BUNDLE.rglob("*.py"))
