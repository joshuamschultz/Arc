"""H-049: the LLM call detail view shows the prompt as ordered, labeled sections.

``build_prompt_sections`` is a read-only projection over the persisted
``request_body`` (arcllm telemetry). arcagent assembles the system prompt as
named XML-ish elements ordered for provider-cache stability (base, identity,
then alphabetical); this projection re-presents what was actually sent in the
operator-legible H-049 order without touching a wire byte or inventing a
section.
"""

from __future__ import annotations

from typing import Any

from arcui.prompt_sections import build_prompt_sections

# A request body shaped like arcllm persists one — two system segments (the
# session-stable + run-stable cache tiers arcagent emits), then the user turn
# carrying its retrieved material inside <agent-context>. Section order here is
# the CACHE order (alphabetical within a tier), deliberately NOT the display
# order, so the test proves the projection reorders.
_SESSION_SEGMENT = "\n\n".join(
    [
        "<base>\nYou are an Arc agent. Each section is an XML element.\n</base>",
        "<identity>\nName: Olivia. Role: research lead.\n</identity>",
        (
            "<capabilities>\n"
            "<available-tools>\n<tool name=\"read\"><description>Read a file</description></tool>\n"
            "</available-tools>\n"
            "<available-skills>\n<skill name=\"seo-audit\"><description>Audit SEO</description></skill>\n"
            "</available-skills>\n"
            "</capabilities>"
        ),
        "<memory_status>\n3 cards recalled today.\n</memory_status>",
        "<policy>\nDeny destructive shell commands.\n</policy>",
        "<procedures>\nHow to file a report.\n</procedures>",
        "<strategy_react>\nThink, then act, one tool at a time.\n</strategy_react>",
    ]
)
_RUN_SEGMENT = "\n\n".join(
    [
        "<connections>\nGitHub connected.\n</connections>",
        "<context>\nWorking on the Q3 launch.\n</context>",
    ]
)
_USER_TURN = (
    "What is the launch status?\n\n"
    "<agent-context>\n<recall>\nLaunch slipped to October.\n</recall>\n</agent-context>"
)


def _request_body() -> dict[str, Any]:
    return {
        "messages": [
            {"role": "system", "content": _SESSION_SEGMENT},
            {"role": "system", "content": _RUN_SEGMENT},
            {"role": "user", "content": _USER_TURN},
        ],
        "tools": [{"name": "read", "description": "Read a file"}],
    }


def _keys(sections: list[dict[str, Any]]) -> list[str]:
    return [s["key"] for s in sections]


def test_sections_are_in_canonical_h049_order() -> None:
    """system prompt · identity · strategies · policies · tool list · skill list
    · context · session data — in exactly that order, ahead of any extras."""
    sections = build_prompt_sections(_request_body())
    keys = _keys(sections)
    canonical = [
        "system_prompt",
        "identity",
        "strategies",
        "policies",
        "tool_list",
        "skill_list",
        "context",
        "session_data",
    ]
    # Every canonical section present, in order (extras may follow after).
    positions = [keys.index(k) for k in canonical]
    assert positions == sorted(positions), f"out of order: {keys}"
    assert keys[: len(canonical)] == canonical, keys


def test_tools_and_skills_split_out_of_capabilities() -> None:
    """The nested <available-tools>/<available-skills> become their own display
    sections — a reader sees the tool list and skill list distinctly, not buried
    in one capabilities blob."""
    sections = {s["key"]: s for s in build_prompt_sections(_request_body())}
    assert "read" in sections["tool_list"]["body"]
    assert "seo-audit" in sections["skill_list"]["body"]
    # The umbrella capabilities tag is not left as a duplicate blob once emptied.
    assert "capabilities" not in {s["key"] for s in build_prompt_sections(_request_body())}


def test_session_data_carries_the_turn_context() -> None:
    """<agent-context> (this turn's retrieved recall/teams) is surfaced as the
    session-data section, not lost among the raw conversation."""
    sections = {s["key"]: s for s in build_prompt_sections(_request_body())}
    assert "Launch slipped to October" in sections["session_data"]["body"]


def test_unmapped_sections_are_preserved_after_the_canonical_ones() -> None:
    """Real sections outside the canonical eight (memory_status, procedures,
    connections, …) are never dropped — they follow, each labeled."""
    keys = _keys(build_prompt_sections(_request_body()))
    for extra in ("memory_status", "procedures", "connections"):
        assert extra in keys, f"{extra} dropped"
        assert keys.index(extra) > keys.index("session_data")


def test_each_section_has_label_and_token_estimate() -> None:
    for s in build_prompt_sections(_request_body()):
        assert s["label"] and isinstance(s["label"], str)
        assert isinstance(s["tokens"], int) and s["tokens"] >= 0
        assert s["body"].strip()


def test_tool_list_falls_back_to_request_tools_when_prompt_lacks_manifest() -> None:
    """A call whose system prompt carries no <available-tools> still shows a tool
    list, built from the request's tool schemas (what was actually sent)."""
    body = {
        "messages": [{"role": "system", "content": "<identity>\nX\n</identity>"}],
        "tools": [
            {"name": "web_search", "description": "Search the web"},
            {"type": "function", "function": {"name": "bash", "description": "Run a shell"}},
        ],
    }
    sections = {s["key"]: s for s in build_prompt_sections(body)}
    assert "tool_list" in sections
    assert "web_search" in sections["tool_list"]["body"]
    assert "bash" in sections["tool_list"]["body"]


def test_absent_or_encrypted_body_yields_no_sections() -> None:
    """No request body (metadata-only / federal-encrypted default) → nothing to
    show, never a crash."""
    assert build_prompt_sections(None) == []
    assert build_prompt_sections({}) == []
    assert build_prompt_sections({"messages": []}) == []
    assert build_prompt_sections("not a dict") == []


def test_only_present_sections_appear_no_fabrication() -> None:
    """A minimal prompt yields only the sections it truly contains — the view
    never invents a section that was not sent."""
    body = {"messages": [{"role": "system", "content": "<base>\nHi.\n</base>"}]}
    keys = _keys(build_prompt_sections(body))
    assert keys == ["system_prompt"]
