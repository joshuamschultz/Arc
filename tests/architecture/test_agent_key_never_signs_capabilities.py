"""The agent key never signs anything under the agent-root ``capabilities/`` tree.

The agent can write that tree, so an agent-signed file there is self-approved
code. Every operator surface (``arc agent create``, blueprint/init materialize,
``arc blueprint sign``, the trust routes) signs through the OPERATOR signer
handle. The only agent-seed signer in the code base is the workspace-authored
artifact path (``_runtime.sign_artifact_file``), which writes into the
contained ``workspace/capabilities`` tree.
"""

from __future__ import annotations

from pathlib import Path

_PACKAGES = Path(__file__).resolve().parents[2] / "packages"

#: Operator surfaces: none may touch an agent seed or the raw-seed signer.
_OPERATOR_SURFACES = ("arccli", "arcui", "arcgateway")
_FORBIDDEN = ("signing_seed", "write_signature(", "agent_signer")

#: The sanctioned agent-seed users (workspace-authored tree; message signing).
_AGENT_SEED_ALLOWED = {
    _PACKAGES / "arcagent/src/arcagent/builtins/capabilities/_runtime.py",
    _PACKAGES / "arcteam/src/arcteam/crypto.py",
}


def _sources(package: str) -> list[Path]:
    return sorted((_PACKAGES / package / "src").rglob("*.py"))


def test_operator_surfaces_never_use_the_agent_seed_or_raw_seed_signer() -> None:
    offenders = [
        f"{path.relative_to(_PACKAGES)}: {token}"
        for package in _OPERATOR_SURFACES
        for path in _sources(package)
        for token in _FORBIDDEN
        if token in path.read_text(encoding="utf-8")
    ]
    assert offenders == []


def test_agent_seed_is_used_only_by_the_sanctioned_signers() -> None:
    offenders = [
        str(path.relative_to(_PACKAGES))
        for path in _sources("arcagent")
        if "signing_seed" in path.read_text(encoding="utf-8") and path not in _AGENT_SEED_ALLOWED
    ]
    assert offenders == []
