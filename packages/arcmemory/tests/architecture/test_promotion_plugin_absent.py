"""SPEC-083 T-1213 — promotion survives the Jev plugin being absent; layer edges hold.

The classifier is a removable extension (decision 13). With it gone — either the
``typesafe_sdk`` extra not installed or arcllm's ``classifiers/jev/`` folder
physically deleted — arcmemory and arcagent still import, a promotion-enabled
brain still builds through the real ``build_brain`` seam, and the nightly sweep
reports the typed ``classifier_unavailable`` with nothing published.

Each dynamic case runs in a fresh subprocess, so ``sys.modules`` poisoning or a
copied arcllm can never leak into this test session. arcmemory's own tests do not
import arcagent/arcteam; the arcagent import happens only inside the subprocess.

Static edges pinned here (the existing guards are not duplicated):

* arcmemory never imports arcteam. (arcagent is already pinned by
  ``test_no_arcagent_import.py``.)
* arcagent never names ``typesafe_sdk``. (``import arcllm`` in arcagent is
  already pinned by ``packages/arcagent/tests/architecture/test_dependency_boundaries.py``.)
"""

from __future__ import annotations

import ast
import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[4]
_PACKAGES = _REPO / "packages"
_ARCMEMORY_SRC = _PACKAGES / "arcmemory" / "src" / "arcmemory"
_ARCAGENT_SRC = _PACKAGES / "arcagent" / "src" / "arcagent"


def _importable_arcllm() -> Path:
    """The arcllm package this interpreter would import (the one under test).

    Located without importing it, so the copy removes the drop-in from exactly
    the code that ships, whether that is the repo tree or an installed wheel.
    """
    spec = importlib.util.find_spec("arcllm")
    assert spec is not None and spec.origin is not None, "arcllm is not importable"
    return Path(spec.origin).parent


# -- static edges ------------------------------------------------------------------


def _import_roots_and_names(tree: ast.AST) -> set[str]:
    """Top-level import roots plus any string constant naming a module for dynamic import."""
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            names.add(node.module.split(".")[0])
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            if node.value.isidentifier() or ("." in node.value and " " not in node.value):
                names.add(node.value.split(".")[0])
    return names


def _offenders(root: Path, forbidden: str) -> list[str]:
    return sorted(
        str(path.relative_to(_REPO))
        for path in root.rglob("*.py")
        if forbidden
        in _import_roots_and_names(ast.parse(path.read_text(encoding="utf-8"), filename=str(path)))
    )


def test_arcmemory_never_imports_arcteam() -> None:
    offenders = _offenders(_ARCMEMORY_SRC, "arcteam")

    assert not offenders, f"arcmemory sits below arcteam and must not import it: {offenders}"


def test_arcagent_never_names_typesafe_sdk() -> None:
    offenders = _offenders(_ARCAGENT_SRC, "typesafe_sdk")

    assert not offenders, f"arcagent must reach the classifier via arcmemory only: {offenders}"


def test_edge_detector_flags_static_and_dynamic_forms() -> None:
    for source in (
        "import arcteam",
        "from arcteam.shared_knowledge import FleetSharedKnowledgeService",
        "import importlib\nimportlib.import_module('arcteam.shared_knowledge')",
    ):
        assert "arcteam" in _import_roots_and_names(ast.parse(source)), source
    assert "typesafe_sdk" in _import_roots_and_names(ast.parse("__import__('typesafe_sdk')"))


# -- dynamic: the plugin is absent ---------------------------------------------------

