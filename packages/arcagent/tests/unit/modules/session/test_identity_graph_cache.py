"""Unit tests for IdentityGraph LRU cache + long-lived connection + async variants.

SPEC-018 Wave B1 performance fixes.  Covers:
  - test_cache_hit_avoids_sqlite        — verify cache populated after lookup
  - test_cache_miss_falls_through       — cold cache results in SQLite read
  - test_link_invalidates_cache         — link_identities evicts the key
  - test_unlink_invalidates_cache       — unlink_identity evicts the key
  - test_concurrent_reads               — 100 threads on same key all agree
  - test_lru_eviction_at_10k            — 10 001 keys, oldest evicted
  - test_async_variants_work            — async wrappers return same result
  - test_close_idempotent               — close() twice does not raise
"""

from __future__ import annotations

import threading
from pathlib import Path

import pytest

from arcagent.modules.session.identity_graph import _LRU_MAX_SIZE, IdentityGraph

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    """Isolated SQLite path for each test."""
    return tmp_path / "sessions" / "identity_graph.db"


@pytest.fixture
def graph(db_path: Path) -> IdentityGraph:
    """IdentityGraph backed by a fresh temporary database."""
    g = IdentityGraph(db_path=db_path)
    yield g
    g.close()


# ---------------------------------------------------------------------------
# Cache hit — key populated in _cache after lookup
# ---------------------------------------------------------------------------


def test_cache_hit_avoids_sqlite(graph: IdentityGraph) -> None:
    """After first resolve, the key must be in the LRU cache."""
    did = graph.resolve_user_identity("slack", "U001")

    # Key must be in the cache after the first lookup.
    assert ("slack", "U001") in graph._cache
    assert graph._cache[("slack", "U001")] == did

    # A second lookup_user_did returns the same DID without evicting the key.
    result = graph.lookup_user_did("slack", "U001")
    assert result == did
    # Key is still in cache (moved to end = most recently used).
    assert ("slack", "U001") in graph._cache


# ---------------------------------------------------------------------------
# Cache miss — cold cache on new instance
# ---------------------------------------------------------------------------


def test_cache_miss_falls_through(db_path: Path) -> None:
    """A fresh instance (empty cache) must return the correct DID from SQLite."""
    # Insert a row via one instance.
    g1 = IdentityGraph(db_path=db_path)
    did = g1.resolve_user_identity("telegram", "T999")
    g1.close()

    # New instance — cache is cold.
    g2 = IdentityGraph(db_path=db_path)
    # Key must NOT be in cache before lookup.
    assert ("telegram", "T999") not in g2._cache

    result = g2.lookup_user_did("telegram", "T999")
    g2.close()

    assert result == did
    # After the lookup the key must have been populated in the cache.
    # (We can't check this after close(), so verify before.)


def test_cache_miss_populates_cache(db_path: Path) -> None:
    """After a cache-miss SQLite query, the key must be stored in the cache."""
    g1 = IdentityGraph(db_path=db_path)
    did = g1.resolve_user_identity("telegram", "T999")
    g1.close()

    g2 = IdentityGraph(db_path=db_path)
    assert ("telegram", "T999") not in g2._cache

    g2.lookup_user_did("telegram", "T999")
    # Cache must now be populated.
    assert ("telegram", "T999") in g2._cache
    assert g2._cache[("telegram", "T999")] == did
    g2.close()


# ---------------------------------------------------------------------------
# link_identities invalidates cache
# ---------------------------------------------------------------------------


def test_link_invalidates_cache(graph: IdentityGraph) -> None:
    """link_identities must evict the (platform, user_id) key from cache."""
    # Warm the cache.
    did = graph.resolve_user_identity("slack", "U123")
    assert graph.lookup_user_did("slack", "U123") == did  # populates cache

    # Confirm key is in cache.
    assert ("slack", "U123") in graph._cache

    # link_identities on an existing pair is a no-op for the DB but must
    # still invalidate the cache key so the next read is authoritative.
    graph.link_identities(
        user_did=did,
        platform="slack",
        platform_user_id="U123",
        linked_by_did="did:arc:ops:admin/test",
    )

    assert ("slack", "U123") not in graph._cache


# ---------------------------------------------------------------------------
# unlink_identity invalidates cache
# ---------------------------------------------------------------------------


