"""Connector reliability (alpha-2 P18-0): a dead source says so, and says why.

DGX evidence: Gmail ``blackarc`` was dead for 33 days on ``404 notFound`` from the
history call (classified TRANSIENT, retried forever); 36 calls failed with
``No auth for gmail ...`` (also TRANSIENT-shaped); one Dropbox file answering 500
aborted the whole page; ``dropbox_upload`` died at the 30 s tool timeout while its
transport allows five minutes. Each test below drives the shipped adapter, not a
stand-in for it.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import httpx
import pytest
from arcagent.extension.attachment import ToolOutcome, ToolResult
from arcagent.extension.source import (
    FetchSourceObject,
    InspectSource,
    SourceError,
    SourceFailureCode,
    SyncSource,
)

from extensions.dropbox.arc_ext_dropbox import build_native_attachment as build_dropbox
from extensions.github.arc_ext_github import _github_failure_code
from extensions.google_workspace.arc_ext_google_workspace.source import GmailSourceAdapter
from extensions.jira.arc_ext_jira import JiraSourceAdapter
from extensions.tests.fake_credential import FakeCredentialHandle


class _ErroringAttachment:
    """An attachment whose every verb fails the way a vendor CLI fails."""

    def __init__(self, detail: str) -> None:
        self._detail = detail
        self.calls: list[str] = []

    async def invoke(self, tool: str, args: dict[str, Any]) -> ToolResult:
        self.calls.append(tool)
        return ToolResult(tool=tool, outcome=ToolOutcome.ERROR, content=self._detail)


# --- Gmail: an aged-out history cursor is a checkpoint problem, not a blip ---------


async def test_gmail_history_404_means_the_checkpoint_is_invalid() -> None:
    """The DGX line: ``gog google_gmail_history exited 5: 404 notFound``."""
    adapter = GmailSourceAdapter(
        _ErroringAttachment("gog google_gmail_history exited 5: 404 notFound: Requested entity")
    )
    cursor = json.dumps({"v": 1, "mode": "history", "history_id": "100"})

    with pytest.raises(SourceError) as raised:
        await adapter.sync_source(SyncSource(connection_id="g", checkpoint=cursor))

    assert raised.value.code is SourceFailureCode.CHECKPOINT_INVALID


async def test_gmail_snapshot_404_is_not_mistaken_for_a_dead_checkpoint() -> None:
    """Only the history call carries a cursor that can age out."""
    adapter = GmailSourceAdapter(_ErroringAttachment("gog exited 5: 404 notFound"))

    with pytest.raises(SourceError) as raised:
        await adapter.sync_source(SyncSource(connection_id="g", checkpoint=None))

    assert raised.value.code is not SourceFailureCode.CHECKPOINT_INVALID


# --- AUTH_REQUIRED: the words a vendor CLI uses when nobody is signed in ----------

_NOBODY_SIGNED_IN = (
    "No auth for gmail josh@example.com",
    "gog: no auth for calendar blackarc",
    "failed to read keyring: no TTY available to prompt for the password",
    "cannot unlock keyring without a terminal (not a tty)",
    'oauth2: "invalid_grant" "Token has been expired or revoked."',
    "Error: not logged in. Run `acli jira auth login`",
    "acli: authentication failed (401 Unauthorized)",
)


@pytest.mark.parametrize("detail", _NOBODY_SIGNED_IN)
async def test_gmail_marks_a_signed_out_account_as_needing_reauthorization(detail: str) -> None:
    adapter = GmailSourceAdapter(_ErroringAttachment(detail))

    with pytest.raises(SourceError) as raised:
        await adapter.sync_source(SyncSource(connection_id="g", checkpoint=None))

    assert raised.value.code is SourceFailureCode.AUTH_REQUIRED


@pytest.mark.parametrize("detail", _NOBODY_SIGNED_IN)
async def test_jira_marks_a_signed_out_account_as_needing_reauthorization(detail: str) -> None:
    adapter = JiraSourceAdapter(_ErroringAttachment(detail))

    with pytest.raises(SourceError) as raised:
        await adapter.inspect_source(InspectSource(connection_id="j"))

    assert raised.value.code is SourceFailureCode.AUTH_REQUIRED


async def test_jira_ordinary_failures_stay_transient() -> None:
    adapter = JiraSourceAdapter(_ErroringAttachment("acli: connection reset by peer"))

    with pytest.raises(SourceError) as raised:
        await adapter.inspect_source(InspectSource(connection_id="j"))

    assert raised.value.code is SourceFailureCode.TRANSIENT


async def test_an_issue_key_that_contains_401_is_not_an_auth_failure() -> None:
    adapter = JiraSourceAdapter(_ErroringAttachment("acli: could not load issue PROJ-401"))

    with pytest.raises(SourceError) as raised:
        await adapter.inspect_source(InspectSource(connection_id="j"))

    assert raised.value.code is SourceFailureCode.TRANSIENT


def test_github_shares_the_one_classifier() -> None:
    assert _github_failure_code("no TTY for keyring") is SourceFailureCode.AUTH_REQUIRED
    assert _github_failure_code("API rate limit exceeded") is SourceFailureCode.RATE_LIMITED
    assert _github_failure_code("TLS handshake timeout") is SourceFailureCode.TRANSIENT


# --- Dropbox ----------------------------------------------------------------------


def _dropbox(monkeypatch: pytest.MonkeyPatch, handler: Callable[[httpx.Request], Any]) -> Any:
    real = httpx.AsyncClient

    def factory(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs["transport"] = httpx.MockTransport(handler)
        return real(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", factory)
    return build_dropbox({"credential": FakeCredentialHandle(bearer_values=["AT"])})


def _token_then(answer: httpx.Response) -> Callable[[httpx.Request], httpx.Response]:
    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == "https://api.dropbox.com/oauth2/token":
            return httpx.Response(200, json={"access_token": "AT", "expires_in": 3600})
        return answer

    return handler


async def test_a_file_dropbox_no_longer_has_is_not_found_not_transient(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attachment = _dropbox(monkeypatch, _token_then(httpx.Response(404, text="gone")))

    with pytest.raises(SourceError) as raised:
        await attachment.fetch_source(
            FetchSourceObject(connection_id="d", object_id="id:one", version="r1")
        )
    await attachment.close_source()

    assert raised.value.code is SourceFailureCode.NOT_FOUND


async def test_a_file_that_keeps_answering_5xx_is_a_transient_failure_of_that_file(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attachment = _dropbox(
        monkeypatch, _token_then(httpx.Response(500, headers={"Retry-After": "0"}))
    )

    with pytest.raises(SourceError) as raised:
        await attachment.fetch_source(
            FetchSourceObject(connection_id="d", object_id="id:one", version="r1")
        )
    await attachment.close_source()

    assert raised.value.code is SourceFailureCode.TRANSIENT


async def test_dropbox_upload_is_not_cut_off_before_its_transport_gives_up() -> None:
    """30 s tool timeout vs a 300 s read / 120 s write transport: ``TOOL_TIMEOUT`` x N."""
    attachment = build_dropbox({"credential": FakeCredentialHandle(bearer_values=["AT"])})
    tools = {tool.name: tool for tool in await attachment.describe_tools()}
    await attachment.close_source()

    upload_timeout = tools["dropbox_upload"].timeout_seconds
    assert upload_timeout is not None and upload_timeout >= 300
    assert tools["dropbox_list"].timeout_seconds is None
