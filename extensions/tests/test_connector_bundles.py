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
from arcagent.extension.manifest import ExtensionManifest, load_manifest
from arcagent.extension.secrets import LocalFileSecretBackend, SecretStore
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


def test_a_third_party_artifact_is_pinned_to_one_build(manifest: ExtensionManifest) -> None:
    """Every bundle naming a third-party artifact pins a version and a digest.

    ``ArtifactPin`` enforces the shapes; this asserts the bundle bothered to
    declare one. Bundles whose only dependency is Arc's own httpx have nothing
    third-party to pin and are exempt.
    """
    if manifest.artifact is None:
        assert not manifest.host_requires, (
            f"{manifest.extension.name} directs a host install but pins no artifact"
        )
        return
    assert manifest.artifact.version and manifest.artifact.sha256


def test_a_host_prerequisite_is_directed_with_an_instruction(
    manifest: ExtensionManifest,
) -> None:
    """Arc shows the instruction and never runs it (REQ-262), so it must be real."""
    for requirement in manifest.host_requires:
        assert len(requirement.instruction) > 40, requirement.name


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
    build_attachment(manifest, bundle)  # refuses an unbuildable config
    commands = _cli_commands(manifest)
    assert {command.tool for command in commands} == set(_declared_tags(manifest))
    for command in commands:
        assert sorted(command.capability_tags) == _declared_tags(manifest)[command.tool]
        assert command.classification == next(
            tool.classification for tool in manifest.tools.declared if tool.name == command.tool
        )


def test_a_cli_command_never_takes_a_bare_positional(manifest: ExtensionManifest) -> None:
    """Every declared argument is passed under a ``--flag``.

    ``CliArgument.token`` renders ``--flag=value`` and nothing else, so a manifest
    that expects a positional produces a token the binary reads as a flag. The
    bundles are written around that; this keeps them there.
    """
    if manifest.extension.attachment != "cli":
        pytest.skip("not a CLI bundle")
    for command in _cli_commands(manifest):
        for argument in command.arguments:
            assert argument.flag.startswith("--"), f"{command.tool}: {argument.name}"


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
    attachment = build_attachment(manifest, bundle)
    served = {spec.name: spec for spec in await attachment.describe_tools()}
    assert sorted(served) == sorted(_declared_tags(manifest))
    for tool in manifest.tools.declared:
        assert served[tool.name].classification == tool.classification
        assert sorted(served[tool.name].capability_tags) == sorted(tool.capability_tags)


async def test_a_native_bundle_without_its_credentials_refuses_by_name(
    bundle: Path, manifest: ExtensionManifest, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unconfigured connection must fail the probe, not attach and 401 later.

    Clearing exactly ``ARC_<EXTENSION>_<SECRET>`` is also the assertion that the
    adapters read that naming and no other: if one of them reached for a
    differently named variable, this test would keep passing on a clean machine
    and start failing on the operator's, which is the wrong way round.
    """
    if manifest.extension.attachment != "native":
        pytest.skip("not a native bundle")
    for secret in manifest.secrets:
        monkeypatch.delenv(f"ARC_{manifest.extension.name.upper()}_{secret.name.upper()}", False)
    attachment = build_attachment(manifest, bundle)
    result = await attachment.probe()
    assert not result.reachable
    assert manifest.extension.name in result.detail


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
        extensions_root=EXTENSIONS_ROOT,
    )


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

    with pytest.raises(ExtensionError) as raised:
        await install_connector(
            _plan(path, manifest, Tier.FEDERAL),
            agent_dir=tmp_path,
            agent="bundle_agent",
            secret_values={secret.name: "unused" for secret in manifest.secrets},
            store=store,
            caller_did=_CALLER,
        )

    error = raised.value
    sending = [tool.name for tool in manifest.tools.declared if is_egress(tool.capability_tags)]
    assert error.details["step"] == "manifest"
    assert error.details["tool"] in sending
    assert "federal" in error.message
    assert not (tmp_path / "arc.env").exists(), "a refused install must write nothing"


@pytest.mark.parametrize("path", EGRESS_BUNDLES, ids=[p.name for p in EGRESS_BUNDLES])
async def test_the_same_bundle_passes_the_gate_at_personal(path: Path, tmp_path: Path) -> None:
    """The refusal above must be the tier's, not the bundle being malformed.

    Personal permits egress from any origin, so the gate lets this through and the
    install fails later — at ``verify`` or ``probe`` — for reasons that are not the
    egress verdict.
    """
    manifest = _manifest_at(path, Tier.PERSONAL)
    store = SecretStore(LocalFileSecretBackend(tmp_path / "arc.env"))

    with pytest.raises(ExtensionError) as raised:
        await install_connector(
            _plan(path, manifest, Tier.PERSONAL),
            agent_dir=tmp_path,
            agent="bundle_agent",
            secret_values={secret.name: "unused" for secret in manifest.secrets},
            store=store,
            caller_did=_CALLER,
        )

    assert raised.value.details["step"] != "manifest"


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

    with pytest.raises(ExtensionError) as raised:
        await install_connector(
            _plan(path, manifest, Tier.FEDERAL),
            agent_dir=tmp_path,
            agent="bundle_agent",
            secret_values={secret.name: "unused" for secret in manifest.secrets},
            store=store,
            caller_did=_CALLER,
        )

    assert raised.value.details["step"] != "manifest"
