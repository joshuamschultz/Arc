"""Where `arc skill evals promote/edit` put their files.

A skill installed under an agent is operator-signed and anchored: dropping an
unsigned eval file beside it either does nothing or lets the improver gate score
bytes nobody approved. So for an installed skill both commands lay the files over
the active bundle as a NEW operator-signed revision through the external anchor
(:class:`arcagent.OperatorSkillRevisionWriter`), exactly as the arcui promote
surface does. A folder that is not under an agent is just an operator's working
copy; it keeps the plain write, which loads only after the operator signs or
imports it.

Fail closed: when the anchored authority cannot activate (no external anchor at
federal, no operator signer, a regressed head) the command stops with
"activation unavailable" and writes nothing.
"""

from __future__ import annotations

import tomllib
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import arcagent
from arctrust import SignerError

from arccli.commands._shared import audit_chain

_REVISIONS_DIR = ".skill-revisions"


class ActivationUnavailableError(RuntimeError):
    """The anchored revision authority cannot activate a signed revision."""


class FileCommitter(Protocol):
    """Anything that can lay reviewed files into a skill (revision or working copy)."""

    def commit(self, skill_name: str, files: Mapping[str, bytes], *, reason: str) -> str: ...


@dataclass(frozen=True)
class SkillActivation:
    """How to read a skill's current files and where a change is committed."""

    #: Folder whose files are the skill's current, trusted content.
    read_dir: Path
    writer: FileCommitter
    skill_name: str


def owning_agent_root(skill_dir: Path) -> Path | None:
    """The agent home (the folder holding ``arcagent.toml``) that owns ``skill_dir``."""
    for parent in skill_dir.resolve().parents:
        if (parent / "arcagent.toml").is_file():
            return parent
    return None


def installed_folder(skill_dir: Path) -> Path:
    """``<root>/skills/<name>`` for a path that may be an active revision folder."""
    resolved = skill_dir.resolve()
    if resolved.parent.parent.name == _REVISIONS_DIR:
        return resolved.parent.parent.parent / "skills" / resolved.parent.name
    return resolved


def _agent_did(agent_root: Path) -> str:
    try:
        config = tomllib.loads((agent_root / "arcagent.toml").read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ActivationUnavailableError(f"cannot read the agent config: {exc}") from exc
    did = str(config.get("identity", {}).get("did", ""))
    if not did.startswith("did:"):
        raise ActivationUnavailableError("the agent has no identity to bind revisions to")
    return did


@contextmanager
def skill_activation(skill_dir: Path, working_copy: FileCommitter) -> Iterator[SkillActivation]:
    """Resolve the activation path for ``skill_dir``; yield it for one command.

    Raises:
        ActivationUnavailableError: the skill is agent-installed and the anchored
            authority cannot activate a revision. Nothing is written.
    """
    agent_root = owning_agent_root(skill_dir)
    if agent_root is None:
        yield SkillActivation(skill_dir, working_copy, skill_dir.name)
        return

    from arccli.commands._serve import build_skill_revision_anchor_factory
    from arccli.commands.operator import operator_signer_and_did

    folder = installed_folder(skill_dir)
    try:
        operator_did, signer = operator_signer_and_did()
    except (OSError, ValueError, RuntimeError, SignerError) as exc:
        raise ActivationUnavailableError(f"cannot resolve the operator signer: {exc}") from exc
    with audit_chain("arc skill", lambda: operator_did) as (sink, _):
        factory = build_skill_revision_anchor_factory(sink)
        if factory is None:
            raise ActivationUnavailableError(
                "no revision anchor is configured for this deployment tier"
            )
        resolver = arcagent.AnchoredSkillRevisionResolver(
            agent_did=_agent_did(agent_root),
            config_path=agent_root / "arcagent.toml",
            anchor_factory=factory,
        )
        writer = arcagent.OperatorSkillRevisionWriter(
            authority=lambda: resolver,
            signer=signer,
            operator_did=operator_did,
            folder_of=lambda _name: folder,
        )
        try:
            active = resolver.active_folder(folder)
        except (OSError, ValueError) as exc:
            raise ActivationUnavailableError(str(exc)) from exc
        yield SkillActivation(active or folder, writer, folder.name)


__all__ = [
    "ActivationUnavailableError",
    "FileCommitter",
    "SkillActivation",
    "installed_folder",
    "owning_agent_root",
    "skill_activation",
]
