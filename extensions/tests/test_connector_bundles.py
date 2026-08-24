"""Every shipped connector bundle, driven through the real extension mechanism.

Nothing here reimplements a check. The manifests go through
:func:`arcagent.extension.manifest.load_manifest`, the CLI bundles are built by
:func:`arcagent.modules.connectors.install.build_attachment`, the skills go
through :func:`arcagent.capabilities.skill_validator.validate_skill_folder`, and
the federal refusal is taken by ``install_connector`` itself. A bundle that
passes here is a bundle the shipped code accepts.

The suite is parametrised off the filesystem, so a bundle added tomorrow is
covered by every rule below without touching this file.

Two assertions are worth naming, because a green suite without them would be
false confidence:

* **Every capability tag must exist in the deployment tag map.** ``external_comms``
  is the trifecta LEG; ``network_egress`` is the TAG that lights it. A manifest
  tagging a sending verb ``external_comms`` reads as security-conscious, resolves
  to no legs at all, and walks straight past the D-580 egress gate. This test is
  what makes that typo impossible to ship.
* **A CLI command's tags must equal its declared tags.** The install path checks
  the manifest declarations first and the LIVE probe result second, and those come
  from different tables. Tag a send in one and not the other and the gate is
  half-armed.
"""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any

import pytest
from arcagent.capabilities.skill_validator import validate_skill_folder
from arcagent.core.errors import ExtensionError
from arcagent.core.session_internal.capability_ledger import TAG_TO_LEGS
from arcagent.core.tier import Tier
from arcagent.extension.cli_attachment import CliCommand
from arcagent.extension.field_formats import normalize
from arcagent.extension.grants import ConnectionRegistry
from arcagent.extension.host_login import authorization_verdict
from arcagent.extension.manifest import ExtensionManifest, load_manifest
from arcagent.extension.platforms import ANY_PLATFORM
from arcagent.extension.secrets import LocalFileSecretBackend, SecretStore
from arcagent.extension.state import ConnectionStateStore, open_connection_state
from arcagent.modules.connectors.install import (
    ConnectorPlan,
    build_attachment,
    install_connector,
)
from arcagent.tools._egress_policy import is_egress

#: The bundles directory this repository ships.
EXTENSIONS_ROOT = Path(__file__).resolve().parents[1]

#: Recorded as the actor on the install path's credential operations.
_CALLER = "did:arc:testorg:executor/bundles"

#: The platforms Arc is deployed on: the DGX fleet, an x86 server, and the
#: laptops it is developed on. A bundle Arc can install must hold the published
#: digest for each, or ``host-setup`` refuses on that host — correctly, and
#: uselessly.
_DEPLOYED_PLATFORMS = ("linux/arm64", "linux/amd64", "darwin/arm64", "darwin/amd64")


async def _connection_state() -> ConnectionStateStore:
    """Open test state through the current backend opener seam."""
    from arcstore.backends.memory import FakeBackend

    backend = FakeBackend()

    async def opener() -> FakeBackend:
        return backend

    return await open_connection_state(opener=opener)


def _bundles() -> list[Path]:
    """Every bundle directory, discovered rather than listed."""
    return sorted(path.parent for path in EXTENSIONS_ROOT.glob("*/extension.toml"))


BUNDLES = _bundles()
BUNDLE_IDS = [path.name for path in BUNDLES]


@pytest.fixture(params=BUNDLES, ids=BUNDLE_IDS)
def bundle(request: pytest.FixtureRequest) -> Path:
    """One bundle directory per test run."""
    path: Path = request.param
    return path


def _manifest_at(bundle: Path, tier: Tier) -> ExtensionManifest:
    """One bundle's manifest, parsed by the shipped parser at ``tier``."""
    return load_manifest((bundle / "extension.toml").read_text(encoding="utf-8"), tier=tier)


@pytest.fixture
def manifest(bundle: Path) -> ExtensionManifest:
    """The bundle's manifest, parsed by the shipped parser at personal tier."""
    return _manifest_at(bundle, Tier.PERSONAL)


def test_there_are_bundles_to_check() -> None:
    """A parametrised suite over an empty list passes and proves nothing."""
    assert len(BUNDLES) >= 8, f"expected the shipped connector bundles, found {BUNDLE_IDS}"


# --- the manifest -------------------------------------------------------------


def test_manifest_parses_and_names_itself_after_its_directory(
    bundle: Path, manifest: ExtensionManifest
) -> None:
    """The catalog turns a name into a path, so the two must agree."""
    assert manifest.extension.name == bundle.name


def test_every_bundle_describes_itself_for_a_person_choosing_from_a_list(
    manifest: ExtensionManifest,
) -> None:
    """SPEC-064 T-006 — a row with no description is a row nobody can choose.

    ``arc connector available``, the web catalog, and the TUI picker all show
    this line and nothing else about the bundle before an operator commits to
    installing it. A shipped bundle without one is undiscoverable in every
    surface at once, which is why this is asserted here rather than left to
    review.
    """
    description = manifest.extension.description
    assert description, f"{manifest.extension.name} ships no [extension].description"
    assert description.endswith("."), "written as a sentence, for a person"
    assert len(description) <= 120, "one line, not a paragraph"


