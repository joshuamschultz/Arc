"""SPEC-083 T-1213 — the Jev drop-in is the only door to ``typesafe_sdk``, and it is removable.

ADR-038 allows exactly one vendor SDK in arcllm, confined to the drop-in folder
``arcllm/classifiers/jev/`` behind the ``arcllm[jev]`` extra. Two guarantees:

1. **Confinement (static).** No source file anywhere in the repo's packages
   imports ``typesafe_sdk`` — by ``import``, ``from``, or a dynamic
   ``importlib.import_module("typesafe_sdk")`` / ``__import__`` string — except
   files under ``arcllm/classifiers/jev/``.
2. **Removability (dynamic).** With the ``classifiers/jev/`` folder physically
   deleted (arcllm copied to ``tmp_path``, folder removed, imported in a
   subprocess with that copy first on ``sys.path``), arcllm imports,
   ``resolve_classifier("jev", ...)`` raises ``ArcLLMClassifierUnavailableError``
   and ``classifier_key_envs()`` is empty. With the folder present but
   ``typesafe_sdk`` absent (``sys.modules["typesafe_sdk"] = None``) arcllm still
   imports and a classify call is the typed "unavailable", not an ImportError.

A positive control runs the same subprocess against an intact copy, so a
harness that silently imported the installed arcllm could not pass.
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
_ARCLLM_SRC = _PACKAGES / "arcllm" / "src" / "arcllm"
_JEV_DIR = _ARCLLM_SRC / "classifiers" / "jev"
_SDK = "typesafe_sdk"


def _importable_arcllm() -> Path:
    """The arcllm package this interpreter would import (the one under test).

    Located without importing it, so the copy removes the drop-in from exactly
    the code that ships, whether that is the repo tree or an installed wheel.
    """
    spec = importlib.util.find_spec("arcllm")
    assert spec is not None and spec.origin is not None, "arcllm is not importable"
    return Path(spec.origin).parent


# -- static confinement ---------------------------------------------------------


def _names_sdk(value: str) -> bool:
    return value == _SDK or value.startswith(_SDK + ".")


def _sdk_references(tree: ast.AST) -> list[int]:
    """Line numbers where ``tree`` imports, or names for dynamic import, ``typesafe_sdk``."""
    lines: list[int] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            lines += [node.lineno for alias in node.names if _names_sdk(alias.name)]
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            if _names_sdk(node.module):
                lines.append(node.lineno)
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            if _names_sdk(node.value):
                lines.append(node.lineno)
    return lines


def _source_files() -> list[Path]:
    return sorted(path for path in _PACKAGES.glob("*/src/**/*.py") if path.is_file())


def test_typesafe_sdk_is_referenced_only_under_the_jev_drop_in() -> None:
    offenders: dict[str, list[int]] = {}
    inside: list[str] = []
    for path in _source_files():
        lines = _sdk_references(ast.parse(path.read_text(encoding="utf-8"), filename=str(path)))
        if not lines:
            continue
        if path.is_relative_to(_JEV_DIR):
            inside.append(str(path.relative_to(_REPO)))
        else:
            offenders[str(path.relative_to(_REPO))] = lines

    assert not offenders, f"typesafe_sdk referenced outside arcllm/classifiers/jev/: {offenders}"
    # Positive control: the scan sees the one legitimate reference, so an empty
    # result above is not a scan that never looked.
    assert inside, "scan found no typesafe_sdk reference in the jev drop-in itself"


def test_detector_flags_every_import_form() -> None:
    samples = {
        "import typesafe_sdk": 1,
        "import typesafe_sdk.client as c": 1,
        "from typesafe_sdk import AsyncTypeSafeClient": 1,
        "import importlib\nimportlib.import_module('typesafe_sdk')": 1,
        "__import__('typesafe_sdk.models')": 1,
        "import typesafe_sdkx\nx = 'typesafe_sdk_like'": 0,
    }
    for source, expected in samples.items():
        assert len(_sdk_references(ast.parse(source))) == expected, source


# -- dynamic removability ---------------------------------------------------------

_PROBE = r"""
import asyncio, json, sys
sys.path.insert(0, sys.argv[1])
if sys.argv[2] == "block-sdk":
    sys.modules["typesafe_sdk"] = None
import arcllm
from arcllm.classify import resolve_classifier

report = {"arcllm_file": arcllm.__file__}
try:
    resolved = resolve_classifier("jev", "jev-1.13.0")
    report["resolve"] = type(resolved).__name__
except Exception as exc:
    report["resolve"] = type(exc).__name__
report["key_envs"] = sorted(arcllm.classifier_key_envs())
report["keys"] = [k.api_key_env for k in arcllm.list_classifier_keys()]
request = arcllm.ClassificationRequest(
    state="Acme renewal closes at $42k/yr.",
    questions={"q": arcllm.NoulSpec(instructions="This is about work.")},
)
try:
    asyncio.run(arcllm.classify("jev", "jev-1.13.0", request, timeout=5.0))
    report["classify"] = "ok"
except Exception as exc:
    report["classify"] = type(exc).__name__
print(json.dumps(report))
"""


def _copy_arcllm(tmp_path: Path, *, drop_jev: bool) -> Path:
    root = tmp_path / "site"
    shutil.copytree(
        _importable_arcllm(), root / "arcllm", ignore=shutil.ignore_patterns("__pycache__")
    )
    if drop_jev:
        shutil.rmtree(root / "arcllm" / "classifiers" / "jev")
    return root


def _probe(tmp_path: Path, site: Path, mode: str) -> dict[str, object]:
    script = tmp_path / "probe.py"
    script.write_text(_PROBE, encoding="utf-8")
    env = {**os.environ, "HOME": str(tmp_path), "ARC_CONFIG_DIR": str(tmp_path / ".arc")}
    env.pop("TYPESAFE_API_KEY", None)
    done = subprocess.run(  # noqa: S603 — fixed argv, no shell, our own interpreter
        [sys.executable, str(script), str(site), mode],
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
        timeout=120,
        check=False,
    )
    assert done.returncode == 0, f"arcllm failed to import:\n{done.stderr}"
    report: dict[str, object] = json.loads(done.stdout.strip().splitlines()[-1])
    assert str(report["arcllm_file"]).startswith(str(site)), "probe imported the wrong arcllm"
    return report


def test_intact_copy_resolves_jev_and_declares_its_key(tmp_path: Path) -> None:
    """Positive control for the removal test: the same harness sees the drop-in."""
    report = _probe(tmp_path, _copy_arcllm(tmp_path, drop_jev=False), "sdk-as-installed")

    assert report["resolve"] == "JevClassifier"
    assert report["key_envs"] == ["TYPESAFE_API_KEY"]


def test_arcllm_without_the_jev_folder_imports_and_reports_unavailable(tmp_path: Path) -> None:
    report = _probe(tmp_path, _copy_arcllm(tmp_path, drop_jev=True), "sdk-as-installed")

    assert report["resolve"] == "ArcLLMClassifierUnavailableError"
    assert report["key_envs"] == []
    assert report["keys"] == []
    assert report["classify"] == "ArcLLMClassifierUnavailableError"


def test_arcllm_with_typesafe_sdk_absent_imports_and_classify_is_unavailable(
    tmp_path: Path,
) -> None:
    report = _probe(tmp_path, _copy_arcllm(tmp_path, drop_jev=False), "block-sdk")

    # Construction stays lazy: the drop-in resolves without touching the SDK.
    assert report["resolve"] == "JevClassifier"
    assert report["classify"] == "ArcLLMClassifierUnavailableError"
