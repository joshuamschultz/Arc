"""A zero-config install gets a real schedule authority; federal fails closed.

Before this, nothing in production constructed a ``ControlArtifactAuthority``, so
``schedule_create`` answered "signed schedule registration unavailable" on every
real deployment. These tests build the agent the way ``arc agent run/serve/chat``
does and use the scheduler tool itself, with no test double anywhere.
"""

from __future__ import annotations

import json
from pathlib import Path

import arcagent
import pytest
from arctrust import LocalControlArtifactAuthority, arc_config

from arccli.commands import _serve
from arccli.commands.agent import _common
from arccli.commands.agent.create import _mint_agent_identity
from arccli.commands.operator import ensure_operator_key


def _agent_config(agent_dir: Path) -> str:
    return (
        "[agent]\n"
        "name = 'ada'\n"
        "org = 'cli'\n"
        "type = 'executor'\n"
        f"workspace = '{agent_dir / 'workspace'}'\n\n"
        "[identity]\n"
        'did = ""\n'
        f"key_dir = '{agent_dir / 'keys'}'\n\n"
        "[telemetry]\n"
        "enabled = false\n\n"
        "[security]\n"
        "tier = 'personal'\n\n"
        "[modules.scheduler]\n"
        "enabled = true\n"
    )


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    arc_home = tmp_path / "arc-home"
    arc_home.mkdir()
    monkeypatch.setenv("ARC_CONFIG_DIR", str(arc_home))
    monkeypatch.setenv("ARCSTORE_DATA_DIR", str(tmp_path / "store"))
    monkeypatch.delenv("ARC_TEAM_ROOT", raising=False)
    ensure_operator_key(arc_home)
    return arc_home


@pytest.fixture
def agent_dir(home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    from arccli.commands.up import agent_states, bootstrap_modules

    directory = tmp_path / "team" / "ada"
    (directory / "workspace").mkdir(parents=True)
    (directory / "arcagent.toml").write_text(_agent_config(directory), encoding="utf-8")
    _mint_agent_identity(directory)
    monkeypatch.setenv("ARC_MODULE_SOURCE", str(Path(arcagent.__file__).parent / "modules"))
    rows = bootstrap_modules(agent_states(tmp_path / "team"))
    assert not [row for row in rows if "REFUSED" in row or "UNREADABLE" in row], rows
    return directory


def test_personal_install_builds_a_local_authority(home: Path) -> None:
    binding = _serve.build_control_artifact_authority()
    assert binding is not None
    assert isinstance(binding.authority, LocalControlArtifactAuthority)
    assert binding.tenant_id
    # Stable for the deployment: a restart binds the same tenant.
    again = _serve.build_control_artifact_authority()
    assert again is not None and again.tenant_id == binding.tenant_id


def test_federal_install_has_no_local_authority(home: Path) -> None:
    arc_config().mkdir(parents=True, exist_ok=True)
    (arc_config() / "arcagent.toml").write_text("[security]\ntier = 'federal'\n", encoding="utf-8")
    assert _serve.build_control_artifact_authority() is None


async def test_personal_install_has_authority_and_schedule_create_succeeds(
    agent_dir: Path,
) -> None:
    from arcagent.modules.scheduler.capabilities import schedule_create

    agent, _config, _path = _common.load_cli_agent(agent_dir)
    await agent.startup()
    try:
        created = json.loads(
            await schedule_create(
                type="cron", expression="0 9 * * *", prompt="Send the morning briefing"
            )
        )
    finally:
        await agent.shutdown()

    assert "error" not in created, created
    approval = created["approval"]
    assert approval["revision"] == 1
    assert approval["actor_did"] == agent.did
    assert approval["agent_did"] == agent.did
    stored = json.loads((agent_dir / "workspace" / "schedules.json").read_text("utf-8"))
    assert [row["approval"]["definition_digest"] for row in stored] == [
        approval["definition_digest"]
    ]


async def test_schedule_cancel_delete_revokes_head(agent_dir: Path) -> None:
    """Deleting a schedule revokes its signed head: the old approval never verifies again."""
    from arcagent.modules.scheduler import _runtime
    from arcagent.modules.scheduler.capabilities import schedule_cancel, schedule_create
    from arcagent.modules.scheduler.occurrence import canonical_definition

    agent, _config, _path = _common.load_cli_agent(agent_dir)
    await agent.startup()
    try:
        created = json.loads(
            await schedule_create(
                type="cron", expression="0 9 * * *", prompt="Send the morning briefing"
            )
        )
        entry = _runtime.state().store.get(created["id"])
        assert entry is not None and entry.approval is not None
        deleted = json.loads(await schedule_cancel(id=created["id"], delete=True))
        assert deleted["status"] == "deleted", deleted
        assert _runtime.state().store.get(created["id"]) is None
    finally:
        await agent.shutdown()

    binding = _serve.build_control_artifact_authority()
    assert binding is not None
    # The revoked head refuses the old approval in a fresh process.
    with pytest.raises(arcagent.ControlArtifactRefusedError):
        await binding.authority.verify_current(
            tenant_id=binding.tenant_id,
            agent_did=agent.did,
            purpose="schedule",
            artifact_id=entry.id,
            canonical_definition=canonical_definition(entry),
            approval=entry.approval,
            occurrence_id="after-delete",
        )


async def test_main_module_agent_has_authority(agent_dir: Path) -> None:
    """`python -m arcagent` builds its agent with the authority: schedule_create succeeds."""
    from arcagent.__main__ import _build_agent
    from arcagent.modules.scheduler.capabilities import schedule_create

    toml = agent_dir / "arcagent.toml"
    toml.write_text(
        toml.read_text(encoding="utf-8") + '\n[llm]\nmodel = "openai/gpt-4o-mini"\n',
        encoding="utf-8",
    )
    agent = _build_agent(agent_dir)
    await agent.startup()
    try:
        created = json.loads(
            await schedule_create(
                type="cron", expression="0 9 * * *", prompt="Send the morning briefing"
            )
        )
    finally:
        await agent.shutdown()

    assert "error" not in created, created
    assert created["approval"]["agent_did"] == agent.did
