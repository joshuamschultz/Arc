"""``arc module`` — the operator lifecycle for signed module bundles (SPEC-066).

Everything here runs against a real, isolated deployment: a real operator key, a
real ``arcbundle`` build, a real Ed25519 verification, a real materialize under
``ARC_CONFIG_DIR``, and the real WORM audit chain. Nothing about the trust path
is stubbed, because a stub would let a broken gate pass.

Every subcommand is exercised in its **bare** form as well as with flags. An
omitted argument contributes no token to a `--help` string, so the shape most
likely to be broken is the one nobody typed in a test.
"""

from __future__ import annotations

import base64
import hashlib
import json
import shutil
import tomllib
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from arcbundle import capability_dir
from arctrust import OperatorKey, generate_keypair
from arctrust.trust_store import invalidate_cache

from arccli.commands import module as module_cmd

_TOOLS = b"from arcagent import tool\n\n\n@tool\ndef fetch(url: str) -> str:\n    return url\n"
_RUNTIME = b"def configure(agent):\n    return None\n"
_SKILL = b"---\nname: browse\n---\n\nHow to browse.\n"


# ---------------------------------------------------------------------------
# An isolated deployment
# ---------------------------------------------------------------------------


@pytest.fixture
def deployment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """A self-contained Arc deployment: config dir, store, team dir, one agent.

    ``ARC_CONFIG_DIR`` moves the module root, the bundle store, the operator key,
    and the trust store together; ``ARCSTORE_DATA_DIR`` moves the WORM chain. The
    cwd is the deployment root because that is how the team dir is discovered.
    """
    root = tmp_path / "deployment"
    (root / "arc").mkdir(parents=True)
    (root / "store").mkdir()
    monkeypatch.setenv("ARC_CONFIG_DIR", str(root / "arc"))
    monkeypatch.setenv("ARCSTORE_DATA_DIR", str(root / "store"))
    monkeypatch.chdir(root)

    agent = root / "team" / "josh_agent"
    agent.mkdir(parents=True)
    (agent / "arcagent.toml").write_text(
        "[agent]\nname = 'josh'\n\n[security]\ntier = 'personal'\n", encoding="utf-8"
    )
    _source_catalog(root, monkeypatch)
    yield root


