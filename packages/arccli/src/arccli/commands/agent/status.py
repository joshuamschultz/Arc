"""`arc agent status` — summary of config, workspace, tools, capabilities, sessions."""

from __future__ import annotations

import argparse
from datetime import UTC, datetime

from arccli.commands.agent._common import (
    _discover_tools,
    _iter_capability_files,
    _iter_skill_folders,
    _load_agent_config,
    _print_kv,
    _resolve_agent_dir,
)


def _status(args: argparse.Namespace) -> None:
    """Show agent status: config, workspace, tools, capabilities, sessions."""
    agent_dir = _resolve_agent_dir(args.path)
    config = _load_agent_config(agent_dir)
    workspace = agent_dir / "workspace"

    agent_name = config.get("agent", {}).get("name", "?")
    model_id = config.get("llm", {}).get("model", "?")
    did = config.get("identity", {}).get("did", "(not set)")

    tool_count = len(_discover_tools(agent_dir))
    skill_count = len(_iter_skill_folders(agent_dir))
    cap_file_count = len(_iter_capability_files(agent_dir))

    sessions_dir = workspace / "sessions"
    session_count = 0
    latest_session = "none"
    if sessions_dir.is_dir():
        session_files = sorted(
            sessions_dir.glob("*.jsonl"),
            key=lambda f: f.stat().st_mtime,
            reverse=True,
        )
        session_count = len(session_files)
        if session_files:
            latest = session_files[0]
            mtime = datetime.fromtimestamp(latest.stat().st_mtime, tz=UTC)
            latest_session = f"{latest.stem} ({mtime.strftime('%Y-%m-%d %H:%M')})"

    _print_kv(
        [
            ("Name", agent_name),
            ("DID", did or "(not set)"),
            ("Model", model_id),
            ("Tools (./tools/)", str(tool_count)),
            ("Capability files", str(cap_file_count)),
            ("Skills", str(skill_count)),
            ("Sessions", str(session_count)),
            ("Latest session", latest_session),
            ("Path", str(agent_dir)),
        ]
    )
