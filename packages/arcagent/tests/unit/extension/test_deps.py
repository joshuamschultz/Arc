"""SPEC-062 T-913 (RED) — ``DependencyResolver``: conflict refusal, reference-counted removal.

COMP-017, serving REQ-264 and REQ-284. Josh chose declared dependencies installed
into the shared environment over per-extension vendoring (SDD "Alternatives
Considered") — lighter and more familiar, at the cost that a version conflict
between two extensions is unresolvable in a shared environment. Two consequences
follow, and these tests pin both:

* **Conflict refusal at install** — an unsatisfiable version constraint between
  the extension being installed and any other already-installed extension is
  refused before it can quietly break a working extension, and the refusal names
  both extensions and both constraints.
* **Reference-counted removal** — removing an extension only drops a package
  declaration nothing else still needs; a package another installed extension
  still declares is retained.
"""

from __future__ import annotations

import pytest

from arcagent.core.errors import ExtensionError
from arcagent.extension.deps import DependencyResolver


@pytest.fixture
def resolver() -> DependencyResolver:
    return DependencyResolver()


def test_plan_install_with_no_conflicts_lists_the_new_packages(
    resolver: DependencyResolver,
) -> None:
    """Nothing else declares these packages yet: both are new to the environment."""
    plan = resolver.plan_install("acme_tickets", ["httpx>=0.27", "pydantic>=2.0"], {})

    assert plan.extension == "acme_tickets"
    assert set(plan.to_install) == {"httpx>=0.27", "pydantic>=2.0"}


def test_plan_install_does_not_recount_a_package_another_extension_already_declares(
    resolver: DependencyResolver,
) -> None:
    """A package another extension already declared is not "new" to the environment."""
    plan = resolver.plan_install(
        "acme_tickets", ["httpx>=0.27"], {"other_ext": ["httpx>=0.27"]}
    )

    assert plan.to_install == ()


def test_plan_install_refuses_conflicting_exact_pins(resolver: DependencyResolver) -> None:
    """Two extensions pinning the same package to different exact versions cannot coexist."""
    with pytest.raises(ExtensionError) as excinfo:
        resolver.plan_install(
            "acme_tickets", ["acme-sdk==1.0.0"], {"other_ext": ["acme-sdk==2.0.0"]}
        )

    message = str(excinfo.value)
    assert "acme_tickets" in message
    assert "other_ext" in message
    assert "acme-sdk" in message


def test_plan_install_refuses_disjoint_version_ranges(resolver: DependencyResolver) -> None:
    """No version satisfies both ranges: refused rather than discovered at runtime."""
    with pytest.raises(ExtensionError):
        resolver.plan_install(
            "acme_tickets", ["acme-sdk>=1.0,<2.0"], {"other_ext": ["acme-sdk>=3.0,<4.0"]}
        )


def test_plan_install_allows_overlapping_ranges(resolver: DependencyResolver) -> None:
    """Ranges that share satisfiable versions are not a conflict."""
    plan = resolver.plan_install(
        "acme_tickets", ["acme-sdk>=1.0,<2.0"], {"other_ext": ["acme-sdk>=1.5,<2.5"]}
    )

    assert plan.extension == "acme_tickets"


def test_plan_install_allows_identical_declarations(resolver: DependencyResolver) -> None:
    """Two extensions declaring the exact same constraint never conflict with themselves."""
    plan = resolver.plan_install(
        "acme_tickets", ["httpx>=0.27,<1"], {"other_ext": ["httpx>=0.27,<1"]}
    )

    assert plan.to_install == ()


def test_plan_install_raises_on_an_unparsable_requirement(resolver: DependencyResolver) -> None:
    with pytest.raises(ExtensionError):
        resolver.plan_install("acme_tickets", ["not a requirement!!"], {})


def test_conflict_refusal_names_both_constraints(resolver: DependencyResolver) -> None:
    with pytest.raises(ExtensionError) as excinfo:
        resolver.plan_install(
            "acme_tickets", ["acme-sdk==1.0.0"], {"other_ext": ["acme-sdk==2.0.0"]}
        )

    assert excinfo.value.details["package"] == "acme-sdk"
    assert "1.0.0" in excinfo.value.details["requirement"]
    assert "2.0.0" in excinfo.value.details["conflicting_requirement"]


def test_plan_removal_drops_packages_nothing_else_declares(resolver: DependencyResolver) -> None:
    plan = resolver.plan_removal("acme_tickets", ["httpx>=0.27", "acme-sdk>=1.0"], {})

    assert set(plan.to_drop) == {"httpx", "acme-sdk"}
    assert plan.retained == ()


def test_plan_removal_retains_packages_another_extension_still_declares(
    resolver: DependencyResolver,
) -> None:
    """Removing one extension must never strip a dependency another one needs."""
    plan = resolver.plan_removal(
        "acme_tickets",
        ["httpx>=0.27", "acme-sdk>=1.0"],
        {"other_ext": ["httpx>=0.27,<1"]},
    )

    assert plan.to_drop == ("acme-sdk",)
    assert plan.retained == ("httpx",)


def test_plan_removal_of_a_package_no_one_else_declared_is_unaffected_by_unrelated_extensions(
    resolver: DependencyResolver,
) -> None:
    plan = resolver.plan_removal(
        "acme_tickets", ["acme-sdk>=1.0"], {"other_ext": ["unrelated-pkg>=1.0"]}
    )

    assert plan.to_drop == ("acme-sdk",)
    assert plan.retained == ()