def _source_catalog(root: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A two-module source catalog, pointed at by the build-host env override."""
    catalog = root / "catalog"
    browser = catalog / "browser"
    (browser / "skills" / "browse").mkdir(parents=True)
    (browser / "capabilities.py").write_bytes(_TOOLS)
    (browser / "_runtime.py").write_bytes(_RUNTIME)
    (browser / "config.py").write_bytes(b"TIMEOUT = 30\n")
    (browser / "skills" / "browse" / "SKILL.md").write_bytes(_SKILL)

    memory = catalog / "memory"
    memory.mkdir(parents=True)
    (memory / "capabilities.py").write_bytes(_TOOLS)
    (memory / "_runtime.py").write_bytes(_RUNTIME)

    monkeypatch.setenv("ARC_MODULE_SOURCE", str(catalog))
    return catalog


def _agent_dir(deployment: Path) -> Path:
    return deployment / "team" / "josh_agent"


def _module_root(deployment: Path) -> Path:
    return deployment / "arc" / "modules"


def _bundle_store(deployment: Path) -> Path:
    return deployment / "arc" / "bundles"


def _run(*args: str) -> None:
    """Invoke ``arc module <args>`` exactly as the registry dispatches it."""
    module_cmd.module_handler(list(args))


def _snapshot(root: Path) -> dict[str, str | None]:
    """Capture a tree as ``relative path -> sha256`` (None for a directory)."""
    if not root.exists():
        return {}
    captured: dict[str, str | None] = {}
    for entry in sorted(root.rglob("*")):
        key = str(entry.relative_to(root))
        captured[key] = hashlib.sha256(entry.read_bytes()).hexdigest() if entry.is_file() else None
    return captured


def _force_rmtree(root: Path) -> None:
    """Delete a materialized tree out of band, widening its 0444/0555 modes first.

    Simulates an operator or a stray script removing the runtime behind Arc's
    back — the state `arc module remove` must report rather than paper over.
    """
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


# ---------------------------------------------------------------------------
# bundle
# ---------------------------------------------------------------------------


def test_bundle_bare_writes_a_signed_bundle_to_the_store(deployment: Path) -> None:
    """The bare form takes no ``-o``: the default store is what makes the
    offline round trip flagless."""
    _run("bundle", "browser")

    bundle = _bundle_store(deployment) / "browser.arcbundle"
    assert (bundle / "manifest.json").is_file()
    assert (bundle / "manifest.sig").is_file()
    assert (bundle / "files" / "capabilities.py").read_bytes() == _TOOLS


def test_bundle_signs_with_the_deployment_operator_key(deployment: Path) -> None:
    """The issuer is the on-box operator DID, not an anonymous stamp — that is
    what lets install trust it without a separate registration step."""
    _run("bundle", "browser")

    manifest = json.loads(
        (_bundle_store(deployment) / "browser.arcbundle" / "manifest.json").read_bytes()
    )
    key = OperatorKey.load(deployment / "arc" / "operator" / "operator.key")
    assert manifest["issuer"].startswith("did:arc:")
    assert key.public_key  # the key really was created on the box, not assumed


def test_bundle_refuses_an_unknown_module(deployment: Path) -> None:
    with pytest.raises(SystemExit) as exit_info:
        _run("bundle", "nonexistent")
    assert exit_info.value.code == 1
    assert not (_bundle_store(deployment) / "nonexistent.arcbundle").exists()


def test_bundle_skips_an_existing_bundle_instead_of_aborting_the_batch(deployment: Path) -> None:
    """An existing bundle is left untouched and the command carries on.

    The invariant that matters is unchanged — a bundle is never silently
    half-overwritten, which would leave it verifying against neither manifest.
    What changed is the cost of hitting it: aborting the whole invocation made
    re-running after a partial failure impossible, because the operator had to
    work out by hand which names had already succeeded. The bundle bytes are
    compared before and after to prove "skipped" really means untouched.
    """
    _run("bundle", "browser")
    bundle = _bundle_store(deployment) / "browser.arcbundle"
    before = (bundle / "manifest.json").read_bytes()

    _run("bundle", "browser")  # no SystemExit: skipped, not refused

    assert (bundle / "manifest.json").read_bytes() == before, "the skip rewrote the bundle"

    _run("bundle", "browser", "--force")  # explicit replacement is still fine
    assert (bundle / "manifest.json").is_file()


def test_bundle_packages_the_good_modules_when_one_name_is_bad(deployment: Path) -> None:
    """One unusable module must not cost the others their bundles.

    The real shape is a stale source directory holding nothing but
    ``__pycache__``, or a config naming a module that no longer exists. Aborting
    the batch on it meant a single bad name took down seventeen good ones —
    which is exactly what happened on a real fleet deploy. The command still
    exits non-zero, so the failure is not swallowed.
    """
    with pytest.raises(SystemExit) as exit_info:
        _run("bundle", "browser", "nonexistent", "memory")

    assert exit_info.value.code == 1
    assert (_bundle_store(deployment) / "browser.arcbundle").is_dir()
    assert (_bundle_store(deployment) / "memory.arcbundle").is_dir()
    assert not (_bundle_store(deployment) / "nonexistent.arcbundle").exists()


def test_bundle_writes_one_bundle_per_named_module(deployment: Path) -> None:
    _run("bundle", "browser", "memory")

    assert (_bundle_store(deployment) / "browser.arcbundle").is_dir()
    assert (_bundle_store(deployment) / "memory.arcbundle").is_dir()


# ---------------------------------------------------------------------------
# install
# ---------------------------------------------------------------------------


def test_install_bare_verifies_materializes_copies_and_enables(deployment: Path) -> None:
    """The four halves of one step. A module installed but not enabled is the
    dead capability on disk this spec exists to eliminate."""
    _run("bundle", "browser")
    _run("install", "browser")

    runtime = _module_root(deployment) / "browser"
    copied = capability_dir(_agent_dir(deployment), "browser")
    assert runtime.is_dir()
    assert (runtime / "_runtime.py").read_bytes() == _RUNTIME
    assert (copied / "capabilities.py").read_bytes() == _TOOLS
    assert (copied / "skills" / "browse" / "SKILL.md").read_bytes() == _SKILL
    assert _config_modules(_agent_dir(deployment))["browser"]["enabled"] is True


def test_install_leaves_module_runtime_out_of_the_agent_tree(deployment: Path) -> None:
    """Runtime under an agent-writable root is a self-modification path
    (ASI05/ASI06); it stays at the deployment root, read-only."""
    _run("bundle", "browser")
    _run("install", "browser")

    copied = capability_dir(_agent_dir(deployment), "browser")
    assert not (copied / "_runtime.py").exists()
    assert not (copied / "config.py").exists()


def test_install_materializes_runtime_read_only(deployment: Path) -> None:
    _run("bundle", "browser")
    _run("install", "browser")

    runtime = _module_root(deployment) / "browser" / "_runtime.py"
    assert runtime.stat().st_mode & 0o222 == 0


def test_install_from_a_staged_file_needs_no_store_and_no_network(
    deployment: Path, tmp_path: Path
) -> None:
    """The air-gapped path: a bundle carried in on media, installed by path.
    Same verification code, so nothing about it is a shortcut (REQ-329)."""
    _run("bundle", "browser")
    carried = tmp_path / "media" / "browser.arcbundle"
    carried.parent.mkdir()
    shutil.move(str(_bundle_store(deployment) / "browser.arcbundle"), str(carried))
    assert not _bundle_store(deployment).joinpath("browser.arcbundle").exists()

    _run("install", "--from", str(carried))

    assert (_module_root(deployment) / "browser").is_dir()
    assert "browser" in _config_modules(_agent_dir(deployment))


def test_install_all_takes_everything_the_deployment_permits(deployment: Path) -> None:
    _run("bundle", "browser", "memory")

    _run("install", "--all")

    assert (_module_root(deployment) / "browser").is_dir()
    assert (_module_root(deployment) / "memory").is_dir()
    assert set(_config_modules(_agent_dir(deployment))) == {"browser", "memory"}


def test_install_all_skips_a_forbidden_module_without_failing(deployment: Path) -> None:
    """REQ-330. A bundle from an issuer this deployment does not trust is not an
    error in the batch — it is a module the deployment was never approved for.
    The permitted ones must still install."""
    _run("bundle", "browser", "memory")
    _resign_with_a_foreign_key(_bundle_store(deployment) / "memory.arcbundle")

    _run("install", "--all")

    assert (_module_root(deployment) / "browser").is_dir()
    assert not (_module_root(deployment) / "memory").exists()
    assert set(_config_modules(_agent_dir(deployment))) == {"browser"}


def test_install_all_still_fails_on_a_tampered_payload(deployment: Path) -> None:
    """`--all` is lenient about policy, never about tampering: a content-hash
    mismatch is an attack, and skipping it would install everything around it
    while swallowing the alarm."""
    _run("bundle", "browser", "memory")
    target = _bundle_store(deployment) / "memory.arcbundle" / "files" / "capabilities.py"
    target.write_bytes(b"# swapped after signing\n")

    with pytest.raises(SystemExit) as exit_info:
        _run("install", "--all")

    assert exit_info.value.code == 1
    assert not (_module_root(deployment) / "browser").exists()
    assert not (_module_root(deployment) / "memory").exists()


def test_a_failed_verify_writes_nothing_at_all(deployment: Path) -> None:
    """REQ-326/327 as a byte-level claim: the destination tree is identical to
    what it was before the refused command ran."""
    _run("bundle", "browser", "memory")
    _run("install", "browser")
    before_modules = _snapshot(_module_root(deployment))
    before_agent = _snapshot(_agent_dir(deployment))

    tampered = _bundle_store(deployment) / "memory.arcbundle" / "files" / "capabilities.py"
    tampered.write_bytes(b"# swapped after signing\n")
    with pytest.raises(SystemExit) as exit_info:
        _run("install", "memory")

    assert exit_info.value.code == 1
    assert _snapshot(_module_root(deployment)) == before_modules
    assert _snapshot(_agent_dir(deployment)) == before_agent


def test_a_refused_bundle_in_a_batch_blocks_its_healthy_sibling(deployment: Path) -> None:
    """Verification of the whole request finishes before any of it is written,
    so a batch never lands half-applied."""
    _run("bundle", "browser", "memory")
    tampered = _bundle_store(deployment) / "memory.arcbundle" / "files" / "capabilities.py"
    tampered.write_bytes(b"# swapped after signing\n")

    with pytest.raises(SystemExit):
        _run("install", "browser", "memory")

    assert not (_module_root(deployment) / "browser").exists()


def test_a_registered_dev_issuer_installs_at_personal(deployment: Path) -> None:
    """The control case for the tier gate below: with `did:arc:dev` registered in
    the trust store, the bundle is genuinely trusted here. Without this, the
    federal refusal could be passing merely because the key was unknown."""
    _run("bundle", "browser")
    _reissue_as_dev(deployment, _bundle_store(deployment) / "browser.arcbundle")

    _run("install", "browser")

    assert (_module_root(deployment) / "browser").is_dir()


def test_install_refuses_a_trusted_dev_signed_bundle_at_federal(deployment: Path) -> None:
    """The dev key is a personal-tier convenience — trusted, but not everywhere.
    The refusal is enforced inside arcbundle, so raising the AGENT's tier alone
    triggers it even though the machine config still says personal, and the CLI
    exposes no flag that could wave it through."""
    _run("bundle", "browser")
    _reissue_as_dev(deployment, _bundle_store(deployment) / "browser.arcbundle")
    (_agent_dir(deployment) / "arcagent.toml").write_text(
        "[agent]\nname = 'josh'\n\n[security]\ntier = 'federal'\n", encoding="utf-8"
    )

    with pytest.raises(SystemExit) as exit_info:
        _run("install", "browser")

    assert exit_info.value.code == 1
    assert not (_module_root(deployment) / "browser").exists()
    assert "module.signature_invalid" in _audit_actions(deployment)


def test_install_bare_with_no_names_explains_itself(deployment: Path) -> None:
    """The shape most likely to be typed and least likely to be tested."""
    with pytest.raises(SystemExit) as exit_info:
        _run("install")
    assert exit_info.value.code == 1


def test_install_refuses_all_combined_with_names(deployment: Path) -> None:
    """Two mutually exclusive requests in one command; guessing which one the
    operator meant is how the wrong module gets installed."""
    _run("bundle", "browser")
    with pytest.raises(SystemExit):
        _run("install", "browser", "--all")
    with pytest.raises(SystemExit):
        _run("install", "browser", "--from", str(_bundle_store(deployment) / "browser.arcbundle"))


def test_install_of_an_unstaged_name_names_the_command_that_would_fix_it(
    deployment: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit):
        _run("install", "browser")
    assert "arc module bundle browser" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# install --from-source (the development inner loop)
# ---------------------------------------------------------------------------


def test_from_source_installs_in_one_step_with_nothing_staged(deployment: Path) -> None:
    """The whole point of the flag: edit the catalog, install, run — no `bundle`
    step, no staged file, and still a real signature over real bytes."""
    _run("install", "--from-source", "browser")

    runtime = _module_root(deployment) / "browser"
    copied = capability_dir(_agent_dir(deployment), "browser")
    assert (runtime / "_runtime.py").read_bytes() == _RUNTIME
    assert (copied / "capabilities.py").read_bytes() == _TOOLS
    assert _config_modules(_agent_dir(deployment))["browser"]["enabled"] is True


def test_from_source_signs_with_the_development_issuer(
    deployment: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The issuer is stamped into the signed bytes, so seeing it here means a
    signature was produced and verified — not that a directory was copied."""
    _run("install", "--from-source", "browser")

    assert "did:arc:dev" in capsys.readouterr().out


def test_from_source_goes_through_the_real_verify_and_materialize_path(
    deployment: Path,
) -> None:
    """SDD D-641 rejected symlinking the catalog because it would create a load
    path no signature covered. The audit chain is where that shows: a verify
    event exists, in the same shape a released bundle produces."""
    _run("install", "--from-source", "browser")

    actions = _audit_actions(deployment)
    assert "module.bundle.verified" in actions
    assert "module.installed" in actions


def test_from_source_materializes_real_files_not_links_to_the_catalog(
    deployment: Path,
) -> None:
    """Nothing is symlinked (D-641). A link back into the source catalog would
    make the installed runtime mutate whenever the checkout did, behind the
    signature that was supposed to pin it."""
    runtime = _module_root(deployment) / "browser" / "_runtime.py"

    _run("install", "--from-source", "browser")

    assert not runtime.is_symlink()
    assert not (_module_root(deployment) / "browser").is_symlink()
    # Read-only like any other materialized module: the dev path gets no
    # weaker treatment once the bytes have landed.
    assert runtime.stat().st_mode & 0o222 == 0


def test_from_source_leaves_no_dev_signed_bundle_in_the_store(deployment: Path) -> None:
    """The bundle is scratch, not stock. One left in the store would be a
    dev-signed artifact sitting where a later `install --all` would find it."""
    _run("install", "--from-source", "browser")

    store = _bundle_store(deployment)
    assert not (store / "browser.arcbundle").exists()
    assert not (list(store.iterdir()) if store.is_dir() else [])


def test_from_source_is_refused_at_federal(deployment: Path) -> None:
    """The CLI cannot wave the dev key through, because the CLI is not what
    decides: `arcbundle.verifier` trusts `did:arc:dev` at personal tier only.
    Raising the AGENT's tier alone is enough to turn the install away."""
    (_agent_dir(deployment) / "arcagent.toml").write_text(
        "[agent]\nname = 'josh'\n\n[security]\ntier = 'federal'\n", encoding="utf-8"
    )

    with pytest.raises(SystemExit) as exit_info:
        _run("install", "--from-source", "browser")

    assert exit_info.value.code == 1
    assert not (_module_root(deployment) / "browser").exists()
    assert "module.signature_invalid" in _audit_actions(deployment)


def test_from_source_is_refused_at_enterprise(deployment: Path) -> None:
    """Personal only means personal only — enterprise is refused too, not just
    the tier the federal test happens to name."""
    (_agent_dir(deployment) / "arcagent.toml").write_text(
        "[agent]\nname = 'josh'\n\n[security]\ntier = 'enterprise'\n", encoding="utf-8"
    )

    with pytest.raises(SystemExit) as exit_info:
        _run("install", "--from-source", "browser")

    assert exit_info.value.code == 1
    assert not (_module_root(deployment) / "browser").exists()


def test_from_source_refuses_an_unknown_module(
    deployment: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as exit_info:
        _run("install", "--from-source", "nonexistent")

    assert exit_info.value.code == 1
    assert "browser" in capsys.readouterr().err  # names what IS available


def test_from_source_refuses_to_be_combined_with_another_selector(deployment: Path) -> None:
    """Two different answers to "which bundle" in one command. Guessing is how
    the wrong module gets installed."""
    _run("bundle", "memory")
    for extra in (
        ["memory"],
        ["--all"],
        ["--from", str(_bundle_store(deployment) / "memory.arcbundle")],
    ):
        with pytest.raises(SystemExit):
            _run("install", "--from-source", "browser", *extra)


def test_from_source_does_not_widen_a_later_install(deployment: Path) -> None:
    """The ephemeral key dies with the command. A dev-signed bundle staged by
    hand afterwards is still refused — deliberately WITHOUT registering the
    issuer, so a pass would mean `--from-source` had left a trust anchor behind."""
    _run("install", "--from-source", "browser")
    _run("bundle", "memory")
    _resign(
        _bundle_store(deployment) / "memory.arcbundle",
        issuer="did:arc:dev",
        private_key=generate_keypair().private_key,
    )

    with pytest.raises(SystemExit):
        _run("install", "memory")

    assert not (_module_root(deployment) / "memory").exists()


# ---------------------------------------------------------------------------
# remove
# ---------------------------------------------------------------------------


def test_remove_is_the_exact_inverse_of_install(deployment: Path) -> None:
    _run("bundle", "browser")
    _run("install", "browser")

    _run("remove", "browser")

    assert not (_module_root(deployment) / "browser").exists()
    assert not capability_dir(_agent_dir(deployment), "browser").exists()
    assert "browser" not in _config_modules(_agent_dir(deployment))


def test_remove_exits_non_zero_when_a_part_cannot_complete(deployment: Path) -> None:
    """REQ-338. The runtime is already gone, so removal cannot complete — and a
    silent success would report a clean box that is not clean."""
    _run("bundle", "browser")
    _run("install", "browser")
    _force_rmtree(_module_root(deployment) / "browser")

    with pytest.raises(SystemExit) as exit_info:
        _run("remove", "browser")

    assert exit_info.value.code == 1


def test_remove_reports_every_failure_and_still_removes_what_it_can(
    deployment: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Stopping at the first failure would strand the other two halves; a config
    entry left behind names a module whose runtime is gone."""
    _run("bundle", "browser")
    _run("install", "browser")
    _force_rmtree(_module_root(deployment) / "browser")

    with pytest.raises(SystemExit):
        _run("remove", "browser")

    assert "runtime" in capsys.readouterr().err
    assert not capability_dir(_agent_dir(deployment), "browser").exists()
    assert "browser" not in _config_modules(_agent_dir(deployment))


def test_remove_of_a_never_installed_module_exits_non_zero(deployment: Path) -> None:
    """There is nothing to remove, and the runtime deletion says so explicitly
    rather than reporting a removal that did not happen."""
    with pytest.raises(SystemExit):
        _run("remove", "browser")


# ---------------------------------------------------------------------------
# list
# ---------------------------------------------------------------------------


def test_list_bare_on_an_empty_deployment_says_so(
    deployment: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The bare form must work before anything exists — it is the first command
    an operator runs."""
    monkeypatch.delenv("ARC_MODULE_SOURCE")
    monkeypatch.setenv("ARC_MODULE_SOURCE", str(deployment / "absent-catalog"))
    _run("list")
    assert "No modules known" in capsys.readouterr().out


def test_list_reports_bundled_installed_and_enabled(
    deployment: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _run("bundle", "browser", "memory")
    _run("install", "browser")
    capsys.readouterr()

    _run("list")

    out = capsys.readouterr().out
    browser_row = next(line for line in out.splitlines() if line.startswith("browser"))
    memory_row = next(line for line in out.splitlines() if line.startswith("memory"))
    assert browser_row.count("yes") == 3  # bundled, installed, enabled
    assert memory_row.count("yes") == 1  # bundled only


def test_list_names_the_roots_it_reported_on(
    deployment: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """An operator proving absence needs the directory to point a scanner at."""
    _run("bundle", "browser")
    capsys.readouterr()

    _run("list")

    out = capsys.readouterr().out
    assert str(_module_root(deployment)) in out
    assert str(_bundle_store(deployment)) in out


# ---------------------------------------------------------------------------
# audit (T-969, on the real chain)
# ---------------------------------------------------------------------------


def test_the_full_lifecycle_lands_on_the_real_worm_chain(deployment: Path) -> None:
    _run("bundle", "browser")
    _run("install", "browser")
    _run("remove", "browser")

    actions = _audit_actions(deployment)
    assert actions.count("module.bundle.verified") == 1
    assert actions.count("module.installed") == 1
    assert actions.count("module.removed") == 1
    assert actions.index("module.bundle.verified") < actions.index("module.installed")


def test_a_refusal_is_recorded_as_a_refusal(deployment: Path) -> None:
    """The event an auditor reconstructs a blocked install from. Emitted by
    arcbundle, so the CLI cannot forget to write it."""
    _run("bundle", "browser")
    _resign_with_a_foreign_key(_bundle_store(deployment) / "browser.arcbundle")

    with pytest.raises(SystemExit):
        _run("install", "browser")

    actions = _audit_actions(deployment)
    assert "module.signature_invalid" in actions
    assert "module.installed" not in actions


def test_a_tampered_payload_is_recorded_as_a_content_mismatch(deployment: Path) -> None:
    _run("bundle", "browser")
    target = _bundle_store(deployment) / "browser.arcbundle" / "files" / "capabilities.py"
    target.write_bytes(b"# swapped after signing\n")

    with pytest.raises(SystemExit):
        _run("install", "browser")

    assert "module.content_hash_mismatch" in _audit_actions(deployment)


def test_the_audit_records_the_operator_not_the_subsystem(deployment: Path) -> None:
    """An install is an operator action; attributing it to the installer process
    would lose the only identity an auditor cares about."""
    _run("bundle", "browser")
    _run("install", "browser")

    worm = next((deployment / "store" / "worm").glob("*.jsonl"))
    events = [json.loads(line)["event"] for line in worm.read_text().splitlines() if line.strip()]
    installed = next(e for e in events if e["action"] == "module.installed")
    key = OperatorKey.load(deployment / "arc" / "operator" / "operator.key")
    assert installed["actor_did"].startswith("did:arc:")
    assert installed["actor_did"] != "did:arc:system:module-installer"
    assert key.public_key


# ---------------------------------------------------------------------------
# registry wiring
# ---------------------------------------------------------------------------


def test_the_copy_layout_is_what_the_loader_root_builder_expects(deployment: Path) -> None:
    """The copy is only useful if the loader can discover it, and the loader's
    root builder is opinionated: tools directly under the root, skills under
    ``skills/``. Asserting against ``append_capability_scan_roots`` itself — not
    a restatement of it — is what makes a future change to that layout fail here
    instead of silently producing an agent with no module tools.
    """
    from arcagent.capabilities.capability_loader import is_untrusted_root
    from arcagent.capabilities.inventory import append_capability_scan_roots

    _run("bundle", "browser")
    _run("install", "browser")

    roots: list[tuple[str, Path]] = []
    copied = capability_dir(_agent_dir(deployment), "browser")
    append_capability_scan_roots(roots, "agent", copied)

    assert ("agent", copied) in roots, "the tool root was not recognised"
    assert ("agent-skills", copied / "skills") in roots, "the skills root was not recognised"
    # Both carry the existing `agent` trust class, so no new trusted root is
    # introduced by the copy (SDD alternatives D-648 / D-649).
    assert all(is_untrusted_root(name) for name, _ in roots)


def test_module_is_reachable_through_the_command_registry() -> None:
    """A command nothing dispatches to is a command that does not exist."""
    from arccli.commands.registry import resolve_command_and_args

    command, rest = resolve_command_and_args(["module", "list"])
    assert command is not None
    assert command.name == "module"
    assert command.cli_only is True  # never offered on a chat surface (LLM06)
    assert rest == ["list"]


# ---------------------------------------------------------------------------
# helpers that re-sign a built bundle
# ---------------------------------------------------------------------------


def _resign(bundle: Path, *, issuer: str, private_key: bytes) -> None:
    """Rewrite a built bundle's manifest under a different issuer and key."""
    from arcbundle import BundleManifest, sign_manifest

    manifest = BundleManifest.model_validate_json((bundle / "manifest.json").read_bytes())
    reissued = manifest.model_copy(update={"issuer": issuer})
    (bundle / "manifest.json").write_bytes(reissued.canonical_bytes())
    (bundle / "manifest.sig").write_bytes(
        sign_manifest(reissued, private_key=private_key, issuer=issuer)
    )


def _resign_with_a_foreign_key(bundle: Path) -> None:
    """Make the bundle validly signed by an issuer this deployment never trusted."""
    _resign(bundle, issuer="did:arc:someone-else", private_key=generate_keypair().private_key)


def _reissue_as_dev(deployment: Path, bundle: Path) -> None:
    """Make the bundle validly dev-signed AND register that issuer as trusted.

    Registering it is the point: the tier gate must turn the bundle away at
    federal even though the deployment does trust the key.
    """
    keypair = generate_keypair()
    _resign(bundle, issuer="did:arc:dev", private_key=keypair.private_key)
    _register_issuer(deployment, "did:arc:dev", keypair.public_key)


def _register_issuer(deployment: Path, did: str, public_key: bytes) -> None:
    """Write a real ``issuers.toml`` entry at the 0600 the trust store demands."""
    trust_dir = deployment / "arc" / "trust"
    trust_dir.mkdir(parents=True, exist_ok=True)
    path = trust_dir / "issuers.toml"
    encoded = base64.b64encode(public_key).decode("ascii")
    path.write_text(f'[issuers."{did}"]\npublic_key = "{encoded}"\n', encoding="utf-8")
    path.chmod(0o600)
    invalidate_cache()
