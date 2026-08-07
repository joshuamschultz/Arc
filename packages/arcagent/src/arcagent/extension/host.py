"""SPEC-062 COMP-018 — ``HostPrerequisiteDirector`` directs the operator, never installs.

A host prerequisite — a runtime, a binary, a system package — is category one of
D-567: something only the machine's owner can put there. Arc detects whether it is
already present and, when it is not, names exactly what the operator must run to
get it. It never runs that command itself.

This is the whole of REQ-262: the machine-level decision stays visible and stays
the operator's. A component that could "helpfully" install the prerequisite would
turn a declared manifest into a way to run an arbitrary command on the host with
no operator in the loop — the same class of hole that keeps
:mod:`arcagent.extension.loader` from ever writing outside the extension's own
folder. So this module holds no subprocess call of any kind; presence detection
is a filesystem/PATH lookup, injected for testability rather than reached for
directly, so a test can prove the negative (no subprocess ever runs) without
depending on what happens to be installed on the machine running the suite.
"""

from __future__ import annotations

import shutil
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from arcagent.extension.manifest import HostRequirement


@dataclass(frozen=True)
class HostVerdict:
    """Whether one declared host prerequisite is present, and what to do if not.

    Attributes:
        name: The prerequisite's declared name (a binary, runtime, or package).
        satisfied: True when the prerequisite was found on the host.
        instruction: Empty when satisfied. Otherwise the exact text to show the
            operator — the manifest's own instruction when it supplied one, or a
            generic fallback that still names the missing prerequisite.
    """

    name: str
    satisfied: bool
    instruction: str = ""


class HostPrerequisiteDirector:
    """Detects declared host prerequisites and directs the operator — never installs one.

    Args:
        path_lookup: How presence is checked. Defaults to :func:`shutil.which`,
            which only reads ``PATH``; injectable so tests never depend on what
            happens to be installed on the machine running the suite.
    """

    def __init__(self, *, path_lookup: Callable[[str], str | None] = shutil.which) -> None:
        self._path_lookup = path_lookup

    def check(self, requirements: Sequence[HostRequirement]) -> list[HostVerdict]:
        """Check every declared host prerequisite, in the order supplied.

        Args:
            requirements: The manifest's ``[[host_requires]]`` entries.

        Returns:
            One verdict per requirement. A missing prerequisite carries the
            exact instruction to show the operator; nothing here ever installs
            it, on this path or any other.
        """
        return [self._check_one(requirement) for requirement in requirements]

    def unsatisfied(self, requirements: Sequence[HostRequirement]) -> list[HostVerdict]:
        """The subset of :meth:`check` that still needs operator action."""
        return [verdict for verdict in self.check(requirements) if not verdict.satisfied]

    def _check_one(self, requirement: HostRequirement) -> HostVerdict:
        if self._path_lookup(requirement.name) is not None:
            return HostVerdict(name=requirement.name, satisfied=True)
        return HostVerdict(
            name=requirement.name,
            satisfied=False,
            instruction=requirement.instruction or _default_instruction(requirement),
        )


def _default_instruction(requirement: HostRequirement) -> str:
    """A generic directive when the manifest did not author its own."""
    version_clause = (
        f" (minimum version {requirement.minimum_version})" if requirement.minimum_version else ""
    )
    return (
        f"install {requirement.name}{version_clause} on this host using your platform's "
        "package manager or the vendor's installer, then ensure it is on PATH"
    )


__all__ = ["HostPrerequisiteDirector", "HostVerdict"]
