"""ArcUI CLI — ``arc ui`` command group.

``arc ui start`` launches the standalone ArcUI dashboard server.
Agents connect via WebSocket using the shared agent token.
Token is persisted to ``~/.arcagent/ui-token`` so agents auto-discover it.
"""

from __future__ import annotations

from pathlib import Path

import click

_TOKEN_FILE = Path.home() / ".arcagent" / "ui-token"


def _mask_token(token: str) -> str:
    """Mask a token for display: show first 8 and last 8 chars."""
    if len(token) <= 16:
        return "****"
    return f"{token[:8]}...{token[-8:]}"


def _persist_agent_token(token: str) -> None:
    """Write agent token to well-known file for auto-discovery by agents."""
    _TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    _TOKEN_FILE.write_text(token)
    _TOKEN_FILE.chmod(0o600)


@click.group("ui")
def ui() -> None:
    """ArcUI dashboard server."""


@ui.command("start")
@click.option("--port", default=8420, type=int, show_default=True, help="Server port.")
@click.option("--host", default="127.0.0.1", show_default=True, help="Bind address.")
@click.option("--viewer-token", default=None, help="Viewer token (auto-generated if omitted).")
@click.option("--operator-token", default=None, help="Operator token (auto-generated if omitted).")
@click.option("--agent-token", default=None, help="Agent token (auto-generated if omitted).")
@click.option(
    "--max-agents", default=100, type=int, show_default=True, help="Max concurrent agents.",
)
@click.option("--show-tokens", is_flag=True, default=False, help="Show full tokens in output.")
@click.option(
    "--traces-dir",
    default=None,
    type=click.Path(exists=False),
    help="Directory containing agent trace JSONL files for historical data.",
)
def ui_start(
    port: int,
    host: str,
    viewer_token: str | None,
    operator_token: str | None,
    agent_token: str | None,
    max_agents: int,
    show_tokens: bool,
    traces_dir: str | None,
) -> None:
    """Start the ArcUI dashboard server.

    Agents connect via WebSocket at /api/agent/connect using the shared
    agent token (auto-persisted to ~/.arcagent/ui-token).
    Browsers connect at /ws using the viewer or operator token.

    Pass --traces-dir to load historical LLM traces from agent workspace
    trace files (e.g., --traces-dir ./team/my_agent/workspace).

    \b
    Examples:
      arc ui start
      arc ui start --port 9000
      arc ui start --traces-dir ./team/my_agent/workspace
      arc ui start --show-tokens
    """
    from arcui import create_app
    from arcui.auth import AuthConfig

    config_dict: dict[str, str] = {}
    if viewer_token:
        config_dict["viewer_token"] = viewer_token
    if operator_token:
        config_dict["operator_token"] = operator_token
    if agent_token:
        config_dict["agent_token"] = agent_token

    auth = AuthConfig(config=config_dict) if config_dict else AuthConfig()

    # Persist agent token for auto-discovery by agents
    _persist_agent_token(auth.agent_token)

    # Set up trace store for historical data
    trace_store = None
    if traces_dir:
        traces_path = Path(traces_dir).resolve()
        if traces_path.exists():
            try:
                from arcllm.trace_store import JSONLTraceStore

                trace_store = JSONLTraceStore(traces_path)
                click.echo(f"  Trace store: {traces_path / 'traces'}")
            except ImportError:
                click.echo("  Warning: arcllm not installed, trace store disabled")
        else:
            click.echo(f"  Warning: traces-dir not found: {traces_path}")

    app = create_app(
        auth_config=auth,
        max_agents=max_agents,
        trace_store=trace_store,
    )

    fmt = str if show_tokens else _mask_token
    click.echo(f"ArcUI dashboard: http://{host}:{port}")
    click.echo(f"  Viewer token:   {fmt(app.state.auth_config.viewer_token)}")
    click.echo(f"  Operator token: {fmt(app.state.auth_config.operator_token)}")
    click.echo(f"  Agent token:    {fmt(app.state.auth_config.agent_token)}")
    click.echo(f"  Token file:     {_TOKEN_FILE}")
    click.echo(f"  Max agents:     {max_agents}")

    import uvicorn

    # Warm-start aggregator from historical traces
    if trace_store is not None:
        import asyncio

        asyncio.run(app.state.aggregator.warm_start(trace_store))

    uvicorn.run(app, host=host, port=port, log_level="info")
