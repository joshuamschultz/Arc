"""Sign an agent's control-plane workspace documents the way the operator does.

``identity.md`` (and the pinned policy rules) are verified against the deployment
operator key at every run start; an unsigned one refuses the run. A test that wants
a real run therefore signs what it writes, with a real operator key and the
sidecar where the agent's own resolver looks (``<config dir>/context/workspace/``).
"""

from __future__ import annotations

from pathlib import Path

from arctrust import OperatorKey, default_operator_key_path
from arctrust.artifact import sign_artifact

from arcagent import signed_workspace_files


def sign_workspace_documents(workspace: Path, config_path: Path) -> None:
    """Sign every signed-document file present in ``workspace`` with the operator key.

    Creates the operator key under the test's ``ARC_CONFIG_DIR`` when absent. Pass the
    same ``config_path`` to ``ArcAgent`` so its resolver and these sidecars share a root.
    """
    key = OperatorKey.load(default_operator_key_path(), generate_if_absent=True)
    for (package, name), path in signed_workspace_files(workspace).items():
        if not path.is_file():
            continue
        manifest = sign_artifact(
            path.read_bytes(), signer_did="did:arc:test-operator", private_key=key.seed
        )
        sidecar = config_path.parent / "context" / package / f"{name}.md.arcsig"
        sidecar.parent.mkdir(parents=True, exist_ok=True)
        sidecar.write_text(manifest.to_json(), encoding="utf-8")