def test_tool_allowlist_is_bounded_and_matches_the_declarations(
    manifest: ExtensionManifest,
) -> None:
    """An omitted or wildcard allowlist is refused above personal (REQ-268).

    Equality rather than containment in both directions: a declared tool missing
    from ``allow`` can never register, and an allowed tool with no declaration
    gets the restrictive default classification silently.
    """
    assert not manifest.tools.is_unbounded
    assert sorted(manifest.tools.allow or []) == sorted(
        tool.name for tool in manifest.tools.declared
    )


def test_every_declared_tool_carries_an_explicit_classification(bundle: Path) -> None:
    """Absent means ``state_modifying``; the manifests still say it (D-576 brief)."""
    document = tomllib.loads((bundle / "extension.toml").read_text(encoding="utf-8"))
    declared = document.get("tools", {}).get("declared", [])
    assert declared, "a bundle with no declared tools grants nothing"
    for tool in declared:
        assert tool.get("classification") in {"read_only", "state_modifying"}, tool


def test_every_capability_tag_is_one_the_deployment_map_knows(
    manifest: ExtensionManifest,
) -> None:
    """A tag outside ``TAG_TO_LEGS`` resolves to no legs and gates nothing.

    This is the ``external_comms`` trap: the leg name looks like the right thing
    to write and would silently disarm both the trifecta gate and D-580.
    """
    for tool in manifest.tools.declared:
        unknown = sorted(set(tool.capability_tags) - set(TAG_TO_LEGS))
        assert not unknown, f"{tool.name} declares tag(s) no deployment map resolves: {unknown}"


def test_no_read_only_tool_claims_to_send(manifest: ExtensionManifest) -> None:
    """A verb that egresses is doing something; ``read_only`` would be a lie."""
    for tool in manifest.tools.declared:
        if is_egress(tool.capability_tags):
            assert tool.classification == "state_modifying", tool.name


def test_a_third_party_artifact_is_pinned_to_one_build(
    bundle: Path, manifest: ExtensionManifest
) -> None:
    """Every bundle naming a third-party artifact pins a version and a digest.

    ``ArtifactPin`` enforces the shapes; this asserts the bundle bothered to
    declare one. Bundles whose only dependency is Arc's own httpx have nothing
    third-party to pin and are exempt.

    D-588 made a host binary the normal case, and not every vendor publishes bytes
    Arc can verify: 1Password ships ``op`` through apt/brew/msi with no digest
    beside the download. A bundle in that position directs the install and pins
    nothing — ``host-setup`` then answers "pins no downloadable build" and hands
    over the manual steps, which is correct. But silence must be a decision, so the
    manifest has to say so in as many words, exactly as a missing ``verify_command``
    does.
    """
    if manifest.artifact is None:
        if manifest.host_requires:
            text = (bundle / "extension.toml").read_text(encoding="utf-8")
            assert "NO `[artifact]`" in text, (
                f"{manifest.extension.name} directs a host install, pins no artifact, and "
                f"gives no reason; an operator gets a button that cannot work and no why"
            )
        return
    assert manifest.artifact.version and manifest.artifact.platforms


def test_a_downloadable_binary_is_pinned_on_every_platform_arc_deploys_to(
    manifest: ExtensionManifest,
) -> None:
    """SPEC-064 — one digest for a whole release is a pin that covers one machine.

    A sha256 is published beside ONE asset. The dropbox bundle pinned
    ``darwin_arm64`` while the deployment ran ``linux/arm64``, so the install had
    to be done by hand: a button could only have skipped verification or refused
    every host but the pinned one. A bundle whose artifact is a placeable binary
    (it names a ``member``) must therefore carry the digest for each platform Arc
    runs on, taken from that release's published checksums file.

    A platform-independent package — an npm tarball, a PyPI sdist — is exempt: it
    is keyed ``any`` and is not something Arc can place on PATH at all.
    """
    pin = manifest.artifact
    if pin is None or ANY_PLATFORM in pin.platforms:
        return
    missing = [platform for platform in _DEPLOYED_PLATFORMS if platform not in pin.platforms]
    assert not missing, f"{pin.package} has no pinned digest for {missing}"


def test_every_pinned_platform_names_a_binary_to_place_or_none_of_them_do(
    manifest: ExtensionManifest,
) -> None:
    """Half a bundle installable is a button that works on one operator's laptop."""
    pin = manifest.artifact
    if pin is None:
        return
    members = {bool(build.member) for build in pin.platforms.values()}
    assert len(members) == 1, f"{pin.package} places a binary on some platforms and not others"


