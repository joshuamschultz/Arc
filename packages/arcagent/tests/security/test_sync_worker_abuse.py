"""Battery: the live sync worker refuses every request it cannot authenticate or authorize.

These attack the real child process over its real socket. Nothing is faked: the
supervisor spawns the worker exactly as ``arc ui start`` does.

* a request sealed with a guessed secret is refused and never acted on;
* a genuine request captured off the socket and replayed is refused;
* a process other than the worker's parent, even holding the secret, is refused;
* an authenticated request naming a store root the worker did not derive itself
  (another agent's workspace, a system path) is refused and writes nothing.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path
from typing import Any

import pytest

from arcagent.modules.connected_data.sync_worker import process_supervisor
from arcagent.modules.connected_data.sync_worker.protocol import (
    WORKER_REQUEST,
    WORKER_RESPONSE,
    Frame,
    encode,
    read_frame,
    request_header,
    seal,
    verify,
)
from arcagent.modules.connected_data.sync_worker.rpc import RemoteError
from arcagent.modules.connected_data.sync_worker.specs import StoreSpec

_DID = "did:arc:test:victim"
_SOURCE = {"connection_id": "wiki", "source_kind": "confluence", "account_id": "acct"}


async def _live_worker() -> Any:
    supervisor = process_supervisor()
    await supervisor.start()
    assert await supervisor.wait_ready(60), supervisor.status()
    return supervisor


def _socket(supervisor: Any) -> Path:
    return supervisor._dir / "worker.sock"  # reason: the attacker knows the socket path


async def _send_raw(path: Path, wire: bytes) -> Frame | None:
    """Send bytes as any local process would; the reply, or None if refused."""
    reader, writer = await asyncio.open_unix_connection(str(path))
    try:
        writer.write(wire)
        await writer.drain()
        try:
            return await asyncio.wait_for(read_frame(reader), 5)
        except asyncio.IncompleteReadError:
            return None
    finally:
        writer.close()
        await writer.wait_closed()


def _agent(tmp_path: Path, name: str, did: str) -> Path:
    agent_dir = tmp_path / "team" / name
    (agent_dir / "workspace").mkdir(parents=True)
    (agent_dir / "arcagent.toml").write_text(
        f'[agent]\nworkspace = "./workspace"\n[identity]\ndid = "{did}"\n', encoding="utf-8"
    )
    return agent_dir


def _write_request(store: StoreSpec) -> dict[str, Any]:
    return {
        "store": store.model_dump(mode="json"),
        "method": "finish_sync",
        "args": {"source": _SOURCE},
    }


async def test_a_request_sealed_with_a_guessed_secret_is_refused() -> None:
    supervisor = await _live_worker()
    forged = seal(Frame(request_header("ping", {})), b"\x00" * 32, direction=WORKER_REQUEST)

    assert await _send_raw(_socket(supervisor), encode(forged)) is None
    assert supervisor.status().state == "up", "a forged request took the worker down"


async def test_a_captured_request_replayed_is_refused() -> None:
    supervisor = await _live_worker()
    genuine = seal(
        Frame(request_header("ping", {})), supervisor._secret, direction=WORKER_REQUEST
    )  # reason: the attacker captured one genuine frame off the socket
    wire = encode(genuine)

    first = await _send_raw(_socket(supervisor), wire)
    assert first is not None and verify(first, supervisor._secret, direction=WORKER_RESPONSE)
    assert await _send_raw(_socket(supervisor), wire) is None, "the replay was answered"


_OTHER_PROCESS = textwrap.dedent(
    """
    import asyncio, json, sys
    from arcagent.modules.connected_data.sync_worker.protocol import (
        WORKER_REQUEST, Frame, encode, read_frame, request_header, seal,
    )

    async def main(path, secret):
        reader, writer = await asyncio.open_unix_connection(path)
        frame = seal(Frame(request_header("ping", {})), secret, direction=WORKER_REQUEST)
        writer.write(encode(frame))
        await writer.drain()
        try:
            await asyncio.wait_for(read_frame(reader), 5)
            print("ANSWERED")
        except asyncio.IncompleteReadError:
            print("REFUSED")

    asyncio.run(main(sys.argv[1], bytes.fromhex(sys.argv[2])))
    """
)


async def test_another_process_holding_the_secret_is_refused(tmp_path: Path) -> None:
    supervisor = await _live_worker()
    script = tmp_path / "intruder.py"
    script.write_text(_OTHER_PROCESS, encoding="utf-8")
    result = await asyncio.to_thread(
        subprocess.run,
        [sys.executable, str(script), str(_socket(supervisor)), supervisor._secret.hex()],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.stdout.strip() == "REFUSED", (result.stdout, result.stderr)


@pytest.mark.parametrize("target", ["other-agent", "system-path"])
async def test_an_authenticated_write_to_a_store_outside_the_allowed_set_is_refused(
    tmp_path: Path, target: str
) -> None:
    supervisor = await _live_worker()
    mine = _agent(tmp_path, "mine", _DID)
    theirs = _agent(tmp_path, "theirs", "did:arc:test:someone-else")
    root = theirs / "workspace" if target == "other-agent" else Path("/etc")
    store = StoreSpec(
        kind="own",
        agent_did=_DID,
        root=str(root),
        authority="owner",
        config_path=str(mine / "arcagent.toml"),
    )

    with pytest.raises(RemoteError) as caught:
        await supervisor.client().call("write", _write_request(store), timeout=30)

    assert caught.value.error["reason"] == "store_root_not_allowed"
    assert not (theirs / "workspace" / "memory").exists(), "the other agent's store was written"
    assert json.dumps(caught.value.error).count("store_root_not_allowed") >= 1
    assert os.getpid() != supervisor.status().pid
