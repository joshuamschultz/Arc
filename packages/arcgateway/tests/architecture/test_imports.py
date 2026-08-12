"""Module boundary enforcement (SPEC-023 SDD §2).

These tests are AST-based — they parse the source files and inspect the
import statements. They do NOT import the modules under test, so a
forbidden import detected here is reported as a clean test failure
rather than a runtime ImportError further up the dependency chain.
"""

from __future__ import annotations

import ast
import re
import tomllib
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[4]
_ARCUI_SRC = _REPO_ROOT / "packages" / "arcui" / "src" / "arcui"
_ARCGATEWAY_SRC = _REPO_ROOT / "packages" / "arcgateway" / "src" / "arcgateway"
_ARCAGENT_SRC = _REPO_ROOT / "packages" / "arcagent" / "src" / "arcagent"


def _imports_in(path: Path) -> set[str]:
    """Return the set of top-level module names imported by ``path``.

    Walks the AST so we catch both ``import x`` and ``from x import y``.
    Returns the dotted prefix only — e.g. ``arcui.routes.chat_ws`` is
    counted as ``arcui``.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                names.add(node.module)
    return names


def _all_python_files(root: Path) -> list[Path]:
    return [p for p in root.rglob("*.py") if "__pycache__" not in p.parts]


def _qualified_names_on(path: Path, root_module: str) -> set[str]:
    """Every dotted name accessed off ``root_module`` in *path*.

    ``arcagent.keys.KeyStore`` yields ``keys.KeyStore``; the caller decides how
    much of the chain to judge.
    """
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except SyntaxError:
        return set()

    found: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Attribute):
            continue
        parts: list[str] = []
        cursor: ast.expr = node
        while isinstance(cursor, ast.Attribute):
            parts.append(cursor.attr)
            cursor = cursor.value
        if isinstance(cursor, ast.Name) and cursor.id == root_module:
            found.add(".".join(reversed(parts)))
    return found


def _arcagent_public_names() -> frozenset[str]:
    """``arcagent.__all__`` read from source — no import of the package needed."""
    init = _ARCAGENT_SRC / "__init__.py"
    if not init.exists():
        return frozenset()
    tree = ast.parse(init.read_text(encoding="utf-8"), filename=str(init))
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "__all__" for t in node.targets
        ):
            if isinstance(node.value, (ast.List, ast.Tuple)):
                return frozenset(e.value for e in node.value.elts if isinstance(e, ast.Constant))
    return frozenset()


def _violations(root: Path, forbidden_prefixes: tuple[str, ...]) -> list[tuple[Path, str]]:
    """List files under root that import any of the forbidden module prefixes."""
    out: list[tuple[Path, str]] = []
    for path in _all_python_files(root):
        for module in _imports_in(path):
            for prefix in forbidden_prefixes:
                if module == prefix or module.startswith(prefix + "."):
                    out.append((path, module))
    return out


# ── arcui imports arcagent ONLY via the three approved seams ─────────────────

#: The complete set of arcagent modules arcui may import. One module per thing
#: arcui does with an agent: mirror what it loaded, connect it to a system, hold
#: the fleet's provider keys.
_APPROVED_ARCAGENT_SEAMS = frozenset(
    {
        "arcagent.capabilities.inventory",
        "arcagent.connections",
        "arcagent.keys",
    }
)


def test_arcui_imports_arcagent_only_via_approved_seams() -> None:
    """arcui reaches arcagent ONLY through the three approved seams — never anything else.

    The original ruling (arcui-reality-mirror, Option A) allowed exactly one:
    capability views must mirror what an agent actually loads, so arcui consumes
    arcagent's real discovery + trust verdicts via ``arcagent.capabilities.
    inventory``. That ruling assumed arcui is only a VIEW of agents.

    SPEC-064 makes it a control surface as well — an operator sets up a
    connection and a provider key from the browser, which SPEC-062 anticipated
    when it put the install sequence in arcagent so "arctui and arcui drive the
    same code rather than re-deriving the sequence." Driving shared code means
    importing it. So the boundary moves by exactly two modules, and both are
    façades written to be imported by a surface: ``arcagent.connections`` (the
    whole connected-account surface, including the refusal type and the audit
    chain's lifetime) and ``arcagent.keys`` (the write-only provider-key store).

    That is the WHOLE widening. Every other arcagent module — ``core.errors``,
    ``core.tier``, ``extension.*``, ``modules.*`` — remains a forbidden layering
    violation, and a route needing one of them is the signal that the façade is
    missing something, not that this list should grow. The seams stay narrow
    because each is one module whose public API is the contract; a surface that
    reached past them would be re-deriving a sequence arcagent owns.

    Trust, signing, and approval live in ``arctrust`` — the leaf foundation that
    imports no siblings — so arcui imports it FREELY (operator keys, arc-home
    resolution, the TOFU approve/disapprove store) as a legitimate lower layer
    for a view. Only ``arcagent`` is restricted, which is why the
    previously-offending ``approvals.py`` and ``system_config.py`` depend on
    arctrust instead of ``arcagent.core.config``.
    """
    if not _ARCUI_SRC.exists():
        pytest.skip("arcui package not found in this checkout")
    bad = [
        (p, m)
        for p, m in _violations(_ARCUI_SRC, ("arcagent",))
        if m != "arcagent" and m not in _APPROVED_ARCAGENT_SEAMS
    ]
    assert not bad, (
        "arcui imports an arcagent SUBMODULE outside the approved seams "
        f"{sorted(_APPROVED_ARCAGENT_SEAMS)} (SPEC-023 §2.2, narrowed by "
        "arcui-reality-mirror, widened by SPEC-064 to the two control-surface "
        "façades):\n" + "\n".join(f"  {p}: {m}" for p, m in bad)
    )


def test_arcui_uses_only_public_arcagent_names() -> None:
    """Root ``import arcagent`` must not become a back door to everything.

    The submodule allowlist above stopped meaning much once the dependency plan
    moved cross-package consumers to one root import plus qualified names — a
    bare ``import arcagent`` names no submodule, so it passes a scan that only
    reads import statements while still reaching anything at all.

    Narrowness therefore moves to the names: every ``arcagent.X`` arcui actually
    touches must be in arcagent's declared ``__all__``. Same shape as ArcAgent's
    own facade guard over ArcRun. A route reaching ``arcagent.core.something``
    is the signal that the facade is missing an export, not that this check
    should relax.
    """
    if not _ARCUI_SRC.exists():
        pytest.skip("arcui package not found in this checkout")
    public = _arcagent_public_names()
    if not public:
        pytest.skip("arcagent package not found in this checkout")

    bad: list[tuple[Path, str]] = []
    for path in _all_python_files(_ARCUI_SRC):
        for name in _qualified_names_on(path, "arcagent"):
            # Attribute chains that start at an approved seam are already
            # covered by the submodule allowlist above.
            if f"arcagent.{name}" in _APPROVED_ARCAGENT_SEAMS:
                continue
            if name.split(".")[0] not in public:
                bad.append((path, f"arcagent.{name}"))

    assert not bad, (
        "arcui reaches arcagent names that are not in arcagent.__all__ — the "
        "facade is missing an export, or the surface is reaching past it:\n"
        + "\n".join(f"  {p}: {n}" for p, n in sorted(set(bad)))
    )


# ── arcgateway MUST NOT import arcui ──────────────────────────────────────────


def test_arcgateway_does_not_import_arcui() -> None:
    """One-way layering: arcui → arcgateway, never the reverse."""
    if not _ARCGATEWAY_SRC.exists():
        pytest.skip("arcgateway package not found in this checkout")
    bad = _violations(_ARCGATEWAY_SRC, ("arcui",))
    assert not bad, "arcgateway imports arcui (forbidden by SDD §2.2):\n" + "\n".join(
        f"  {p}: {m}" for p, m in bad
    )


# ── Adapters MUST NOT import arcui or arcagent ───────────────────────────────


def test_adapters_do_not_import_arcui_or_arcagent() -> None:
    """Adapters are platform abstractions — they consume gateway primitives only."""
    adapters_dir = _ARCGATEWAY_SRC / "adapters"
    if not adapters_dir.exists():
        pytest.skip("arcgateway.adapters not present")
    bad = _violations(adapters_dir, ("arcui", "arcagent"))
    assert not bad, "adapter imports an upstream module (forbidden by SDD §2.2):\n" + "\n".join(
        f"  {p}: {m}" for p, m in bad
    )


# ── Web adapter is a leaf — must not import bootstrap (composition root) ─────


def test_web_adapter_does_not_import_bootstrap() -> None:
    """Composition flows top-down: bootstrap → adapter, never the reverse."""
    web_path = _ARCGATEWAY_SRC / "adapters" / "web.py"
    if not web_path.exists():
        pytest.skip("web adapter not present")
    imports = _imports_in(web_path)
    assert "arcgateway.bootstrap" not in imports, (
        "WebPlatformAdapter must remain a leaf — bootstrap is its composer"
    )


# ── Gateway core is platform-agnostic ────────────────────────────────────────


#: Platforms whose names must never appear in the registry's own source.
_PLATFORM_NAMES = ("telegram", "slack", "mattermost", "discord")


def test_the_registry_names_no_platform() -> None:
    """Discovery is a scan, so ``registry.py`` cannot know a platform's name.

    SPEC-065 REQ-308. This replaces the pre-T-940 pair of guards, which asserted
    that remote platforms lived in *separate distributions* — the opposite of
    what the spec now requires. One of them survived T-940 only by looking for
    ``telegram.py`` while the platform had become the directory ``telegram/``:
    it passed on a technicality while asserting a principle that had become
    false, which is worse than failing.

    The invariant that actually earns its keep now is that adding or deleting a
    platform needs no edit to the DISCOVERY path.

    Scoped to the discovery functions on purpose. ``OFFICIAL_ADAPTERS`` in the
    same module does name platforms, and legitimately so — it is a trust
    allowlist (federal tier blocks unofficial platforms, and every load is
    audited official/unofficial), which is a different axis from discovery.
    Vetting is supposed to be an explicit list; finding is not.
    """
    source = (_ARCGATEWAY_SRC / "adapters" / "registry.py").read_text(encoding="utf-8")
    tree = ast.parse(source)

    discovery = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
        and node.name in {"discover_adapters", "_load_descriptor"}
    ]
    assert discovery, "discovery functions not found — has registry.py been restructured?"

    named = sorted(
        {
            name
            for node in discovery
            for name in _PLATFORM_NAMES
            if name in ast.get_source_segment(source, node, padded=True).lower()
        }
    )

    assert not named, (
        f"the discovery path in adapters/registry.py names {named} — discovery must "
        "stay a scan for the PLATFORM descriptor, so adding a folder needs no edit "
        "here (REQ-308)"
    )


def test_every_in_tree_platform_exports_the_descriptor() -> None:
    """A platform folder without ``PLATFORM`` is invisible to the scan.

    The failure this catches is silent: the folder is present, its code looks
    wired, and the gateway simply never loads it.
    """
    adapters_dir = _ARCGATEWAY_SRC / "adapters"
    if not adapters_dir.exists():
        pytest.skip("arcgateway.adapters not present")

    missing = [
        folder.name
        for folder in adapters_dir.iterdir()
        if folder.is_dir() and not folder.name.startswith("_") and (folder / "__init__.py").exists()
        if "PLATFORM" not in (folder / "__init__.py").read_text(encoding="utf-8")
    ]

    assert not missing, (
        f"adapter folders export no PLATFORM descriptor and will never be "
        f"discovered: {sorted(missing)}"
    )


# ── arcgateway MUST NOT import a model package (SPEC-065 REQ-301) ────────────

#: The two packages that know model vocabulary. arcgateway is two layers above
#: both (arcllm <- arcrun <- arcagent <- arcgateway) and reaching either would
#: skip arcagent entirely.
_MODEL_PACKAGES = ("arcllm", "arcrun")


def test_model_package_detector_can_actually_fail(tmp_path: Path) -> None:
    """Positive control: prove the scan below reports a real violation.

    A boundary test that can only ever pass is worse than no test — it reads as
    evidence while checking nothing. Plant every import form and require the
    detector to catch all of them.
    """
    planted = tmp_path / "planted.py"
    planted.write_text(
        "import arcllm\nfrom arcrun import run\nimport arcllm.types\n",
        encoding="utf-8",
    )

    found = _violations(tmp_path, _MODEL_PACKAGES)

    assert {module for _, module in found} == {"arcllm", "arcrun", "arcllm.types"}


def test_arcgateway_imports_no_model_package() -> None:
    """Media translation is the agent's job, not the gateway's (SPEC-065 COMP-009).

    The gateway carries a *workspace reference* through to arcagent and never
    holds a content block, which is why it needs no model vocabulary at all. If
    a media part ever had to become an ImageBlock here, the bytes would be in
    the gateway's hands — and the layer that has the bytes is the layer that
    leaks them into a queue or a log.
    """
    if not _ARCGATEWAY_SRC.exists():
        pytest.skip("arcgateway package not found in this checkout")

    bad = _violations(_ARCGATEWAY_SRC, _MODEL_PACKAGES)

    assert not bad, (
        "arcgateway imports a model package — translation belongs in arcagent "
        "(SPEC-065 COMP-009):\n" + "\n".join(f"  {p}: {m}" for p, m in bad)
    )


def test_arcgateway_metadata_declares_no_model_package() -> None:
    """Source and packaging must state the same graph; a dependency is a promise."""
    pyproject = _REPO_ROOT / "packages" / "arcgateway" / "pyproject.toml"
    with pyproject.open("rb") as handle:
        declared = tomllib.load(handle)["project"].get("dependencies", [])

    names = {
        re.split(r"[^A-Za-z0-9_.-]", entry.strip(), maxsplit=1)[0].lower() for entry in declared
    }

    assert not names & {"arcllm", "arcrun", "arc-llm", "arc-run"}
