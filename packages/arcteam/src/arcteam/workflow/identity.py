"""RunnerIdentity — the runner's own actor DID (SPEC-061 COMP-011, REQ-232).

The runner writes Run rows and task rows on nobody's behalf but its own, so it
needs an identity that is neither an agent's nor the dashboard's shared
``did:arc:ui:operator`` role. It resolves that identity from the deployment's
on-disk operator key with ``generate_if_absent=False`` — the same load arcui
messaging performs (``arcui/messaging.py:131-140``) — and **fails closed** when
the key is absent: a runner with no identity must not run, because every row it
creates would be unattributable.

The ``workflow-runner`` agent type is what separates this DID from the operator
DID derived from the very same key: two distinct actors, one deployment key.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from arctrust import OperatorKey
from arctrust.identity import did_from_public_key

from arcteam.crypto import MessageSigner

RUNNER_AGENT_TYPE = "workflow-runner"


class RunnerIdentityUnavailableError(RuntimeError):
    """No runner identity could be resolved. The runner must not start."""


@dataclass(frozen=True)
class RunnerIdentity:
    """The runner's DID plus the signing material its narration needs."""

    did: str
    public_key_hex: str
    _seed: bytes

    @classmethod
    def load(cls, key_path: Path, *, org: str = "local") -> RunnerIdentity:
        """Resolve the identity from the on-disk operator key. Never generates one."""
        try:
            key = OperatorKey.load(Path(key_path), generate_if_absent=False)
        except (OSError, ValueError, RuntimeError) as exc:
            raise RunnerIdentityUnavailableError(
                f"No operator key at {key_path}; the workflow runner cannot identify itself"
            ) from exc
        return cls(
            did=did_from_public_key(key.public_key, org=org, agent_type=RUNNER_AGENT_TYPE),
            public_key_hex=key.public_key.hex(),
            _seed=key.seed,
        )

    def message_signer(self) -> MessageSigner:
        """Bind narration envelopes to this DID."""
        return MessageSigner(did=self.did, private_key=self._seed)


__all__ = ["RUNNER_AGENT_TYPE", "RunnerIdentity", "RunnerIdentityUnavailableError"]
