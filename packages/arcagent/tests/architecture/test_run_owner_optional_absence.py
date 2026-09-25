"""The agent nucleus starts and runs with the optional run-intent module removed."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path


def test_standalone_agent_runs_without_run_intents_directory(tmp_path: Path) -> None:
    source = Path(__file__).resolve().parents[2] / "src" / "arcagent"
    isolated = tmp_path / "arcagent"
    shutil.copytree(source, isolated, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    shutil.rmtree(isolated / "modules" / "run_intents")
    script = """
import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch
import arcagent
import arcrun

async def fake_stream(*args, **kwargs):
    yield arcrun.TokenEvent(text='works')
    yield arcrun.TurnEndEvent(final_text='works')

async def main():
    workspace = Path.cwd() / 'workspace'
    workspace.mkdir()
    config = arcagent.ArcAgentConfig(
        agent={'name': 'standalone', 'org': 'testorg', 'type': 'executor', 'workspace': str(workspace)},
        llm={'model': 'test/model'},
        identity={'did': '', 'key_dir': str(Path.cwd() / 'keys'), 'vault_path': ''},
        telemetry={'enabled': False},
        context={'max_tokens': 10000},
    )
    agent = arcagent.ArcAgent(config=config)
    with patch('arcagent.core.model_manager.load_eval_model', return_value=MagicMock(close=AsyncMock())):
        with patch('arcagent.core.agent_dispatch.arcrun.run_stream', side_effect=fake_stream):
            await agent.startup()
            try:
                session = await agent.session('unit:absence')
                events = [event async for event in agent.run('hello', session=session)]
                assert isinstance(events[-1], arcrun.TurnEndEvent)
            finally:
                await agent.shutdown()

asyncio.run(main())
"""
    env = dict(os.environ)
    env["PYTHONPATH"] = str(tmp_path) + os.pathsep + env.get("PYTHONPATH", "")
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr
