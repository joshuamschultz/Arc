"""SPEC-062 T-912 (RED) — ``HostPrerequisiteDirector`` detects and instructs, never installs.

COMP-018, serving REQ-262. A host prerequisite — a runtime, a binary, a system
package — is a machine-level change, and a machine-level change is the operator's
decision, not Arc's. So this component only ever does two things: says whether the
prerequisite is already there, and, when it is not, names exactly what to run to
get it. It never runs an installer itself.

These tests pin:

* presence detection is pluggable (no assumption about what happens to be on the
  machine running the suite);
* a missing prerequisite carries the manifest's own instruction when the manifest
  supplied one, or a generic fallback naming the prerequisite when it did not;
* :meth:`HostPrerequisiteDirector.unsatisfied` filters to only what still needs
  operator action;
* no code path here ever spawns a subprocess — the one way an "install" could
  sneak in unnoticed.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable

import pytest

from arcagent.extension.host import HostPrerequisiteDirector, HostVerdict
from arcagent.extension.manifest import HostRequirement


def _always_present(_: str) -> str | None:
    return "/usr/bin/found"


def _always_missing(_: str) -> str | None:
    return None


def _lookup_from(present: set[str]) -> Callable[[str], str | None]:
    def _lookup(name: str) -> str | None:
        return f"/usr/bin/{name}" if name in present else None

    return _lookup


def test_check_marks_a_present_prerequisite_satisfied() -> None:
    """Found on the host: satisfied, no instruction to show."""
    director = HostPrerequisiteDirector(path_lookup=_always_present)

    verdicts = director.check([HostRequirement(name="node")])

    assert verdicts == [HostVerdict(name="node", satisfied=True, instruction="")]


def test_check_marks_a_missing_prerequisite_unsatisfied() -> None:
    """Not found on the host: unsatisfied, and an instruction is present."""
    director = HostPrerequisiteDirector(path_lookup=_always_missing)

    verdicts = director.check([HostRequirement(name="node")])

    assert len(verdicts) == 1
    assert verdicts[0].satisfied is False
    assert verdicts[0].instruction != ""


def test_missing_prerequisite_uses_the_manifest_instruction_verbatim() -> None:
    """When the manifest authored an instruction, that exact text is what shows."""
    director = HostPrerequisiteDirector(path_lookup=_always_missing)
    requirement = HostRequirement(name="node", instruction="run `brew install node`")

    verdicts = director.check([requirement])

    assert verdicts[0].instruction == "run `brew install node`"


def test_missing_prerequisite_falls_back_to_a_generic_instruction_naming_it() -> None:
    """No manifest instruction: the fallback still names the exact prerequisite."""
    director = HostPrerequisiteDirector(path_lookup=_always_missing)

    verdicts = director.check([HostRequirement(name="ripgrep")])

    assert "ripgrep" in verdicts[0].instruction


def test_check_preserves_requirement_order() -> None:
    """Verdicts come back in the order the requirements were declared."""
    director = HostPrerequisiteDirector(path_lookup=_lookup_from({"node"}))
    requirements = [HostRequirement(name="node"), HostRequirement(name="ripgrep")]

    verdicts = director.check(requirements)

    assert [v.name for v in verdicts] == ["node", "ripgrep"]
    assert verdicts[0].satisfied is True
    assert verdicts[1].satisfied is False


def test_unsatisfied_filters_to_only_the_missing_prerequisites() -> None:
    """The convenience filter drops what is already present."""
    director = HostPrerequisiteDirector(path_lookup=_lookup_from({"node"}))
    requirements = [HostRequirement(name="node"), HostRequirement(name="ripgrep")]

    missing = director.unsatisfied(requirements)

    assert [v.name for v in missing] == ["ripgrep"]


def test_check_with_no_requirements_returns_nothing() -> None:
    director = HostPrerequisiteDirector(path_lookup=_always_present)

    assert director.check([]) == []


def test_director_never_spawns_a_subprocess(monkeypatch: pytest.MonkeyPatch) -> None:
    """The one way an implicit install could sneak in is a subprocess call.

    ``subprocess.run``/``Popen`` are patched to raise if invoked at all, then the
    director is driven through both the present and the missing path — REQ-262
    forbids Arc installing a host prerequisite on the operator's behalf, and this
    is the test that would catch a regression that tried.
    """

    def _forbidden(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("HostPrerequisiteDirector must never spawn a subprocess")

    monkeypatch.setattr(subprocess, "run", _forbidden)
    monkeypatch.setattr(subprocess, "Popen", _forbidden)

    director = HostPrerequisiteDirector(path_lookup=_lookup_from({"node"}))
    requirements = [HostRequirement(name="node"), HostRequirement(name="ripgrep")]

    verdicts = director.check(requirements)

    assert len(verdicts) == 2
