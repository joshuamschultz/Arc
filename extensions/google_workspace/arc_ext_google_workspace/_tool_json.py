"""Read a native tool's result for a source adapter: its JSON, or its own words."""

from __future__ import annotations

import json
from typing import Any

from arcagent.extension.attachment import ToolResult
from arcagent.extension.source import SourceError, SourceFailureCode, classify_cli_failure


class CredentialFailureResult(ToolResult):
    """A tool failure whose cause is Arc's own typed credential error.

    It is still a plain ``ToolResult`` to the agent (``content`` is the readable
    text); a source adapter reads ``failure`` instead of guessing from the prose.
    """

    failure: SourceFailureCode
    retry_after: float | None = None


def tool_payload(result: Any, label: str, *, list_key: str) -> dict[str, Any]:
    """The tool's JSON, or the tool's own words about why there is none.

    A failed call returns readable prose in ``content``. Parsing that as JSON
    reported "returned invalid JSON" over every real cause (an expired grant, a
    revoked scope, a rate limit) and left an operator with nothing to act on.
    """
    if isinstance(result, CredentialFailureResult):
        raise SourceError(result.failure, result.content[:256], retry_after=result.retry_after)
    if getattr(result, "outcome", None) is not None and str(result.outcome) != "ok":
        # Classify on the WHOLE message, report a truncated one. Google puts the
        # reason at the END, after a request URL long enough that a 256-character
        # detail cut "invalid_grant" off, so a revoked token classified as
        # transient and was retried every cycle forever.
        full = str(result.content)
        raise SourceError(classify_cli_failure(full), full[:256])
    try:
        parsed = json.loads(result.content)
    except json.JSONDecodeError as exc:
        detail = str(result.content)[:200].strip() or "an empty response"
        raise SourceError(
            SourceFailureCode.TRANSIENT, f"{label} returned no JSON: {detail}"
        ) from exc
    return parsed if isinstance(parsed, dict) else {list_key: parsed}
