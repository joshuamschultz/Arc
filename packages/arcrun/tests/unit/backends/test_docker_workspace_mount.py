"""C1 — DockerBackend workspace bind-mount argv construction (no daemon).

The container gets the agent's workspace at ``/workspace:rw`` and each protected
subpath (identity.md/policy.md/context.md) mounted read-only OVER the rw share so
the shell cannot rewrite it. Host ``~/.arc`` and ``.audit`` are NEVER mounted — the
whole point of REQ-021 is that they are simply absent inside the container.

All docker CLI calls are mocked; this asserts the ``docker run`` argv only.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from arcrun.backends.docker import DockerBackend, _docker_run_detached


class _FakeProc:
    def __init__(self, *, stdout: bytes, returncode: int = 0) -> None:
        self._stdout = stdout
        self.returncode = returncode

    async def communicate(self) -> tuple[bytes, bytes]:
        return self._stdout, b""


def _capture_run_argv(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, ...]]:
    seen: list[tuple[str, ...]] = []

    async def fake_exec(*argv: str, **kwargs: object) -> _FakeProc:
        seen.append(argv)
        return _FakeProc(stdout=b"container-cafe\n")

    monkeypatch.setattr("arcrun.backends.docker.asyncio.create_subprocess_exec", fake_exec)
    return seen


@pytest.mark.asyncio
async def test_docker_run_mounts_workspace_rw_and_subpath_ro(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    (tmp_path / "identity.md").write_text("did:arc:agent:alpha")
    seen = _capture_run_argv(monkeypatch)

    cid = await _docker_run_detached(
        image="python:3.11-slim",
        pids_limit=64,
        network="none",
        workspace_mount=tmp_path,
        readonly_subpaths=[Path("identity.md")],
    )
    assert cid == "container-cafe"

    argv = seen[0]
    joined = " ".join(argv)

    # Workspace mounted read-write, working dir set to it.
    assert f"{tmp_path}:/workspace:rw" in argv
    assert "--workdir" in argv
    assert argv[argv.index("--workdir") + 1] == "/workspace"

    # Protected subpath mounted read-only over the rw workspace.
    assert f"{tmp_path}/identity.md:/workspace/identity.md:ro" in argv

    # REQ-021: host ~/.arc and .audit are NEVER mounted.
    assert ".arc" not in joined
    assert ".audit" not in joined

    # Hardening flags stay intact alongside the bind mount.
    assert "--cap-drop=ALL" in argv
    assert "--security-opt=no-new-privileges" in argv
    assert "--network=none" in argv
    assert "--read-only" in argv
    assert "--tmpfs=/tmp:noexec,nosuid,size=64m" in argv


@pytest.mark.asyncio
async def test_docker_run_skips_absent_subpath(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # policy.md does not exist → no ro mount is added for it.
    seen = _capture_run_argv(monkeypatch)

    await _docker_run_detached(
        image="python:3.11-slim",
        pids_limit=64,
        network="none",
        workspace_mount=tmp_path,
        readonly_subpaths=[Path("policy.md")],
    )
    argv = seen[0]
    assert not any("policy.md" in a for a in argv)


@pytest.mark.asyncio
async def test_docker_run_without_workspace_has_no_bind_mount(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen = _capture_run_argv(monkeypatch)

    await _docker_run_detached(image="python:3.11-slim", pids_limit=64, network="none")
    argv = seen[0]
    assert "/workspace" not in " ".join(argv)
    assert "-v" not in argv


def test_backend_stores_workspace_mount(tmp_path: Path) -> None:
    backend = DockerBackend(workspace_mount=tmp_path, readonly_subpaths=[Path("identity.md")])
    assert backend._workspace_mount == tmp_path
    assert backend._readonly_subpaths == [Path("identity.md")]