def test_a_login_arc_can_finish_declares_it_and_no_other_one_does(
    manifest: ExtensionManifest,
) -> None:
    """SPEC-064 — the manifest, not a guess, decides whether a button is offered.

    ``token_command`` may only be set on the prerequisite that also carries
    ``authorize_command``: a runtime like Node has no account of its own, and a
    login command attached to it would offer a button that signs nothing in.
    """
    for required in manifest.host_requires:
        if required.token_command:
            assert required.authorize_command, (
                f"{required.name} declares a non-interactive login but no login at all"
            )
            assert required.token_command.split()[0] == required.name, (
                f"{required.name}'s token_command must invoke {required.name} and nothing else"
            )


def test_a_host_prerequisite_is_directed_with_an_instruction(
    manifest: ExtensionManifest,
) -> None:
    """Arc shows the instruction and never runs it (REQ-262), so it must be real."""
    for requirement in manifest.host_requires:
        assert len(requirement.instruction) > 40, requirement.name


def test_a_bundle_arc_holds_no_credential_for_names_the_command_that_authorises_it(
    manifest: ExtensionManifest,
) -> None:
    """A connector with no ``[[secrets]]`` must still be connectable by a human.

    Its token lives in the binary's own keyring, so Arc has nothing to prompt for
    and both surfaces used to answer "declares no credentials; nothing to supply"
    — true, and no help at all to the operator who then had a ``Connected``
    connection serving nothing. ``authorize_command`` is that operator's next
    step, so exactly one host prerequisite has to carry it: the account-holding
    binary, never the runtime it happens to need.
    """
    if manifest.secrets or not manifest.host_requires:
        return
    commands = [required.authorize_command for required in manifest.host_requires]
    assert [command for command in commands if command], (
        f"{manifest.extension.name} declares no [[secrets]] and no authorize_command, "
        f"so nothing can tell an operator how to authorise it"
    )


def test_every_credential_a_spawning_bundle_declares_has_somewhere_to_go(
    manifest: ExtensionManifest,
) -> None:
    """A stored credential that reaches no program is the defect this seam closed.

    A ``native`` bundle receives its credentials in its own adapter code, so it
    needs no placement. Every other kind reaches its service by starting a program,
    and a program takes a credential from its environment or from nowhere — which
    is why ``build_attachment`` refused a ``cli`` bundle declaring one at all
    before ``[secrets.placement]`` existed.

    Asserted over the manifests rather than only in the refusal, because the
    refusal fires at install: a bundle shipped without a placement would be
    discovered by an operator halfway through connecting it.
    """
    if manifest.extension.attachment == "native":
        return
    delivered_by_a_command = manifest.fields_named_by_commands()
    unplaced = [
        declared.name
        for declared in manifest.secrets
        if declared.placement is None and declared.name not in delivered_by_a_command
    ]
    assert not unplaced, (
        f"{manifest.extension.name} attaches as {manifest.extension.attachment!r} and "
        f"declares credential(s) {unplaced} with no [secrets.placement], so nothing "
        f"would deliver them to the program that needs them"
    )


def test_every_credential_is_prompted_for_in_words_a_person_can_follow(
    manifest: ExtensionManifest,
) -> None:
    """The prompt IS the interface: it is the only text beside the paste field.

    A field labelled "Entra ID tenant ID" tells a non-technical operator nothing
    about where to find one, and every connector that stalled half-connected
    stalled there. A ``[[secrets]]`` entry is by definition something Arc asks a
    person for, so its prompt has to name the screen it comes from and say what to
    click — which no length check can prove, but a one-clause label always fails.
    """
    for declared in manifest.secrets:
        assert len(declared.prompt) > 80, (
            f"{manifest.extension.name}.{declared.name} prompts with "
            f"{declared.prompt!r}, which does not tell anyone where to get one"
        )


#: Word fragments that mean a field carries a credential. A bundle may decide that
#: its base URL is not secret; it may not decide that about something called a
#: token. Kept in the test suite rather than in core on purpose — core must hold no
#: opinion about what a service's fields are called (REQ-280) — but the reverse
#: mistake is the catastrophic one, so it is caught before a bundle ships.
_CREDENTIAL_WORDS = ("token", "secret", "password", "passphrase", "credential", "api_key")


def test_nothing_that_reads_like_a_credential_is_declared_visible(
    manifest: ExtensionManifest,
) -> None:
    """``sensitive = false`` is an opt-out, and it must never be taken on a token.

    The flag exists so a base URL is not hidden behind password dots. Applied to a
    credential it does the opposite of its purpose: it un-masks the input, and it
    opens the read-back path, so the value would come back on a response.
    """
    for declared in manifest.secrets:
        if declared.sensitive:
            continue
        offending = [word for word in _CREDENTIAL_WORDS if word in declared.name.lower()]
        assert not offending, (
            f"{manifest.extension.name}.{declared.name} is declared non-sensitive but its "
            f"name says it is a credential ({offending[0]!r})"
        )


