"""SPEC-043 REQ-010b/c — tier-ladder approval-set resolution."""

from __future__ import annotations

from arcagent.tools._transport import RegisteredTool, ToolTransport
from arcagent.tools.approval_policy import resolve_approval_set


def _tool(name: str, *, skill: bool = False) -> RegisteredTool:
    return RegisteredTool(
        name=name,
        description=name,
        input_schema={},
        transport=ToolTransport.NATIVE,
        execute=lambda **_k: "ok",
        skill_backed=skill,
    )


_TOOLS = [_tool("read_file"), _tool("send_email"), _tool("summarize", skill=True)]


class TestTierLadder:
    def test_personal_is_empty_by_default(self) -> None:
        assert resolve_approval_set(_TOOLS, "personal") == frozenset()

    def test_personal_honors_opt_in(self) -> None:
        got = resolve_approval_set(_TOOLS, "personal", opt_in=frozenset({"send_email"}))
        assert got == frozenset({"send_email"})

    def test_enterprise_all_plain_tools_not_skills(self) -> None:
        got = resolve_approval_set(_TOOLS, "enterprise")
        # Every plain tool requires approval; the skill-backed one does NOT.
        assert got == frozenset({"read_file", "send_email"})
        assert "summarize" not in got

    def test_federal_covers_skills_and_tools(self) -> None:
        got = resolve_approval_set(_TOOLS, "federal")
        assert got == frozenset({"read_file", "send_email", "summarize"})

    def test_skill_backed_tool_pauses_federal_not_enterprise(self) -> None:
        assert "summarize" in resolve_approval_set(_TOOLS, "federal")
        assert "summarize" not in resolve_approval_set(_TOOLS, "enterprise")


class TestArcrunPredicateStaysMembership:
    def test_arcrun_needs_approval_is_pure_membership(self) -> None:
        # arcrun's trigger is ``tc.name in approval_required_tools`` — no tier
        # logic in the loop. The resolved set is the only input.
        import inspect

        from arcrun.strategies import react

        src = inspect.getsource(react._resolve_approval)
        assert "approval_required_tools" in src
        # No tier vocabulary leaks into the loop's predicate.
        for word in ("federal", "enterprise", "personal", "skill_backed"):
            assert word not in src
