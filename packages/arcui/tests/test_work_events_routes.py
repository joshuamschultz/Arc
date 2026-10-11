"""SPEC-088 T-1371 (RED): work-event stream, page and keys routes (COMP-014).

Covers REQ-626 (publish the work-events key, never the operator key), REQ-628 (scoped SSE
stream with filters, resume, clearance, heartbeat and a per-principal cap) and REQ-631
(subscribe and close audited).

Contract this file pins for the builder (T-1372), new module ``arcui.routes.work_events``
exporting ``routes`` (Starlette routes):

* ``GET /api/work-events/stream`` -> ``text/event-stream``.
* ``GET /api/work-events?agent=&after=&limit=`` -> ``{"events": [envelope, ...]}`` where
  each envelope carries ``seq`` and the pointer fields; ``seq > after`` ascending, at most
  ``limit``.
* ``GET /api/work-events/keys?agent=`` -> ``{"key_id", "algorithm", "public_key"}``
  (``public_key`` standard base64) of the COMP-019 work-events key; ``503`` when no
  work-events key exists. Never the operator key.

All three take ``agent`` (the agent DID) and call
``require_scope(request, Scope.EVENTS_SUBSCRIBE, agent_did=agent, ...)``: an app without
``agent``, or whose ``events:subscribe`` is bound to another agent, gets the audited 403.
Operator sessions may omit ``agent`` and then see every agent. Filters (stream and page):
``run_id``, ``correlation_id`` and ``kinds`` (comma-separated list). Only events whose
classification the deployment clearance dominates are ever sent.

Stream frames: ``id: <seq>``, ``event: <kind>``, ``data: <envelope JSON>`` separated by a
blank line; the ``Last-Event-ID`` request header resumes after that ``seq``; a comment
frame (a line starting with ``:``) is sent at least every 15 s. At most 4 open streams per
principal DID: the 5th gets 429 until one closes. Opening a stream writes one mutation
audit row ``operation == "work_event.subscribe"`` (outcome ``applied``) and its end one
row ``operation == "work_event.close"``, both naming the acting identity.

``app.state`` the routes read:

* ``work_event_log`` - the arcstore ``WorkEventLog``.
* ``work_event_signer`` - the COMP-019 capability (``sign``, ``public_key``,
  ``algorithm``, ``key_id``) or ``None``.
* ``inbox_clearance`` - the deployment clearance label (the same attribute COMP-004
  validates task classification against; default ``"UNCLASSIFIED"``).
* ``work_events_sleep`` (async ``(seconds) -> None``, default ``asyncio.sleep``) and
  ``work_events_clock`` (``() -> float`` monotonic seconds, default
  ``time.monotonic``) - the stream's poll/heartbeat timing seam, injected here so the
  15 s heartbeat is proven on a fake clock.

SSE is driven by calling the ASGI app directly (a real ``receive``/``send`` pair, the
client disconnect is a real ``http.disconnect``), so every stream is opened, read and
closed deterministically. Doubles: the principal (``principal_factory``), the audit WORM
spy, and the timing seam. The log is the real ``WorkEventLog`` over the in-memory backend.
"""

from __future__ import annotations

import asyncio
import base64
import json
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

import httpx
import pytest
from arcstore.backends.memory import FakeBackend
from arcstore.work_events import WorkEvent, WorkEventLog
from arctrust import InProcessSigner
from arctrust.users import OPERATOR
from packages.arcui.tests.principal_fixtures import agent_did
from starlette.applications import Starlette

from arcui.principals import Scope

OLIVIA = agent_did()
BOB = agent_did()
STREAM = "/api/work-events/stream"
PAGE = "/api/work-events"
KEYS = "/api/work-events/keys"
WAIT_S = 5.0


# --- doubles ---------------------------------------------------------------------------


class WorkEventsSigner:
    """COMP-019-shaped capability over a fixed test key."""

    def __init__(self, seed: bytes) -> None:
        self._signer = InProcessSigner(seed)

    @property
    def public_key(self) -> bytes:
        return self._signer.public_key

    @property
    def algorithm(self) -> str:
        return self._signer.algorithm

    @property
    def key_id(self) -> str:
        import hashlib

        return hashlib.sha256(self.public_key).hexdigest()[:16]

    def sign(self, message: bytes) -> bytes:
        return self._signer.sign(message)


