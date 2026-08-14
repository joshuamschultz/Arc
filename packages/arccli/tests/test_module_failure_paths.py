"""``arc module`` — the paths whose whole job is to fail correctly (SPEC-066).

The happy paths of this command group are covered in ``test_cli_module.py``.
What is covered here is the other half, and it is the half that matters most in
a federal deployment: the branches that exist only to refuse, to degrade, or to
report a partial result. A refusal path that has never executed is a refusal
nobody has seen work, and the failure mode of a broken one is silent — an
install that should have stopped, a tier that reads as ``personal`` because a
config could not be parsed, a removal that reports success over a module still
sitting on disk.

Three groups:

* **Fail-closed reads.** ``_configured_tier`` decides the stringency every
  signature in the deployment is verified at. An unreadable or nonsense tier
  must stop the command, never fall back to the permissive default.
* **Partial completion.** ``arc module remove`` has three legs (runtime,
  capability copies, config entry) and REQ-338 requires all three to be
  attempted, the failures named, and a non-zero exit. Both non-runtime legs are
  driven here through real failures, not stubs — a symlinked capability
  directory and a config directory that cannot be written.
* **Ergonomic refusals.** Every "you cannot mean that" exit: no agents, unknown
  or ambiguous ``--agent``, missing catalog, missing bundle, empty ``--all``,
  a malformed config that must survive untouched.

Isolation is the same two env vars the rest of the suite uses: ``ARC_CONFIG_DIR``
moves the module root, bundle store, operator key, and trust store together, and
``ARCSTORE_DATA_DIR`` moves the WORM chain. Nothing here may touch the
developer's real ``~/.arc``.
"""

from __future__ import annotations

import json
import os
import shutil
import tomllib
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import arcbundle
import pytest
from arcbundle import capability_dir
from arctrust import generate_keypair
from arctrust.paths import bundles_dir, config_file, default_operator_key_path, module_root
from arctrust.paths import trust_dir as trust_store_dir
from arctrust.trust_store import invalidate_cache

from arccli.commands import module as module_cmd

_TOOLS = b"from arcagent import tool\n\n\n@tool\ndef fetch(url: str) -> str:\n    return url\n"
_RUNTIME = b"def configure(agent):\n    return None\n"

#: Whether this process can be denied by file permissions at all. Root is not,
#: so the two permission-driven cases below would silently pass without failing.
_PERMISSIONS_BITE = os.geteuid() != 0

_AGENT_TOML = "[agent]\nname = 'josh'\n\n[security]\ntier = 'personal'\n"


# ---------------------------------------------------------------------------
# An isolated deployment
# ---------------------------------------------------------------------------


@pytest.fixture
def deployment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """A self-contained Arc deployment: config dir, store, team dir, one agent."""
    root = tmp_path / "deployment"
    (root / "arc").mkdir(parents=True)
    (root / "store").mkdir()
    monkeypatch.setenv("ARC_CONFIG_DIR", str(root / "arc"))
    monkeypatch.setenv("ARCSTORE_DATA_DIR", str(root / "store"))
    monkeypatch.chdir(root)

    agent = root / "team" / "josh_agent"
    agent.mkdir(parents=True)
    (agent / "arcagent.toml").write_text(_AGENT_TOML, encoding="utf-8")

    catalog = root / "catalog"
    browser = catalog / "browser"
    browser.mkdir(parents=True)
    (browser / "capabilities.py").write_bytes(_TOOLS)
    (browser / "_runtime.py").write_bytes(_RUNTIME)
    memory = catalog / "memory"
    memory.mkdir(parents=True)
    (memory / "capabilities.py").write_bytes(_TOOLS)
    (memory / "_runtime.py").write_bytes(_RUNTIME)
    # A module folder with nothing in it: real, reachable, and unpackageable.
    (catalog / "hollow").mkdir(parents=True)
    monkeypatch.setenv("ARC_MODULE_SOURCE", str(catalog))

    yield root


def _agent_dir(deployment: Path) -> Path:
    return deployment / "team" / "josh_agent"


def _arc_home(deployment: Path) -> Path:
    """The deployment's Arc home — what ``ARC_CONFIG_DIR`` points the CLI at."""
    return deployment / "arc"


def _module_root(deployment: Path) -> Path:
    return module_root(_arc_home(deployment))


def _bundle_store(deployment: Path) -> Path:
    return bundles_dir(_arc_home(deployment))


