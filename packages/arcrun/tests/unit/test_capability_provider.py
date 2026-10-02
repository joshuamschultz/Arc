"""Phase C (SPEC-027 / ADR-023) — arcrun.run takes a CapabilityProvider.

The loop advertises the provider's lean specs to the model and routes every
tool call through ``provider.invoke`` (carrying caller_did). A skill body is
pulled only when the model calls the built-in ``use_skill`` meta-tool, which
routes to ``provider.load``.
"""

from __future__ import annotations

from typing import Any

import pytest

from arcrun import (
    CapabilityProvider,
    CapabilityResult,
    CapabilitySpec,
    SkillDocument,
    StaticProvider,
    Tool,
    ToolContext,
    provider_tools,
)


class _FakeProvider:
    """In-memory provider: one tool, one lazy skill. Records invoke/load calls."""

    def __init__(self) -> None:
        self.invoked: list[tuple[str, dict[str, Any], str]] = []
        self.loaded: list[tuple[str, str]] = []
        self.contexts: list[ToolContext | None] = []

    def advertise(self) -> list[CapabilitySpec]:
        return [
            CapabilitySpec(
                name="echo",
                description="Echo the text back.",
                input_schema={
                    "type": "object",
                    "properties": {"text": {"type": "string"}},
                    "required": ["text"],
                },
                kind="tool",
            ),
            CapabilitySpec(
                name="deploy_runbook",
                description="Use when deploying to production.",
                input_schema={"type": "object", "properties": {}},
                kind="skill",
            ),
        ]

    async def load(self, name: str, *, caller_did: str) -> SkillDocument | None:
        self.loaded.append((name, caller_did))
        if name == "deploy_runbook":
            return SkillDocument(
                name=name,
                body="STEP 1: drain traffic. STEP 2: ship. STEP 3: verify.",
                files=("references/rollback.md", "scripts/drain.py"),
            )
        return None

    async def invoke(
        self,
        name: str,
        args: dict[str, Any],
        *,
        caller_did: str,
        context: ToolContext | None = None,
    ) -> CapabilityResult:
        self.invoked.append((name, args, caller_did))
        self.contexts.append(context)
        if name == "echo":
            return CapabilityResult(content=f"echo: {args.get('text', '')}")
        return CapabilityResult(content=f"unknown tool {name}", is_error=True)


def test_fake_provider_satisfies_protocol() -> None:
    assert isinstance(_FakeProvider(), CapabilityProvider)
    assert isinstance(StaticProvider([]), CapabilityProvider)


@pytest.mark.asyncio
async def test_provider_drives_advertise_and_invoke() -> None:
    """provider_tools advertises the tool schema and dispatches invoke with caller_did."""
    provider = _FakeProvider()
    tools = provider_tools(provider, caller_did="did:arc:user:alice")

    by_name = {t.name: t for t in tools}
    # The plain tool is directly invocable; the skill is folded into use_skill.
    assert "echo" in by_name
    assert "deploy_runbook" not in by_name
    assert "use_skill" in by_name
    # The skill is advertised lean in the use_skill menu (no body).
    assert "deploy_runbook" in by_name["use_skill"].description
    assert "STEP 1" not in by_name["use_skill"].description

    # Invoking the tool routes to provider.invoke carrying caller_did.
    from arcrun.capabilities import detached_context

    out = await by_name["echo"].execute({"text": "hi"}, detached_context())
    assert out == "echo: hi"
    assert provider.invoked == [("echo", {"text": "hi"}, "did:arc:user:alice")]


@pytest.mark.asyncio
async def test_invoke_receives_the_loops_live_context() -> None:
    """A provider-routed tool sees the run's live ToolContext (parent_state etc.),
    so a policy that meters the run (SPEC-038 provider budget) is not blind and
    does not fail closed on every call."""
    provider = _FakeProvider()
    echo = next(t for t in provider_tools(provider, caller_did="did:x") if t.name == "echo")
    from arcrun.capabilities import detached_context

    live = detached_context()
    await echo.execute({"text": "hi"}, live)

    assert provider.contexts == [live]


@pytest.mark.asyncio
async def test_use_skill_loads_body_lazily() -> None:
    """The skill body enters context only when use_skill is called (ADR-023 AC-4.2)."""
    provider = _FakeProvider()
    tools = provider_tools(provider, caller_did="did:arc:agent:x")
    use_skill = next(t for t in tools if t.name == "use_skill")

    from arcrun.capabilities import detached_context

    # Unused skill never loaded.
    assert provider.loaded == []

    body = await use_skill.execute({"name": "deploy_runbook"}, detached_context())
    assert "STEP 1" in body
    assert provider.loaded == [("deploy_runbook", "did:arc:agent:x")]


@pytest.mark.asyncio
async def test_use_skill_returns_root_and_file_inventory() -> None:
    """J4 B4: progressive disclosure — the model learns the skill root and every
    bundled file, and which tools reach them, so ``references/x.md`` in a body is
    reachable without guessing a host path."""
    provider = _FakeProvider()
    use_skill = next(
        t for t in provider_tools(provider, caller_did="did:arc:agent:x") if t.name == "use_skill"
    )
    from arcrun.capabilities import detached_context

    out = await use_skill.execute({"name": "deploy_runbook"}, detached_context())

    assert 'root="skill://deploy_runbook/"' in out
    assert "- references/rollback.md" in out
    assert "- scripts/drain.py" in out
    assert "read_skill_file" in out and "run_skill_script" in out


def test_skill_document_without_files_renders_body_only() -> None:
    doc = SkillDocument(name="plain", body="just do it")
    rendered = doc.render()
    assert "just do it" in rendered
    assert "read_skill_file" not in rendered


@pytest.mark.asyncio
async def test_invoke_extra_is_stashed_on_context() -> None:
    """A provider's CapabilityResult.extra is passed through onto ctx.tool_extra
    so the executor can spool it verbatim (U13). arcrun assigns it no meaning."""

    class _ExtraProvider(_FakeProvider):
        async def invoke(
            self,
            name: str,
            args: dict[str, Any],
            *,
            caller_did: str,
            context: ToolContext | None = None,
        ) -> CapabilityResult:
            return CapabilityResult(
                content="ok", extra={"activated_skill": "s", "skill_activated": True}
            )

    tools = provider_tools(_ExtraProvider(), caller_did="did:arc:agent:x")
    echo = next(t for t in tools if t.name == "echo")

    from arcrun.capabilities import detached_context

    ctx = detached_context()
    assert ctx.tool_extra is None
    await echo.execute({"text": "hi"}, ctx)
    assert ctx.tool_extra == {"activated_skill": "s", "skill_activated": True}


@pytest.mark.asyncio
async def test_static_provider_wraps_a_tool_list() -> None:
    """StaticProvider adapts a fixed tool list — advertise + invoke round-trip."""

    async def _greet(args: dict[str, Any], ctx: Any) -> str:
        return f"hello {args['who']}"

    tool = Tool(
        name="greet",
        description="Greet someone.",
        input_schema={"type": "object", "properties": {"who": {"type": "string"}}},
        execute=_greet,
    )
    provider = StaticProvider([tool])

    specs = provider.advertise()
    assert [s.name for s in specs] == ["greet"]
    result = await provider.invoke("greet", {"who": "world"}, caller_did="did:arc:test")
    assert result.content == "hello world"
    assert result.is_error is False

    missing = await provider.invoke("nope", {}, caller_did="did:arc:test")
    assert missing.is_error is True