#: Bundles that authenticate with NOTHING, because the operating system already
#: did it. ``sqlite`` reaches a file: a local one is gated by the file's own
#: permissions and a remote one by the operator's existing ssh key, so a path and
#: a hostname are the only values it can ask for and neither is a secret.
#:
#: This is the same argument as ``token_command``, one step further — Arc stores
#: no credential at all, so there is none to leak. Named rather than inferred:
#: adding a bundle here is a deliberate act a reviewer sees, and the failure it
#: would otherwise mask (a real credential marked visible) stays caught for
#: everything else.
_HOST_AUTHORIZED = frozenset({"sqlite"})


def test_a_bundle_that_asks_for_values_asks_for_at_least_one_credential(
    manifest: ExtensionManifest,
) -> None:
    """Otherwise the opt-out was taken on the field that authorises the connection.

    ``[[secrets]]`` exists so Arc can authenticate. A bundle whose every field is
    configuration has nothing to authenticate with — which in practice means the
    credential is in that list and was marked visible by mistake.

    Unless the credential is deliberately not in that list. A bundle whose binary
    holds its own token — ``jira``, whose ``acli`` writes it into its own config —
    stores none, so its declared fields SHOULD all be configuration, and the token
    crosses once on the stdin of ``token_command``. That is the stronger position,
    not the weaker one: Arc keeps no second copy to leak.

    Or unless there is no credential to hold. See ``_HOST_AUTHORIZED``.
    """
    if not manifest.secrets:
        return
    if any(required.token_command for required in manifest.host_requires):
        return
    if manifest.extension.name in _HOST_AUTHORIZED:
        return
    assert any(declared.sensitive for declared in manifest.secrets), (
        f"{manifest.extension.name} declares only non-sensitive fields, so nothing it "
        f"asks for could authorise the connection"
    )


#: The address the operator really typed, and what it must become. A bundle asking
#: for a web address must accept it: a bare hostname is what a browser bar shows a
#: person, and the failure it produced — httpx's "Request URL is missing an
#: 'http://' or 'https://' protocol", surfaced raw at probe time — is unactionable
#: for the person who typed it.
_TYPED_ADDRESS = "ctgfederal.com.atlassian.net"


def test_a_bundle_asking_for_a_web_address_accepts_one_typed_by_a_person(
    manifest: ExtensionManifest,
) -> None:
    """Driven through the shipped normaliser, with the reported input.

    Asserted per bundle rather than once on the function, because the defect was a
    bundle that asked for a URL and declared no shape for it. A field NAME ending in
    ``_url`` / ``_uri`` / ``_endpoint`` and declaring no format fails here — the name
    is the bundle author's own word for what they are asking for, and it does not
    catch a field like ``vault_id`` whose value is merely found in an address.
    """
    for declared in manifest.secrets:
        if declared.format != "https_url":
            assert not declared.name.lower().endswith(("_url", "_uri", "_endpoint")), (
                f"{manifest.extension.name}.{declared.name} asks for a web address but "
                f"declares no format, so a bare hostname would reach the connector unusable"
            )
            continue
        assert normalize(declared.format, declared.name, _TYPED_ADDRESS) == (
            f"https://{_TYPED_ADDRESS}"
        )
        assert not declared.sensitive, (
            f"{manifest.extension.name}.{declared.name} is a web address, and hiding it "
            f"behind password dots is what stopped an operator seeing their own typo"
        )


def test_a_sign_in_check_invokes_the_binary_it_belongs_to(
    manifest: ExtensionManifest,
) -> None:
    """The same bound the login takes: the field authorises one program.

    A check also has to come with a way to DO the sign-in, and there are two of
    those — the same two ``Authorization`` models. A bundle whose binary holds its
    own credential answers with ``authorize_command``, the thing an operator types.
    A bundle Arc holds the credential for answers with ``[[secrets]]``: the operator
    pastes a token and Arc places it in the binary's environment on every call, and
    for a 1Password service account there IS no host command to name. Requiring one
    anyway would put a command in front of an operator that does not exist.
    """
    for required in manifest.host_requires:
        if required.verify_command:
            assert required.verify_command.split()[0] == required.name, (
                f"{required.name}'s verify_command must invoke {required.name} and nothing else"
            )
            assert required.authorize_command or manifest.secrets, (
                f"{required.name} declares a way to check a sign-in but no way to do one — "
                f"neither an authorize_command nor a credential for the operator to supply"
            )


