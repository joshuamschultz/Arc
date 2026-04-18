"""SPEC-017 Phase 4 — Parallel tool dispatch.

Read-only batches run concurrently via ``asyncio.gather(return_exceptions=True)``
bounded by a semaphore. Any state-modifying tool in the batch forces
the entire batch sequential (SPEC-017 R-020 through R-025).

Implicit-dependency heuristic: if one tool call's argument value appears
as another call's argument value (likely a shared file path), the batch
runs sequential even if all tools are read-only.
"""

from __future__ import annotations

import asyncio
from typing import Any


class _FakeTool:
    """Tool stub exposing the attributes the dispatcher inspects."""

    def __init__(
        self,
        name: str,
        classification: str = "read_only",
        *,
        delay: float = 0.0,
        result: str = "ok",
        raises: Exception | None = None,
    ) -> None:
        self.name = name
        self.classification = classification
        self._delay = delay
        self._result = result
        self._raises = raises

    async def execute(self, _args: dict[str, Any], _ctx: Any) -> str:
        if self._delay:
            await asyncio.sleep(self._delay)
        if self._raises is not None:
            raise self._raises
        return self._result


class _FakeRegistry:
    def __init__(self, tools: list[_FakeTool]) -> None:
        self._by_name = {t.name: t for t in tools}

    def get(self, name: str) -> _FakeTool | None:
        return self._by_name.get(name)

    def get_classification(self, name: str) -> str:
        tool = self._by_name.get(name)
        return tool.classification if tool else "state_modifying"


class _ToolCall:
    def __init__(self, name: str, arguments: dict[str, Any], id_: str = "tc") -> None:
        self.name = name
        self.arguments = arguments
        self.id = id_


class TestClassifyBatch:
    """Task 4.2 — `classify_batch` partitions tool calls by classification."""

    def test_all_read_only_returns_parallelizable(self) -> None:
        from arcrun.parallel_dispatch import BatchClassifier

        registry = _FakeRegistry(
            [
                _FakeTool("read", "read_only"),
                _FakeTool("grep", "read_only"),
                _FakeTool("ls", "read_only"),
            ]
        )
        calls = [
            _ToolCall("read", {"path": "/a"}),
            _ToolCall("grep", {"pattern": "x", "path": "/b"}),
            _ToolCall("ls", {"path": "/c"}),
        ]
        classifier = BatchClassifier(registry)
        verdict = classifier.classify(calls)
        assert verdict.can_parallelize is True
        assert verdict.reason == "all_read_only"

    def test_any_state_modifying_forces_sequential(self) -> None:
        from arcrun.parallel_dispatch import BatchClassifier

        registry = _FakeRegistry(
            [
                _FakeTool("read", "read_only"),
                _FakeTool("bash", "state_modifying"),
            ]
        )
        calls = [_ToolCall("read", {"path": "/a"}), _ToolCall("bash", {"cmd": "ls"})]
        verdict = BatchClassifier(registry).classify(calls)
        assert verdict.can_parallelize is False
        assert "state_modifying" in verdict.reason

    def test_unknown_tool_is_conservative_sequential(self) -> None:
        """Unknown tools are treated as state_modifying (fail-closed)."""
        from arcrun.parallel_dispatch import BatchClassifier

        registry = _FakeRegistry([_FakeTool("read", "read_only")])
        calls = [_ToolCall("read", {}), _ToolCall("mystery", {})]
        verdict = BatchClassifier(registry).classify(calls)
        assert verdict.can_parallelize is False


class TestImplicitDependencyDetection:
    """Task 4.6-4.7 — Parameter-match heuristic: shared file-path
    argument forces sequential even if all tools are read_only."""

    def test_shared_path_argument_forces_sequential(self) -> None:
        from arcrun.parallel_dispatch import BatchClassifier

        registry = _FakeRegistry([_FakeTool("read", "read_only"), _FakeTool("grep", "read_only")])
        # Both tools reference the same path — treat as dependent
        calls = [
            _ToolCall("read", {"path": "/tmp/x"}),
            _ToolCall("grep", {"path": "/tmp/x", "pattern": "q"}),
        ]
        verdict = BatchClassifier(registry).classify(calls)
        assert verdict.can_parallelize is False
        assert "dependency" in verdict.reason or "shared" in verdict.reason

    def test_different_paths_allow_parallel(self) -> None:
        from arcrun.parallel_dispatch import BatchClassifier

        registry = _FakeRegistry([_FakeTool("read", "read_only"), _FakeTool("grep", "read_only")])
        calls = [
            _ToolCall("read", {"path": "/tmp/a"}),
            _ToolCall("grep", {"path": "/tmp/b", "pattern": "q"}),
        ]
        verdict = BatchClassifier(registry).classify(calls)
        assert verdict.can_parallelize is True