@dataclass
class FakeTime:
    """The stream's timing seam: ``sleep`` advances a fake monotonic clock and yields."""

    now: float = 1000.0
    slept: list[float] = field(default_factory=list)

    def clock(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds
        await asyncio.sleep(0)


# --- an SSE stream driven over raw ASGI ------------------------------------------------


@dataclass
class Frame:
    id: str | None = None
    event: str | None = None
    data: str | None = None
    comment: bool = False

    @property
    def envelope(self) -> dict[str, Any]:
        assert self.data is not None
        parsed: dict[str, Any] = json.loads(self.data)
        return parsed


def _parse(raw: bytes) -> list[Frame]:
    frames: list[Frame] = []
    text = raw.decode("utf-8").replace("\r\n", "\n")
    for block in text.split("\n\n")[:-1]:  # the last piece is an unfinished frame
        frame = Frame()
        for line in block.split("\n"):
            if line.startswith(":"):
                frame.comment = True
            elif line.startswith("id:"):
                frame.id = line[3:].strip()
            elif line.startswith("event:"):
                frame.event = line[6:].strip()
            elif line.startswith("data:"):
                frame.data = line[5:].strip()
        frames.append(frame)
    return frames


class Stream:
    """One GET to the ASGI app with a client that can disconnect on demand."""

    def __init__(
        self,
        app: Starlette,
        query: str,
        headers: dict[str, str] | None = None,
        clock: Any = None,
    ) -> None:
        self._app = app
        self._clock = clock
        self._query = query
        self._headers = [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()]
        self._disconnect = asyncio.Event()
        self._started = asyncio.Event()
        self._request_sent = False
        self.status: int | None = None
        self.headers: dict[str, str] = {}
        self.body = b""
        self.comment_times: list[float] = []  # fake-clock time of each chunk with a comment
        self._task: asyncio.Task[None] | None = None

    async def _receive(self) -> dict[str, Any]:
        if not self._request_sent:
            self._request_sent = True
            return {"type": "http.request", "body": b"", "more_body": False}
        await self._disconnect.wait()
        return {"type": "http.disconnect"}

    async def _send(self, message: dict[str, Any]) -> None:
        if message["type"] == "http.response.start":
            self.status = message["status"]
            self.headers = {k.decode().lower(): v.decode() for k, v in message["headers"]}
            self._started.set()
        elif message["type"] == "http.response.body":
            chunk = message.get("body", b"")
            self.body += chunk
            if self._clock is not None and any(
                line.startswith(b":") for line in chunk.split(b"\n")
            ):
                self.comment_times.append(self._clock())

    async def open(self) -> Stream:
        scope = {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": STREAM,
            "raw_path": STREAM.encode(),
            "query_string": self._query.encode(),
            "root_path": "",
            "headers": [(b"host", b"testserver"), *self._headers],
            "client": ("127.0.0.1", 5000),
            "server": ("testserver", 80),
        }
        self._task = asyncio.create_task(self._app(scope, self._receive, self._send))
        await asyncio.wait_for(self._started.wait(), WAIT_S)
        return self

    def frames(self) -> list[Frame]:
        return _parse(self.body)

    def events(self) -> list[Frame]:
        return [f for f in self.frames() if f.event is not None]

    async def wait_for(self, predicate: Any) -> list[Frame]:
        async def _poll() -> None:
            while not predicate(self.frames()):
                if self._task is not None and self._task.done():
                    self._task.result()
                    raise AssertionError("the stream ended early")
                await asyncio.sleep(0.001)

        await asyncio.wait_for(_poll(), WAIT_S)
        return self.frames()

    async def wait_events(self, count: int) -> list[Frame]:
        await self.wait_for(lambda frames: sum(f.event is not None for f in frames) >= count)
        return self.events()

    async def wait_heartbeat_after_events(self) -> None:
        """A comment after the last event proves the stream polled past what it sent."""

        def _seen(frames: list[Frame]) -> bool:
            last_event = max((i for i, f in enumerate(frames) if f.event), default=-1)
            return any(f.comment for f in frames[last_event + 1 :])

        await self.wait_for(_seen)

    async def close(self) -> None:
        self._disconnect.set()
        if self._task is not None:
            await asyncio.wait_for(self._task, WAIT_S)


def _query(**params: str) -> str:
    from urllib.parse import urlencode

    return urlencode(params)


# --- rig -------------------------------------------------------------------------------


@dataclass
class Rig:
    app: Starlette
    worm: Any
    log: WorkEventLog
    factory: Any
    time: FakeTime
    signer: WorkEventsSigner
    operator: InProcessSigner
    seqs: dict[str, int] = field(default_factory=dict)

    def act_as(self, principal: Any) -> Any:
        self.factory.act_as(self.app, principal)
        return principal

    def as_app(self, agents: tuple[str, ...] = (OLIVIA,), **kw: Any) -> Any:
        return self.act_as(self.factory.app({Scope.EVENTS_SUBSCRIBE: agents}, **kw))

    def as_operator(self) -> Any:
        return self.act_as(self.factory.user((OPERATOR,)))

    async def append(self, name: str, **fields: Any) -> str:
        event_id = f"evt-{name}-{uuid.uuid4().hex[:8]}"
        payload: dict[str, Any] = {
            "event_id": event_id,
            "kind": "run.finished",
            "occurred_at": "2026-10-10T12:00:00+00:00",
            "agent_did": OLIVIA,
            "run_id": "run-1",
            "correlation_id": "order-1",
            "status": "done",
            "classification": "UNCLASSIFIED",
        }
        payload.update(fields)
        assert await self.log.append(WorkEvent(**payload))
        (stored,) = [e for e in await self.log.read(0, limit=1000) if e.event_id == event_id]
        assert stored.seq is not None
        self.seqs[name] = stored.seq
        return event_id

    def stream(self, headers: dict[str, str] | None = None, **params: str) -> Stream:
        return Stream(self.app, _query(**params), headers, clock=self.time.clock)

    async def get(self, path: str, params: dict[str, Any] | None = None) -> httpx.Response:
        """A plain (non-streaming) request through the ASGI app."""
        transport = httpx.ASGITransport(app=self.app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as c:
            return await c.get(path, params=params)

    def rows(self, operation: str) -> list[Any]:
        return [row for row in self.worm.written if row.operation == operation]


@pytest.fixture
async def rig(principal_factory: Any) -> Rig:
    from arcui.routes import work_events

    app = Starlette(routes=list(work_events.routes))
    worm = principal_factory.install(app)
    backend = FakeBackend()
    await backend.start()
    log = WorkEventLog(backend)
    fake_time = FakeTime()
    signer = WorkEventsSigner(bytes(range(32)))
    operator = InProcessSigner(bytes(range(32, 64)))
    app.state.work_event_log = log
    app.state.work_event_signer = signer
    app.state.operator_signer_factory = lambda *_a, **_k: operator
    app.state.inbox_clearance = "UNCLASSIFIED"
    app.state.work_events_sleep = fake_time.sleep
    app.state.work_events_clock = fake_time.clock
    return Rig(app, worm, log, principal_factory, fake_time, signer, operator)


@pytest.fixture
async def streams() -> AsyncIterator[list[Stream]]:
    """Streams a test opened; closed at teardown so no task outlives the test."""
    opened: list[Stream] = []
    yield opened
    for stream in opened:
        await stream.close()


# --- authorization -----------------------------------------------------------------------


class TestStreamAuthorization:
    async def test_app_without_agent_param_is_refused_403(self, rig: Rig) -> None:
        app = rig.as_app()

        resp = await rig.get(STREAM)

        assert resp.status_code == 403
        denied = [row for row in rig.worm.written if row.outcome == "denied"]
        assert len(denied) == 1 and denied[0].actor_did == app.did

    async def test_app_bound_to_another_agent_is_refused_403(self, rig: Rig) -> None:
        rig.as_app(agents=(BOB,))

        resp = await rig.get(STREAM, params={"agent": OLIVIA})

        assert resp.status_code == 403
        assert resp.json()["scope"] == "events:subscribe"

    async def test_app_without_events_subscribe_is_refused(self, rig: Rig) -> None:
        rig.act_as(rig.factory.app({Scope.RUNS_READ: (OLIVIA,)}))

        assert (await rig.get(STREAM, {"agent": OLIVIA})).status_code == 403


# --- the stream ------------------------------------------------------------------------


class TestStream:
    async def test_events_arrive_in_seq_order_with_id_event_and_data(
        self, rig: Rig, streams: list[Stream]
    ) -> None:
        rig.as_app()
        first = await rig.append("a", kind="gate.waiting", status="waiting", node_id="review")
        second = await rig.append("b", kind="run.failed", status="failed")
        third = await rig.append("c")

        stream = await rig.stream(agent=OLIVIA).open()
        streams.append(stream)
        frames = await stream.wait_events(3)
        await stream.close()

        assert stream.status == 200
        assert stream.headers["content-type"].startswith("text/event-stream")
        assert [f.id for f in frames] == [str(rig.seqs[n]) for n in ("a", "b", "c")]
        assert [int(f.id or 0) for f in frames] == sorted(int(f.id or 0) for f in frames)
        assert [f.event for f in frames] == ["gate.waiting", "run.failed", "run.finished"]
        assert [f.envelope["event_id"] for f in frames] == [first, second, third]
        assert frames[0].envelope["node_id"] == "review"
        assert "callback_url" not in frames[0].envelope

    async def test_new_events_are_sent_after_the_stream_opens(
        self, rig: Rig, streams: list[Stream]
    ) -> None:
        rig.as_app()
        stream = await rig.stream(agent=OLIVIA).open()
        streams.append(stream)
        await stream.wait_for(lambda frames: any(f.comment for f in frames))

        late = await rig.append("late")
        (frame,) = await stream.wait_events(1)
        await stream.close()

        assert frame.envelope["event_id"] == late

    async def test_last_event_id_resumes_after_that_seq(
        self, rig: Rig, streams: list[Stream]
    ) -> None:
        rig.as_app()
        await rig.append("a")
        second = await rig.append("b")
        third = await rig.append("c")

        stream = await rig.stream({"Last-Event-ID": str(rig.seqs["a"])}, agent=OLIVIA).open()
        streams.append(stream)
        await stream.wait_events(2)
        await stream.wait_heartbeat_after_events()
        await stream.close()

        assert [f.envelope["event_id"] for f in stream.events()] == [second, third]

    async def test_kinds_filter(self, rig: Rig, streams: list[Stream]) -> None:
        rig.as_app()
        await rig.append("finished")
        waiting = await rig.append("waiting", kind="gate.waiting", status="waiting")
        failed = await rig.append("failed", kind="run.failed", status="failed")

        stream = await rig.stream(agent=OLIVIA, kinds="gate.waiting,run.failed").open()
        streams.append(stream)
        await stream.wait_events(2)
        await stream.wait_heartbeat_after_events()
        await stream.close()

        assert [f.envelope["event_id"] for f in stream.events()] == [waiting, failed]

    async def test_run_id_and_correlation_id_filters(
        self, rig: Rig, streams: list[Stream]
    ) -> None:
        rig.as_app()
        await rig.append("other-run", run_id="run-2", correlation_id="order-1")
        await rig.append("other-corr", run_id="run-1", correlation_id="order-2")
        wanted = await rig.append("wanted", run_id="run-1", correlation_id="order-1")

        stream = await rig.stream(agent=OLIVIA, run_id="run-1", correlation_id="order-1").open()
        streams.append(stream)
        await stream.wait_events(1)
        await stream.wait_heartbeat_after_events()
        await stream.close()

        assert [f.envelope["event_id"] for f in stream.events()] == [wanted]

    async def test_other_agent_and_higher_classification_events_never_sent(
        self, rig: Rig, streams: list[Stream]
    ) -> None:
        rig.app.state.inbox_clearance = "CONFIDENTIAL"
        rig.as_app()
        await rig.append("bob", agent_did=BOB)
        await rig.append("secret", classification="SECRET")
        visible = await rig.append("cui", classification="CUI")

        stream = await rig.stream(agent=OLIVIA).open()
        streams.append(stream)
        await stream.wait_events(1)
        await stream.wait_heartbeat_after_events()
        await rig.append("bob-late", agent_did=BOB)
        await rig.append("secret-late", classification="TOP_SECRET")
        await stream.wait_heartbeat_after_events()
        await stream.close()

        assert [f.envelope["event_id"] for f in stream.events()] == [visible]

    async def test_operator_session_may_omit_agent_and_sees_every_agent(
        self, rig: Rig, streams: list[Stream]
    ) -> None:
        rig.as_operator()
        mine = await rig.append("olivia")
        theirs = await rig.append("bob", agent_did=BOB)

        stream = await rig.stream().open()
        streams.append(stream)
        await stream.wait_events(2)
        await stream.close()

        assert stream.status == 200
        assert {f.envelope["event_id"] for f in stream.events()} == {mine, theirs}

    async def test_heartbeat_comment_within_15_seconds(
        self, rig: Rig, streams: list[Stream]
    ) -> None:
        rig.as_app()
        started = rig.time.now

        stream = await rig.stream(agent=OLIVIA).open()
        streams.append(stream)
        await stream.wait_for(lambda frames: any(f.comment for f in frames))
        await stream.close()

        assert stream.events() == []
        elapsed = stream.comment_times[0] - started
        assert 0 < elapsed <= 15.0, f"first heartbeat after {elapsed} fake seconds"
        gaps = [
            b - a for a, b in zip(stream.comment_times, stream.comment_times[1:], strict=False)
        ]
        assert all(gap <= 15.0 for gap in gaps), f"heartbeat gaps {gaps}"
        assert rig.time.slept, "the stream must wait through the injected sleep"


# --- the per-principal cap --------------------------------------------------------------


class TestStreamCap:
    async def test_fifth_concurrent_stream_for_one_principal_is_429(
        self, rig: Rig, streams: list[Stream]
    ) -> None:
        app = rig.as_app()
        for _ in range(4):
            stream = await rig.stream(agent=OLIVIA).open()
            streams.append(stream)
            assert stream.status == 200

        fifth = await rig.stream(agent=OLIVIA).open()
        streams.append(fifth)

        assert fifth.status == 429

        rig.as_app()  # a new app principal (fresh DID) is not capped by the first one's streams
        other = await rig.stream(agent=OLIVIA).open()
        streams.append(other)
        assert other.status == 200

        rig.act_as(app)
        await streams[0].close()
        again = await rig.stream(agent=OLIVIA).open()
        streams.append(again)
        assert again.status == 200, "closing a stream must release its slot"


# --- audit -------------------------------------------------------------------------------


async def test_subscribe_and_close_are_audited(rig: Rig, streams: list[Stream]) -> None:
    app = rig.as_app()

    stream = await rig.stream(agent=OLIVIA).open()
    streams.append(stream)
    await stream.wait_for(lambda frames: any(f.comment for f in frames))
    (subscribed,) = rig.rows("work_event.subscribe")
    assert rig.rows("work_event.close") == []
    await stream.close()

    (closed,) = rig.rows("work_event.close")
    assert subscribed.outcome == "applied"
    assert subscribed.actor_did == app.did and closed.actor_did == app.did
    assert subscribed.actor_role == "app"
    assert OLIVIA in subscribed.target or OLIVIA in subscribed.detail


# --- the JSON page ---------------------------------------------------------------------


class TestPage:
    async def test_page_returns_events_after_seq_with_limit_and_filters(self, rig: Rig) -> None:
        rig.as_app()
        await rig.append("a")
        b = await rig.append("b", kind="gate.waiting", status="waiting")
        c = await rig.append("c")
        await rig.append("bob", agent_did=BOB)
        await rig.append("secret", classification="SECRET")
        d = await rig.append("d")

        resp = await rig.get(PAGE, {"agent": OLIVIA, "after": rig.seqs["a"], "limit": 2})
        rest = await rig.get(PAGE, {"agent": OLIVIA, "after": rig.seqs["c"], "limit": 50})
        kinds = await rig.get(PAGE, {"agent": OLIVIA, "after": 0, "kinds": "gate.waiting"})

        assert resp.status_code == 200, resp.text
        page = resp.json()["events"]
        assert [e["event_id"] for e in page] == [b, c]
        assert [e["seq"] for e in page] == [rig.seqs["b"], rig.seqs["c"]]
        assert [e["event_id"] for e in rest.json()["events"]] == [d]
        assert [e["event_id"] for e in kinds.json()["events"]] == [b]

    async def test_page_run_id_and_correlation_filters(self, rig: Rig) -> None:
        rig.as_app()
        await rig.append("x", run_id="run-2")
        wanted = await rig.append("y", run_id="run-1", correlation_id="order-9")

        resp = await rig.get(
            PAGE, params={"agent": OLIVIA, "run_id": "run-1", "correlation_id": "order-9"}
        )

        assert [e["event_id"] for e in resp.json()["events"]] == [wanted]

    async def test_page_scope_is_checked(self, rig: Rig) -> None:
        rig.as_app(agents=(BOB,))
        assert (await rig.get(PAGE, {"agent": OLIVIA})).status_code == 403
        assert (await rig.get(PAGE)).status_code == 403


# --- the published key -----------------------------------------------------------------


class TestKeys:
    async def test_keys_returns_the_work_events_key_not_the_operator_key(self, rig: Rig) -> None:
        rig.as_app()

        resp = await rig.get(KEYS, params={"agent": OLIVIA})

        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert set(body) == {"key_id", "algorithm", "public_key"}
        assert body["key_id"] == rig.signer.key_id
        assert body["algorithm"] == "ed25519"
        published = base64.b64decode(body["public_key"], validate=True)
        assert published == rig.signer.public_key
        assert published != rig.operator.public_key

    async def test_keys_without_a_work_events_key_never_falls_back_to_the_operator(
        self, rig: Rig
    ) -> None:
        rig.app.state.work_event_signer = None
        rig.as_app()

        resp = await rig.get(KEYS, params={"agent": OLIVIA})

        assert resp.status_code == 503
        operator = rig.operator.public_key
        assert base64.b64encode(operator).decode() not in resp.text
        assert operator.hex() not in resp.text

    async def test_keys_scope_is_checked(self, rig: Rig) -> None:
        rig.as_app(agents=(BOB,))

        assert (await rig.get(KEYS, {"agent": OLIVIA})).status_code == 403
        assert (await rig.get(KEYS)).status_code == 403
