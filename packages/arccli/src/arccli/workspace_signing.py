"""Sign an agent's control-plane workspace documents with the operator key.

``identity.md`` and ``policy_pinned.md`` are verified against the deployment
operator key at every run start (J2 F2); the detached signature lives under the
agent's ``context/workspace/`` overlay root, where the agent's own tools cannot
reach it. Scaffolding (``arc agent create``, blueprint apply) and ``arc prompt
sign-workspace`` all sign through this one function, with the same
``(operator DID, seed)`` pair ``arc prompt edit`` uses.
"""

from __future__ import annotations

from pathlib import Path

import arcagent
from arctrust.artifact import sign_artifact

_SIDECAR_SUFFIX = ".arcsig"


def sign_workspace_documents(agent_dir: Path, signer: tuple[str, bytes]) -> list[str]:
    """Sign every signed-document file present in ``agent_dir/workspace``.

    Returns the file names signed. Absent documents are skipped — there is nothing
    to verify for them at run start either.
    """
    signer_did, seed = signer
    signed: list[str] = []
    for (package, name), path in arcagent.signed_workspace_files(agent_dir / "workspace").items():
        if not path.is_file():
            continue
        signature = sign_artifact(path.read_bytes(), signer_did=signer_did, private_key=seed)
        sidecar = agent_dir / "context" / package / f"{name}.md{_SIDECAR_SUFFIX}"
        sidecar.parent.mkdir(parents=True, exist_ok=True)
        sidecar.write_text(signature.to_json(), encoding="utf-8")
        signed.append(path.name)
    return signed


__all__ = ["sign_workspace_documents"]
