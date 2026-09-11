#!/usr/bin/env python3
"""Run the deterministic alpha transport validation matrix.

The matrix stays limited to local tests and static gates. Network-backed
dependency auditing remains an explicit opt-in because release CI supplies the
approved vulnerability database and policy configuration.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class Check:
    name: str
    command: tuple[str, ...]


CHECKS = (
    Check(
        "cli-streaming",
        (
            "-m",
            "pytest",
            "packages/arccli/tests/test_agent_streaming.py",
            "packages/arccli/tests/test_agent_run_serve_chat.py",
            "packages/arccli/tests/test_agent_run_session_id.py",
        ),
    ),
    Check(
        "gateway-streaming",
        (
            "-m",
            "pytest",
            "packages/arcgateway/tests/integration/test_stream_bridge_wiring.py",
            "packages/arcgateway/tests/integration/test_stream_flood_control.py",
            "packages/arcgateway/tests/platform/telegram/test_telegram_streaming.py",
            "packages/arcgateway/tests/platform/slack/test_dual_adapter_chat.py",
            "packages/arcgateway/tests/platform/mattermost/test_mattermost_adapter.py",
            "packages/arcgateway/tests/unit/test_in_process_adapter.py",
            "packages/arcgateway/tests/unit/test_web_adapter.py",
        ),
    ),
    Check(
        "tui-streaming",
        (
            "-m",
            "pytest",
            "packages/arctui/src/arctui/tests/smoke/test_arctui_streaming.py",
            "packages/arctui/src/arctui/tests/unit/test_transcript.py",
        ),
    ),
    Check(
        "provider-routing-streaming",
        (
            "-m",
            "pytest",
            "packages/arcllm/tests/test_routing_policy_alpha.py",
            "packages/arcllm/tests/test_stream_accumulator.py",
            "packages/arcllm/tests/test_types.py",
        ),
    ),
    Check(
        "transport-lint",
        (
            "-m",
            "ruff",
            "check",
            "packages/arccli/src/arccli/commands/agent/run.py",
            "packages/arccli/src/arccli/commands/agent/chat.py",
            "packages/arcgateway/src/arcgateway/stream_bridge.py",
            "packages/arcgateway/src/arcgateway/adapters/in_process.py",
            "packages/arcgateway/src/arcgateway/adapters/mattermost/adapter.py",
            "packages/arctui/src/arctui/app.py",
        ),
    ),
)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="append", choices=[item.name for item in CHECKS])
    parser.add_argument("--json", action="store_true", dest="as_json")
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    selected = tuple(item for item in CHECKS if not args.check or item.name in args.check)
    results: list[dict[str, object]] = []
    for check in selected:
        completed = subprocess.run(  # noqa: S603 — commands are immutable release-matrix entries
            (sys.executable, *check.command),
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
        )
        results.append(
            {
                **asdict(check),
                "command": [sys.executable, *check.command],
                "returncode": completed.returncode,
                "output": completed.stdout + completed.stderr,
            }
        )

    if args.as_json:
        print(json.dumps({"checks": results}, indent=2))
    else:
        for result in results:
            status = "PASS" if result["returncode"] == 0 else "FAIL"
            print(f"[{status}] {result['name']}")
            if result["returncode"] != 0:
                print(result["output"])
    return 0 if all(result["returncode"] == 0 for result in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
