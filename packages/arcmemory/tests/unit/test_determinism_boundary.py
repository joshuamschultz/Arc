"""RED — the trigger/rank path is model-free and boundary-clean (SPEC-072 COMP-011).

The working-set update, detector evaluation, decision-point cue extraction, and temporal
ranking/supersession must make NO LLM or embedder call (REQ-361): they are pure and
deterministic, and none even accepts a model/embedder to call. arcmemory imports neither
arcagent nor arcrun-loop internals — pinned by the existing architecture tests plus this
signature/determinism guard so a future change that sneaks a model onto the hot path goes
red here.
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

from arcmemory.detectors import WorkingSet, evaluate_moment
from arcmemory.retrieve import _date_ordinal, _rrf_fuse, _within_window
from arcmemory.stores.semantic import superseded_view
from arcmemory.timeline import read_timeline
from arcmemory.types import Recall, TimeWindow

_FORBIDDEN_PARAMS = ("embedder", "model", "llm", "distiller")


def test_trigger_and_rank_helpers_accept_no_model_seam() -> None:
    for fn in (
        WorkingSet.update,
        evaluate_moment,
        _within_window,
        _rrf_fuse,
        _date_ordinal,
        superseded_view,
        read_timeline,
    ):
        params = inspect.signature(fn).parameters
        for forbidden in _FORBIDDEN_PARAMS:
            assert forbidden not in params, f"{fn.__name__} must not take a {forbidden} seam"


def test_working_set_update_is_deterministic() -> None:
    a = WorkingSet(32, 5)
    b = WorkingSet(32, 5)
    assert a.update("s", ["Alpha", "the", "Beta"]) == b.update("s", ["Alpha", "the", "Beta"])


def test_recency_tiebreak_and_window_are_deterministic() -> None:
    recalls = [
        Recall(source="a", content="x", score=0.0, established="2020-01-01"),
        Recall(source="b", content="y", score=0.0, established="2026-01-01"),
    ]
    assert [r.source for r in _rrf_fuse([[recalls[0]], [recalls[1]]])] == ["b", "a"]
    window = TimeWindow(start="2026-01-01", end="2026-12-31")
    assert [r.source for r in _within_window(recalls, window)] == ["b"]


def test_new_source_modules_do_not_import_arcagent_or_arcrun() -> None:
    """The temporal/working-set code stays behind the Brain port — no upward import."""
    src = Path(inspect.getfile(read_timeline)).parent
    forbidden = {"arcagent"}
    for path in (src / "timeline.py", src / "detectors.py", src / "retrieve.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        roots: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                roots.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                roots.add(node.module.split(".")[0])
        assert not (roots & forbidden), f"{path.name} imports {roots & forbidden}"
        assert "arcrun" not in roots, f"{path.name} imports arcrun (only react_adapter may)"
