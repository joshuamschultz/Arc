"""CliAttachment bounds: numeric ceilings, output ceilings, downloads, owned env.

A mail verb that takes a page size must not take ``1000000``; one whose output is
a whole mailbox must not hand the model a gigabyte; one that downloads an
attachment must write only where Arc says, never where the model says; and a
variable a bundle PLACES must come from the connection or not at all — never
inherited from the service environment, where it would silently pick another
account. Each is asserted against a real child process.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

from arcagent.extension.attachment import ToolOutcome
from arcagent.extension.cli_attachment import CliAttachment, CliCommand
from arcagent.extension.secrets import Secret

#: Echoes its argv and one environment variable as JSON; writes a file when asked.
_SCRIPT = """
import json, os, sys
args = sys.argv[1:]
out = [a.split("=", 1)[1] for a in args if a.startswith("--out=")]
size = [int(a.split("=", 1)[1]) for a in args if a.startswith("--size=")]
if out:
    with open(out[0], "wb") as handle:
        handle.write(b"x" * (size[0] if size else 10))
big = [int(a.split("=", 1)[1]) for a in args if a.startswith("--big=")]
print(json.dumps({"argv": args, "account": os.environ.get("SVC_ACCOUNT"), "pad": "y" * (big[0] if big else 0)}))
"""


@pytest.fixture
def script(tmp_path: Path) -> Path:
    path = tmp_path / "svc.py"
    path.write_text(_SCRIPT, encoding="utf-8")
    return path


def _attachment(script: Path, command: dict[str, object], **kwargs: object) -> CliAttachment:
    return CliAttachment(
        binary=sys.executable,
        commands=[CliCommand.model_validate({"tool": "svc", **command})],
        **kwargs,  # type: ignore[arg-type]
    )


def _argv(content: str) -> list[str]:
    return list(json.loads(content)["argv"])


async def test_a_numeric_argument_above_its_ceiling_is_refused_unrun(script: Path) -> None:
    attachment = _attachment(
        script,
        {"argv": [str(script)], "arguments": [{"name": "limit", "flag": "--max", "maximum": 100}]},
    )
    ok = await attachment.invoke("svc", {"limit": "20"})
    assert "--max=20" in _argv(ok.content)
    for bad in ("101", "0", "-5", "abc", "1e3", " 7"):
        result = await attachment.invoke("svc", {"limit": bad})
        assert result.outcome is ToolOutcome.ERROR
        assert "limit" in result.content


async def test_output_above_its_ceiling_is_an_error_not_a_flood(script: Path) -> None:
    attachment = _attachment(
        script,
        {
            "argv": [str(script)],
            "arguments": [{"name": "big", "flag": "--big"}],
            "max_output_bytes": 1000,
        },
    )
    assert (await attachment.invoke("svc", {"big": "10"})).outcome is ToolOutcome.OK
    result = await attachment.invoke("svc", {"big": "5000"})
    assert result.outcome is ToolOutcome.ERROR
    assert "too large" in result.content


def _download(script: Path, tmp_path: Path, max_bytes: int = 100) -> CliAttachment:
    return _attachment(
        script,
        {
            "argv": [str(script)],
            "arguments": [
                {"name": "size", "flag": "--size"},
                {"name": "file_name", "description": "Name to save as."},
            ],
            "download": {"argument": "file_name", "flag": "--out", "max_bytes": max_bytes},
        },
        download_dir=tmp_path / "downloads",
    )


async def test_a_download_lands_in_the_download_directory_under_a_plain_name(
    script: Path, tmp_path: Path
) -> None:
    result = await _download(script, tmp_path).invoke("svc", {"file_name": "report.pdf"})
    assert result.outcome is ToolOutcome.OK, result.content
    target = tmp_path / "downloads" / "report.pdf"
    assert target.is_file()
    assert f"--out={target}" in _argv(result.content)
    # The model-chosen name never reaches argv as its own token.
    assert "report.pdf" not in _argv(result.content)


@pytest.mark.parametrize(
    "name", ["../escape.pdf", "a/b.pdf", "/etc/passwd", ".hidden", "", "x\x00y", "con:t", "-rf"]
)
async def test_a_name_that_is_not_a_plain_file_name_is_refused(
    script: Path, tmp_path: Path, name: str
) -> None:
    result = await _download(script, tmp_path).invoke("svc", {"file_name": name})
    assert result.outcome is ToolOutcome.ERROR
    assert not (tmp_path / "escape.pdf").exists()


async def test_an_oversized_download_is_removed(script: Path, tmp_path: Path) -> None:
    result = await _download(script, tmp_path, max_bytes=50).invoke(
        "svc", {"file_name": "big.bin", "size": "500"}
    )
    assert result.outcome is ToolOutcome.ERROR
    assert "larger than" in result.content
    assert not (tmp_path / "downloads" / "big.bin").exists()


async def test_a_symlink_where_the_file_would_go_is_refused(script: Path, tmp_path: Path) -> None:
    downloads = tmp_path / "downloads"
    downloads.mkdir()
    (downloads / "trap.pdf").symlink_to(tmp_path / "outside.txt")
    result = await _download(script, tmp_path).invoke("svc", {"file_name": "trap.pdf"})
    assert result.outcome is ToolOutcome.ERROR
    assert not (tmp_path / "outside.txt").exists()


async def test_a_download_command_without_a_directory_refuses(script: Path) -> None:
    attachment = _attachment(
        script,
        {
            "argv": [str(script)],
            "arguments": [{"name": "file_name"}],
            "download": {"argument": "file_name", "flag": "--out", "max_bytes": 10},
        },
    )
    result = await attachment.invoke("svc", {"file_name": "a.pdf"})
    assert result.outcome is ToolOutcome.ERROR
    assert "no download" in result.content


async def test_an_owned_variable_is_never_inherited(
    script: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SVC_ACCOUNT", "someone-else@example.com")
    unplaced = _attachment(script, {"argv": [str(script)]}, owned_env=frozenset({"SVC_ACCOUNT"}))
    assert json.loads((await unplaced.invoke("svc", {})).content)["account"] is None
    placed = _attachment(
        script,
        {"argv": [str(script)]},
        env={"SVC_ACCOUNT": Secret("mine@example.com")},
        owned_env=frozenset({"SVC_ACCOUNT"}),
        visible_env=frozenset({"SVC_ACCOUNT"}),
    )
    assert json.loads((await placed.invoke("svc", {})).content)["account"] == "mine@example.com"
    assert os.environ["SVC_ACCOUNT"] == "someone-else@example.com"
