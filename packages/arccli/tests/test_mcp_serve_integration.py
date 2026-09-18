"""SPEC-082 /review backfill — ``arc mcp serve`` real wiring (no mocks).

``test_mcp_serve_command.py`` proves the handler routes to the right collaborators,
but it patches every one of them: the real pipe wiring in ``_process_streams`` and
the real agent load in ``_load_arcagent`` never ran. The lessons file is explicit —
exercise the real pipe wiring, not a mock — so these two tests drive the genuine
code paths:

- ``_process_streams`` is driven with real OS pipes standing in for stdin/stdout;
  a byte line pushed into the stdin pipe is read back through the returned
  ``StreamReader``, proving the fds are wired, not stubbed.
- ``_load_arcagent`` is called against a real scaffolded agent directory (the three
  sibling TOML files ``arc agent create`` writes) and must return a started-able
  ``ArcAgent`` plus its config and path.
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import pytest

from arccli.commands import mcp
from arccli.commands.agent._common import (
    _DEFAULT_ARCLLM_CONFIG,
    _DEFAULT_ARCRUN_CONFIG,
    render_agent_config,
)


def test_process_streams_wires_stdin_stdout_to_real_asyncio_streams(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Real OS pipes: a line written into the stdin pipe is read back via the reader.

    ``_process_streams`` binds ``sys.stdin`` / ``sys.stdout`` through
    ``loop.connect_read_pipe`` / ``connect_write_pipe``. Pytest's capture replaces
    those with objects that have no usable fd, so the test substitutes real
    ``os.pipe`` file objects — the same wiring the command uses at runtime.
    """
    stdin_read_fd, stdin_write_fd = os.pipe()
    stdout_read_fd, stdout_write_fd = os.pipe()

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    stdin_file = os.fdopen(stdin_read_fd, "r")
    stdout_file = os.fdopen(stdout_write_fd, "w", buffering=1)
    monkeypatch.setattr(sys, "stdin", stdin_file)
    monkeypatch.setattr(sys, "stdout", stdout_file)

    try:
        reader, writer = mcp._process_streams()

        assert isinstance(reader, asyncio.StreamReader)
        assert isinstance(writer, asyncio.StreamWriter)

        # A byte line fed into the underlying stdin pipe surfaces through the reader.
        os.write(stdin_write_fd, b"jsonrpc-line\n")
        line = loop.run_until_complete(asyncio.wait_for(reader.readline(), timeout=2))
        assert line == b"jsonrpc-line\n"
    finally:
        writer.close()
        loop.run_until_complete(asyncio.sleep(0))
        loop.close()
        asyncio.set_event_loop(None)
        for fd in (stdin_write_fd, stdout_read_fd):
            try:
                os.close(fd)
            except OSError:
                pass


def test_load_arcagent_returns_a_started_able_agent(tmp_path: Path) -> None:
    """The real loader turns a scaffolded agent directory into an ``ArcAgent`` triple.

    Scaffolds the three sibling TOML files ``arc agent create`` produces, then calls
    the genuine ``_load_arcagent`` (which delegates to the shared agent loader and
    constructs a real ``ArcAgent`` with a fleet). Asserts the returned agent, config,
    and path are the real objects the ``arc mcp serve`` handler feeds to
    ``build_mcp_door`` — no delegation stub.
    """
    (tmp_path / "arcagent.toml").write_text(
        render_agent_config(name="probe", did="did:arc:test:probe"), encoding="utf-8"
    )
    (tmp_path / "arcllm.toml").write_text(_DEFAULT_ARCLLM_CONFIG, encoding="utf-8")
    (tmp_path / "arcrun.toml").write_text(_DEFAULT_ARCRUN_CONFIG, encoding="utf-8")

    agent, config, config_path = mcp._load_arcagent(str(tmp_path))

    # A started-able ArcAgent: startup/shutdown are the coroutines the handler drives.
    assert asyncio.iscoroutinefunction(agent.startup)
    assert asyncio.iscoroutinefunction(agent.shutdown)
    assert config.agent.name == "probe"
    assert config_path == tmp_path / "arcagent.toml"


def test_load_arcagent_exits_when_no_config_present(tmp_path: Path) -> None:
    """An agent directory with no ``arcagent.toml`` fails closed with a non-zero exit."""
    with pytest.raises(SystemExit) as exc:
        mcp._load_arcagent(str(tmp_path))
    assert exc.value.code != 0