#: What each shipped bundle's declared sign-in check really prints: the exit code
#: and the output a host produces in each of the two states.
#:
#: This table is the point of the field, and its entries are evidence rather than
#: illustration. ``dbxcli version`` succeeds on a dbxcli with no credential at
#: all, and the panel rendered that success as "Signed in"; a replacement command
#: that ALSO fails to tell the two states apart would be the same defect wearing
#: a new name.
#:
#: ``gog auth list`` is that trap and the reason ``verify_pattern`` exists.
#: Measured on the deployment: SIGNED OUT it prints "No tokens stored" and exits
#: **0** — the identical exit code to success. Any check reading only the exit
#: code reports an unauthorised Google Workspace as signed in, which is what the
#: operator is looking at right now. ``ms-365-mcp-server --verify-login`` has the
#: same shape: it calls process.exit(0) unconditionally and puts the answer in
#: its JSON.
#:
#: (m) marks output measured on the deployment host; the rest is taken from the
#: upstream that publishes the command.
_RECORDED_OUTPUT: dict[str, tuple[tuple[int, str], tuple[int, str]]] = {
    # bundle: (signed out, signed in)
    "dropbox": (
        # (m) exit 2, not 1 — another reason the verdict tests "not zero".
        (2, 'Error: no saved Dropbox credentials; run "dbxcli login" first'),
        (0, "Joshua Schultz\njoshua@blackarc.example\nBusiness"),
    ),
    "github": (
        (1, "You are not logged into any GitHub hosts. To log in, run: gh auth login"),
        # (m)
        (0, "github.com\n  ✓ Logged in to github.com account joshuamschultz (keyring)"),
    ),
    "onepassword": (
        # (m) Both refusals measured on the deployment; op separates them by exit
        # code too (1 with no session, 9 with an unusable token). The signed-in body
        # is the one state the deployment could not produce — it holds no valid
        # service-account token — so it is taken from op 2.35.0's own output structs
        # and the pattern matches the identity field of either account shape.
        (1, "[ERROR] 2026/08/09 14:36:55 no active session found for account joshuaschultz"),
        (
            0,
            '{"URL":"https://my.1password.com","IntegrationID":"6P4HHXFVGVCE7",'
            '"UserType":"SERVICE_ACCOUNT"}',
        ),
    ),
    "jira": (
        # (m) Both states measured on the deployment, minutes apart: signed out
        # first, then again after a token landed. `acli` does separate them by exit
        # code, but `Authentication Type:` is asserted as well — it is printed only
        # where there is an account to describe, and the refusal's own "authenticate"
        # is lowercase and carries no such label, so neither signal alone decides.
        (1, "✗ Error: unauthorized: use 'acli jira auth login' to authenticate"),
        (
            0,
            "✓ Authenticated\n  Site: ctgfederal.atlassian.net\n"
            "  Email: jschultz@ctgfederal.com\n  Authentication Type: api_token",
        ),
    ),
    "google_workspace": (
        # (m) THE case that breaks an exit-code-only check: signed out, exit 0.
        (0, "No tokens stored"),
        (0, "joshua@blackarc.example\tdefault\tgmail,drive,calendar\t2026-07-14\toauth"),
    ),
    "microsoft365": (
        (0, '{"success":false,"message":"Login failed - no token received"}'),
        (0, '{"success":true,"userData":{"displayName":"Joshua Schultz"}}'),
    ),
}


def test_a_declared_sign_in_check_really_tells_the_two_states_apart(
    manifest: ExtensionManifest,
) -> None:
    """Replayed through :func:`authorization_verdict` — the deployment's own predicate.

    A bundle whose check cannot distinguish signed-in from signed-out is a bundle
    that will report one of them wrongly, and reporting "connected" over an empty
    account is the failure this whole field exists to end.
    """
    checked = [required for required in manifest.host_requires if required.verify_command]
    if not checked:
        return
    recorded = _RECORDED_OUTPUT.get(manifest.extension.name)
    assert recorded is not None, (
        f"{manifest.extension.name} declares a verify_command with no recorded output to "
        f"prove it distinguishes the two states — add its real output to _RECORDED_OUTPUT"
    )
    (out_code, out_text), (in_code, in_text) = recorded
    for required in checked:
        assert not authorization_verdict(required, out_code, out_text)
        assert authorization_verdict(required, in_code, in_text)


def test_a_bundle_with_no_sign_in_check_is_recorded_as_a_deliberate_choice(
    bundle: Path, manifest: ExtensionManifest
) -> None:
    """Silence must be a decision, not an omission.

    A no-credential bundle with no ``verify_command`` reports its sign-in as
    unknown forever, so the manifest has to say why nothing can check it —
    ``readwise`` publishes no local command that distinguishes the two states.
    """
    if manifest.secrets or not any(r.authorize_command for r in manifest.host_requires):
        return
    if any(required.verify_command for required in manifest.host_requires):
        return
    text = (bundle / "extension.toml").read_text(encoding="utf-8")
    assert "NO `verify_command`" in text, (
        f"{manifest.extension.name} declares no verify_command and no comment saying why; "
        f"its sign-in will read as not known in every surface"
    )


# --- the attachment -----------------------------------------------------------


def _declared_tags(manifest: ExtensionManifest) -> dict[str, list[str]]:
    return {tool.name: sorted(tool.capability_tags) for tool in manifest.tools.declared}


