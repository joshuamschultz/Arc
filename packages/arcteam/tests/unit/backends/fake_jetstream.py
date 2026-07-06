"""In-memory fake of the NATS JetStream calls ``NatsBackend`` uses.

This double lets the arcteam unit suite exercise ``NatsBackend`` with zero
running ``nats-server``. It implements only the JetStream surface the backend
touches: streams (``add_stream``/``publish``/``get_msg``/``get_last_msg``/
``stream_info``), KV buckets (``key_value``/``create_key_value`` + get/put/
delete/keys), and durable pull consumers (``pull_subscribe`` + fetch/ack).

Semantics mirror JetStream where the backend depends on them: publish returns
a monotonically increasing per-stream sequence, and a durable consumer's ack
floor persists across re-subscription so a "restart" resumes from the last
acked message.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from nats.errors import TimeoutError as NatsTimeout
from nats.js.api import KeyValueConfig
from nats.js.errors import (
    BucketNotFoundError,
    KeyNotFoundError,
    NoKeysError,
    NotFoundError,
)


@dataclass
class _Entry:
    value: bytes


class FakeKV:
    """In-memory KeyValue bucket."""

    def __init__(self) -> None:
        self._data: dict[str, bytes] = {}

    async def get(self, key: str) -> _Entry:
        if key not in self._data:
            raise KeyNotFoundError()
        return _Entry(value=self._data[key])

    async def put(self, key: str, value: bytes) -> int:
        self._data[key] = value
        return len(self._data)

    async def delete(self, key: str) -> bool:
        self._data.pop(key, None)
        return True

    async def keys(self) -> list[str]:
        if not self._data:
            raise NoKeysError()
        return list(self._data.keys())


@dataclass
class _StreamMsg:
    seq: int
    subject: str
    data: bytes


@dataclass
class _State:
    last_seq: int = 0


@dataclass
class _StreamInfo:
    state: _State = field(default_factory=_State)


class _RawMsg:
    def __init__(self, seq: int, subject: str, data: bytes) -> None:
        self.seq = seq
        self.subject = subject
        self.data = data


class _FetchedMsg:
    """A message handed to a consumer; ``ack`` advances the durable floor."""

    def __init__(self, js: FakeJetStream, stream: str, durable: str, msg: _StreamMsg) -> None:
        self._js = js
        self._stream = stream
        self._durable = durable
        self.data = msg.data
        self.seq = msg.seq

    async def ack(self) -> None:
        key = (self._stream, self._durable)
        self._js.floors[key] = max(self._js.floors.get(key, 0), self.seq)


class _PullSub:
    def __init__(self, js: FakeJetStream, stream: str, durable: str) -> None:
        self._js = js
        self._stream = stream
        self._durable = durable

    async def fetch(self, batch: int, timeout: float | None = None) -> list[_FetchedMsg]:
        floor = self._js.floors.get((self._stream, self._durable), 0)
        pending = [m for m in self._js.streams[self._stream] if m.seq > floor]
        if not pending:
            raise NatsTimeout()
        chosen = pending[:batch]
        return [_FetchedMsg(self._js, self._stream, self._durable, m) for m in chosen]


class FakeJetStream:
    """Minimal JetStream context double."""

    def __init__(self) -> None:
        self.streams: dict[str, list[_StreamMsg]] = {}
        self.subjects: dict[str, str] = {}  # subject -> stream name
        self.kvs: dict[str, FakeKV] = {}
        self.floors: dict[tuple[str, str], int] = {}

    async def add_stream(self, config: Any = None, **params: Any) -> _StreamInfo:
        name = params.get("name") or getattr(config, "name", None)
        subjects = params.get("subjects") or getattr(config, "subjects", []) or []
        if name not in self.streams:
            self.streams[name] = []
        for subj in subjects:
            self.subjects[subj] = name
        info = _StreamInfo()
        info.state.last_seq = len(self.streams[name])
        return info

    async def stream_info(self, name: str, subjects_filter: str | None = None) -> _StreamInfo:
        if name not in self.streams:
            raise NotFoundError()
        info = _StreamInfo()
        info.state.last_seq = self.streams[name][-1].seq if self.streams[name] else 0
        return info

    async def publish(
        self,
        subject: str,
        payload: bytes = b"",
        timeout: float | None = None,
        stream: str | None = None,
        headers: dict[str, Any] | None = None,
    ) -> Any:
        name = stream or self.subjects.get(subject)
        if name is None:
            raise NotFoundError()
        msgs = self.streams.setdefault(name, [])
        seq = (msgs[-1].seq + 1) if msgs else 1
        msgs.append(_StreamMsg(seq=seq, subject=subject, data=payload))

        class _Ack:
            def __init__(self, seq: int, stream: str) -> None:
                self.seq = seq
                self.stream = stream

        return _Ack(seq, name)

    async def get_msg(self, stream_name: str, seq: int | None = None, **_: Any) -> _RawMsg:
        for m in self.streams.get(stream_name, []):
            if m.seq == seq:
                return _RawMsg(m.seq, m.subject, m.data)
        raise NotFoundError()

    async def get_last_msg(self, stream_name: str, subject: str, **_: Any) -> _RawMsg:
        msgs = self.streams.get(stream_name, [])
        if not msgs:
            raise NotFoundError()
        m = msgs[-1]
        return _RawMsg(m.seq, m.subject, m.data)

    async def key_value(self, bucket: str) -> FakeKV:
        if bucket not in self.kvs:
            raise BucketNotFoundError()
        return self.kvs[bucket]

    async def create_key_value(
        self, config: KeyValueConfig | None = None, **params: Any
    ) -> FakeKV:
        bucket = params.get("bucket") or (config.bucket if config else None)
        assert bucket is not None
        kv = self.kvs.setdefault(bucket, FakeKV())
        return kv

    async def pull_subscribe(
        self, subject: str, durable: str | None = None, stream: str | None = None, **_: Any
    ) -> _PullSub:
        name = stream or self.subjects.get(subject)
        assert name is not None
        assert durable is not None
        return _PullSub(self, name, durable)


def payload(data: dict[str, Any]) -> bytes:
    """Encode a record the way the backend does (for assembling fixtures)."""
    return json.dumps(data, ensure_ascii=False).encode("utf-8")
