"""Intake must only inspect bytes; it may not execute an uploaded payload."""

from __future__ import annotations

import ast
import subprocess
import zipfile
from pathlib import Path

from arcagent.modules.capability_import.archive import intake


def test_intake_never_executes_or_spawns_uploaded_content(tmp_path: Path, monkeypatch) -> None:
    archive = tmp_path / "payload.zip"
    with zipfile.ZipFile(archive, "w") as handle:
        handle.writestr("tools/evil.py", b"raise RuntimeError('executed')\n")
    calls: list[str] = []
    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: calls.append("run"))
    monkeypatch.setattr(subprocess, "Popen", lambda *args, **kwargs: calls.append("popen"))

    intake(archive, tmp_path / "agent" / "capabilities")

    assert calls == []
    source = Path(__file__).parents[3] / "src" / "arcagent" / "modules" / "capability_import"
    imports = ast.parse((source / "archive.py").read_text(encoding="utf-8"))
    forbidden = {"subprocess", "socket", "urllib", "httpx", "requests", "importlib"}
    assert not {
        alias.name.split(".")[0]
        for node in ast.walk(imports)
        if isinstance(node, (ast.Import, ast.ImportFrom))
        for alias in node.names
    }.intersection(forbidden)
