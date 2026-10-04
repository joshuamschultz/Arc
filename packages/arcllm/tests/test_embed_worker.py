"""Embed worker: one thread owns the model; batching, priority lane, bounded queue.

Item 74. Every test forces the interleaving it asserts with Events: a fake
model's first ``encode`` blocks until the test has queued the competing work,
so ordering is decided by the worker, never by timing luck.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import Iterator

import numpy as np
import pytest

from arcllm import embed_worker
from arcllm import embeddings as emb
from arcllm.exceptions import ArcLLMEmbeddingUnavailableError

_WAIT = 5.0


class _GatedModel:
    """First encode blocks on ``release`` so the test can queue work behind it."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.threads: set[str] = set()
        self.started = threading.Event()
        self.release = threading.Event()
        self.active = 0
        self.max_active = 0
        self._guard = threading.Lock()

    def encode(self, texts: list[str], **_: object) -> np.ndarray:
        with self._guard:
            self.calls.append(list(texts))
            self.threads.add(threading.current_thread().name)
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            first = len(self.calls) == 1
        if first:
            self.started.set()
            assert self.release.wait(_WAIT)
        with self._guard:
            self.active -= 1
        return np.zeros((len(texts), 4))


@pytest.fixture(autouse=True)
def _clean_worker() -> Iterator[None]:
    emb.clear_embedder_cache()
    yield
    emb.clear_embedder_cache()


@pytest.fixture
def model(monkeypatch: pytest.MonkeyPatch) -> _GatedModel:
    gated = _GatedModel()
    monkeypatch.setattr(emb, "_load_sentence_transformer", lambda _name: gated)
    return gated


async def _start_blocked(embedder: emb.LocalEmbedder, model: _GatedModel) -> asyncio.Task[object]:
    """Submit one embed and wait until the worker is inside its (blocked) encode."""
    task = asyncio.create_task(emb.embed(["first"], model="m", provider=embedder))
    assert await asyncio.to_thread(model.started.wait, _WAIT)
    return task


async def test_concurrent_embeds_are_batched_into_one_encode_call(model: _GatedModel) -> None:
    embedder = emb.LocalEmbedder("m")
    first = await _start_blocked(embedder, model)
    queued = [
        asyncio.create_task(emb.embed([f"t{i}"], model="m", provider=embedder)) for i in range(15)
    ]
    await asyncio.sleep(0.05)  # let all 15 reach the queue while encode #1 is blocked
    model.release.set()
    await asyncio.gather(first, *queued)
    assert len(model.calls) <= 2
    assert sum(len(c) for c in model.calls) == 16


