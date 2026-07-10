"""Live incident (task #21): a user pasted a Browserbase API token into chat
and the agent wrote it verbatim to ``workspace/secrets/browserbase.md``.
Doctrine (CLAUDE.md): credentials never touch the filesystem.

These tests prove every content-writing built-in tool (write, edit,
create_skill, update_skill, create_tool, update_tool) refuses a
secret-shaped payload, audits the denial, and leaves ordinary content
untouched. ``store_secret`` proves the honest alternative: it never
accepts or persists the secret value, only tells the caller where the
operator should place it.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from arcagent.builtins.capabilities import _runtime
from arcagent.core.errors import ToolError

_AWS_KEY = "AKIAIOSFODNN7EXAMPLE"
_BROWSERBASE_SHAPED = "browserbase_api_key: bb_live_9f8a7c6d5e4b3a2f1e0d9c8b"


@pytest.fixture(autouse=True)
def _reset_runtime() -> None:
    _runtime.reset()


@pytest.fixture
def audit_events() -> list[tuple[str, dict[str, Any]]]:
    return []


@pytest.fixture
def workspace(tmp_path: Path, audit_events: list[tuple[str, dict[str, Any]]]) -> Path:
    ws = tmp_path / "workspace"
    ws.mkdir()
    (ws / "capabilities").mkdir()

    def sink(event_type: str, details: dict[str, Any]) -> None:
        audit_events.append((event_type, details))

    _runtime.configure(workspace=ws, audit_sink=sink)
    return ws


@pytest.mark.asyncio
class TestWriteDeniesSecrets:
    async def test_aws_key_denied(
        self, workspace: Path, audit_events: list[tuple[str, Any]]
    ) -> None:
        from arcagent.builtins.capabilities.write import write

        with pytest.raises(ToolError) as exc_info:
            await write(file_path="secrets/aws.md", content=f"key = {_AWS_KEY}")
        assert exc_info.value.code == "TOOL_SECRET_WRITE_DENIED"
        assert not (workspace / "secrets" / "aws.md").exists()
        assert audit_events
        assert audit_events[-1][0] == "tool.secret_write.denied"

    async def test_browserbase_shaped_token_denied(self, workspace: Path) -> None:
        """The literal live incident: an unprefixed, keyword-labeled token."""
        from arcagent.builtins.capabilities.write import write

        with pytest.raises(ToolError) as exc_info:
            await write(
                file_path="secrets/browserbase.md",
                content=f"# Browserbase\n\n{_BROWSERBASE_SHAPED}\n",
            )
        assert exc_info.value.code == "TOOL_SECRET_WRITE_DENIED"
        assert not (workspace / "secrets" / "browserbase.md").exists()

    async def test_normal_write_unaffected(self, workspace: Path) -> None:
        from arcagent.builtins.capabilities.write import write

        result = await write(file_path="notes.txt", content="just some notes")
        assert "Written" in result
        assert (workspace / "notes.txt").read_text() == "just some notes"


@pytest.mark.asyncio
class TestEditDeniesSecrets:
    async def test_edit_denied_when_new_string_is_secret_shaped(self, workspace: Path) -> None:
        from arcagent.builtins.capabilities.edit import edit

        (workspace / "f.txt").write_text("placeholder")
        with pytest.raises(ToolError) as exc_info:
            await edit(file_path="f.txt", old_string="placeholder", new_string=_AWS_KEY)
        assert exc_info.value.code == "TOOL_SECRET_WRITE_DENIED"
        assert (workspace / "f.txt").read_text() == "placeholder"

    async def test_normal_edit_unaffected(self, workspace: Path) -> None:
        from arcagent.builtins.capabilities.edit import edit

        (workspace / "f.txt").write_text("foo bar")
        result = await edit(file_path="f.txt", old_string="bar", new_string="baz")
        assert "Replaced" in result


@pytest.mark.asyncio
class TestSelfModToolsDenySecrets:
    async def test_create_skill_denied(self, workspace: Path) -> None:
        from arcagent.builtins.capabilities.create_skill import create_skill

        with pytest.raises(ToolError) as exc_info:
            await create_skill(
                name="leaky",
                description="x",
                triggers=[],
                tools=[],
                body=f"remember this: {_AWS_KEY}",
            )
        assert exc_info.value.code == "TOOL_SECRET_WRITE_DENIED"
        assert not (workspace / "capabilities" / "skills" / "leaky").exists()

    async def test_update_skill_denied(self, workspace: Path) -> None:
        from arcagent.builtins.capabilities.update_skill import update_skill

        skill_md = workspace / "capabilities" / "skills" / "s" / "SKILL.md"
        skill_md.parent.mkdir(parents=True)
        skill_md.write_text(
            "---\nname: s\nversion: 1.0.0\ndescription: x\ntriggers: [a]\ntools: [read]\n---\n"
            "\noriginal body\n"
        )
        with pytest.raises(ToolError) as exc_info:
            await update_skill(name="s", new_body=_AWS_KEY, version_bump="patch")
        assert exc_info.value.code == "TOOL_SECRET_WRITE_DENIED"
        assert "original body" in skill_md.read_text()

    async def test_create_tool_denied(self, workspace: Path) -> None:
        from arcagent.builtins.capabilities.create_tool import create_tool

        source = f"# {_AWS_KEY}\nfrom arcagent.tools._decorator import tool\n"
        with pytest.raises(ToolError) as exc_info:
            await create_tool(name="leaky", source=source)
        assert exc_info.value.code == "TOOL_SECRET_WRITE_DENIED"
        assert not (workspace / "capabilities" / "leaky.py").exists()

    async def test_update_tool_denied(self, workspace: Path) -> None:
        from arcagent.builtins.capabilities.update_tool import update_tool

        original = (
            "from arcagent.tools._decorator import tool\n"
            "@tool(description='x', version=\"1.0.0\")\n"
            "async def fn() -> str:\n    return 'a'\n"
        )
        (workspace / "capabilities" / "fn.py").write_text(original)
        new_source = original.replace("1.0.0", "1.0.1") + f"# {_AWS_KEY}\n"
        with pytest.raises(ToolError) as exc_info:
            await update_tool(name="fn", new_source=new_source, version_bump="patch")
        assert exc_info.value.code == "TOOL_SECRET_WRITE_DENIED"
        assert (workspace / "capabilities" / "fn.py").read_text() == original


@pytest.mark.asyncio
class TestStoreSecret:
    async def test_never_accepts_a_value_argument(self) -> None:
        """The tool's signature itself must not offer a place to paste a secret."""
        import inspect

        from arcagent.builtins.capabilities.store_secret import store_secret

        params = inspect.signature(store_secret).parameters
        assert "value" not in params
        assert "secret" not in params

    async def test_personal_tier_points_at_operator_env_file(self, workspace: Path) -> None:
        from arcagent.builtins.capabilities.store_secret import store_secret

        result = await store_secret(name="browserbase-api-key")
        assert "BROWSERBASE_API_KEY" in result
        assert ".env" in result
        assert "operator" in result.lower()

    async def test_federal_tier_points_at_vault(self, tmp_path: Path) -> None:
        from arcagent.builtins.capabilities.store_secret import store_secret

        ws = tmp_path / "workspace"
        ws.mkdir()
        _runtime.configure(workspace=ws, tier="federal")
        result = await store_secret(name="browserbase-api-key")
        assert "vault" in result.lower()

    async def test_rejects_bad_name(self, workspace: Path) -> None:
        from arcagent.builtins.capabilities.store_secret import store_secret

        result = await store_secret(name="../../etc/passwd")
        assert "Error" in result

    async def test_never_writes_to_workspace(self, workspace: Path) -> None:
        from arcagent.builtins.capabilities.store_secret import store_secret

        await store_secret(name="browserbase-api-key")
        assert list(workspace.rglob("*")) == [workspace / "capabilities"]