_PROBE = r"""
import asyncio, json, sys, tempfile
from pathlib import Path

site, mode = sys.argv[1], sys.argv[2]
if site != "-":
    sys.path.insert(0, site)
if mode == "block-sdk":
    sys.modules["typesafe_sdk"] = None

import arcllm
import arcmemory
import arcmemory.provider as provider
import arcagent
import arcagent.modules.memory
from arctrust import AgentIdentity
from arcmemory.distill import EventExtraction, FactExtraction, InsightMint, ProcedureExtraction
from arcmemory.stores.insight import InsightStore
from arcmemory.types import Insight


class NullDistiller:
    # Hygiene (and so the nightly sweep) only runs with a distiller wired.
    async def extract_facts(self, events): return FactExtraction()
    async def mint_insights(self, events, facts): return InsightMint()
    async def extract_procedures(self, events, existing): return ProcedureExtraction()
    async def extract_events(self, episodes): return EventExtraction()


class DurableSink:
    def __init__(self): self.events = []
    def write(self, event): self.events.append(event)
    def write_durable(self, event): self.events.append(event)


class Publisher:
    def __init__(self): self.references = []
    async def publish(self, reference, **_):
        self.references.append(reference)
        return "shared:" + reference
    async def demotions(self): return {}


# The ONE seam replaced: a model-free distiller, so no LLM call is attempted.
provider.build_distiller = lambda *args, **kwargs: NullDistiller()
workspace = Path(tempfile.mkdtemp()) / "agent"
identity = AgentIdentity.generate("test", "agent")
sink, publisher = DurableSink(), Publisher()
brain = provider.build_brain({
    "workspace": workspace,
    "agent_did": identity.did,
    "tier": "personal",
    "audit_sink": sink,
    "identity": identity,
    "policy_pipeline": None,
    "backend_config": {"embed_backend": "none"},
    "promotion_config": {"enabled": True},
    "promotion_publisher": publisher,
})
InsightStore(workspace).write(
    Insight(id="acme-renewal", statement="Acme renewal closes at $42k/yr.", trigger="a renewal")
)
result = asyncio.run(brain.consolidate())
print(json.dumps({
    "arcllm_file": arcllm.__file__,
    "sdk_loaded": sys.modules.get("typesafe_sdk") is not None,
    "status": result.get("promotion_status"),
    "evaluated": result.get("promotion_evaluated"),
    "published": publisher.references,
    "egress_events": [e.outcome for e in sink.events if e.action == "memory.promotion.egress"],
}))
"""


def _probe(tmp_path: Path, site: Path | None, mode: str) -> dict[str, object]:
    script = tmp_path / "probe.py"
    script.write_text(_PROBE, encoding="utf-8")
    env = {
        **os.environ,
        "HOME": str(tmp_path),
        "ARC_CONFIG_DIR": str(tmp_path / ".arc"),
        "ARC_TEAM_ROOT": str(tmp_path / "arc"),
    }
    env.pop("TYPESAFE_API_KEY", None)
    done = subprocess.run(
        [sys.executable, str(script), str(site) if site else "-", mode],
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
        timeout=180,
        check=False,
    )
    assert done.returncode == 0, f"import/build failed with the plugin absent:\n{done.stderr}"
    report: dict[str, object] = json.loads(done.stdout.strip().splitlines()[-1])
    if site is not None:
        assert str(report["arcllm_file"]).startswith(str(site)), "probe imported the wrong arcllm"
    return report


def _arcllm_without_jev(tmp_path: Path) -> Path:
    site = tmp_path / "site"
    shutil.copytree(
        _importable_arcllm(), site / "arcllm", ignore=shutil.ignore_patterns("__pycache__")
    )
    shutil.rmtree(site / "arcllm" / "classifiers" / "jev")
    return site


def test_sdk_absent_brain_builds_and_sweep_is_classifier_unavailable(tmp_path: Path) -> None:
    report = _probe(tmp_path, None, "block-sdk")

    assert report["sdk_loaded"] is False
    assert report["published"] == []
    assert report["status"] == "classifier_unavailable", report


def test_jev_folder_removed_brain_builds_and_sweep_is_classifier_unavailable(
    tmp_path: Path,
) -> None:
    report = _probe(tmp_path, _arcllm_without_jev(tmp_path), "sdk-as-installed")

    assert report["published"] == []
    assert report["status"] == "classifier_unavailable", report
