"""ArcUI CLI — ``arc ui`` command group.

``arc ui start`` launches the standalone ArcUI dashboard server.
Agents connect via WebSocket using the agent token.
"""

from __future__ import annotations

import click


def _mask_token(token: str) -> str:
    """Mask a token for display: show first 8 and last 8 chars."""
    if len(token) <= 16:
        return "****"
    return f"{token[:8]}...{token[-8:]}"


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
def ui_start(
    port: int,
    host: str,
    viewer_token: str | None,
    operator_token: str | None,
    agent_token: str | None,
    max_agents: int,
    show_tokens: bool,
) -> None:
    """Start the ArcUI dashboard server.

    Agents connect via WebSocket at /api/agent/connect using the agent token.
    Browsers connect at /ws using the viewer or operator token.

    \b
    Examples:
      arc ui start
      arc ui start --port 9000
      arc ui start --agent-token my-secret
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

    app = create_app(auth_config=auth, max_agents=max_agents)

    fmt = str if show_tokens else _mask_token
    click.echo(f"ArcUI dashboard: http://{host}:{port}")
    click.echo(f"  Viewer token:   {fmt(app.state.auth_config.viewer_token)}")
    click.echo(f"  Operator token: {fmt(app.state.auth_config.operator_token)}")
    click.echo(f"  Agent token:    {fmt(app.state.auth_config.agent_token)}")
    click.echo(f"  Max agents:     {max_agents}")

    import uvicorn

    app.state.event_buffer.start()
    uvicorn.run(app, host=host, port=port, log_level="info")