def _run(*args: str) -> None:
    """Invoke ``arc module <args>`` exactly as the registry dispatches it."""
    module_cmd.module_handler(list(args))


def _force_rmtree(root: Path) -> None:
    """Delete a materialized tree, widening its 0444/0555 modes first."""
    root.chmod(0o700)
    for path in root.rglob("*"):
        path.chmod(0o700)
    shutil.rmtree(root)


def _audit_actions(deployment: Path) -> list[str]:
    """Every action recorded on the deployment's real WORM chain, in order."""
    worm = deployment / "store" / "worm"
    actions: list[str] = []
    for path in sorted(worm.glob("*.jsonl")) if worm.is_dir() else []:
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                actions.append(json.loads(line)["event"]["action"])
    return actions


def _config_modules(agent_dir: Path) -> dict[str, Any]:
    data = tomllib.loads((agent_dir / "arcagent.toml").read_text(encoding="utf-8"))
    modules = data.get("modules", {})
    return modules if isinstance(modules, dict) else {}


def _resign(bundle: Path, *, issuer: str, private_key: bytes) -> None:
    """Rewrite a built bundle's manifest under a different issuer and key."""
    from arcbundle import BundleManifest, sign_manifest

    manifest = BundleManifest.model_validate_json((bundle / "manifest.json").read_bytes())
    reissued = manifest.model_copy(update={"issuer": issuer})
    (bundle / "manifest.json").write_bytes(reissued.canonical_bytes())
    (bundle / "manifest.sig").write_bytes(
        sign_manifest(reissued, private_key=private_key, issuer=issuer)
    )


# ---------------------------------------------------------------------------
# Tier — the read that decides how hard every signature is checked
# ---------------------------------------------------------------------------