async def test_batch_respects_item_cap(
    model: _GatedModel, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(embed_worker, "_worker", embed_worker.EmbedWorker(max_batch_items=3))
    embedder = emb.LocalEmbedder("m")
    first = await _start_blocked(embedder, model)
    queued = [
        asyncio.create_task(emb.embed([f"t{i}"], model="m", provider=embedder)) for i in range(7)
    ]
    await asyncio.sleep(0.05)
    model.release.set()
    await asyncio.gather(first, *queued)
    assert max(len(c) for c in model.calls) == 3


async def test_each_caller_gets_only_its_own_slice(
    model: _GatedModel, monkeypatch: pytest.MonkeyPatch
) -> None:
    class _EchoModel(_GatedModel):
        def encode(self, texts: list[str], **_: object) -> np.ndarray:
            super().encode(texts)
            return np.array([[float(len(t)), 0, 0, 0] for t in texts])

    echo = _EchoModel()
    monkeypatch.setattr(emb, "_load_sentence_transformer", lambda _name: echo)
    embedder = emb.LocalEmbedder("m")
    first = await _start_blocked(embedder, echo)
    a = asyncio.create_task(emb.embed(["aa", "aaa"], model="m", provider=embedder))
    b = asyncio.create_task(emb.embed(["bbbbb"], model="m", provider=embedder))
    await asyncio.sleep(0.05)
    echo.release.set()
    await first
    assert [v[0] for v in (await a).vectors] == [2.0, 3.0]
    assert [v[0] for v in (await b).vectors] == [5.0]


async def test_retrieve_lane_preempts_ingest_batch(model: _GatedModel) -> None:
    embedder = emb.LocalEmbedder("m")
    first = await _start_blocked(embedder, model)
    ingest = [
        asyncio.create_task(
            emb.embed([f"ingest{i}"], model="m", provider=embedder, operation="embed:ingest")
        )
        for i in range(3)
    ]
    await asyncio.sleep(0.05)
    search = asyncio.create_task(
        emb.embed(["query"], model="m", provider=embedder, operation="retrieve:recall")
    )
    await asyncio.sleep(0.05)
    model.release.set()
    await asyncio.gather(first, search, *ingest)
    assert model.calls[1] == ["query"]  # served before the older ingest requests
    assert model.calls[2] == ["ingest0", "ingest1", "ingest2"]


async def test_queue_bound_degrades_instead_of_blocking(
    model: _GatedModel, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(embed_worker, "_worker", embed_worker.EmbedWorker(max_queue=2))
    embedder = emb.LocalEmbedder("m")
    first = await _start_blocked(embedder, model)
    queued = [
        asyncio.create_task(emb.embed([f"t{i}"], model="m", provider=embedder)) for i in range(2)
    ]
    await asyncio.sleep(0.05)
    with caplog.at_level(logging.WARNING, logger="arcllm.embed_worker"):
        with pytest.raises(ArcLLMEmbeddingUnavailableError, match="queue is full"):
            await asyncio.wait_for(emb.embed(["overflow"], model="m", provider=embedder), 1.0)
    assert "queue full" in caplog.text  # degrade LOUD
    model.release.set()
    await asyncio.gather(first, *queued)


async def test_shutdown_fails_pending_and_stops_the_thread(model: _GatedModel) -> None:
    embedder = emb.LocalEmbedder("m")
    first = await _start_blocked(embedder, model)
    pending = asyncio.create_task(emb.embed(["late"], model="m", provider=embedder))
    await asyncio.sleep(0.05)
    worker = embed_worker.get_worker()
    stopper = threading.Thread(target=embed_worker.shutdown_worker)
    stopper.start()
    with pytest.raises(ArcLLMEmbeddingUnavailableError, match="shutting down"):
        await asyncio.wait_for(pending, _WAIT)
    model.release.set()  # in-flight encode finishes; nothing new starts
    await first
    await asyncio.to_thread(stopper.join, _WAIT)
    assert not worker._thread.is_alive()
    assert model.calls == [["first"]]  # no native work after shutdown


async def test_embed_after_shutdown_restarts_the_worker(model: _GatedModel) -> None:
    model.release.set()
    embedder = emb.LocalEmbedder("m")
    await emb.embed(["a"], model="m", provider=embedder)
    embed_worker.shutdown_worker()
    await emb.embed(["b"], model="m", provider=embedder)
    assert [c[0] for c in model.calls] == ["a", "b"]


async def test_model_loads_once_on_the_worker_thread(monkeypatch: pytest.MonkeyPatch) -> None:
    loads: list[str] = []
    gated = _GatedModel()
    gated.release.set()

    def _load(_name: str) -> _GatedModel:
        loads.append(threading.current_thread().name)
        return gated

    monkeypatch.setattr(emb, "_load_sentence_transformer", _load)
    embedder = emb.LocalEmbedder("m")
    for _ in range(3):
        await asyncio.gather(*(emb.embed(["x"], model="m", provider=embedder) for _ in range(4)))
    assert loads == ["arcllm-embed-worker"]
    assert gated.threads == {"arcllm-embed-worker"}


async def test_a_cancelled_waiter_never_strands_the_requests_queued_with_it(
    model: _GatedModel,
) -> None:
    """2026-10-04 MC hang triage: could a timed-out (cancelled) recall strand a future?

    A bus handler timeout cancels its embed while the request sits in the queue
    behind an in-flight encode. The worker must skip only that request, resolve
    every other one in the same batch, and keep serving later requests from both
    lanes. Disproved as the hang's cause: no future is left unresolved.
    """
    embedder = emb.LocalEmbedder("m")
    first = await _start_blocked(embedder, model)
    doomed = asyncio.create_task(
        emb.embed(["doomed"], model="m", provider=embedder, operation="retrieve:surface")
    )
    kept = asyncio.create_task(
        emb.embed(["kept"], model="m", provider=embedder, operation="retrieve:surface")
    )
    background = asyncio.create_task(
        emb.embed(["bg"], model="m", provider=embedder, operation="embed:backfill")
    )
    await asyncio.sleep(0.05)
    doomed.cancel()
    model.release.set()
    await asyncio.wait_for(asyncio.gather(first, kept, background), _WAIT)
    with pytest.raises(asyncio.CancelledError):
        await doomed
    later = await asyncio.wait_for(
        emb.embed(["later"], model="m", provider=embedder, operation="retrieve:surface"), _WAIT
    )
    assert len(later.vectors) == 1
    assert all("doomed" not in call for call in model.calls[1:])