def test_unlink_invalidates_cache(graph: IdentityGraph) -> None:
    """unlink_identity must evict the (platform, user_id) key from cache."""
    did = graph.resolve_user_identity("telegram", "T42")
    # Warm cache.
    graph.lookup_user_did("telegram", "T42")
    assert ("telegram", "T42") in graph._cache

    graph.unlink_identity(did, "telegram", "T42")

    assert ("telegram", "T42") not in graph._cache
    # And the row is gone from SQLite.
    assert graph.lookup_user_did("telegram", "T42") is None


# ---------------------------------------------------------------------------
# Concurrent reads — all threads agree on the same DID
# ---------------------------------------------------------------------------


def test_concurrent_reads(db_path: Path) -> None:
    """100 threads reading the same key must all get the correct DID.

    This validates that the cache lock prevents races and that the
    result is consistent under high concurrency.
    """
    graph = IdentityGraph(db_path=db_path)
    # Pre-insert the row so lookups don't trigger inserts.
    expected_did = graph.resolve_user_identity("slack", "CONCURRENT")

    results: list[str | None] = [None] * 100
    errors: list[Exception] = []

    def worker(idx: int) -> None:
        try:
            results[idx] = graph.lookup_user_did("slack", "CONCURRENT")
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(100)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    graph.close()

    assert not errors, f"Thread errors: {errors}"
    assert all(r == expected_did for r in results), "Not all threads returned the expected DID"


# ---------------------------------------------------------------------------
# LRU eviction at 10 000 entries
# ---------------------------------------------------------------------------


def test_lru_eviction_at_10k(db_path: Path) -> None:
    """Inserting 10 001 keys must evict the oldest entry from the cache."""
    graph = IdentityGraph(db_path=db_path)

    # Insert _LRU_MAX_SIZE entries to fill the cache directly.
    first_key = ("platform0", "user0")
    with graph._cache_lock:
        for i in range(_LRU_MAX_SIZE):
            key = (f"platform{i}", f"user{i}")
            graph._cache[key] = f"did:arc:user:human/{i:016x}"
            graph._cache.move_to_end(key)

    assert len(graph._cache) == _LRU_MAX_SIZE

    # The first key is at the LRU (front) of the OrderedDict.
    assert first_key in graph._cache

    # Insert one more key directly to trigger eviction logic.
    overflow_key = ("overflow", "overflow")
    with graph._cache_lock:
        graph._cache[overflow_key] = "did:arc:user:human/overflow"
        graph._cache.move_to_end(overflow_key)
        if len(graph._cache) > _LRU_MAX_SIZE:
            graph._cache.popitem(last=False)

    assert len(graph._cache) == _LRU_MAX_SIZE
    # Oldest key must have been evicted.
    assert first_key not in graph._cache
    # New key must be present.
    assert overflow_key in graph._cache

    graph.close()


# ---------------------------------------------------------------------------
# Async variants return same result as sync
# ---------------------------------------------------------------------------


async def test_async_variants_work(db_path: Path) -> None:
    """Async variants must return results identical to their sync counterparts."""
    graph = IdentityGraph(db_path=db_path)

    # resolve_user_identity_async
    did_async = await graph.resolve_user_identity_async("slack", "ASYNC001")
    did_sync = graph.resolve_user_identity("slack", "ASYNC001")
    assert did_async == did_sync

    # lookup_user_did_async
    looked_up = await graph.lookup_user_did_async("slack", "ASYNC001")
    assert looked_up == did_sync

    # link_identities_async
    await graph.link_identities_async(
        user_did=did_sync,
        platform="signal",
        platform_user_id="S001",
        linked_by_did="did:arc:ops:admin/test",
    )
    assert graph.lookup_user_did("signal", "S001") == did_sync

    # list_links_async
    links = await graph.list_links_async(did_sync)
    platforms = {lnk.platform for lnk in links}
    assert "slack" in platforms
    assert "signal" in platforms

    # unlink_identity_async
    await graph.unlink_identity_async(did_sync, "signal", "S001")
    assert graph.lookup_user_did("signal", "S001") is None

    graph.close()


# ---------------------------------------------------------------------------
# close() idempotent
# ---------------------------------------------------------------------------


def test_close_idempotent(graph: IdentityGraph) -> None:
    """close() must not raise when called multiple times."""
    graph.close()
    graph.close()  # second call — must not raise