def test_an_unparseable_tier_config_stops_the_command(
    deployment: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Fail closed, loudly.

    Guessing ``personal`` for a config that cannot be parsed is the one wrong
    answer available here: it would verify a development signature on a box
    whose unreadable config may well have said ``federal``.
    """
    machine_config = config_file("arcagent.toml", _arc_home(deployment))
    machine_config.parent.mkdir(parents=True, exist_ok=True)
    machine_config.write_text("[security\ntier = ", encoding="utf-8")

    with pytest.raises(SystemExit) as exit_info:
        _run("install", "--from-source", "browser")

    assert exit_info.value.code == 1
    assert "cannot read the security tier" in capsys.readouterr().err
    assert not (_module_root(deployment) / "browser").exists()


def test_an_unknown_tier_value_stops_the_command(
    deployment: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A tier nobody defined cannot be ordered against the ones that are.

    Treating an unrecognised name as the weakest tier would make ``tier =
    "fedaral"`` a silent downgrade to personal — a typo that quietly disables
    the deployment's whole stringency floor.
    """
    (_agent_dir(deployment) / "arcagent.toml").write_text(
        "[agent]\nname = 'josh'\n\n[security]\ntier = 'bogus'\n", encoding="utf-8"
    )

    with pytest.raises(SystemExit) as exit_info:
        _run("install", "--from-source", "browser")

    assert exit_info.value.code == 1
    assert "unknown tier 'bogus'" in capsys.readouterr().err
    assert not (_module_root(deployment) / "browser").exists()


def test_a_deployment_that_declares_no_tier_verifies_at_personal(deployment: Path) -> None:
    """The zero-config default, proven by what it permits rather than asserted.

    Neither config names a tier, so the verification tier is the personal
    default — and the observable consequence of that is the one thing personal
    allows and nothing else does: a development signature is accepted.
    """
    (_agent_dir(deployment) / "arcagent.toml").write_text(
        "[agent]\nname = 'josh'\n", encoding="utf-8"
    )
    assert not config_file("arcagent.toml", _arc_home(deployment)).exists()

    _run("install", "--from-source", "browser")

    assert (_module_root(deployment) / "browser" / "_runtime.py").read_bytes() == _RUNTIME


# ---------------------------------------------------------------------------
# Agent resolution — a module is enabled per agent, so the target must be exact
# ---------------------------------------------------------------------------


def test_a_deployment_with_no_agents_refuses_rather_than_guessing(
    deployment: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """There is no agent to enable a module for, and inventing one is not an option."""
    empty = tmp_path / "no-team"
    empty.mkdir()
    monkeypatch.chdir(empty)

    with pytest.raises(SystemExit) as exit_info:
        _run("remove", "browser")

    assert exit_info.value.code == 1
    assert "no agents found" in capsys.readouterr().err


def test_an_unknown_agent_is_refused_and_the_known_ones_are_named(
    deployment: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Naming the roster is what turns a refusal into a fix: the operator's next
    command is a corrected ``--agent``, not a hunt for the id."""
    with pytest.raises(SystemExit) as exit_info:
        _run("install", "--agent", "ghost", "browser")

    assert exit_info.value.code == 1
    err = capsys.readouterr().err
    assert "unknown agent 'ghost'" in err
    assert "josh" in err


def test_an_ambiguous_agent_is_refused_rather_than_defaulted(
    deployment: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Silently enabling a capability on an agent nobody named is exactly the
    excessive agency (LLM06) this command group closes."""
    second = deployment / "team" / "kate_agent"
    second.mkdir(parents=True)
    (second / "arcagent.toml").write_text(
        "[agent]\nname = 'kate'\n\n[security]\ntier = 'personal'\n", encoding="utf-8"
    )

    with pytest.raises(SystemExit) as exit_info:
        _run("install", "--from-source", "browser")

    assert exit_info.value.code == 1
    err = capsys.readouterr().err
    assert "multiple agents" in err
    assert "josh" in err and "kate" in err
    assert not (_module_root(deployment) / "browser").exists()


def test_naming_the_agent_installs_for_that_agent_and_no_other(deployment: Path) -> None:
    """The remedy the ambiguity refusal above points at must actually work — and
    it must land on one agent only. A module is enabled per agent, so an
    ``--agent`` that resolved and then enabled fleet-wide would grant the
    capability to an agent nobody named."""
    second = deployment / "team" / "kate_agent"
    second.mkdir(parents=True)
    (second / "arcagent.toml").write_text(
        "[agent]\nname = 'kate'\n\n[security]\ntier = 'personal'\n", encoding="utf-8"
    )

    _run("install", "--from-source", "browser", "--agent", "josh")

    assert _config_modules(_agent_dir(deployment))["browser"]["enabled"] is True
    assert (capability_dir(_agent_dir(deployment), "browser") / "capabilities.py").is_file()
    assert _config_modules(second) == {}
    assert not capability_dir(second, "browser").exists()


def test_list_survives_a_roster_that_cannot_be_read(
    deployment: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """``module list`` reports deployment state that is true with no agent at all.

    A broken roster costs it the per-agent column and nothing else — the
    bundled and installed columns are properties of the box, and an operator
    diagnosing the broken roster is exactly who needs to read them.
    """

    def _unreadable(**kwargs: object) -> list[object]:
        raise RuntimeError("roster unreadable")

    monkeypatch.setattr("arcgateway.team_roster.list_team", _unreadable)

    _run("list")

    out = capsys.readouterr().out
    assert "no agent resolved" in out
    assert "browser" in out, "the deployment-wide columns were lost with the roster"


def test_list_scoped_to_an_agent_reports_that_agents_enabled_column(
    deployment: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The ``--agent`` form of the listing, and the unknown-agent form beside it."""
    _run("bundle", "browser")
    _run("install", "browser")
    capsys.readouterr()

    _run("list", "--agent", "josh")
    named = capsys.readouterr().out
    _run("list", "--agent", "ghost")
    unknown = capsys.readouterr().out

    assert "Enabled (josh)" in named
    assert (
        next(line for line in named.splitlines() if line.startswith("browser")).count("yes") == 3
    )
    # An unknown agent resolves to no agent rather than to the only one present:
    # reporting some other agent's enabled set under the requested name would be
    # a wrong answer dressed as a right one.
    assert "no agent resolved" in unknown


def test_list_degrades_when_an_agent_config_fails_validation(
    deployment: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Listing must not die on a config it cannot validate.

    ``list`` is the command an operator runs to diagnose a broken deployment,
    so it degrades to "nothing reported as enabled" rather than exiting — the
    installed column, which is the one that answers "is it on this box", is
    unaffected by the agent's config at all.
    """
    _run("bundle", "browser")
    _run("install", "browser")
    (_agent_dir(deployment) / "arcagent.toml").write_text(
        "[agent]\nname = 'josh'\n\n[modules.browser]\nenabled = 'yes-please'\n", encoding="utf-8"
    )
    capsys.readouterr()

    _run("list")

    row = next(line for line in capsys.readouterr().out.splitlines() if line.startswith("browser"))
    assert row.count("yes") == 2, "an unvalidatable config was read as an enablement"


# ---------------------------------------------------------------------------
# Trust — an unusable key or store is a refusal, and says which
# ---------------------------------------------------------------------------


def test_a_tampered_operator_key_refuses_the_install_it_would_have_trusted(
    deployment: Path,
) -> None:
    """The operator key is the deployment's own trust anchor. When it cannot be
    resolved the bundle it signed is simply not trusted — absence is refusal,
    which is the fail-closed direction — and no crash escapes."""
    _run("bundle", "browser")
    key = default_operator_key_path(_arc_home(deployment))
    key.chmod(0o600)
    key.write_text("not a key at all", encoding="utf-8")

    with pytest.raises(SystemExit) as exit_info:
        _run("install", "browser")

    assert exit_info.value.code == 1
    assert not (_module_root(deployment) / "browser").exists()


def test_an_unusable_trust_store_is_reported_as_a_fault_not_a_policy_decision(
    deployment: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A syntax error in ``issuers.toml`` turns bundles away exactly like an
    absent entry does. Without the report, an operator would read a permissions
    or syntax problem as "this deployment was never approved for that module"
    and go looking in the wrong place entirely."""
    _run("bundle", "browser")
    _resign(
        _bundle_store(deployment) / "browser.arcbundle",
        issuer="did:arc:someone-else",
        private_key=generate_keypair().private_key,
    )
    trust = trust_store_dir(_arc_home(deployment))
    trust.mkdir(parents=True, exist_ok=True)
    store = trust / "issuers.toml"
    store.write_text('[issuers."did:arc:someone-else"\npublic_key = ', encoding="utf-8")
    store.chmod(0o600)
    invalidate_cache()

    with pytest.raises(SystemExit) as exit_info:
        _run("install", "browser")

    assert exit_info.value.code == 1
    assert "trust store unusable" in capsys.readouterr().err
    assert not (_module_root(deployment) / "browser").exists()


def test_a_bundle_with_an_unreadable_manifest_is_refused(deployment: Path) -> None:
    """The issuer is peeked at before verification purely to look a key up, so a
    manifest that is not even JSON must resolve to no issuer and be refused —
    not raise out of the lookup before the verifier ever gets its say."""
    _run("bundle", "browser")
    carried = _bundle_store(deployment) / "browser.arcbundle"
    (carried / "manifest.json").write_bytes(b"{not json at all")

    with pytest.raises(SystemExit) as exit_info:
        _run("install", "--from", str(carried))

    assert exit_info.value.code == 1
    assert not (_module_root(deployment) / "browser").exists()


# ---------------------------------------------------------------------------
# Audit — never interrupts the action it audits
# ---------------------------------------------------------------------------


def test_an_unopenable_audit_chain_degrades_and_says_so(
    deployment: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """NIST AU-5: auditing never interrupts the action it audits.

    The install still completes and the operator is told on stderr that it went
    unrecorded — a silent degrade would be the worse half of both options,
    leaving a real change with no record and nobody aware of the gap.
    """
    _run("bundle", "browser")

    def _unopenable(arc_dir: Path | None, data_dir: Path) -> object:
        raise OSError("chain locked by another writer")

    monkeypatch.setattr("arccli.commands.operator.operator_worm_sink", _unopenable)

    _run("install", "browser")

    assert (_module_root(deployment) / "browser" / "_runtime.py").read_bytes() == _RUNTIME
    assert _config_modules(_agent_dir(deployment))["browser"]["enabled"] is True
    assert "audit chain unavailable" in capsys.readouterr().err
    assert "module.installed" not in _audit_actions(deployment)


# ---------------------------------------------------------------------------
# install — the argv refusals
# ---------------------------------------------------------------------------


def test_install_from_a_path_that_holds_no_bundle_is_refused(
    deployment: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The air-gapped path's own typo case: media not mounted, path misspelled."""
    absent = deployment / "media" / "browser.arcbundle"

    with pytest.raises(SystemExit) as exit_info:
        _run("install", "--from", str(absent))

    assert exit_info.value.code == 1
    assert "no bundle directory at" in capsys.readouterr().err


def test_install_all_with_an_empty_bundle_store_is_refused(
    deployment: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """``--all`` of nothing is not a successful install of nothing.

    Exiting zero here would let a provisioning script that staged no bundles
    report a completed bring-up over a deployment with no modules at all.
    """
    with pytest.raises(SystemExit) as exit_info:
        _run("install", "--all")

    assert exit_info.value.code == 1
    assert "no bundles staged" in capsys.readouterr().err


def test_install_all_that_skips_everything_says_nothing_was_installed(
    deployment: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """``--all`` skips what the deployment is not permitted to install (REQ-330).

    When that is everything, the command still succeeds — no bundle was refused
    on tampering — but it must state the outcome. "Skipped" lines with no
    verdict under them read as progress.
    """
    _run("bundle", "browser", "memory")
    for name in ("browser", "memory"):
        _resign(
            _bundle_store(deployment) / f"{name}.arcbundle",
            issuer="did:arc:someone-else",
            private_key=generate_keypair().private_key,
        )
    capsys.readouterr()

    _run("install", "--all")

    out = capsys.readouterr().out
    assert "Nothing installed." in out
    assert "Skipped browser.arcbundle" in out and "Skipped memory.arcbundle" in out
    assert not (_module_root(deployment) / "browser").exists()
    assert _config_modules(_agent_dir(deployment)) == {}


def test_install_from_source_reports_a_module_it_cannot_package(
    deployment: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A source folder with no files produces no payload to sign.

    The development loop still goes the long way round — package, sign, verify —
    so a packaging failure surfaces here rather than as an unsigned install.
    """
    with pytest.raises(SystemExit) as exit_info:
        _run("install", "--from-source", "hollow")

    assert exit_info.value.code == 1
    assert "could not package hollow" in capsys.readouterr().err
    assert not (_module_root(deployment) / "hollow").exists()


def test_bundle_reports_a_module_it_cannot_package(
    deployment: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The same failure on the build host, where the bundle is actually made."""
    with pytest.raises(SystemExit) as exit_info:
        _run("bundle", "hollow")

    assert exit_info.value.code == 1
    assert "could not package hollow" in capsys.readouterr().err
    assert not (_bundle_store(deployment) / "hollow.arcbundle").exists()


def test_a_missing_source_catalog_names_the_variable_that_fixes_it(
    deployment: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Once modules leave the wheel the catalog is a checkout, and a build host
    that has not been told where it is gets the variable name, not a traceback."""
    monkeypatch.setenv("ARC_MODULE_SOURCE", str(deployment / "absent-catalog"))

    with pytest.raises(SystemExit) as exit_info:
        _run("bundle", "browser")

    assert exit_info.value.code == 1
    assert "ARC_MODULE_SOURCE" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# install_module_for_agent — the fleet-facing entry point ``arc up`` drives
# ---------------------------------------------------------------------------


def test_a_bundle_error_reaches_the_caller_as_a_module_install_error(
    deployment: Path,
) -> None:
    """The translation ``arc up`` depends on.

    A raise rather than an exit is what lets a bring-up report every agent
    instead of dying on the first refusal — but only if the refusal actually
    arrives as ``ModuleInstallError``. A ``BundleError`` escaping raw would
    abort the fleet loop, which is the exact behaviour the type exists to
    prevent. The original message is carried through so the operator still
    reads why the bundle was refused, not merely that it was.
    """
    _run("bundle", "browser")
    tampered = _bundle_store(deployment) / "browser.arcbundle" / "files" / "capabilities.py"
    tampered.write_bytes(b"# swapped after signing\n")

    with pytest.raises(module_cmd.ModuleInstallError) as exit_info:
        module_cmd.install_module_for_agent(
            "browser", agent_root=_agent_dir(deployment), agent_id="josh"
        )

    cause = exit_info.value.__cause__
    assert isinstance(cause, arcbundle.BundleError)
    assert str(exit_info.value) == str(cause)
    assert not (_module_root(deployment) / "browser").exists()


def test_a_hardened_deployment_is_told_to_stage_a_bundle_not_to_widen_its_rules(
    deployment: Path,
) -> None:
    """Above personal there is no development path, and the message has to name
    the alternative. An operator whose only stated option is "the signature was
    refused" reaches for the trust rules; one who is told to bundle on the low
    side and stage it does the correct thing instead."""
    (_agent_dir(deployment) / "arcagent.toml").write_text(
        "[agent]\nname = 'josh'\n\n[security]\ntier = 'federal'\n", encoding="utf-8"
    )

    with pytest.raises(module_cmd.ModuleInstallError) as exit_info:
        module_cmd.install_module_for_agent(
            "browser", agent_root=_agent_dir(deployment), agent_id="josh"
        )

    message = str(exit_info.value)
    assert "federal" in message
    assert "arc module bundle browser" in message
    assert not (_module_root(deployment) / "browser").exists()


def test_a_module_with_neither_a_bundle_nor_a_source_is_refused(
    deployment: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Nothing staged and nothing to build from: there is no third source, and
    the remedy names both of the two."""
    monkeypatch.setenv("ARC_MODULE_SOURCE", str(deployment / "absent-catalog"))

    with pytest.raises(module_cmd.ModuleInstallError) as exit_info:
        module_cmd.install_module_for_agent(
            "browser", agent_root=_agent_dir(deployment), agent_id="josh"
        )

    message = str(exit_info.value)
    assert "arc module bundle browser" in message
    assert "ARC_MODULE_SOURCE" in message


# ---------------------------------------------------------------------------
# The config document — enabling is a write, and a bad file is not overwritten
# ---------------------------------------------------------------------------


def test_enabling_a_module_creates_a_config_that_does_not_exist_yet(tmp_path: Path) -> None:
    """Install and enable are one step, so enabling cannot require a file to
    already be there."""
    config = tmp_path / "fresh" / "arcagent.toml"

    module_cmd.enable_module(config, "browser")

    assert (
        tomllib.loads(config.read_text(encoding="utf-8"))["modules"]["browser"]["enabled"] is True
    )


def test_a_malformed_config_is_refused_and_left_byte_identical(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Rewriting a config the parser could not read would destroy whatever the
    operator actually had there — including, on a real box, the identity and
    security blocks that were never the problem."""
    config = tmp_path / "arcagent.toml"
    original = b"[modules.browser\nenabled = true\n"
    config.write_bytes(original)

    with pytest.raises(SystemExit) as exit_info:
        module_cmd.disable_module(config, "browser")

    assert exit_info.value.code == 1
    assert config.read_bytes() == original
    assert "cannot parse" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# remove — REQ-338, all three legs, each failing on its own
# ---------------------------------------------------------------------------


def test_remove_names_the_capabilities_leg_when_the_copy_cannot_be_deleted(
    deployment: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """REQ-338 for the second leg.

    A symlink where the capability copy should be is refused rather than
    followed — deleting through it would remove whatever it points at. The
    removal cannot complete, so the command must exit non-zero naming
    *capabilities*, while still reporting the two legs that did complete: an
    operator who is told only "removal failed" has no idea what is left.
    """
    _run("bundle", "browser")
    _run("install", "browser")
    copied = capability_dir(_agent_dir(deployment), "browser")
    _force_rmtree(copied)
    copied.symlink_to(deployment / "arc")

    with pytest.raises(SystemExit) as exit_info:
        _run("remove", "browser")

    assert exit_info.value.code == 1
    err = capsys.readouterr().err
    assert "capabilities:" in err
    assert "runtime" in err and "config entry" in err, "the completed legs went unreported"
    # The other two legs really did happen, rather than merely being claimed.
    assert not (_module_root(deployment) / "browser").exists()
    assert "browser" not in _config_modules(_agent_dir(deployment))
    assert copied.is_symlink(), "the symlink was followed and deleted"
    assert (deployment / "arc").is_dir(), "removal reached through the symlink"


@pytest.mark.skipif(not _PERMISSIONS_BITE, reason="running as root: file modes deny nothing")
def test_remove_names_the_config_leg_when_the_entry_cannot_be_dropped(
    deployment: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """REQ-338 for the third leg.

    The config entry is what makes a module load. A removal that deleted the
    runtime and the copies but left ``[modules.browser]`` behind would leave the
    agent naming a capability whose files are gone — so this exits non-zero and
    names *config entry*, even though two thirds of the work succeeded.
    """
    _run("bundle", "browser")
    _run("install", "browser")
    agent = _agent_dir(deployment)
    agent.chmod(0o500)  # readable, listable, not writable: no new temp file
    try:
        with pytest.raises(SystemExit) as exit_info:
            _run("remove", "browser")
    finally:
        agent.chmod(0o700)

    assert exit_info.value.code == 1
    err = capsys.readouterr().err
    assert "config entry:" in err
    assert "removed: runtime, capabilities" in err
    # The entry really did survive — the failure report is not a formality.
    assert "browser" in _config_modules(agent)
    assert not (_module_root(deployment) / "browser").exists()
    assert not capability_dir(agent, "browser").exists()
