"""Every package a deployment actually runs must be reachable from ``uv sync``.

Background:
    A workspace member is only installed if something depends on it. ``arcui``
    was a member that nothing depended on, so ``uv sync`` resolved a tree
    without it and every surface serving the dashboard died at startup on
    "No module named 'arcui'" — the container ENTRYPOINT, deploy/cloud, the
    systemd unit, and ``arc tui``'s attach-or-serve path.

    That failure is expensive to read. ``arc ui start`` is the LAST step of
    deploy/entrypoint.sh, so the agents scaffold, log Ready, and print a
    dashboard URL before the process dies. It looks like an agent fault.

    It had also already happened once and been absorbed as an operational rule
    ("always sync with --all-packages") instead of being fixed, which is exactly
    why it recurred. A rule in a runbook protects the person who read the
    runbook; a dependency edge protects everyone.

What this asserts:
    Each package below is in the *resolved dependency closure* of the root
    ``arc`` project in uv.lock — i.e. a plain ``uv sync`` installs it. The
    closure is read from the lock rather than from pyproject so the test fails
    on an un-relocked pyproject edit too, which is the state that would actually
    ship a broken image (``uv sync --frozen`` in the Dockerfile reads the lock).

    ``--all-packages`` is deliberately NOT the standard being tested. It papers
    over a missing edge, and extras (``arcmemory[local]``) are not installed by
    it at all.

Usage::

    uv run pytest tests/architecture/test_deployment_closure.py -v
"""

from __future__ import annotations

import tomllib
from pathlib import Path

# Packages a deployment invokes directly, with the surface that needs each.
# A package here must survive a plain `uv sync`; anything reached only through
# another package's dependencies is intentionally absent (it is covered by the
# closure walk, not by this list).
_DEPLOYMENT_PACKAGES: dict[str, str] = {
    "arc-agent": "the agent runtime itself",
    "arccmd": "the `arc` console entry point",
    "arcui": "`arc ui start` — dashboard, /ws/chat, embedded gateway",
    "arcgateway": "gateway core (with in-tree platform adapters), wired by `arc ui start`",
    "arcmemory": 'the default brain (brain = "arcmemory") for scaffolded agents',
    "arcskill": 'the default skills adapter (skills.adapter = "arcskill")',
}


def _repo_root() -> Path:
    """Repository root — this file lives at <root>/tests/architecture/."""
    return Path(__file__).parent.parent.parent.resolve()


def _lock_packages() -> dict[str, dict]:
    """Every ``[[package]]`` entry in uv.lock, keyed by name."""
    with (_repo_root() / "uv.lock").open("rb") as handle:
        return {pkg["name"]: pkg for pkg in tomllib.load(handle)["package"]}


def _closure(packages: dict[str, dict], root: str) -> set[str]:
    """Names reachable from ``root`` through resolved (non-dev) dependencies."""
    seen: set[str] = set()
    stack = [root]
    while stack:
        name = stack.pop()
        if name in seen or name not in packages:
            continue
        seen.add(name)
        stack.extend(dep["name"] for dep in packages[name].get("dependencies", []))
    return seen


def test_deployment_packages_are_in_the_default_sync_closure() -> None:
    """A plain `uv sync` must install every package a deployment runs."""
    packages = _lock_packages()
    assert "arc" in packages, "root project 'arc' not found in uv.lock"

    reachable = _closure(packages, "arc")
    missing = {
        name: reason for name, reason in _DEPLOYMENT_PACKAGES.items() if name not in reachable
    }

    assert not missing, (
        "These packages are NOT reachable from a plain `uv sync`:\n"
        + "\n".join(f"  {name} — {reason}" for name, reason in sorted(missing.items()))
        + "\n\n"
        "A workspace member is only installed if something depends on it. Add the\n"
        "package to [project].dependencies in the root pyproject.toml (with a comment\n"
        "saying which surface needs it), then re-run `uv lock`.\n\n"
        "Do NOT fix this by switching a deployment to `uv sync --all-packages`:\n"
        "it hides the missing edge, and it does not install extras such as\n"
        "arcmemory[local] — which is how semantic recall silently degraded before.\n"
    )


def test_every_deployment_package_is_a_real_workspace_member() -> None:
    """Guard the list itself: a typo here would make the test vacuously pass."""
    packages = _lock_packages()
    unknown = sorted(name for name in _DEPLOYMENT_PACKAGES if name not in packages)
    assert not unknown, (
        "Unknown package name(s) in _DEPLOYMENT_PACKAGES: "
        + ", ".join(unknown)
        + "\nThese match nothing in uv.lock, so they assert nothing. Fix the spelling "
        "or drop the entry."
    )
