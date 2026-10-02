"""nightly-meeting-ingest script nodes run deterministically, with no model and no network.

The scripts are driven exactly as the runner drives a script node
(``_run_script_node``): a subprocess whose inputs arrive in ``ARC_*``
environment variables and whose stdout is the node's JSON output.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tomllib
from pathlib import Path
from typing import Any

from arcteam.workflow.models import parse_definition
from arcteam.workflow.validator import validate_definition

BUNDLE = Path(__file__).resolve().parents[2] / "docs/runbooks/alpha-2/nightly-meeting-ingest"
EMPTY_DAY = BUNDLE / "scripts" / "no_new_meetings.py"


def test_no_new_meetings_script_runs_without_llm() -> None:
    done = subprocess.run(
        [sys.executable, str(EMPTY_DAY)], capture_output=True, text=True, timeout=30
    )
    assert done.returncode == 0
    assert json.loads(done.stdout) == {"status": "nothing_new"}


def test_archive_is_a_tool_node_on_the_dropbox_tool() -> None:
    node = next(n for n in _bundle_definition().nodes if n.id == "archive_transcripts")
    assert node.kind == "tool" and node.tool == "dropbox_archive_copy"
    assert node.args["files"] == "$nodes.filter_new.output.new_files"


def _bundle_definition() -> Any:
    with (BUNDLE / "workflow.toml").open("rb") as handle:
        return parse_definition(tomllib.load(handle))


def test_bundle_scripts_and_schema_pass_the_workflow_validator() -> None:
    issues = validate_definition(_bundle_definition(), bundle_root=BUNDLE)
    ours = [i for i in issues if i.field == "script" or i.node_id == "archive_transcripts"]
    assert ours == [], ours


def test_script_takes_no_network_or_shell() -> None:
    text = EMPTY_DAY.read_text(encoding="utf-8")
    for banned in ("import socket", "urllib", "requests", "httpx", "shell=True", "os.system"):
        assert banned not in text
