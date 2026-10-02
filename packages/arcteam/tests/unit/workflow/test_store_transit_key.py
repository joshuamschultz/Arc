"""A vault-held (federal) operator key verifies workflows through the ONE resolver.

Under ``vault_transit`` there is no private key file, and any key file that does
exist is NOT the authority. The store must pin the transit's public key, and a
definition signed through the transit handle must verify against it.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from arctrust import (
    FileNotaryTransit,
    OperatorKey,
    generate_keypair,
    operator_public_key_for,
    operator_signer_for,
)

from arcteam.workflow import DefinitionStore, parse_definition, sign_definition_with_signer

_DOCUMENT = {
    "workflow": {"id": "onboarding", "owner": "@sales"},
    "trigger": {"type": "manual"},
    "node": [{"id": "collect", "kind": "agent", "agent": "@sales", "prompt": "prompts/c.md"}],
}
_FILES = {"prompts/c.md": b"Collect."}


@dataclass
class _Federal:
    custody: str = "vault_transit"
    signing_algorithm: str = "ed25519"
    operator_key_dir: str = ""
    notary_keystore: str = ""


def test_store_verifies_with_transit_public_key_when_no_key_file(tmp_path: Path) -> None:
    FileNotaryTransit.provision(tmp_path / "notary", "operator", generate_keypair().private_key)
    security = _Federal(operator_key_dir=str(tmp_path))
    store = DefinitionStore(
        tmp_path / "workflows",
        tier="federal",
        operator_public_key=operator_public_key_for(security),
    )
    store.save_draft(
        parse_definition(_DOCUMENT),
        actor_did="did:arc:agent:sales",
        expected_version=None,
        files=_FILES,
    )

    signer = operator_signer_for(security)
    sign_definition_with_signer(
        store, "onboarding", signer_did="did:arc:operator:x", signer=signer
    )

    assert store.load("onboarding").status == "signed"
    assert store.load_for_run("onboarding") is not None


def test_a_stale_on_disk_key_is_not_the_federal_authority(tmp_path: Path) -> None:
    stale = OperatorKey.load(tmp_path / "operator.key", generate_if_absent=True)
    FileNotaryTransit.provision(tmp_path / "notary", "operator", generate_keypair().private_key)
    security = _Federal(operator_key_dir=str(tmp_path))

    pinned = operator_public_key_for(security)

    assert pinned != stale.public_key
