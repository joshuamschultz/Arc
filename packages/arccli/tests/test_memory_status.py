"""``arc memory status`` — can an operator see that semantic recall is off?

The whole point of this command is that nobody should have to read an audit log to
learn that hybrid recall lost its vector third. It must say so plainly, name the
fix, and exit non-zero so a deployment check can gate on it.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from arccli.commands.memory import memory_handler


class _LiveEmbedder:
    async def embed_texts(self, texts: list[str]) -> list[list[float]]:
        return [[0.0] * 384 for _ in texts]


def _patch_embedder(monkeypatch: pytest.MonkeyPatch, embedder: Any) -> None:
    monkeypatch.setattr("arccli.commands.memory.build_embedder", lambda *_a, **_k: embedder)


def test_status_reports_a_live_channel(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _patch_embedder(monkeypatch, _LiveEmbedder())

    memory_handler(["status"])

    out = capsys.readouterr().out
    assert "semantic recall" in out.lower()
    assert "LIVE" in out


def test_status_says_loudly_when_the_embedder_is_missing(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """No embedder -> a clear DEGRADED verdict, the reason, and the install command."""
    _patch_embedder(monkeypatch, None)

    with pytest.raises(SystemExit) as exc:
        memory_handler(["status"])

    assert exc.value.code == 1
    combined = capsys.readouterr()
    text = combined.out + combined.err
    assert "DEGRADED" in text
    assert "uv sync --all-packages" in text


def test_status_reports_per_workspace_vector_coverage(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """An indexed workspace shows its chunk / vector counts."""
    from arcmemory.db import MemoryDB

    _patch_embedder(monkeypatch, _LiveEmbedder())
    workspace = tmp_path / "agent"
    (workspace / "memory" / "entities").mkdir(parents=True)
    MemoryDB(workspace).connect().close()  # a real, discoverable index

    memory_handler(["status", str(workspace)])

    out = capsys.readouterr().out
    assert str(workspace) in out
    assert "chunks" in out
