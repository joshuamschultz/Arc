"""Performance budget: spool write p95 < 5 ms (SPEC-026 NFR-2, Task 1.9)."""

from __future__ import annotations

import time
from pathlib import Path

from arcstore.records import SpoolRecord
from arcstore.spool import record


def test_record_under_5ms_p95(tmp_path: Path) -> None:
    target = tmp_path / "operational.jsonl"
    samples: list[float] = []
    for i in range(200):
        rec = SpoolRecord(kind="llm_call", actor_did="did:a", request_id=f"r{i}", prompt_tokens=i)
        t0 = time.perf_counter()
        record(rec, path=target)
        samples.append((time.perf_counter() - t0) * 1000.0)

    samples.sort()
    p95 = samples[int(0.95 * len(samples)) - 1]
    assert p95 < 5.0, f"p95={p95:.3f}ms exceeds 5ms budget"