def _cli_commands(manifest: ExtensionManifest) -> list[CliCommand]:
    raw: Any = manifest.config.get("cli", {}).get("commands", [])
    return [CliCommand.model_validate(command) for command in raw]


def test_a_cli_bundle_builds_and_its_commands_match_its_declarations(
    bundle: Path, manifest: ExtensionManifest
) -> None:
    """The two tables the egress gate reads must say the same thing.

    ``_refuse_declared_egress`` reads ``[[tools.declared]]``; ``_refuse_probed_egress``
    reads what the attachment serves, which for a CLI comes from
    ``[[config.cli.commands]]``. Tagging a send in one table only arms half the gate.
    """
    if manifest.extension.attachment != "cli":
        pytest.skip("not a CLI bundle")
    build_attachment(manifest, bundle, {})  # refuses an unbuildable config
    commands = _cli_commands(manifest)
    assert {command.tool for command in commands} == set(_declared_tags(manifest))
    for command in commands:
        assert sorted(command.capability_tags) == _declared_tags(manifest)[command.tool]
        assert command.classification == next(
            tool.classification for tool in manifest.tools.declared if tool.name == command.tool
        )


def test_a_cli_command_takes_a_positional_only_behind_a_declared_terminator(
    manifest: ExtensionManifest,
) -> None:
    """A bare value is legal only where the binary leaves no alternative, and only after ``--``.

    ``--flag=value`` is the rule, because a model's value can then never be read as
    syntax. One shipped verb cannot use it: ``acli jira workitem view`` takes the
    work item key positionally and answers ``--key`` with "unknown flag: --key".
    ``CliCommand`` refuses a positional that is not behind a ``--``, and this
    asserts the same thing over the bundles as they ship — a manifest is where the
    mistake would be made.
    """
    if manifest.extension.attachment != "cli":
        pytest.skip("not a CLI bundle")
    for command in _cli_commands(manifest):
        positional = [argument.name for argument in command.arguments if not argument.flag]
        if not positional:
            for argument in command.arguments:
                assert argument.flag.startswith("--"), f"{command.tool}: {argument.name}"
            continue
        assert command.argv[-1] == "--", (
            f"{command.tool} takes {positional} positionally without ending its argv "
            f"in '--', so the value occupies a flag position"
        )


async def test_a_native_bundle_serves_exactly_what_its_manifest_declares(
    bundle: Path, manifest: ExtensionManifest
) -> None:
    """The implementation's own tool list must agree with the manifest's grant.

    A native attachment answers ``describe_tools`` from its own code, so this is
    where an adapter drifting from its manifest shows up — and the manifest is
    what the egress gate and the registry read.
    """
    if manifest.extension.attachment != "native":
        pytest.skip("not a native bundle")
    attachment = build_attachment(manifest, bundle, {})
    served = {spec.name: spec for spec in await attachment.describe_tools()}
    assert sorted(served) == sorted(_declared_tags(manifest))
    for tool in manifest.tools.declared:
        assert served[tool.name].classification == tool.classification
        assert sorted(served[tool.name].capability_tags) == sorted(tool.capability_tags)


