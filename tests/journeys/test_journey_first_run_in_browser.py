"""Journey: a fresh install, run entirely from the browser (J1-7 + J1-8).

Fresh install -> the sign-in screen offers "create the first operator account"
-> the operator types the one-time setup code from the server log -> signs in ->
creates an agent on the Fleet page -> the agent appears in the roster -> it
starts and answers. No step uses a terminal, and nothing the browser is shown
names a command.

Real: the arcui app and its auth middleware, the account store's hashing and
roles, the operator signer, ``arcagent.scaffold.create_agent``, the roster, and
``ArcAgent`` startup with the signed identity check. Scripted: the LLM wire only.
"""

from __future__ import annotations

import logging
import re
from typing import Any

import pytest
from starlette.testclient import TestClient

from packages.arcui.tests.user_authority import user_store_factory

from .conftest import Deployment, ScriptedLLM

_CODE = re.compile(r"ARC FIRST-RUN SETUP CODE: ([A-Z0-9-]+)")
_PASSWORD = "first-operator-password"


@pytest.fixture
def browser(deployment: Deployment) -> TestClient:
    """The dashboard a customer reaches on a fresh install: no accounts yet."""
    import arctrust
    from arcstore.backends.memory import FakeBackend
    from arctrust.operator import OperatorKey
    from arcui.auth import AuthConfig
    from arcui.server import create_app

    app = create_app(
        auth_config=AuthConfig(),
        user_store_factory=user_store_factory(deployment.home.parent / "accounts"),
        team_root=deployment.team_root,
        operator_signer_factory=lambda: OperatorKey.load(
            arctrust.default_operator_key_path(), generate_if_absent=False
        ).into_signer(),
        arcstore_backend=FakeBackend(),
    )
    return TestClient(app)


def _no_command(text: str) -> None:
    for command in ("arc user", "arc team", "arc agent", "arc ui"):
        assert command not in text, f"the browser was shown a command: {command!r}"


async def test_fresh_install_to_a_running_agent_without_a_terminal(
    browser: TestClient,
    deployment: Deployment,
    scripted_llm: ScriptedLLM,
    caplog: pytest.LogCaptureFixture,
) -> None:
    # 1. The sign-in screen asks the server what to offer.
    with caplog.at_level(logging.WARNING):
        mode = browser.get("/api/auth/mode")
    assert mode.json() == {"login_available": False, "setup_available": True}
    _no_command(mode.text)
    code = _CODE.findall(caplog.text)[-1]

    # 2. Create the first operator with the code from the server log.
    setup = browser.post(
        "/api/auth/setup",
        json={"setup_code": code, "email": "owner@example.com", "password": _PASSWORD},
    )
    assert setup.status_code == 200, setup.text
    session = {"Authorization": f"Bearer {setup.json()['token']}"}
    assert browser.get("/api/auth/me", headers=session).json()["role"] == "operator"

    # 3. Create an agent from the Fleet page.
    created = browser.post(
        "/api/agents", json={"name": "scout", "model": "scripted/model"}, headers=session
    )
    assert created.status_code == 201, created.text
    _no_command(created.text)

    # 4. It appears.
    roster = browser.get("/api/team/roster", headers=session).json()["agents"]
    assert "scout" in [a["agent_id"] for a in roster]

    # 5. It runs: the gateway builds it from the files the browser just made, and
    #    its operator-signed identity passes the run-start check.
    agent = await _start(deployment.team_root / "scout")
    try:
        scripted_llm.replies.append("Scout reporting in.")
        assert "Scout reporting in." in await _say(agent, "Are you there?")
    finally:
        await agent.shutdown()


async def _start(agent_dir: Any) -> Any:
    import arcagent

    config_path = agent_dir / "arcagent.toml"
    agent = arcagent.ArcAgent(arcagent.load_config(config_path), config_path=config_path)
    await agent.startup()
    return agent


async def _say(agent: Any, text: str) -> str:
    session = await agent.session("first-run")
    chunks: list[str] = []
    async for event in agent.run(text, session=session):
        piece = getattr(event, "text", None) or getattr(event, "content", None)
        if isinstance(piece, str):
            chunks.append(piece)
    return "".join(chunks)
