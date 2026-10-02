"""Dedicated embed worker: one thread owns every local torch model.

A shared ``SentenceTransformer`` is not thread-safe. Concurrent ``encode``
calls race inside transformers/torch native code and intermittently SIGSEGV
the interpreter. Instead of a lock that parks N threads, ONE worker thread
loads the models and runs every encode. Callers enqueue ``(texts, future)``
and never block the event loop.

* Batching: the worker drains up to ``MAX_BATCH_ITEMS`` texts / ``MAX_BATCH_BYTES``
  bytes of same-model requests per encode call, then resolves each future with
  its own slice.
* Priority: retrieval requests ride a lane served before ingest/background
  requests, so a search never waits behind an ingest batch.
* Bounded: past ``MAX_QUEUE`` pending requests, ``submit`` raises
  ``ArcLLMEmbeddingUnavailableError`` (loudly: log + metric) so callers degrade
  instead of stalling the loop.
* Shutdown: ``shutdown`` fails pending work, lets any in-flight encode finish,
  and joins the thread. It is registered with ``atexit`` so no native work runs
  during interpreter teardown.
"""

from __future__ import annotations

import asyncio
import atexit
import logging
import threading
from collections import deque
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from opentelemetry import metrics

from arcllm.exceptions import ArcLLMEmbeddingUnavailableError

logger = logging.getLogger(__name__)

MAX_BATCH_ITEMS = 256
MAX_BATCH_BYTES = 1024 * 1024
MAX_QUEUE = 1024
_SHUTDOWN_JOIN_SECONDS = 30.0

_rejected_counter = metrics.get_meter("arcllm").create_counter(
    "arcllm.embed.queue_rejected",
    description="Embed requests rejected because the worker queue was full",
)


@dataclass(eq=False)
class _Request:
    key: object  # identity of the model owner; batches never mix keys
    model_name: str
    load_model: Callable[[], Any]
    texts: list[str]
    loop: asyncio.AbstractEventLoop
    future: asyncio.Future[Any]
    size_bytes: int


def _resolve(request: _Request, *, result: Any = None, error: BaseException | None = None) -> None:
    """Resolve a request's future on its own loop (the worker is another thread)."""

    def _apply() -> None:
        if request.future.done():
            return
        if error is not None:
            request.future.set_exception(error)
        else:
            request.future.set_result(result)

    try:
        request.loop.call_soon_threadsafe(_apply)
    except RuntimeError:  # loop already closed: nobody is waiting
        logger.debug("embed result dropped: caller's event loop is closed")


