"""Item 73: the Tools rows carry the manifest's ``idempotent`` flag."""

from __future__ import annotations

from pathlib import Path

from arcui.routes.agent_detail.tools import agent_tool_rows

_MANIFEST = """
[[tools.declared]]
name = "send_mail"
classification = "external_effect"
idempotent = false

[[tools.declared]]
name = "list_mail"
classification = "read_only"
"""


def test_tool_rows_carry_idempotent_flag(tmp_path: Path) -> None:
    ext = tmp_path / "extensions" / "mail"
    ext.mkdir(parents=True)
    (ext / "extension.toml").write_text(_MANIFEST, encoding="utf-8")

    rows, *_ = agent_tool_rows("sales", tmp_path, ["send_mail", "list_mail", "plain_tool"])
    by_name = {row["name"]: row for row in rows}

    assert by_name["send_mail"]["idempotent"] is False
    assert by_name["list_mail"]["idempotent"] is True
    assert by_name["plain_tool"]["idempotent"] is True
