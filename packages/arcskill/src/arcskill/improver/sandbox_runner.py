"""Concrete golden-task ``EvalRunner`` over the arcskill sandbox (SPEC-044 P3.3, DC-5).

:class:`HubEvalRunner` is the default production :class:`~arcskill.improver.seams.EvalRunner`.
It is the **security boundary** for untrusted, model-authored skill code (ASI05/REQ-023):
a candidate's golden-task suite runs in the tier-appropriate sandbox — Firecracker at
federal, Docker at enterprise/personal — never in the agent process.

Execution model
---------------
The runner *materializes* a :class:`BundleView` into a throwaway bundle directory (the
skill's files, with the candidate ``text``/``scripts`` overlaid), writes a tiny stdlib
**golden-task harness** into it, and runs that harness inside the sandbox. The harness
imports each golden ``test_*`` function and runs it, printing one machine-readable
``ARC_EVAL<TAB>nodeid<TAB>PASS|FAIL`` line per case. Parsing those lines yields per-case
:class:`EvalOutcome`s — the granularity the strict-improvement gate needs to see which
cases flip fail→pass without regressing any.

The harness uses only the standard library (no ``pytest`` binary in the minimal,
network-isolated sandbox image) while treating the golden files as ordinary
pytest-format modules (``evals/test_*.py::test_*``).

Tier policy (REQ-023, D-6)
--------------------------
* a sandbox is available → run there (Firecracker preferred; federal without Firecracker
  fails closed via :class:`SandboxRequired`, even if Docker is present);
* no sandbox + federal/enterprise → fail-closed (:class:`SandboxRequired`);
* no sandbox + personal → degrade to a host subprocess with an audit-warning.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
from pathlib import Path
from typing import Literal

from arcskill.hub.config import HubConfig, TierPolicy
from arcskill.hub.dry_run import execute_in_sandbox, is_firecracker_available
from arcskill.hub.errors import SandboxRequired
from arcskill.improver.models import BundleView, EvalCase, EvalOutcome

_logger = logging.getLogger("arcskill.improver.sandbox_runner")

_HARNESS_NAME = "_arc_eval_harness.py"
_NODES_NAME = "_arc_eval_nodes.txt"
_RESULT_PREFIX = "ARC_EVAL"

# Stdlib golden-task harness executed inside the sandbox. Reads node ids from a
# sibling file, runs each test function, emits one ARC_EVAL line per case. Trusted
# code (we author it); the untrusted skill code it imports is what the sandbox contains.
_HARNESS_SOURCE = '''\
"""Arc golden-task harness (SPEC-044) — stdlib only, no pytest binary required."""
import importlib.util
import sys
import traceback
from pathlib import Path

_ROOT = Path(__file__).resolve().parent
for _p in (_ROOT, _ROOT / "scripts", _ROOT / "evals", _ROOT / "src"):
    if _p.is_dir():
        sys.path.insert(0, str(_p))


def _run_one(nodeid):
    filepart, _, func = nodeid.partition("::")
    path = _ROOT / filepart
    spec = importlib.util.spec_from_file_location("arc_eval_" + path.stem, path)
    if spec is None or spec.loader is None:
        return "FAIL", "cannot load " + filepart
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
        getattr(module, func)()
    except Exception as exc:  # noqa: BLE001 — any error is a failing case
        detail = "".join(traceback.format_exception_only(type(exc), exc)).strip()
        return "FAIL", detail.replace("\\t", " ").replace("\\n", " ")[:200]
    return "PASS", ""


def main():
    nodes_file = _ROOT / "_arc_eval_nodes.txt"
    node_ids = [ln.strip() for ln in nodes_file.read_text().splitlines() if ln.strip()]
    for nodeid in node_ids:
        verdict, detail = _run_one(nodeid)
        print("ARC_EVAL\\t" + nodeid + "\\t" + verdict + "\\t" + detail, flush=True)


if __name__ == "__main__":
    main()
'''


def docker_available() -> bool:
    """True when the ``docker`` CLI is on ``$PATH`` (module-level for test override)."""
    return bool(shutil.which("docker"))


class HubEvalRunner:
    """Default ``EvalRunner``: run a candidate's golden suite in the arcskill sandbox."""

    def __init__(self, *, tier: str = "personal", timeout_s: int = 30) -> None:
        self._tier = tier
        self._timeout_s = timeout_s

    async def run(self, view: BundleView, cases: list[EvalCase]) -> list[EvalOutcome]:
        """Materialize ``view``, run its golden ``cases`` in the sandbox, parse outcomes."""
        if not cases:
            return []
        import tempfile

        with tempfile.TemporaryDirectory(prefix="arcskill_eval_") as tmp:
            bundle = Path(tmp) / "bundle"
            _materialize(view, bundle, cases)
            command = f"python {_HARNESS_NAME}"
            stdout = await self._execute(bundle, command)
        return _parse_outcomes(stdout, cases)

    async def _execute(self, bundle: Path, command: str) -> str:
        """Run ``command`` in the tier sandbox, or (personal-only) the host fallback."""
        if is_firecracker_available() or docker_available():
            config = HubConfig(tier=TierPolicy(level=_tier_level(self._tier)))
            result = await execute_in_sandbox(
                bundle, command, config, timeout_s=self._timeout_s, mount=True
            )
            return result.stdout
        if self._tier != "personal":
            raise SandboxRequired(
                f"tier {self._tier!r} requires a sandbox (Firecracker/Docker) to run the "
                f"golden-task suite, but none is available (fail-closed)"
            )
        _logger.warning(
            "AUDIT WARN: no sandbox available; running golden-task suite on the HOST "
            "(personal tier only). Install Docker or Firecracker to restore isolation."
        )
        return await _run_on_host(bundle, command, self._timeout_s)


