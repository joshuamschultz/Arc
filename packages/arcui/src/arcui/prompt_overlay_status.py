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
from arcprompt import PromptCatalog, PromptResolver, PromptUnparseable, PromptUnsigned
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


@dataclass(frozen=True)
class RejectedPrompt:
    """One prompt or signed document the agent refuses to run with, and why."""

    package: str
    name: str
    reason: str


def rejected_prompts(agent_root: Path) -> list[RejectedPrompt]:
    """Every override or signed workspace document the agent would refuse at run start.

    One bad signature stops ALL of the agent's runs (fail-closed, by design), so the
    fleet view must surface it before the operator finds out by a silent failure
    (J2 F7). Only files that exist are checked — a prompt with no override is stock
    and cannot be rejected — so this stays cheap enough for a per-card poll.
    """
    resolver = agent_prompt_resolver(agent_root)
    rejected: list[RejectedPrompt] = []
    for ref in PromptCatalog().catalog():
        if not resolver.overlay_path(ref.package, ref.name).is_file():
            continue
        state = overlay_state(resolver, ref.package, ref.name, stock_body="")
        if state.status == "rejected":
            rejected.append(RejectedPrompt(ref.package, ref.name, state.reason))
    for (package, name), path in arcagent.signed_workspace_files(agent_root / "workspace").items():
        try:
            resolver.resolve_signed_file(package, name, path)
        except PromptUnsigned:
            overlay = resolver.overlay_path(package, name)
            sidecar = overlay.with_name(overlay.name + SIGNATURE_SUFFIX)
            rejected.append(RejectedPrompt(package, name, _document_reason(path, sidecar)))
        except OSError:
            reason = f"{path.name} cannot be read."
            rejected.append(RejectedPrompt(package, name, reason))
    return rejected


def _document_reason(path: Path, sidecar: Path) -> str:
    """Why a signed workspace document was refused: unsigned, edited, or wrong key."""
    if not sidecar.is_file():
        return f"{path.name} has no operator signature."
    try:
        manifest = ArtifactSignature.from_json(sidecar.read_text(encoding="utf-8"))
        digest = content_sha256(path.read_bytes())
    except (ValueError, OSError):
        return f"{path.name} has an unreadable signature file."
    if digest != manifest.artifact_sha256:
        return f"{path.name} was edited after it was signed."
    return f"{path.name} is not signed by this deployment's operator key."


__all__ = [
    "OverlayState",
    "OverlayStatus",
    "RejectedPrompt",
    "agent_prompt_resolver",
    "overlay_state",
    "rejected_prompts",
]
