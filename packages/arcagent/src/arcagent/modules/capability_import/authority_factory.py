"""Bind a live agent identity to externally anchored skill revisions."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from threading import Lock

from arctrust import MonotonicAnchor

from arcagent.modules.capability_import.revisions import AnchoredSkillRevisionResolver


class LiveSkillRevisionResolver:
    """Resolve skill bytes with the DID established by the running agent."""

    def __init__(
        self,
        *,
        agent_did: Callable[[], str],
        config_path: Path,
        anchor_factory: Callable[[str, str], MonotonicAnchor],
    ) -> None:
        self._agent_did = agent_did
        self._config_path = config_path
        self._anchor_factory = anchor_factory
        self._lock = Lock()
        self._bound: AnchoredSkillRevisionResolver | None = None
        self._bound_did: str | None = None

    def _resolver(self) -> AnchoredSkillRevisionResolver:
        did = self._agent_did()
        if not did.startswith("did:"):
            raise RuntimeError("agent identity is unavailable for skill revisions")
        with self._lock:
            if self._bound_did is not None and self._bound_did != did:
                raise RuntimeError("agent identity changed after skill authority binding")
            if self._bound is None:
                self._bound = AnchoredSkillRevisionResolver(
                    agent_did=did,
                    config_path=self._config_path,
                    anchor_factory=self._anchor_factory,
                )
                self._bound_did = did
            return self._bound

    def resolve(self, folder: Path, scan_root: str) -> Path:
        """Return only the currently anchored and verified skill body."""
        return self._resolver().resolve(folder, scan_root)

    def read_current(self, folder: Path, path: Path) -> str | None:
        """Verify the current head again before exposing its body."""
        return self._resolver().read_current(folder, path)


__all__ = ["LiveSkillRevisionResolver"]