class TestParallelDispatcher:
    """Tasks 4.3, 4.8, 4.12 — Parallel gather with semaphore + partial failure."""

    async def test_parallel_dispatch_returns_submission_order(self) -> None:
        from arcrun.parallel_dispatch import ParallelDispatcher

        order: list[str] = []

        async def _recording(name: str, delay: float) -> str:
            await asyncio.sleep(delay)
            order.append(name)
            return name

        async def run(call: _ToolCall) -> tuple[_ToolCall, Any]:
            delay = call.arguments["delay"]
            result = await _recording(call.name, delay)
            return call, result

        calls = [
            _ToolCall("slow", {"delay": 0.05}),
            _ToolCall("fast1", {"delay": 0.0}),
            _ToolCall("fast2", {"delay": 0.0}),
        ]
        dispatcher = ParallelDispatcher(max_parallel=10)
        results = await dispatcher.dispatch(calls, run)

        # Execution order can vary, but RESULT ordering must match submission
        assert [r[0].name for r in results] == ["slow", "fast1", "fast2"]

    async def test_semaphore_bounds_concurrency(self) -> None:
        from arcrun.parallel_dispatch import ParallelDispatcher

        active = {"now": 0, "peak": 0}

        async def run(call: _ToolCall) -> tuple[_ToolCall, Any]:
            active["now"] += 1
            active["peak"] = max(active["peak"], active["now"])
            await asyncio.sleep(0.01)
            active["now"] -= 1
            return call, "ok"

        calls = [_ToolCall(f"t{i}", {}) for i in range(20)]
        dispatcher = ParallelDispatcher(max_parallel=5)
        await dispatcher.dispatch(calls, run)
        assert active["peak"] <= 5, f"peak concurrency {active['peak']} exceeded limit 5"

    async def test_partial_failure_returns_exception_not_abort(self) -> None:
        from arcrun.parallel_dispatch import ParallelDispatcher

        async def run(call: _ToolCall) -> tuple[_ToolCall, Any]:
            if call.name == "fail":
                raise ValueError("boom")
            return call, f"result:{call.name}"

        calls = [_ToolCall("ok1", {}), _ToolCall("fail", {}), _ToolCall("ok2", {})]
        dispatcher = ParallelDispatcher(max_parallel=10)
        results = await dispatcher.dispatch(calls, run)

        assert results[0] == (calls[0], "result:ok1")
        assert isinstance(results[1][1], ValueError)
        assert results[2] == (calls[2], "result:ok2")


class TestAuditOrdering:
    """Task 4.10-4.11 — monotonic sequence numbers preserve submission order."""

    async def test_seq_numbers_match_submission_order(self) -> None:
        from arcrun.parallel_dispatch import ParallelDispatcher

        dispatched: list[tuple[int, str]] = []

        async def run(call: _ToolCall) -> tuple[_ToolCall, Any]:
            seq = call.arguments["_seq"]
            dispatched.append((seq, call.name))
            return call, "ok"

        calls = [_ToolCall(f"t{i}", {}) for i in range(5)]
        dispatcher = ParallelDispatcher(max_parallel=10, assign_seq=True)
        await dispatcher.dispatch(calls, run)

        # Sequences assigned at dispatch time must be monotonic
        seqs = [call.arguments.get("_seq") for call in calls]
        assert seqs == list(range(len(calls)))


class TestDispatchAll:
    """Task 4.13-4.14 — Top-level ``dispatch_batch`` entry point."""

    async def test_read_only_batch_runs_parallel(self) -> None:
        from arcrun.parallel_dispatch import BatchClassifier, dispatch_batch

        registry = _FakeRegistry(
            [_FakeTool("read", "read_only"), _FakeTool("grep", "read_only")]
        )

        async def run(call: _ToolCall) -> tuple[_ToolCall, Any]:
            await asyncio.sleep(0.02)
            return call, f"ran:{call.name}"

        calls = [
            _ToolCall("read", {"path": "/a"}),
            _ToolCall("grep", {"path": "/b", "pattern": "x"}),
        ]
        classifier = BatchClassifier(registry)

        start = asyncio.get_event_loop().time()
        results = await dispatch_batch(calls, run, classifier=classifier, max_parallel=10)
        elapsed = asyncio.get_event_loop().time() - start

        assert [r[1] for r in results] == ["ran:read", "ran:grep"]
        # 2 tools @ 20ms each: parallel should be ~20ms, sequential ~40ms
        assert elapsed < 0.035, f"batch took {elapsed:.3f}s; parallel expected"

    async def test_state_modifying_batch_runs_sequential(self) -> None:
        from arcrun.parallel_dispatch import BatchClassifier, dispatch_batch

        registry = _FakeRegistry(
            [_FakeTool("read", "read_only"), _FakeTool("write", "state_modifying")]
        )

        order: list[str] = []

        async def run(call: _ToolCall) -> tuple[_ToolCall, Any]:
            await asyncio.sleep(0.01)
            order.append(call.name)
            return call, "ok"

        calls = [_ToolCall("read", {"path": "/a"}), _ToolCall("write", {"path": "/a"})]
        classifier = BatchClassifier(registry)
        await dispatch_batch(calls, run, classifier=classifier, max_parallel=10)
        # Sequential — observed order equals submission order
        assert order == ["read", "write"]
