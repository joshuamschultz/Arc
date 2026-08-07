"""SPEC-062 COMP-017 — ``DependencyResolver``: declared deps, conflict refusal.

Josh chose declared dependencies installed into the shared environment over
per-extension vendoring (SDD "Alternatives Considered") — lighter and more
familiar than an isolated environment per extension, at the cost that a version
conflict between two extensions is unresolvable once it exists. Three
consequences follow from that trade, and this module is exactly those three:

* **Conflict refusal at install.** If installing an extension would require a
  version of a package that no other installed extension's own declaration can
  also accept, the install is refused before it happens — discovering the
  conflict later, when a call fails at runtime, is the failure mode this exists
  to prevent.
* **Conflict refusal at upgrade.** Declarations change at two moments, not one.
  An extension already living in the environment can break a neighbour simply by
  raising a constraint in its next release, which is the residual risk the SDD
  names; the same check therefore runs before an upgrade replaces the old
  declarations, and the version being replaced never vetoes its replacement.
* **Reference-counted removal.** Removing an extension must never strip a
  package another installed extension still declares, so a removal reference-
  counts declarations rather than dropping everything the removed extension
  brought in. An upgrade releases what its new version stopped declaring by the
  same rule.

This module holds no state of its own — every method takes the current
declarations as an argument. The single source of truth for "what does each
extension declare" is :class:`~arcagent.extension.state.ConnectionStateStore`'s
persisted ``dependency_declarations`` (COMP-019); this resolver only computes
plans over whatever the caller hands it.

Requirement strings are parsed with :mod:`packaging`, the standard library for
PEP 508/440 requirement and version-specifier parsing — hand-rolling that syntax
would be a second, worse implementation of what already ships correctly.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from packaging.requirements import InvalidRequirement, Requirement
from packaging.specifiers import SpecifierSet
from packaging.version import InvalidVersion, Version

from arcagent.core.errors import ExtensionError


@dataclass(frozen=True)
class InstallPlan:
    """What installing one extension's declared dependencies would do.

    Attributes:
        extension: The extension being installed.
        to_install: Requirement strings not already declared by any other
            installed extension — new packages this install adds to the
            shared environment.
    """

    extension: str
    to_install: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class UpgradePlan:
    """What moving one extension from one set of declarations to another would do.

    An upgrade is an install and a removal in the same step, so it carries both
    halves: what the shared environment must add, and what the superseded
    declarations release.

    Attributes:
        extension: The extension being upgraded.
        to_install: Requirement strings the environment must satisfy that it does
            not already — a package nothing declared before, or one whose
            constraint this upgrade changed.
        to_drop: Package names the new version no longer declares and no other
            installed extension needs.
        retained: Package names the new version dropped but another installed
            extension still declares — left alone.
    """

    extension: str
    to_install: tuple[str, ...] = field(default_factory=tuple)
    to_drop: tuple[str, ...] = field(default_factory=tuple)
    retained: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class RemovalPlan:
    """What removing one extension's declared dependencies would drop.

    Attributes:
        extension: The extension being removed.
        to_drop: Package names no other installed extension still declares —
            safe to remove from the shared environment.
        retained: Package names this extension declared that at least one
            other installed extension still needs — left alone.
    """

    extension: str
    to_drop: tuple[str, ...] = field(default_factory=tuple)
    retained: tuple[str, ...] = field(default_factory=tuple)


class DependencyResolver:
    """Refuses install-time conflicts; reference-counts removal.

    Holds no state: every method takes the current declarations as an argument,
    so the caller's persisted record stays the single source of truth.
    """

    def plan_install(
        self,
        extension: str,
        requires: Sequence[str],
        installed: Mapping[str, Sequence[str]],
    ) -> InstallPlan:
        """Check ``requires`` against every other already-installed extension.

        Args:
            extension: The extension being installed.
            requires: Its declared requirement strings.
            installed: Every OTHER already-installed extension's declared
                requirement strings, keyed by extension name. ``extension``
                itself must not appear here — a reinstall is a removal
                followed by an install, not a self-comparison.

        Returns:
            The install plan: which requirement strings are new to the
            environment (no other extension already declares that package).

        Raises:
            ExtensionError: A requirement in ``requires`` is unparsable, or its
                version constraint cannot be satisfied together with a
                constraint another installed extension already declares for
                the same package. The message and ``details`` name both
                extensions and both constraints.
        """
        parsed = _parse_all(extension, requires)
        by_name = _index_by_name(installed)

        for requirement in parsed:
            for other_extension, other_requirement in by_name.get(requirement.name, ()):
                if _conflicts(requirement.specifier, other_requirement.specifier):
                    raise ExtensionError(
                        code="EXTENSION_DEPENDENCY_CONFLICT",
                        message=(
                            f"extension '{extension}' requires {requirement} but "
                            f"'{other_extension}' already requires {other_requirement}; "
                            f"no version of '{requirement.name}' satisfies both"
                        ),
                        details={
                            "extension": extension,
                            "conflicting_extension": other_extension,
                            "package": requirement.name,
                            "requirement": str(requirement),
                            "conflicting_requirement": str(other_requirement),
                        },
                    )

        already_declared = set(by_name)
        to_install = tuple(
            str(requirement) for requirement in parsed if requirement.name not in already_declared
        )
        return InstallPlan(extension=extension, to_install=to_install)

    def plan_upgrade(
        self,
        extension: str,
        *,
        current: Sequence[str],
        requires: Sequence[str],
        installed: Mapping[str, Sequence[str]],
    ) -> UpgradePlan:
        """Check a new version's declarations before the upgrade replaces the old ones.

        The shared environment holds one version of a package, not one per
        extension, so an already-installed extension can break another simply by
        raising a constraint in its next release. That is the residual risk the
        SDD names, and it is the same conflict check as install — run at the
        other moment declarations change.

        Args:
            extension: The extension being upgraded.
            current: Its declarations BEFORE the upgrade. Excluded from the
                conflict check: the version being replaced must never veto the
                version replacing it.
            requires: Its declarations after the upgrade.
            installed: Every OTHER installed extension's declarations, keyed by
                extension name. ``extension`` itself must not appear here.

        Returns:
            What the environment must add, and what the superseded declarations
            release (reference-counted, so nothing another extension still needs
            is dropped).

        Raises:
            ExtensionError: A new requirement is unparsable, or conflicts with a
                constraint another installed extension declares for the same
                package. The message names both extensions and both constraints.
        """
        install = self.plan_install(extension, requires, installed)
        held = _parse_lenient(current)
        # A package another extension declares is already present at a version
        # compatible with both (the conflict above would have refused otherwise),
        # so plan_install already excluded it. What remains to filter is this
        # extension's OWN unchanged declarations — an identical constraint needs
        # no work, while a changed one means the pinned build changed and the
        # environment must be moved to it.
        unchanged = {str(requirement) for requirement in held.values()}
        to_install = tuple(
            requirement for requirement in install.to_install if requirement not in unchanged
        )
        kept = {requirement.name for requirement in _parse_all(extension, requires)}
        released = [raw for raw, req in held.items() if req.name not in kept]
        removal = self.plan_removal(extension, released, installed)
        return UpgradePlan(
            extension=extension,
            to_install=to_install,
            to_drop=removal.to_drop,
            retained=removal.retained,
        )

    def plan_removal(
        self,
        extension: str,
        requires: Sequence[str],
        installed: Mapping[str, Sequence[str]],
    ) -> RemovalPlan:
        """Reference-count ``extension``'s declarations against every other extension.

        Args:
            extension: The extension being removed.
            requires: Its declared requirement strings — what it brought in.
            installed: Every OTHER still-installed extension's declared
                requirement strings, keyed by extension name.

        Returns:
            Which package names are safe to drop (nothing else declares them)
            and which are retained (at least one other extension still needs
            them). Removing ``extension`` must never strip a ``retained`` name.
        """
        parsed = _parse_all(extension, requires)
        by_name = _index_by_name(installed)

        to_drop: list[str] = []
        retained: list[str] = []
        for requirement in parsed:
            target = retained if requirement.name in by_name else to_drop
            target.append(requirement.name)
        return RemovalPlan(extension=extension, to_drop=tuple(to_drop), retained=tuple(retained))


def _parse_all(extension: str, requires: Sequence[str]) -> list[Requirement]:
    """Parse every requirement string, refusing the whole batch on the first bad one."""
    parsed: list[Requirement] = []
    for raw in requires:
        try:
            parsed.append(Requirement(raw))
        except InvalidRequirement as exc:
            raise ExtensionError(
                code="EXTENSION_DEPENDENCY_INVALID",
                message=(
                    f"extension '{extension}' declares an unparsable requirement {raw!r}: {exc}"
                ),
                details={"extension": extension, "requirement": raw},
            ) from exc
    return parsed


def _parse_lenient(requires: Sequence[str]) -> dict[str, Requirement]:
    """Parse already-persisted declarations, skipping any that no longer parse.

    Same reasoning as :func:`_index_by_name`: these were accepted at an earlier
    install, so one bad stored string must not be able to block an upgrade.
    """
    parsed: dict[str, Requirement] = {}
    for raw in requires:
        try:
            parsed[raw] = Requirement(raw)
        except InvalidRequirement:
            continue
    return parsed


def _index_by_name(
    installed: Mapping[str, Sequence[str]],
) -> dict[str, list[tuple[str, Requirement]]]:
    """Every other extension's parsed requirements, grouped by package name.

    A requirement that fails to parse here is skipped rather than raised: it
    was accepted at some earlier install, and one already-installed
    extension's stored declaration must never be able to block a different
    extension's install or removal outright.
    """
    by_name: dict[str, list[tuple[str, Requirement]]] = defaultdict(list)
    for other_extension, raw_requirements in installed.items():
        for raw in raw_requirements:
            try:
                requirement = Requirement(raw)
            except InvalidRequirement:
                continue
            by_name[requirement.name].append((other_extension, requirement))
    return dict(by_name)


def _conflicts(a: SpecifierSet, b: SpecifierSet) -> bool:
    """Whether no version could satisfy both ``a`` and ``b``.

    ``packaging`` has no direct "is this specifier set satisfiable" check —
    versions form a dense, totally ordered space, not a finite universe to
    enumerate. The candidates tested here are every literal version either side
    names, plus each one's ``.post0`` build — which PEP 440 sorts immediately
    after the literal but before anything greater — so an exclusive bound
    sitting right next to another side's inclusive bound (``>1.0`` against
    ``<=1.0``, or two identical ranges like ``>1.0,<3.0``) is exercised without
    needing full interval arithmetic. When neither side names a version at all
    (both unconstrained), the combined set has no clauses and is trivially
    satisfiable.
    """
    combined = a & b
    if not any(True for _ in combined):
        return False
    candidates: set[Version] = set()
    for specifier_set in (a, b):
        for spec in specifier_set:
            try:
                version = Version(spec.version)
            except InvalidVersion:
                continue
            candidates.add(version)
            candidates.add(Version(f"{version}.post0"))
    return not any(candidate in combined for candidate in candidates)


__all__ = ["DependencyResolver", "InstallPlan", "RemovalPlan", "UpgradePlan"]
