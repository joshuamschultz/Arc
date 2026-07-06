"""#1 regression — execute_python stages code INTO the container, not via host path.

The tier-routed container path (make_execute_tool → DockerBackend) must hand the
agent's source to the interpreter INSIDE the container. The original bug wrote a
host ``tempfile.TemporaryDirectory()`` and passed it as ``docker exec --workdir``,
a path that does not exist in the ``--read-only`` container, so every container
execution failed. These tests mock the docker CLI and assert the code is fed over
stdin to ``python3 -`` with no host tempdir referenced.
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import patch

import pytest

from arcrun.builtins.execute import make_execute_tool
from arcrun.events import EventBus
from arcrun.types import ToolContext


def _ctx() -> ToolContext:
    return ToolContext(
        run_id="stage-test",
        tool_call_id="tc",
        turn_number=1,
        event_bus=EventBus(run_id="stage-test"),
        cancelled=asyncio.Event(),
    )


class _FakeStdin:
    def __init__(self, sink: list[bytes]) -> None:
        self._sink = sink

    def write(self, data: bytes) -> None:
        self._sink.append(data)

    def close(self) -> None:
        pass


class _FakeProc:
    def __init__(
        self, *, stdout: bytes = b"", stderr: bytes = b"", returncode: int = 0, stdin_sink=None
    ) -> None:
        self._stdout = stdout
        self._stderr = stderr
        self.returncode = returncode
        self.stdin = _FakeStdin(stdin_sink) if stdin_sink is not None else None

    async def communicate(self) -> tuple[bytes, bytes]:
        return self._stdout, self._stderr

    async def wait(self) -> int:
        return self.returncode

    def kill(self) -> None:
        pass


@pytest.mark.asyncio
async def test_container_code_staged_via_stdin_not_host_path() -> None:
    exec_argv: list[tuple[str, ...]] = []
    stdin_writes: list[bytes] = []
    code = "print('staged inside container')"

    async def fake_exec(*argv: str, **kwargs: object):
        if argv[:3] == ("docker", "run", "-d"):
            return _FakeProc(stdout=b"container-cafe\n")
        if argv[:2] == ("docker", "exec"):
            exec_argv.append(argv)
            return _FakeProc(stdout=b"staged inside container\n", stdin_sink=stdin_writes)
        return _FakeProc()  # docker rm -f

    # personal-default on a non-KVM host routes to the container (docker) backend.
    tool = make_execute_tool(tier="personal")
    with patch("arcrun.backends.docker.asyncio.create_subprocess_exec", new=fake_exec):
        raw = await tool.execute({"code": code}, _ctx())

    result = json.loads(raw)
    assert "staged inside container" in result["stdout"]
    assert result["exit_code"] == 0

    # The code reached the container over stdin (staged inside), not a host path.
    assert b"".join(stdin_writes) == code.encode()

    # Exactly one docker exec; it runs `python3 -` and never points --workdir at a
    # host tempdir (the original bug). The only workdir is the in-container /tmp.
    assert len(exec_argv) == 1
    argv = exec_argv[0]
    assert "python3 -" in argv
    assert code not in argv  # code is NOT an argv element (would leak / be unstaged)
    if "--workdir" in argv:
        workdir = argv[argv.index("--workdir") + 1]
        assert workdir == "/tmp"
