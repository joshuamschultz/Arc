"""SPEC-073 Phase 4 (Polish) — COMP-015 (T-1048) boundary + determinism guard.

Static AST scans over ``src/arcmemory`` (mirrors ``test_no_arcagent_import.py``):

1. No module imports ``arcagent`` (re-asserted here for the new data-source modules).
2. No module imports a scheduler/connector/gateway package -- the ingest trigger
   is a connector-side signed workflow run; arcmemory imports none of that wiring.
3. ``import arcrun`` is confined to the single adapter (``react_adapter.py``).
4. DETERMINISM: the deterministic ingest/rank modules (``sync.py``, ``router.py``,
   ``fusion.py``, ``index/backend.py``, ``ingest.py``) import no model/LLM seam --
   the embedder is injected into ``DocIndex``/``SurfaceIndex``, never imported by
   these modules directly.
5. POISON-OBJECT seam: ``fusion.py`` (the fusion/gate code) does not import
   ``index.backend``'s concrete backend by name -- swapping the backend needs no
   fusion/gate change.
"""

from __future__ import annotations

import ast
from pathlib import Path

_SRC = Path(__file__).resolve().parents[2] / "src" / "arcmemory"

_FORBIDDEN_ALWAYS = {"arcagent"}
_FORBIDDEN_CONNECTOR = {"arcgateway", "croniter", "cronsim", "apscheduler"}
_ARCRUN = "arcrun"
_ARCRUN_ADAPTER = "react_adapter.py"

_DETERMINISTIC_MODULES = {
    "sync.py",
    "router.py",
    "fusion.py",
    Path("index") / "backend.py",
    "ingest.py",
}
_FORBIDDEN_MODEL_SEAMS = {"arcllm", "arcprompt"}


def _imported_roots(tree: ast.AST) -> set[str]:
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            roots.add(node.module.split(".")[0])
    return roots


def _parse(path: Path) -> ast.AST:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


# -- (1) arcagent boundary, re-asserted for the new modules ---------------------


def test_no_datasource_module_imports_arcagent() -> None:
    offenders: dict[str, set[str]] = {}
    for path in _SRC.rglob("*.py"):
        bad = _imported_roots(_parse(path)) & _FORBIDDEN_ALWAYS
        if bad:
            offenders[str(path.relative_to(_SRC))] = bad
    assert not offenders, f"arcmemory must not import {_FORBIDDEN_ALWAYS}: {offenders}"


# -- (2) no scheduler/connector/gateway wiring -----------------------------------


def test_no_module_imports_a_scheduler_or_connector_package() -> None:
    offenders: dict[str, set[str]] = {}
    for path in _SRC.rglob("*.py"):
        bad = _imported_roots(_parse(path)) & _FORBIDDEN_CONNECTOR
        if bad:
            offenders[str(path.relative_to(_SRC))] = bad
    assert not offenders, (
        f"arcmemory must not import scheduler/connector/gateway packages "
        f"({_FORBIDDEN_CONNECTOR}) -- the trigger is a connector-side signed "
        f"workflow_run, never a package arcmemory pulls in itself: {offenders}"
    )


# -- (3) arcrun confined to the single adapter -----------------------------------


def test_arcrun_confined_to_the_single_adapter() -> None:
    offenders: list[str] = []
    for path in _SRC.rglob("*.py"):
        if path.name == _ARCRUN_ADAPTER:
            continue
        if _ARCRUN in _imported_roots(_parse(path)):
            offenders.append(str(path.relative_to(_SRC)))
    assert not offenders, f"arcrun must stay confined to {_ARCRUN_ADAPTER}: {offenders}"


# -- (4) determinism: no model/LLM seam in the deterministic modules ------------


def test_deterministic_ingest_rank_modules_import_no_model_seam() -> None:
    offenders: dict[str, set[str]] = {}
    for rel in _DETERMINISTIC_MODULES:
        path = _SRC / rel
        assert path.exists(), f"expected deterministic module missing: {rel}"
        bad = _imported_roots(_parse(path)) & _FORBIDDEN_MODEL_SEAMS
        if bad:
            offenders[str(rel)] = bad
    assert not offenders, (
        f"deterministic ingest/rank modules must import no model/LLM seam -- the "
        f"embedder is injected into DocIndex/SurfaceIndex, not imported here: {offenders}"
    )


# -- (5) poison-object seam: fusion never names the concrete backend ------------


def test_fusion_does_not_import_the_concrete_index_backend() -> None:
    tree = _parse(_SRC / "fusion.py")
    offenders: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            if node.module == "arcmemory.index.backend" or node.module.endswith(
                ".index.backend"
            ):
                offenders.add(node.module)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name in ("arcmemory.index.backend", "index.backend"):
                    offenders.add(alias.name)
    assert not offenders, (
        f"fusion.py must not import index.backend's concrete backend by name -- "
        f"swapping the backend must never require a fusion/gate change: {offenders}"
    )


def test_detector_would_catch_a_violation() -> None:
    """The gate is real: synthetic violations of each rule are flagged."""
    tree = ast.parse(
        "import arcagent.core\nimport arcgateway\nfrom arcrun import loop\n"
        "import arcllm\n"
    )
    roots = _imported_roots(tree)
    assert roots & _FORBIDDEN_ALWAYS == _FORBIDDEN_ALWAYS
    assert roots & _FORBIDDEN_CONNECTOR == {"arcgateway"}
    assert _ARCRUN in roots
    assert roots & _FORBIDDEN_MODEL_SEAMS == {"arcllm"}

    poison_tree = ast.parse("from arcmemory.index.backend import SqliteIndexBackend\n")
    poison_module = next(
        node.module
        for node in ast.walk(poison_tree)
        if isinstance(node, ast.ImportFrom) and node.module
    )
    assert poison_module == "arcmemory.index.backend"
