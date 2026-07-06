"""Integration test: NatsBackend against a real nats-server with JetStream.

Skipped automatically when the ``nats-server`` binary is not on PATH, so the
default unit suite stays server-free (the fake-JetStream tests cover the same
surface). Marked ``slow`` because it spins up a real server process.
"""

from __future__ import annotations

import shutil
import socket
import subprocess
import tempfile
import time
from collections.abc import AsyncGenerator, Iterator
from pathlib import Path

import pytest

from arcteam.backends.nats import NatsBackend

pytestmark = [
    pytest.mark.slow,
    pytest.mark.asyncio,
    pytest.mark.skipif(shutil.which("nats-server") is None, reason="nats-server not installed"),
]

STREAMS = "messages/streams"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port: int = s.getsockname()[1]
        return port


@pytest.fixture(scope="module")
def server_url() -> Iterator[str]:
    port = _free_port()
    store = tempfile.mkdtemp(prefix="arcteam-js-")
    proc = subprocess.Popen(
        ["nats-server", "-js", "-p", str(port), "-sd", store],  # noqa: S607  # dev tool resolved via PATH, guarded by shutil.which
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                break
        except OSError:
            time.sleep(0.05)
    else:
        proc.terminate()
        pytest.fail("nats-server did not start")
    try:
        yield f"nats://127.0.0.1:{port}"
    finally:
        proc.terminate()
        proc.wait(timeout=5)
        shutil.rmtree(Path(store), ignore_errors=True)


@pytest.fixture
async def backend(server_url: str) -> AsyncGenerator[NatsBackend, None]:
    be = await NatsBackend.connect(server_url)
    try:
        yield be
    finally:
        await be.close()


async def test_records_streams_and_durable_resume(backend: NatsBackend) -> None:
    # Records roundtrip through JetStream KV.
    await backend.write("reg", "did:arc:local:agent/a1", {"handle": "a1"})
    assert await backend.read("reg", "did:arc:local:agent/a1") == {"handle": "a1"}
    assert await backend.list_keys("reg") == ["did:arc:local:agent/a1"]

    # Streams: publish assigns monotonic JetStream sequences.
    for i in range(3):
        seq, _ = await backend.append_auto_seq(STREAMS, "arc.agent.a1", {"body": str(i)})
        assert seq == i + 1
    rows = await backend.read_stream(STREAMS, "arc.agent.a1", after_seq=1)
    assert [r["body"] for r in rows] == ["1", "2"]
    last = await backend.read_last(STREAMS, "arc.agent.a1")
    assert last is not None and last["body"] == "2"

    # Durable consumer resumes from its last ack after a rebind (REQ-021).
    consumer = await backend.open_consumer(STREAMS, "arc.agent.a1", "a1-inbox")
    first = await consumer.fetch(2)
    assert [m.data["body"] for m in first] == ["0", "1"]
    for m in first:
        await m.ack()
    resumed = await backend.open_consumer(STREAMS, "arc.agent.a1", "a1-inbox")
    rest = await resumed.fetch(10)
    assert [m.data["body"] for m in rest] == ["2"]
