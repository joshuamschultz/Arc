"""The operator-side reads and the one install sequence every surface shares.

``arc module`` and the dashboard's Maintenance tab both list staged bundles, work
out which tier a bundle is verified at, decide which issuer keys are trusted for
it, and materialize it for one agent. These live here once, so the two surfaces
cannot disagree about what a bundle is allowed to do.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from arctrust import generate_keypair

from arcbundle import (
    BundleTierError,
    StagedBundle,
    bundle_matches_installed,
    install_verified,
    peek_issuer,
    staged_bundles,
    trusted_issuers,
    verification_tier,
    verify_bundle,
)


def test_staged_bundles_lists_each_bundle_with_its_version_and_issuer(
    tmp_path: Path, make_bundle
) -> None:
    store = tmp_path / "bundles"
    make_bundle(store / "browser.arcbundle", module="browser", version="1.2.0")
    (store / "not-a-bundle").mkdir()
    (store / "readme.txt").write_text("x")

    staged = staged_bundles(store)

    assert set(staged) == {"browser"}
    bundle = staged["browser"]
    assert isinstance(bundle, StagedBundle)
    assert (bundle.version, bundle.issuer) == ("1.2.0", "did:arc:release")
    assert bundle.path == store / "browser.arcbundle"


def test_a_missing_store_has_no_staged_bundles(tmp_path: Path) -> None:
    assert staged_bundles(tmp_path / "nowhere") == {}


def test_a_bundle_with_an_unreadable_manifest_is_listed_without_a_version(tmp_path: Path) -> None:
    store = tmp_path / "bundles"
    (store / "broken.arcbundle").mkdir(parents=True)
    (store / "broken.arcbundle" / "manifest.json").write_text("{not json")

    staged = staged_bundles(store)

    assert staged["broken"].version is None
    assert staged["broken"].issuer is None


def test_matches_installed_is_true_only_for_identical_content(tmp_path: Path, make_bundle) -> None:
    modules_root = tmp_path / "modules"
    store = tmp_path / "bundles"
    fixture = make_bundle(
        store / "browser.arcbundle",
        module="browser",
        payload={"capabilities.py": b"CAPS = 1\n", "_runtime.py": b"def configure(): ...\n"},
    )
    staged = staged_bundles(store)["browser"]
    assert bundle_matches_installed(staged, modules_root) is False  # not installed yet

    verified = verify_bundle(fixture.path, tier="personal", trusted_issuers=fixture.trusted())
    install_verified(verified, modules_root=modules_root, agent_root=tmp_path / "agent")
    assert bundle_matches_installed(staged, modules_root) is True

    # A newer bundle with different content is an update, not a match.
    make_bundle(
        tmp_path / "bundles2" / "browser.arcbundle",
        module="browser",
        version="2.0.0",
        payload={"capabilities.py": b"CAPS = 2\n"},
    )
    newer = staged_bundles(tmp_path / "bundles2")["browser"]
    assert bundle_matches_installed(newer, modules_root) is False


def test_peek_issuer_reads_the_claim_without_trusting_it(tmp_path: Path, make_bundle) -> None:
    fixture = make_bundle(tmp_path / "b.arcbundle", issuer="did:arc:someone")
    assert peek_issuer(fixture.path) == "did:arc:someone"
    assert peek_issuer(tmp_path / "missing") == ""


def test_verification_tier_is_the_stricter_of_the_configs(tmp_path: Path) -> None:
    machine = tmp_path / "machine.toml"
    agent = tmp_path / "agent.toml"
    machine.write_text('[security]\ntier = "enterprise"\n')
    agent.write_text('[security]\ntier = "federal"\n')

    assert verification_tier(machine, agent) == "federal"
    assert verification_tier(machine, None) == "enterprise"
    assert verification_tier(tmp_path / "absent.toml") == "personal"


def test_an_unknown_or_unreadable_tier_stops_rather_than_guessing(tmp_path: Path) -> None:
    unknown = tmp_path / "a.toml"
    unknown.write_text('[security]\ntier = "lenient"\n')
    broken = tmp_path / "b.toml"
    broken.write_text("[security\n")

    with pytest.raises(BundleTierError):
        verification_tier(unknown)
    with pytest.raises(BundleTierError):
        verification_tier(broken)


def test_trusted_issuers_includes_the_operator_and_never_an_unknown_claim(
    tmp_path: Path, make_bundle, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ARC_CONFIG_DIR", str(tmp_path / "arc-home"))
    fixture = make_bundle(tmp_path / "b.arcbundle", issuer="did:arc:stranger")
    operator_key = generate_keypair().public_key

    trusted = trusted_issuers(fixture.path, operator=("did:arc:operator", operator_key))

    assert trusted == {"did:arc:operator": operator_key}
    assert trusted_issuers(fixture.path, operator=None) == {}
