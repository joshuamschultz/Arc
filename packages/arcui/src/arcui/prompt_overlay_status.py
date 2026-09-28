"""What the agent will do with a prompt override — decided by the agent's own resolver.

ArcUI's prompt view must never tell a different story than the agent. The agent
resolves every prompt through the overlay-aware resolver ``arcagent.build_prompt_resolver``
builds — pinned to the deployment operator's key — and a present-but-broken override
makes the run fail closed (REQ-125). So the view asks that SAME resolver:

* no override file              -> ``stock``      (the packaged body is effective)
* override verifies             -> ``overridden`` (the override body is effective)
* override present, not accepted -> ``rejected``  (the agent refuses to run; nothing
  is effective, and the override's text is never presented as the prompt in use)

The rejection reason is diagnostic only — the verdict is the resolver's. It names
the failure an operator can act on: no signature, an unreadable signature, bytes
edited after signing, or a signature from a key that is not the pinned operator key.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import arcagent
from arcprompt import PromptResolver, PromptUnparseable, PromptUnsigned
from arcprompt.resolver import SIGNATURE_SUFFIX
from arctrust.artifact import ArtifactSignature, content_sha256
from arctrust.policy import read_agent_tier

OverlayStatus = Literal["stock", "overridden", "rejected"]


@dataclass(frozen=True)
class OverlayState:
    """One prompt's status for one agent, and the body the agent would use."""

    status: OverlayStatus
    #: The body the agent sends to the model; ``""`` when the agent refuses to run.
    effective: str
    #: Why a ``rejected`` override was refused; ``""`` otherwise.
    reason: str = ""


def agent_prompt_resolver(agent_root: Path) -> PromptResolver:
    """The resolver the agent itself builds: rooted at its folder, pinned to the operator key."""
    return arcagent.build_prompt_resolver(
        agent_root / "arcagent.toml", read_agent_tier(agent_root)
    )


def overlay_state(
    resolver: PromptResolver, package: str, name: str, stock_body: str
) -> OverlayState:
    """Resolve ``package/name`` exactly as the agent will and report the outcome."""
    overlay = resolver.overlay_path(package, name)
    if not overlay.is_file():
        return OverlayState(status="stock", effective=stock_body)
    try:
        document = resolver.resolve(package, name)
    except PromptUnsigned:
        return OverlayState(status="rejected", effective="", reason=_unsigned_reason(overlay))
    except PromptUnparseable:
        reason = "The override file is not a valid prompt document."
        return OverlayState(status="rejected", effective="", reason=reason)
    except OSError:
        reason = "The override file cannot be read."
        return OverlayState(status="rejected", effective="", reason=reason)
    return OverlayState(status="overridden", effective=document.body)


def _unsigned_reason(overlay: Path) -> str:
    """Name which signature check the override failed (the resolver already refused it)."""
    sidecar = overlay.with_name(overlay.name + SIGNATURE_SUFFIX)
    if not sidecar.is_file():
        return "The override has no signature."
    try:
        manifest = ArtifactSignature.from_json(sidecar.read_text(encoding="utf-8"))
        digest = content_sha256(overlay.read_bytes())
    except (ValueError, OSError):
        return "The override's signature file cannot be read."
    if digest != manifest.artifact_sha256:
        return "The override was edited after it was signed."
    return "The override is not signed by this deployment's operator key."


__all__ = ["OverlayState", "OverlayStatus", "agent_prompt_resolver", "overlay_state"]
