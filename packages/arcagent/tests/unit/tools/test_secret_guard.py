"""Secret-shaped content scanning — ``arcagent.tools._secret_guard``.

Reuses arcllm's structured-prefix secret patterns (AWS/GitHub/JWT/PEM/DB
URL/Anthropic/OpenAI/Google/Slack — ADR-423) read-only, plus one
arcagent-local keyword-anchored heuristic for generic unprefixed tokens —
the exact shape of the live incident (task #21): a pasted Browserbase API
token has no recognizable prefix, so arcllm's scoped pattern list alone
would miss it.
"""

from __future__ import annotations

import pytest

from arcagent.core.errors import ToolError
from arcagent.tools._secret_guard import enforce_no_secret_content, find_secret


class TestFindSecret:
    def test_aws_key_detected(self) -> None:
        assert find_secret("key = AKIAIOSFODNN7EXAMPLE") == "AWS_ACCESS_KEY"

    def test_github_token_detected(self) -> None:
        token = "gh" + "p_" + "A" * 40
        assert find_secret(f"export GITHUB_TOKEN={token}") == "GITHUB_TOKEN"

    def test_generic_keyword_assignment_detected(self) -> None:
        # Browserbase-shaped: no recognizable prefix, but keyword + long token.
        content = "browserbase_api_key: bb_live_9f8a7c6d5e4b3a2f1e0d9c8b"
        assert find_secret(content) == "GENERIC_API_TOKEN"

    def test_generic_bearer_token_detected(self) -> None:
        content = "Authorization: Bearer 9f8a7c6d5e4b3a2f1e0d9c8b7a6f5e4d"
        assert find_secret(content) == "GENERIC_API_TOKEN"

    def test_plain_prose_not_flagged(self) -> None:
        assert find_secret("This is a normal markdown note about the weather.") is None

    def test_ordinary_code_not_flagged(self) -> None:
        source = (
            "from arcagent.tools._decorator import tool\n"
            "@tool(description='greet', version='1.0.0')\n"
            "async def hello() -> str:\n"
            "    return 'hi'\n"
        )
        assert find_secret(source) is None

    def test_short_placeholder_not_flagged(self) -> None:
        # Short/placeholder values must not trip the generic heuristic.
        assert find_secret("api_key = 'changeme'") is None
        assert find_secret("password: <your-password>") is None


class TestEnforceNoSecretContent:
    def test_allows_clean_content(self) -> None:
        enforce_no_secret_content("hello world", tool_name="write", file_path="notes.txt")

    def test_denies_secret_shaped_content(self) -> None:
        events: list[tuple[str, dict[str, object]]] = []
        with pytest.raises(ToolError) as exc_info:
            enforce_no_secret_content(
                "key = AKIAIOSFODNN7EXAMPLE",
                tool_name="write",
                file_path="secrets/aws.md",
                caller_did="did:arc:josh_agent",
                audit_sink=lambda et, d: events.append((et, d)),
            )
        assert exc_info.value.code == "TOOL_SECRET_WRITE_DENIED"
        assert events
        event_type, details = events[-1]
        assert event_type == "tool.secret_write.denied"
        assert details["tool"] == "write"
        assert details["actor_did"] == "did:arc:josh_agent"
        assert details["secret_type"] == "AWS_ACCESS_KEY"

    def test_audit_sink_failure_does_not_mask_denial(self) -> None:
        def _raising_sink(event_type: str, details: dict[str, object]) -> None:
            raise RuntimeError("sink exploded")

        with pytest.raises(ToolError):
            enforce_no_secret_content(
                "key = AKIAIOSFODNN7EXAMPLE",
                tool_name="write",
                file_path="x.md",
                audit_sink=_raising_sink,
            )
