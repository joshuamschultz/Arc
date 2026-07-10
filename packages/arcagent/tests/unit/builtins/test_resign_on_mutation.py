"""Task #28 — a mutated signed artifact must not carry a stale signature.

Root-cause investigation: create_skill/create_tool/update_skill/update_tool
ALL already call ``_runtime.sign_artifact_file`` after writing (SPEC-033,
commit 2104079) — so update_skill/update_tool were not, in fact, missing a
re-sign call. The actual gap is the GENERIC ``write``/``edit`` tools: they
know nothing about the Sign pillar, so an agent that mutates an
already-signed capability file (e.g. hand-editing ``SKILL.md`` with the
plain ``edit`` tool instead of calling ``update_skill``) silently leaves a
stale ``.arcsig`` sidecar on disk — bytes changed, signature didn't. The
next load-time verify (``artifact_signing.verify_file``) then fails closed,
exactly matching the reported symptom: "browserbase SKILL.md's
artifact_sha256 no longer matches content."

These tests reproduce the gap via the incident's actual mutation path
(generic ``edit``/``write``, not ``update_skill``) and prove the fix:
any tool that mutates a file which already carries a ``.arcsig`` sidecar
must refresh it. Files that were never signed stay unsigned — write/edit
must not start spamming ``.arcsig`` sidecars onto ordinary workspace files.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from arctrust.identity import AgentIdentity

from arcagent.builtins.capabilities import _runtime
from arcagent.capabilities import artifact_signing


@pytest.fixture(autouse=True)
def _reset_runtime() -> None:
    _runtime.reset()


@pytest.fixture
def identity() -> AgentIdentity:
    return AgentIdentity.generate(org="blackarc", agent_type="executor")


@pytest.fixture
def workspace(tmp_path: Path, identity: AgentIdentity) -> Path:
    ws = tmp_path / "workspace"
    ws.mkdir()
    (ws / "capabilities").mkdir()
    _runtime.configure(workspace=ws, identity=identity)
    return ws


@pytest.mark.asyncio
class TestGenericEditInvalidatesSignature:
    """Reproduces the incident: edit()/write() bypass the Sign pillar."""

    async def test_edit_on_signed_skill_leaves_stale_sidecar_before_fix_lens(
        self, workspace: Path, identity: AgentIdentity
    ) -> None:
        """Documents the bug shape directly against artifact_signing (no tool
        dependency) so the assertion is unambiguous about what "stale" means.
        """
        skill_md = workspace / "capabilities" / "skills" / "browserbase" / "SKILL.md"
        skill_md.parent.mkdir(parents=True)
        original = "---\nname: browserbase\nversion: 1.0.0\n---\n\nbody v1\n"
        skill_md.write_text(original, encoding="utf-8")
        artifact_signing.write_signature(
            skill_md,
            original.encode("utf-8"),
            signer_did=identity.did,
            private_key=identity.signing_seed,
        )
        assert artifact_signing.verify_file(skill_md, original.encode("utf-8")) is True

        # Simulate a plain edit — bytes on disk change, sidecar does not.
        mutated = original.replace("body v1", "body v2 (edited by hand)")
        skill_md.write_text(mutated, encoding="utf-8")
        assert artifact_signing.verify_file(skill_md, mutated.encode("utf-8")) is False

    async def test_edit_tool_resigns_previously_signed_skill(
        self, workspace: Path, identity: AgentIdentity
    ) -> None:
        from arcagent.builtins.capabilities.edit import edit

        skill_md = workspace / "capabilities" / "skills" / "browserbase" / "SKILL.md"
        skill_md.parent.mkdir(parents=True)
        original = "---\nname: browserbase\nversion: 1.0.0\n---\n\nbody v1\n"
        skill_md.write_text(original, encoding="utf-8")
        _runtime.sign_artifact_file(skill_md, original.encode("utf-8"))
        assert artifact_signing.verify_file(skill_md, original.encode("utf-8")) is True

        await edit(
            file_path="capabilities/skills/browserbase/SKILL.md",
            old_string="body v1",
            new_string="body v2 (edited via generic tool)",
        )

        current = skill_md.read_text(encoding="utf-8").encode("utf-8")
        assert artifact_signing.verify_file(skill_md, current) is True

    async def test_write_tool_resigns_previously_signed_capability(
        self, workspace: Path, identity: AgentIdentity
    ) -> None:
        from arcagent.builtins.capabilities.write import write

        target = workspace / "capabilities" / "hello.py"
        original = "async def fn(): return 1\n"
        target.write_text(original, encoding="utf-8")
        _runtime.sign_artifact_file(target, original.encode("utf-8"))
        assert artifact_signing.verify_file(target, original.encode("utf-8")) is True

        await write(file_path="capabilities/hello.py", content="async def fn(): return 2\n")

        current = target.read_bytes()
        assert artifact_signing.verify_file(target, current) is True

    async def test_write_never_signs_a_previously_unsigned_file(self, workspace: Path) -> None:
        """write/edit must NOT start signing ordinary workspace files."""
        from arcagent.builtins.capabilities.write import write

        await write(file_path="notes.txt", content="just some notes")
        sidecar = artifact_signing.sidecar_path(workspace / "notes.txt")
        assert not sidecar.exists()

    async def test_edit_never_signs_a_previously_unsigned_file(self, workspace: Path) -> None:
        from arcagent.builtins.capabilities.edit import edit

        (workspace / "notes.txt").write_text("foo bar")
        await edit(file_path="notes.txt", old_string="foo", new_string="baz")
        sidecar = artifact_signing.sidecar_path(workspace / "notes.txt")
        assert not sidecar.exists()


@pytest.mark.asyncio
class TestSelfModToolsAlreadyResign:
    """Regression guard: create_skill/create_tool/update_skill/update_tool
    already re-sign on every mutation (SPEC-033, commit 2104079) — this was
    NOT the actual bug, and must stay correct.
    """

    async def test_update_skill_resigns(self, workspace: Path) -> None:
        from arcagent.builtins.capabilities.create_skill import create_skill
        from arcagent.builtins.capabilities.update_skill import update_skill

        await create_skill(name="s", description="x", triggers=["a"], tools=["read"])
        skill_md = workspace / "capabilities" / "skills" / "s" / "SKILL.md"
        assert artifact_signing.verify_file(skill_md, skill_md.read_bytes()) is True

        await update_skill(name="s", new_body="updated body", version_bump="patch")
        assert artifact_signing.verify_file(skill_md, skill_md.read_bytes()) is True

    async def test_update_tool_resigns(self, workspace: Path) -> None:
        from arcagent.builtins.capabilities.create_tool import create_tool
        from arcagent.builtins.capabilities.update_tool import update_tool

        original = (
            "from arcagent.tools._decorator import tool\n"
            "@tool(description='x', version=\"1.0.0\")\n"
            "async def fn() -> str:\n    return 'a'\n"
        )
        await create_tool(name="fn", source=original)
        target = workspace / "capabilities" / "fn.py"
        assert artifact_signing.verify_file(target, target.read_bytes()) is True

        new_source = original.replace("1.0.0", "1.0.1").replace("'a'", "'b'")
        await update_tool(name="fn", new_source=new_source, version_bump="patch")
        assert artifact_signing.verify_file(target, target.read_bytes()) is True


@pytest.mark.asyncio
class TestFailHonestOnSigningFailure:
    """Doctrine: 'if signing fails, the tool result must say the artifact was
    written but is UNSIGNED and will not load, and audit it — never silently
    succeed.' Covers both the no-signing-identity case and an actual
    exception raised by the underlying crypto call.
    """

    async def test_create_skill_reports_unsigned_when_no_identity(self, tmp_path: Path) -> None:
        from arcagent.builtins.capabilities.create_skill import create_skill

        ws = tmp_path / "workspace"
        ws.mkdir()
        (ws / "capabilities").mkdir()
        events: list[tuple[str, dict[str, object]]] = []
        _runtime.configure(workspace=ws, audit_sink=lambda et, d: events.append((et, d)))

        result = await create_skill(name="s", description="x", triggers=["a"], tools=["read"])

        assert "Created skill" in result
        assert "UNSIGNED" in result
        assert events
        assert events[-1][0] == "tool.artifact_unsigned"
        assert events[-1][1]["tool"] == "create_skill"

    async def test_create_tool_reports_unsigned_when_no_identity(self, tmp_path: Path) -> None:
        from arcagent.builtins.capabilities.create_tool import create_tool

        ws = tmp_path / "workspace"
        ws.mkdir()
        (ws / "capabilities").mkdir()
        events: list[tuple[str, dict[str, object]]] = []
        _runtime.configure(workspace=ws, audit_sink=lambda et, d: events.append((et, d)))

        result = await create_tool(name="hello", source="async def fn(): return 1\n")

        assert "Created tool" in result
        assert "UNSIGNED" in result
        assert events and events[-1][0] == "tool.artifact_unsigned"

    async def test_update_skill_reports_unsigned_when_identity_lost(
        self, workspace: Path, identity: AgentIdentity
    ) -> None:
        from arcagent.builtins.capabilities.create_skill import create_skill
        from arcagent.builtins.capabilities.update_skill import update_skill

        await create_skill(name="s", description="x", triggers=["a"], tools=["read"])

        # Identity no longer configured for signing at update time.
        events: list[tuple[str, dict[str, object]]] = []
        _runtime.configure(workspace=workspace, audit_sink=lambda et, d: events.append((et, d)))

        result = await update_skill(name="s", new_body="new body", version_bump="patch")

        assert "Updated skill" in result
        assert "UNSIGNED" in result
        assert events and events[-1][0] == "tool.artifact_unsigned"

    async def test_update_tool_reports_unsigned_when_signing_raises(
        self, workspace: Path, identity: AgentIdentity, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from arcagent.builtins.capabilities.create_tool import create_tool
        from arcagent.builtins.capabilities.update_tool import update_tool

        original = (
            "from arcagent.tools._decorator import tool\n"
            "@tool(description='x', version=\"1.0.0\")\n"
            "async def fn() -> str:\n    return 'a'\n"
        )
        await create_tool(name="fn", source=original)

        events: list[tuple[str, dict[str, object]]] = []
        _runtime.configure(
            workspace=workspace,
            identity=identity,
            audit_sink=lambda et, d: events.append((et, d)),
        )

        def _boom(*args: object, **kwargs: object) -> None:
            raise RuntimeError("HSM unreachable")

        monkeypatch.setattr(artifact_signing, "write_signature", _boom)

        new_source = original.replace("1.0.0", "1.0.1").replace("'a'", "'b'")
        result = await update_tool(name="fn", new_source=new_source, version_bump="patch")

        # The write must still succeed — signing failure is reported, not fatal.
        assert "Updated tool" in result
        assert "UNSIGNED" in result
        target = workspace / "capabilities" / "fn.py"
        assert "'b'" in target.read_text()
        assert events and events[-1][0] == "tool.artifact_unsigned"

    async def test_write_reports_unsigned_when_resign_fails(
        self, workspace: Path, identity: AgentIdentity
    ) -> None:
        from arcagent.builtins.capabilities.write import write

        target = workspace / "capabilities" / "hello.py"
        original = "async def fn(): return 1\n"
        target.write_text(original, encoding="utf-8")
        _runtime.sign_artifact_file(target, original.encode("utf-8"))

        events: list[tuple[str, dict[str, object]]] = []
        _runtime.configure(workspace=workspace, audit_sink=lambda et, d: events.append((et, d)))

        result = await write(
            file_path="capabilities/hello.py", content="async def fn(): return 2\n"
        )

        assert "Written" in result
        assert "UNSIGNED" in result
        assert events and events[-1][0] == "tool.artifact_unsigned"

    async def test_edit_reports_unsigned_when_resign_fails(
        self, workspace: Path, identity: AgentIdentity
    ) -> None:
        from arcagent.builtins.capabilities.edit import edit

        target = workspace / "capabilities" / "hello.py"
        original = "async def fn(): return 1\n"
        target.write_text(original, encoding="utf-8")
        _runtime.sign_artifact_file(target, original.encode("utf-8"))

        events: list[tuple[str, dict[str, object]]] = []
        _runtime.configure(workspace=workspace, audit_sink=lambda et, d: events.append((et, d)))

        result = await edit(
            file_path="capabilities/hello.py", old_string="return 1", new_string="return 2"
        )

        assert "Replaced" in result
        assert "UNSIGNED" in result
        assert events and events[-1][0] == "tool.artifact_unsigned"

    async def test_successful_signing_never_appends_warning(
        self, workspace: Path, identity: AgentIdentity
    ) -> None:
        """Regression guard against a warning-on-every-call false positive."""
        from arcagent.builtins.capabilities.create_tool import create_tool

        result = await create_tool(name="hello", source="async def fn(): return 1\n")
        assert "UNSIGNED" not in result