async def test_a_native_bundle_without_its_credentials_refuses_by_name(
    bundle: Path, manifest: ExtensionManifest, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unconfigured connection must fail the probe, not attach and 401 later.

    The environment is deliberately POISONED with the variable each adapter used to
    fall back to. The store is the one path a credential travels: it is audited,
    per-instance, and tier-selected, where ``ARC_<EXTENSION>_<SECRET>`` is none of
    those — one variable would silently serve two connected accounts of the same
    bundle and would bypass the vault a federal deployment configured. So a bundle
    handed no credentials must refuse **even with those variables set**, and it must
    name the fields it is missing.
    """
    if manifest.extension.attachment != "native":
        pytest.skip("not a native bundle")
    for secret in manifest.secrets:
        monkeypatch.setenv(
            f"ARC_{manifest.extension.name.upper()}_{secret.name.upper()}", "from-the-environment"
        )

    result = await build_attachment(manifest, bundle, {}).probe()

    assert not result.reachable
    assert manifest.extension.name in result.detail
    for secret in manifest.secrets:
        assert secret.name in result.detail, "a refusal must name the field it is missing"


def test_native_implementation_lives_below_the_bundle_root(
    bundle: Path, manifest: ExtensionManifest
) -> None:
    """Adapter code must not sit where the capability scanner reads tools.

    ``_scan_root`` registers every top-level ``.py`` as an agent tool candidate and
    import-policy-gates it by tier (federal permits ``__future__`` and ``arcagent``
    only), so an adapter at the bundle root would be refused at federal.
    """
    if manifest.extension.attachment != "native":
        pytest.skip("not a native bundle")
    assert not list(bundle.glob("*.py"))
    entrypoint = str(manifest.config["native"]["entrypoint"])
    assert (bundle / entrypoint).is_dir()


def test_native_entrypoints_do_not_collide_across_bundles() -> None:
    """``sys.modules`` caches by bare name, so two bundles cannot share one."""
    entrypoints = [
        _manifest_at(path, Tier.PERSONAL).config.get("native", {}).get("entrypoint")
        for path in BUNDLES
    ]
    named = [name for name in entrypoints if name]
    assert len(named) == len(set(named)), named


# --- the skill ----------------------------------------------------------------


def test_every_bundle_ships_a_skill_the_real_validator_accepts(bundle: Path) -> None:
    """D-570 part three. The skill teaches judgment; the manifest grants authority."""
    folders = sorted(path.parent for path in bundle.glob("skills/*/SKILL.md"))
    assert folders, f"{bundle.name} ships no skill"
    for folder in folders:
        result = validate_skill_folder(folder, f"extension:{bundle.name}-skills")
        assert result.ok, [(error.code, error.detail) for error in result.errors]


def test_the_skill_names_only_verbs_the_manifest_grants(
    bundle: Path, manifest: ExtensionManifest
) -> None:
    """A skill teaching a verb nobody granted teaches a call that cannot happen."""
    granted = {tool.name for tool in manifest.tools.declared}
    for path in bundle.glob("skills/*/SKILL.md"):
        text = path.read_text(encoding="utf-8")
        for line in text.splitlines():
            if not line.startswith("| `"):
                continue
            verb = line.split("`")[1]
            assert verb in granted, f"{path.name} documents ungranted verb {verb!r}"


# --- the egress gate (D-580) --------------------------------------------------


def _plan(bundle: Path, manifest: ExtensionManifest, tier: Tier) -> ConnectorPlan:
    """A plan built from the real manifest, so the gate reads real declarations."""
    return ConnectorPlan(
        instance="primary",
        extension=manifest.extension.name,
        bundle=bundle,
        manifest=manifest,
        unsatisfied_host=(),
        secrets=tuple(manifest.secrets),
        approval_mode=manifest.approval.default,
        tier=tier,
        extensions_root=(EXTENSIONS_ROOT,),
    )


#: A value each declared shape accepts. The subject of the tests below is the
#: egress gate, so a placeholder that a field's own format would refuse would stop
#: them at the ``secrets`` step and prove nothing about the gate.
_SHAPED_PLACEHOLDER: dict[str, str] = {
    "": "unused",
    "https_url": "unused.example.net",
    "api_token": "unused",
    "email": "unused@example.net",
}


def _placeholders(manifest: ExtensionManifest) -> dict[str, str]:
    """One acceptable value per declared credential, keyed by field name."""
    return {declared.name: _SHAPED_PLACEHOLDER[declared.format] for declared in manifest.secrets}


def _egress_bundles() -> list[Path]:
    """Bundles declaring at least one sending verb."""
    sending = []
    for path in BUNDLES:
        parsed = _manifest_at(path, Tier.PERSONAL)
        if any(is_egress(tool.capability_tags) for tool in parsed.tools.declared):
            sending.append(path)
    return sending


EGRESS_BUNDLES = _egress_bundles()


def test_some_bundle_actually_declares_a_send() -> None:
    """Without one, the federal refusal below would pass vacuously."""
    assert EGRESS_BUNDLES, "no bundle declares an egress tool; the gate test proves nothing"


@pytest.mark.parametrize("path", EGRESS_BUNDLES, ids=[p.name for p in EGRESS_BUNDLES])
async def test_an_egress_bundle_is_refused_at_federal_before_anything_is_written(
    path: Path, tmp_path: Path
) -> None:
    """D-580: at federal an extension may read and may never send.

    Driven through ``install_connector`` rather than the predicate, because the
    decision that matters is that the refusal lands at INSTALL — before a
    credential is collected and before the agent is ever offered the verb. The
    error names the offending tool so an operator can act on it.
    """
    manifest = _manifest_at(path, Tier.FEDERAL)
    store = SecretStore(LocalFileSecretBackend(tmp_path / "arc.env"))
    registry = ConnectionRegistry(tmp_path)

    with pytest.raises(ExtensionError) as raised:
        await install_connector(
            _plan(path, manifest, Tier.FEDERAL),
            connections=registry,
            agents=("bundle_agent",),
            secret_values=_placeholders(manifest),
            store=store,
            caller_did=_CALLER,
            state=await _connection_state(),
        )

    error = raised.value
    sending = [tool.name for tool in manifest.tools.declared if is_egress(tool.capability_tags)]
    assert error.details["step"] == "manifest"
    assert error.details["tool"] in sending
    assert "federal" in error.message
    assert not (tmp_path / "arc.env").exists(), "a refused install must write nothing"
    # The grant list is the other thing an install writes, and the one that
    # decides access: a refused install leaving one behind would be a connection
    # nobody approved, already handed to an agent.
    assert not registry.path.exists(), "a refused install must define no connection"


@pytest.mark.parametrize("path", EGRESS_BUNDLES, ids=[p.name for p in EGRESS_BUNDLES])
async def test_the_same_bundle_passes_the_gate_at_personal(path: Path, tmp_path: Path) -> None:
    """The refusal above must be the tier's, not the bundle being malformed.

    Personal permits egress from any origin, so the egress gate — which is the
    ``manifest`` step, and the first thing ``install_connector`` runs — is never
    what stops this. Whether the install then completes or fails at a later step
    depends on whether a fabricated credential happens to reach the real service,
    which is not this test's subject: the assertion is that ``manifest`` is not
    the reason.
    """
    manifest = _manifest_at(path, Tier.PERSONAL)
    store = SecretStore(LocalFileSecretBackend(tmp_path / "arc.env"))

    try:
        await install_connector(
            _plan(path, manifest, Tier.PERSONAL),
            connections=ConnectionRegistry(tmp_path),
            agents=("bundle_agent",),
            secret_values=_placeholders(manifest),
            store=store,
            caller_did=_CALLER,
            state=await _connection_state(),
        )
    except ExtensionError as refused:
        assert refused.details["step"] != "manifest"


@pytest.mark.parametrize(
    "path",
    [p for p in BUNDLES if p not in EGRESS_BUNDLES],
    ids=[p.name for p in BUNDLES if p not in EGRESS_BUNDLES],
)
async def test_a_read_only_bundle_clears_the_federal_egress_gate(
    path: Path, tmp_path: Path
) -> None:
    """Federal reads freely. A bundle with no send must not be caught by the gate."""
    manifest = _manifest_at(path, Tier.FEDERAL)
    store = SecretStore(LocalFileSecretBackend(tmp_path / "arc.env"))

    try:
        await install_connector(
            _plan(path, manifest, Tier.FEDERAL),
            connections=ConnectionRegistry(tmp_path),
            agents=("bundle_agent",),
            secret_values=_placeholders(manifest),
            store=store,
            caller_did=_CALLER,
            state=await _connection_state(),
        )
    except ExtensionError as refused:
        assert refused.details["step"] != "manifest"


def test_every_bundle_calls_itself_what_its_vendor_calls_it(
    manifest: ExtensionManifest,
) -> None:
    """SPEC-064 — ``onepassword`` is a coordinate, ``1Password`` is a product.

    ``name`` is validated, lowercase, and used for paths, config keys, secret refs
    and CLI arguments, so it cannot carry a capital or a space — which is why every
    surface was showing an operator ``onepassword``, ``google_workspace`` and
    ``microsoft365``. A person recognises a product's own spelling or they do not.

    Asserted per bundle rather than left to review because the fallback is silent:
    a bundle that forgets renders its coordinate and nothing looks broken.
    """
    label = manifest.extension.display_name
    assert label, f"{manifest.extension.name} declares no display_name"
    assert label == label.strip()
    assert manifest.extension.label == label


def test_a_display_name_never_replaces_the_coordinate(manifest: ExtensionManifest) -> None:
    """The two are different things and a surface must not be able to confuse them."""
    assert manifest.extension.name == manifest.extension.name.lower()
    assert " " not in manifest.extension.name


def test_a_host_authorized_bundle_really_stores_no_credential() -> None:
    """The exemption above must stay narrow: no sensitive field, and no host token.

    Without this, ``_HOST_AUTHORIZED`` would be a place to hide a bundle that
    does have a credential and marked it visible — which is exactly the defect
    the exempted test exists to catch.
    """
    for name in _HOST_AUTHORIZED:
        path = next(p for p in BUNDLES if p.name == name)
        manifest = _manifest_at(path, Tier.PERSONAL)
        assert not any(declared.sensitive for declared in manifest.secrets), (
            f"{name} is listed as host-authorized but declares a credential"
        )
        assert not any(required.token_command for required in manifest.host_requires), (
            f"{name} is listed as host-authorized but takes a token from a binary"
        )


def test_no_bundle_imports_itself_through_the_repository(bundle: Path) -> None:
    """A bundle is COPIED to a deployment; the repository is not there with it.

    An installed bundle lives at ``<extensions>/<name>/`` with only that directory
    on ``sys.path``, so ``from extensions.sqlite.arc_ext_sqlite...`` resolves
    perfectly in this checkout and fails on every real box with "No module named
    'extensions'". Caught on a live deploy, at the moment an operator pressed
    connect.

    Checked by reading the source rather than by importing, because importing it
    HERE is exactly what makes the defect invisible.
    """
    offenders = []
    for path in bundle.rglob("*.py"):
        if "__pycache__" in path.parts:
            continue
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            stripped = line.strip()
            if stripped.startswith(("from extensions.", "import extensions.")):
                offenders.append(f"{path.relative_to(bundle)}:{number}")
    assert not offenders, (
        f"{bundle.name} imports itself through the repository, which a deployment "
        f"does not have: {offenders}. Use a relative import."
    )