def _tier_level(tier: str) -> Literal["federal", "enterprise", "personal"]:
    """Map an improver tier string to a hub ``TierPolicy`` level (default personal)."""
    if tier == "federal":
        return "federal"
    if tier == "enterprise":
        return "enterprise"
    return "personal"


def _materialize(view: BundleView, dest: Path, cases: list[EvalCase]) -> None:
    """Build a bundle dir: skill files + candidate overlay + harness + node list."""
    dest.mkdir(parents=True, exist_ok=True)
    if view.skill_dir is not None and view.skill_dir.is_dir():
        shutil.copytree(view.skill_dir, dest, dirs_exist_ok=True)
    if view.text:
        (dest / "SKILL.md").write_text(view.text, encoding="utf-8")
    for rel, data in view.scripts.items():
        target = _safe_join(dest, rel)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    (dest / _HARNESS_NAME).write_text(_HARNESS_SOURCE, encoding="utf-8")
    (dest / _NODES_NAME).write_text(
        "\n".join(c.node or c.id for c in cases) + "\n", encoding="utf-8"
    )


def _safe_join(root: Path, rel: str) -> Path:
    """Join ``rel`` under ``root``, rejecting path-traversal (ASI05/REQ-023)."""
    target = (root / rel).resolve()
    if root.resolve() not in target.parents and target != root.resolve():
        raise ValueError(f"unsafe overlay path escapes bundle: {rel!r}")
    return target


async def _run_on_host(bundle: Path, command: str, timeout_s: int) -> str:
    """Personal-tier fallback: run the harness on the host, return captured stdout."""
    proc = await asyncio.create_subprocess_exec(
        *command.split(),
        cwd=str(bundle),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
        env={"PYTHONDONTWRITEBYTECODE": "1", "PATH": _host_path()},
    )
    try:
        stdout_b, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        return ""
    return stdout_b.decode("utf-8", errors="replace")


def _host_path() -> str:
    import os

    return os.environ.get("PATH", "/usr/bin:/bin")


def _parse_outcomes(stdout: str, cases: list[EvalCase]) -> list[EvalOutcome]:
    """Parse ``ARC_EVAL`` harness lines into one :class:`EvalOutcome` per case.

    A case with no line is a conservative failure — a truncated or crashed sandbox
    run must never be read as a pass (fail-closed).
    """
    results: dict[str, tuple[bool, str]] = {}
    for line in stdout.splitlines():
        if not line.startswith(_RESULT_PREFIX + "\t"):
            continue
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        nodeid, verdict = parts[1], parts[2]
        detail = parts[3] if len(parts) > 3 else ""
        results[nodeid] = (verdict == "PASS", detail)
    outcomes: list[EvalOutcome] = []
    for case in cases:
        node = case.node or case.id
        passed, detail = results.get(node, (False, "no result from sandbox"))
        outcomes.append(EvalOutcome(case_id=case.id, passed=passed, detail=detail))
    return outcomes


__all__ = ["HubEvalRunner", "docker_available"]
