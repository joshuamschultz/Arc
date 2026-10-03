"""D12 and J-U1: who holds a bundle's credential, read from the shipped manifests."""

from __future__ import annotations

from pathlib import Path

import pytest

from arcagent.core.tier import Tier
from arcagent.extension.connection_health import custody_of
from arcagent.extension.manifest import ExtensionManifest, load_manifest

EXTENSIONS = Path(__file__).resolve().parents[5] / "extensions"


def _manifest(name: str) -> ExtensionManifest:
    return load_manifest((EXTENSIONS / name / "extension.toml").read_text(), tier=Tier.PERSONAL)


def test_a_cli_with_a_vaulted_token_is_arc_custody() -> None:
    assert custody_of(_manifest("github")) == "arc"


def test_a_cli_whose_login_lives_in_its_own_store_is_host_custody() -> None:
    assert custody_of(_manifest("readwise_reader")) == "host"


@pytest.mark.parametrize("name", ["dropbox", "slack", "postgresql", "composio"])
def test_arc_stored_credentials_are_arc_custody(name: str) -> None:
    assert custody_of(_manifest(name)) == "arc"


@pytest.mark.parametrize(
    "name", ["dropbox", "google_workspace", "microsoft365", "jira", "confluence"]
)
def test_an_oauth_managed_refresh_token_is_never_required_on_the_add_form(name: str) -> None:
    manifest = _manifest(name)
    assert manifest.oauth is not None
    declared = {s.name: s for s in manifest.secrets}
    assert declared[manifest.oauth.refresh_token_secret].required is False
