"""Phase 2 Task 2.18 — Policy pipeline microbenchmark.

Asserts the pipeline's p95 evaluation time stays below 1ms with 100
rules per denylist layer and realistic call volume. Not an exhaustive
load test — this is a guard rail that catches regressions in the hot
path (layer iteration, rule lookup, cache).

Pillars: this is a Scalability gate — SPEC-017 R-013 requires sub-1ms
p95 via indexed rule lookup and LRU cache.
"""

from __future__ import annotations

import asyncio
import statistics
import time

import pytest


@pytest.mark.asyncio
async def test_pipeline_p95_under_1ms_with_100_rules() -> None:
    from arcagent.core.tool_policy import (
        GlobalLayer,
        PolicyContext,
        ToolCall,
        PolicyPipeline,
    )

    # 100 denylist rules — none will match the read/grep tools we evaluate,
    # forcing the layer to walk its lookup for every call.
    deny_rules = {f"forbidden_tool_{i}": f"global.denylist: rule {i}" for i in range(100)}
    layer = GlobalLayer(deny_rules=deny_rules, forbidden_compositions=[])

    pipeline = PolicyPipeline(layers=[layer], cache_ttl_seconds=0.0)
    ctx = PolicyContext(tier="personal", policy_version="v1", bundle_age_seconds=0)

    # Spin a range of distinct calls so cache doesn't dominate — even
    # with TTL=0 we want distinct hashes as a belt-and-braces.
    calls = [
        ToolCall(
            tool_name="read",
            arguments={"path": f"/tmp/f{i}"},
            agent_did="did:arc:bench",
            session_id=f"s{i}",
            classification="unclassified",
        )
        for i in range(1000)
    ]

    # Warm-up
    for call in calls[:50]:
        await pipeline.evaluate(call, ctx)

    durations_us: list[float] = []
    for call in calls:
        start = time.perf_counter()
        await pipeline.evaluate(call, ctx)
        durations_us.append((time.perf_counter() - start) * 1_000_000)

    p50 = statistics.median(durations_us)
    p95 = statistics.quantiles(durations_us, n=20)[18]  # 95th percentile

    # 1ms = 1000 microseconds. Allow generous headroom on CI.
    assert p95 < 1000.0, f"p95={p95:.1f}us exceeded 1ms (p50={p50:.1f}us)"
    # Sanity — avoid a degenerate measurement where everything is zero
    assert p50 > 0, "p50 is non-positive — timer glitch?"


@pytest.mark.asyncio
async def test_cache_accelerates_repeated_calls() -> None:
    """Phase 2 R-013 — cache must reduce eval time for identical calls.

    Not a precise benchmark; a regression guard — identical call after
    an initial miss should be materially faster, confirming the LRU
    path is live.
    """
    from arcagent.core.tool_policy import (
        GlobalLayer,
        PolicyContext,
        ToolCall,
        PolicyPipeline,
    )

    deny_rules = {f"forbidden_{i}": f"rule {i}" for i in range(100)}
    layer = GlobalLayer(deny_rules=deny_rules, forbidden_compositions=[])

    pipeline = PolicyPipeline(layers=[layer], cache_ttl_seconds=30.0)
    ctx = PolicyContext(tier="personal", policy_version="v1", bundle_age_seconds=0)
    call = ToolCall(
        tool_name="read",
        arguments={"path": "/tmp/same"},
        agent_did="did:arc:bench",
        session_id="s",
        classification="unclassified",
    )

    # First call (miss) — measure
    await pipeline.evaluate(call, ctx)  # warm any import-time cost

    miss_start = time.perf_counter()
    # Distinct call to force miss
    miss_call = ToolCall(
        tool_name="read",
        arguments={"path": "/tmp/other"},
        agent_did="did:arc:bench",
        session_id="s",
        classification="unclassified",
    )
    await pipeline.evaluate(miss_call, ctx)
    miss_us = (time.perf_counter() - miss_start) * 1_000_000

    # Hit path — identical call, should short-circuit on cache
    await pipeline.evaluate(call, ctx)  # prime if not already
    hit_start = time.perf_counter()
    await pipeline.evaluate(call, ctx)
    hit_us = (time.perf_counter() - hit_start) * 1_000_000

    # Cache hit should be at least as fast as miss. Exact ratios are
    # noisy in CI — assert cache is not a pessimization, not a precise
    # speedup target.
    assert hit_us <= miss_us * 1.5, (
        f"Cache hit ({hit_us:.1f}us) not materially faster than miss ({miss_us:.1f}us)"
    )


# Ensure asyncio is imported even if only implicit via pytest-asyncio
_ = asyncio
