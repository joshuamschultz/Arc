"""SPEC-062 COMP-017 — the same conflict refusal has to run at UPGRADE time.

The SDD names the residual risk of Josh's shared-environment choice plainly:
"the residual risk is that a later upgrade of one extension breaks another".
:meth:`~arcagent.extension.deps.DependencyResolver.plan_install` only guards the
moment an extension arrives. An extension that is already installed can raise or
narrow its declarations on a later version and silently make another installed
extension unsatisfiable — the shared environment has one version of a package,
not one per extension.

These tests pin the upgrade half:

* an upgrade whose NEW declarations conflict with another installed extension is
  refused, naming both extensions and both constraints,
* the extension's OWN superseded declarations never count against it — an
  upgrade is not a self-conflict,
* an upgrade reports what the environment must add and what it may drop, so the
  shared environment converges on the new declarations instead of accumulating
  every version an extension ever asked for.
"""

from __future__ import annotations

import pytest

from arcagent.core.errors import ExtensionError
from arcagent.extension.deps import DependencyResolver


@pytest.fixture
def resolver() -> DependencyResolver:
    return DependencyResolver()


def test_upgrade_is_refused_when_the_new_pin_breaks_another_extension(
    resolver: DependencyResolver,
) -> None:
    """The failure the SDD names: a later upgrade of one extension breaks another."""
    with pytest.raises(ExtensionError) as exc:
        resolver.plan_upgrade(
            "acme_tickets",
            current=["httpx>=0.27,<1.0"],
            requires=["httpx>=2.0"],
            installed={"other_connector": ["httpx<1.0"]},
        )

    assert exc.value.code == "EXTENSION_DEPENDENCY_CONFLICT"
    assert exc.value.details["conflicting_extension"] == "other_connector"
    assert exc.value.details["package"] == "httpx"


def test_upgrade_does_not_conflict_with_its_own_superseded_declaration(
    resolver: DependencyResolver,
) -> None:
    """The version being replaced must not veto the version replacing it."""
    plan = resolver.plan_upgrade(
        "acme_tickets",
        current=["httpx<1.0"],
        requires=["httpx>=2.0"],
        installed={},
    )

    assert plan.extension == "acme_tickets"
    assert plan.to_install == ("httpx>=2.0",)


def test_upgrade_reports_added_and_dropped_packages(resolver: DependencyResolver) -> None:
    """A dependency the new version no longer declares is released."""
    plan = resolver.plan_upgrade(
        "acme_tickets",
        current=["httpx>=0.27", "lxml>=5.0"],
        requires=["httpx>=0.27", "orjson>=3.0"],
        installed={},
    )

    assert plan.to_install == ("orjson>=3.0",)
    assert plan.to_drop == ("lxml",)


def test_upgrade_retains_a_dropped_package_another_extension_still_needs(
    resolver: DependencyResolver,
) -> None:
    """Reference counting holds on upgrade exactly as it does on removal."""
    plan = resolver.plan_upgrade(
        "acme_tickets",
        current=["lxml>=5.0"],
        requires=[],
        installed={"other_connector": ["lxml>=5.0"]},
    )

    assert plan.to_drop == ()
    assert plan.retained == ("lxml",)


def test_upgrade_does_not_reinstall_a_package_another_extension_already_declares(
    resolver: DependencyResolver,
) -> None:
    """The shared environment already has it; the upgrade adds nothing."""
    plan = resolver.plan_upgrade(
        "acme_tickets",
        current=[],
        requires=["httpx>=0.27"],
        installed={"other_connector": ["httpx>=0.27"]},
    )

    assert plan.to_install == ()


def test_upgrade_raises_on_an_unparsable_new_requirement(resolver: DependencyResolver) -> None:
    """An upgrade is validated as strictly as an install."""
    with pytest.raises(ExtensionError) as exc:
        resolver.plan_upgrade(
            "acme_tickets", current=[], requires=["not a requirement!!"], installed={}
        )

    assert exc.value.code == "EXTENSION_DEPENDENCY_INVALID"
