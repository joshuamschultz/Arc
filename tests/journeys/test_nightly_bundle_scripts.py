"""nightly-meeting-ingest script nodes run deterministically, with no model and no network.

The scripts are driven exactly as the runner drives a script node
(``_run_script_node``): a subprocess whose inputs arrive in ``ARC_*``
environment variables and whose stdout is the node's JSON output.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tomllib
from pathlib import Path
from typing import Any

import pytest
from arcteam.workflow.models import parse_definition
from arcteam.workflow.validator import validate_definition

BUNDLE = Path(__file__).resolve().parents[2] / "docs/runbooks/alpha-2/nightly-meeting-ingest"
ARCHIVE = BUNDLE / "scripts" / "archive_transcripts.py"
EMPTY_DAY = BUNDLE / "scripts" / "no_new_meetings.py"


def _run(
    script: Path, tmp_path: Path, new_files: list[Any], **extra_env: str
) -> subprocess.CompletedProcess[str]:
    source = tmp_path / "dropbox"
    workdir = tmp_path / "work"
    source.mkdir(exist_ok=True)
    workdir.mkdir(exist_ok=True)
    env = {
        **os.environ,
        "ARC_WORKDIR": str(workdir),
        "ARC_BUNDLE_ROOT": str(BUNDLE),
        "ARC_MEETINGS_SOURCE": str(source),
        "ARC_UPSTREAM": json.dumps(
            {"filter_new": {"new_files": new_files, "count": len(new_files)}}
        ),
        **extra_env,
    }
    return subprocess.run(
        [sys.executable, str(script)], env=env, capture_output=True, text=True, timeout=30
    )


def _put(tmp_path: Path, relative: str, body: bytes) -> None:
    target = tmp_path / "dropbox" / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(body)


def _archived(tmp_path: Path, meeting: str, name: str) -> Path:
    return tmp_path / "work" / "crm" / "meetings" / "transcripts" / meeting / name


def test_nightly_bundle_script_nodes_run_without_llm(tmp_path: Path) -> None:
    _put(tmp_path, "standup/2026-10-01.txt", b"hello transcript")
    done = _run(ARCHIVE, tmp_path, ["/Meetings/standup/2026-10-01.txt"])
    assert done.returncode == 0, done.stderr
    out = json.loads(done.stdout)
    assert out["status"] == "archived" and out["count"] == 1
    assert _archived(tmp_path, "standup", "2026-10-01.txt").read_bytes() == b"hello transcript"

    empty = _run(EMPTY_DAY, tmp_path, [])
    assert empty.returncode == 0
    assert json.loads(empty.stdout) == {"status": "nothing_new"}


def test_archive_is_idempotent(tmp_path: Path) -> None:
    _put(tmp_path, "standup/a.txt", b"one")
    first = json.loads(_run(ARCHIVE, tmp_path, ["standup/a.txt"]).stdout)
    second = _run(ARCHIVE, tmp_path, ["standup/a.txt"])
    assert second.returncode == 0, second.stderr
    again = json.loads(second.stdout)
    assert first["archived"] and not first["skipped"]
    assert again["archived"] == [] and len(again["skipped"]) == 1


def test_archive_output_is_deterministic(tmp_path: Path) -> None:
    for name in ("b.txt", "a.txt"):
        _put(tmp_path, f"m/{name}", name.encode())
    out = json.loads(_run(ARCHIVE, tmp_path, ["m/b.txt", "m/a.txt"]).stdout)
    assert out["archived"] == sorted(out["archived"])


def test_archive_refuses_path_escape(tmp_path: Path) -> None:
    (tmp_path / "secret.txt").write_text("outside")
    done = _run(ARCHIVE, tmp_path, ["../secret.txt"])
    assert done.returncode != 0
    assert "escapes" in done.stderr
    assert not (tmp_path / "work" / "crm").exists()


def test_archive_refuses_symlink_escape(tmp_path: Path) -> None:
    (tmp_path / "secret.txt").write_text("outside")
    link = tmp_path / "dropbox" / "m" / "link.txt"
    link.parent.mkdir(parents=True)
    link.symlink_to(tmp_path / "secret.txt")
    done = _run(ARCHIVE, tmp_path, ["m/link.txt"])
    assert done.returncode != 0
    assert not _archived(tmp_path, "m", "link.txt").exists()


def test_archive_never_overwrites_a_different_archived_copy(tmp_path: Path) -> None:
    _put(tmp_path, "m/a.txt", b"original")
    assert _run(ARCHIVE, tmp_path, ["m/a.txt"]).returncode == 0
    _put(tmp_path, "m/a.txt", b"edited later")
    done = _run(ARCHIVE, tmp_path, ["m/a.txt"])
    assert done.returncode != 0
    assert "conflict" in done.stderr
    assert _archived(tmp_path, "m", "a.txt").read_bytes() == b"original"


def test_archive_missing_source_fails_loudly_after_archiving_the_rest(tmp_path: Path) -> None:
    _put(tmp_path, "m/ok.txt", b"fine")
    done = _run(ARCHIVE, tmp_path, ["m/gone.txt", "m/ok.txt"])
    assert done.returncode != 0
    assert "gone.txt" in done.stderr
    # Progress is kept, so the retry only has the failed file left to do.
    assert _archived(tmp_path, "m", "ok.txt").read_bytes() == b"fine"


def test_archive_bounds_file_count(tmp_path: Path) -> None:
    files = [f"m/{i}.txt" for i in range(501)]
    done = _run(ARCHIVE, tmp_path, files)
    assert done.returncode != 0
    assert "too many" in done.stderr


def test_archive_bounds_file_size(tmp_path: Path) -> None:
    _put(tmp_path, "m/big.txt", b"x" * 2048)
    done = _run(ARCHIVE, tmp_path, ["m/big.txt"], ARC_ARCHIVE_MAX_FILE_BYTES="1024")
    assert done.returncode != 0
    assert "too large" in done.stderr


def test_archive_refuses_an_upstream_reference_instead_of_a_list(tmp_path: Path) -> None:
    done = _run(ARCHIVE, tmp_path, {"artifact_ref": {"task_id": "t-1"}})  # type: ignore[arg-type]
    assert done.returncode != 0
    assert "new_files" in done.stderr


def test_archive_copy_is_byte_verbatim(tmp_path: Path) -> None:
    body = bytes(range(256)) * 10
    _put(tmp_path, "m/audio.bin", body)
    assert _run(ARCHIVE, tmp_path, [{"path": "m/audio.bin"}]).returncode == 0
    copied = _archived(tmp_path, "m", "audio.bin").read_bytes()
    assert hashlib.sha256(copied).digest() == hashlib.sha256(body).digest()


def _bundle_definition() -> Any:
    with (BUNDLE / "workflow.toml").open("rb") as handle:
        return parse_definition(tomllib.load(handle))


def test_bundle_scripts_and_schema_pass_the_workflow_validator() -> None:
    issues = validate_definition(_bundle_definition(), bundle_root=BUNDLE)
    ours = [i for i in issues if i.field in ("script",) or i.node_id == "archive_transcripts"]
    assert ours == [], ours


@pytest.mark.parametrize("script", [ARCHIVE, EMPTY_DAY])
def test_scripts_take_no_network_or_shell(script: Path) -> None:
    text = script.read_text(encoding="utf-8")
    for banned in ("import socket", "urllib", "requests", "httpx", "shell=True", "os.system"):
        assert banned not in text