class EmbedWorker:
    """One thread, two lanes, bounded. See the module docstring."""

    def __init__(
        self,
        *,
        max_batch_items: int = MAX_BATCH_ITEMS,
        max_batch_bytes: int = MAX_BATCH_BYTES,
        max_queue: int = MAX_QUEUE,
    ) -> None:
        self._max_batch_items = max_batch_items
        self._max_batch_bytes = max_batch_bytes
        self._max_queue = max_queue
        self._cond = threading.Condition()
        self._priority: deque[_Request] = deque()
        self._background: deque[_Request] = deque()
        self._models: dict[object, Any] = {}
        self._closed = False
        self._thread = threading.Thread(target=self._run, name="arcllm-embed-worker", daemon=True)
        self._thread.start()

    @property
    def closed(self) -> bool:
        return self._closed

    # -- producer side (event-loop thread) ---------------------------------

    def submit(
        self,
        *,
        key: object,
        model_name: str,
        load_model: Callable[[], Any],
        texts: list[str],
        priority: bool,
    ) -> asyncio.Future[Any]:
        """Enqueue ``texts``; the future resolves to the raw encoded array."""
        loop = asyncio.get_running_loop()
        request = _Request(
            key=key,
            model_name=model_name,
            load_model=load_model,
            texts=texts,
            loop=loop,
            future=loop.create_future(),
            size_bytes=sum(len(t.encode("utf-8", "ignore")) for t in texts),
        )
        with self._cond:
            if self._closed:
                raise ArcLLMEmbeddingUnavailableError(model_name, "the embed worker is shut down")
            depth = len(self._priority) + len(self._background)
            if depth >= self._max_queue:
                _rejected_counter.add(1, {"model": model_name})
                logger.warning(
                    "embed queue full (%d pending, max %d): rejecting request for %s",
                    depth,
                    self._max_queue,
                    model_name,
                )
                raise ArcLLMEmbeddingUnavailableError(
                    model_name, f"the embed queue is full ({depth} pending); degrade and retry"
                )
            (self._priority if priority else self._background).append(request)
            self._cond.notify()
        return request.future

    def shutdown(self, timeout: float = _SHUTDOWN_JOIN_SECONDS) -> None:
        """Fail pending requests, finish any in-flight encode, join the thread."""
        with self._cond:
            pending: list[_Request] = []
            if not self._closed:
                self._closed = True
                pending = [*self._priority, *self._background]
                self._priority.clear()
                self._background.clear()
            self._cond.notify_all()
        for request in pending:
            _resolve(
                request,
                error=ArcLLMEmbeddingUnavailableError(
                    request.model_name, "the embed worker is shutting down"
                ),
            )
        if self._thread is not threading.current_thread():
            self._thread.join(timeout)
            if self._thread.is_alive():
                logger.error("embed worker did not stop within %.0fs", timeout)

    # -- consumer side (worker thread) -------------------------------------

    def _next_batch(self) -> list[_Request] | None:
        with self._cond:
            while not self._priority and not self._background:
                if self._closed:
                    return None
                self._cond.wait()
            lane = self._priority if self._priority else self._background
            batch = [lane.popleft()]
            items, size = len(batch[0].texts), batch[0].size_bytes
            while lane and lane[0].key is batch[0].key:
                nxt = lane[0]
                if (
                    items + len(nxt.texts) > self._max_batch_items
                    or size + nxt.size_bytes > self._max_batch_bytes
                ):
                    break
                batch.append(lane.popleft())
                items += len(nxt.texts)
                size += nxt.size_bytes
            return batch

    def _run(self) -> None:
        while (batch := self._next_batch()) is not None:
            self._process(batch)

    def _process(self, batch: Sequence[_Request]) -> None:
        live = [r for r in batch if not r.future.cancelled()]
        if not live:
            return
        head = live[0]
        try:
            model = self._models.get(head.key)
            if model is None:
                model = head.load_model()  # loads once, on this thread
                self._models[head.key] = model
            texts = [t for r in live for t in r.texts]
            # normalize_embeddings -> unit vectors, so downstream cosine == dot.
            raw = model.encode(texts, convert_to_numpy=True, normalize_embeddings=True)
        except ArcLLMEmbeddingUnavailableError as exc:
            logger.warning("embed model unavailable for %s: %s", head.model_name, exc)
            for request in live:
                _resolve(request, error=exc)
            return
        except Exception as exc:
            logger.exception(
                "embed encode failed for %s (%d requests)", head.model_name, len(live)
            )
            for request in live:
                _resolve(request, error=exc)
            return
        offset = 0
        for request in live:
            count = len(request.texts)
            _resolve(request, result=raw[offset : offset + count])
            offset += count


_worker: EmbedWorker | None = None
_worker_lock = threading.Lock()


def get_worker() -> EmbedWorker:
    """Return the process-wide worker, starting (or restarting after shutdown) it."""
    global _worker
    with _worker_lock:
        if _worker is None or _worker.closed:
            _worker = EmbedWorker()
        return _worker


def shutdown_worker() -> None:
    """Stop the process-wide worker (idempotent). The next ``get_worker`` restarts it."""
    global _worker
    with _worker_lock:
        worker, _worker = _worker, None
    if worker is not None:
        worker.shutdown()


atexit.register(shutdown_worker)
