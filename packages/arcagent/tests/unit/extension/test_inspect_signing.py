"""SPEC-047 HIGH-1 — `arc ext inspect` signed-status is PINNED to the agent DID key.

A scan-many capability is signed by the agent's own DID key (the self-modification tools,
``_runtime.sign_artifact_file``). Inspection must pin the ``.arcsig`` to that key so a
wrong-key self-signed artifact reads "unsigned" — the same verdict the live loader reaches
at enterprise/federal — instead of a false "signed" from an unpinned (TOFU-only) check.
"""

from __future__ import annotations

from pathlib import Path

from arctrust.identity import AgentIdentity

from arcagent.capabilities import artifact_signing
from arcagent.extension.inspect import _signed_status


def _write_signed(path: Path, signer: AgentIdentity) -> None:
    path.write_text("# capability source\n", encoding="utf-8")
    artifact_signing.write_signature(
        path, path.read_bytes(), signer_did=signer.did, private_key=signer.signing_seed
    )


def test_signed_status_signed_when_pinned_key_matches(tmp_path: Path) -> None:
    agent = AgentIdentity.generate(org="blackarc", agent_type="executor")
    cap = tmp_path / "greet.py"
    _write_signed(cap, agent)
    assert _signed_status(cap, agent.public_key) == "signed"


def test_signed_status_unsigned_when_pinned_key_differs(tmp_path: Path) -> None:
    attacker = AgentIdentity.generate(org="evil", agent_type="executor")
    agent = AgentIdentity.generate(org="blackarc", agent_type="executor")
    cap = tmp_path / "greet.py"
    _write_signed(cap, attacker)  # self-signed by the attacker, not the agent
    # Pinned to the agent DID key → the wrong-key sidecar does NOT verify.
    assert _signed_status(cap, agent.public_key) == "unsigned"


def test_signed_status_no_sidecar_is_unsigned(tmp_path: Path) -> None:
    cap = tmp_path / "bare.py"
    cap.write_text("# no sidecar\n", encoding="utf-8")
    agent = AgentIdentity.generate(org="blackarc", agent_type="executor")
    assert _signed_status(cap, agent.public_key) == "unsigned"
