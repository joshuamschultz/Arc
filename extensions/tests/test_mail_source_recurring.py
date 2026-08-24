"""Recurring-sync contracts for the Gmail and Outlook source adapters."""

from __future__ import annotations

import importlib
import json
from types import SimpleNamespace
from typing import Any

import pytest
from arcagent.extension.source import ListSourceResources, SourceObjectKind, SyncSource


class _GmailAttachment:
    def __init__(self) -> None:
        self.round = 0
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.messages = {
            "a": {"id": "a", "historyId": "100", "body": "first"},
            "b": {"id": "b", "historyId": "101", "body": "second"},
            "c": {"id": "c", "historyId": "102", "body": "third"},
            "d": {"id": "d", "historyId": "103", "body": "fourth"},
        }

    async def invoke(self, tool: str, arguments: dict[str, Any]) -> Any:
        self.calls.append((tool, arguments))
        if tool == "google_gmail_messages":
            token = arguments.get("page_token")
            if self.round == 0:
                payload = {
                    None: {"messages": [{"id": "a"}, {"id": "b"}], "nextPageToken": "p2"},
                    "p2": {"messages": [{"id": "c"}], "historyId": "102"},
                }[token]
            else:
                payload = {"messages": [{"id": "d"}], "historyId": "103"}
            return SimpleNamespace(content=json.dumps(payload))
        if tool == "google_gmail_history":
            self.round = 1
            return SimpleNamespace(
                content=json.dumps(
                    {
                        "history": [
                            {"id": "103", "messagesAdded": [{"message": {"id": "d"}}]},
                            {"id": "103", "messagesDeleted": [{"message": {"id": "a"}}]},
                        ],
                        "historyId": "103",
                    }
                )
            )
        if tool == "google_gmail_message":
            return SimpleNamespace(content=json.dumps(self.messages[arguments["id"]]))
        if tool == "google_gmail_labels":
            return SimpleNamespace(
                content=json.dumps(
                    {"labels": [{"id": "INBOX", "name": "Inbox"}, {"id": "SENT", "name": "Sent"}]}
                )
            )
        raise AssertionError(tool)


class _OutlookAttachment:
    def __init__(self) -> None:
        self.round = 0
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.messages = {
            "a": {
                "id": "a",
                "changeKey": "key-a",
                "lastModifiedDateTime": "2026-08-23T10:00:00Z",
                "bodyPreview": "first",
            },
            "b": {
                "id": "b",
                "changeKey": "key-b",
                "lastModifiedDateTime": "2026-08-23T10:01:00Z",
                "bodyPreview": "second",
            },
            "c": {
                "id": "c",
                "changeKey": "key-c",
                "lastModifiedDateTime": "2026-08-23T10:02:00Z",
                "bodyPreview": "third",
            },
            "d": {
                "id": "d",
                "changeKey": "key-d",
                "lastModifiedDateTime": "2026-08-23T10:03:00Z",
                "bodyPreview": "fourth",
            },
        }

    async def invoke(self, tool: str, arguments: dict[str, Any]) -> Any:
        self.calls.append((tool, arguments))
        if tool == "list-mail-messages":
            skip = arguments.get("skip", 0)
            if self.round == 0:
                payload = {
                    0: {
                        "value": [self.messages["a"], self.messages["b"]],
                        "@odata.nextLink": "https://graph.microsoft.com/v1.0/me/messages?$skip=2",
                    },
                    2: {"value": [self.messages["c"]]},
                }[skip]
            else:
                payload = {
                    "value": [self.messages["d"], {"id": "a", "@removed": {"reason": "deleted"}}]
                }
            return SimpleNamespace(content=json.dumps(payload))
        if tool == "get-mail-message":
            return SimpleNamespace(content=json.dumps(self.messages[arguments["message_id"]]))
        raise AssertionError(tool)


@pytest.mark.asyncio
async def test_gmail_pages_then_uses_history_for_changed_and_deleted_messages() -> None:
    module = importlib.import_module("extensions.google_workspace.arc_ext_google_workspace.source")
    attachment = _GmailAttachment()
    adapter = module.GmailSourceAdapter(attachment)

    first = await adapter.sync_source(SyncSource(connection_id="gmail", page_size=2))
    second = await adapter.sync_source(
        SyncSource(connection_id="gmail", checkpoint=first.next_checkpoint, page_size=2)
    )
    third = await adapter.sync_source(
        SyncSource(connection_id="gmail", checkpoint=second.next_checkpoint, page_size=2)
    )

    assert first.has_more and [item.object_id for item in first.objects] == ["a", "b"]
    assert not second.has_more and [item.object_id for item in second.objects] == ["c"]
    assert [item.object_id for item in third.objects] == ["d", "a"]
    assert third.objects[1].kind is SourceObjectKind.DELETED
    assert third.objects[1].deleted
    assert all(isinstance(item.metadata["revision"], int) for item in first.objects)
    # A label is a term in Gmail's query syntax, not a flag: `gog` has no
    # --label and refuses the call outright when one is sent.
    assert (
        "google_gmail_messages",
        {"query": "in:anywhere", "limit": "2", "page_token": "p2"},
    ) in attachment.calls
    assert any(tool == "google_gmail_history" for tool, _ in attachment.calls)


@pytest.mark.asyncio
async def test_outlook_pages_repeat_and_normalize_graph_deletions() -> None:
    module = importlib.import_module("extensions.microsoft365.arc_ext_microsoft365.source")
    attachment = _OutlookAttachment()
    adapter = module.OutlookSourceAdapter(attachment)

    first = await adapter.sync_source(SyncSource(connection_id="outlook", page_size=2))
    second = await adapter.sync_source(
        SyncSource(connection_id="outlook", checkpoint=first.next_checkpoint, page_size=2)
    )
    attachment.round = 1
    third = await adapter.sync_source(
        SyncSource(connection_id="outlook", checkpoint=second.next_checkpoint, page_size=2)
    )

    assert first.has_more and [item.object_id for item in first.objects] == ["a", "b"]
    assert not second.has_more and [item.object_id for item in second.objects] == ["c"]
    assert [item.object_id for item in third.objects] == ["d", "a"]
    assert third.objects[1].kind is SourceObjectKind.DELETED
    assert third.objects[1].deleted
    assert all(isinstance(item.metadata["revision"], int) for item in first.objects)
    assert ("list-mail-messages", {"folder": "inbox", "top": 2, "skip": 2}) in attachment.calls


@pytest.mark.asyncio
async def test_gmail_reads_the_message_out_of_its_envelope() -> None:
    """A read returns {message, body, headers, ...} with the id inside `message`.

    Reading the id off the envelope found nothing, so every message looked
    unavailable and one of them ended the whole account's sync.
    """
    module = importlib.import_module("extensions.google_workspace.arc_ext_google_workspace.source")

    unwrapped = module._unwrap_message(
        {
            "message": {"id": "abc123", "historyId": "42", "internalDate": "1700000000000"},
            "body": "the decoded text",
            "headers": {"Subject": "Hello"},
        }
    )

    assert unwrapped["id"] == "abc123"
    assert unwrapped["historyId"] == "42"
    # The body is what gets indexed, so it must travel with the message.
    assert unwrapped["body"] == "the decoded text"


def test_an_unenveloped_message_is_left_alone() -> None:
    """A payload that is already the message must not be mangled."""
    module = importlib.import_module("extensions.google_workspace.arc_ext_google_workspace.source")

    assert module._unwrap_message({"id": "abc123"})["id"] == "abc123"


@pytest.mark.asyncio
async def test_gmail_fetches_the_body_from_inside_the_envelope() -> None:
    """Fetch reads the same envelope the message read does.

    Unwrapping in only one of them left the fetch with no revision at all, so
    the sync got as far as listing and then died on the first body it pulled.
    """
    module = importlib.import_module("extensions.google_workspace.arc_ext_google_workspace.source")

    unwrapped = module._unwrap_message(
        {
            "message": {"id": "abc", "historyId": "77"},
            "body": "the full decoded message text",
            "snippet": "one line preview",
        }
    )

    assert module._revision(unwrapped) == "77"
    # The body, not the preview: indexing the snippet would make a mail account
    # searchable only by its previews.
    assert unwrapped["body"] == "the full decoded message text"


@pytest.mark.asyncio
async def test_gmail_offers_all_mail_and_no_duplicate_inbox() -> None:
    """Two rows sharing one id made the obvious pair of clicks unsavable.

    The picker listed a synthetic "Inbox" beside the real INBOX label under the
    same resource_id, so checking both sent a duplicate and the save was refused
    with "resource_ids must be a non-empty unique list". What was missing was
    everything: an inbox is a small corner of an account.
    """
    module = importlib.import_module("extensions.google_workspace.arc_ext_google_workspace.source")
    attachment = _GmailAttachment()
    adapter = module.GmailSourceAdapter(attachment)

    resources = await adapter.list_source_resources(ListSourceResources(connection_id="blackarc"))

    ids = [resource.resource_id for resource in resources]
    assert len(ids) == len(set(ids))
    assert module._ALL_MAIL in ids


def test_all_mail_selects_the_whole_account() -> None:
    module = importlib.import_module("extensions.google_workspace.arc_ext_google_workspace.source")

    assert module._query_for(module._ALL_MAIL) == "in:anywhere"
    assert module._query_for("SENT") == "label:SENT"


class _RefusingAttachment:
    """A ``gog`` that fails every call with one message."""

    def __init__(self, content: str) -> None:
        self._content = content

    async def invoke(self, tool: str, arguments: dict[str, Any]) -> Any:
        return SimpleNamespace(tool=tool, outcome="error", content=self._content)


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        (
            'round trip: base token source: oauth2: "invalid_grant" '
            '"Token has been expired or revoked."',
            "auth_required",
        ),
        ("googleapi: Error 429: User Rate Limit Exceeded, rateLimitExceeded", "rate_limited"),
        ("read tcp 10.0.0.1:443: connection reset by peer", "transient"),
    ],
)
async def test_a_gmail_failure_is_classified_so_an_operator_can_act(
    content: str, expected: str
) -> None:
    """A revoked grant needs a person, not a retry — and must not be called transient.

    Reported as TRANSIENT, the coordinator retried a dead account every cycle
    forever and the dashboard called it a temporary problem, while the only fix
    was for someone to re-run ``gog auth add`` at the host.
    """
    module = importlib.import_module("extensions.google_workspace.arc_ext_google_workspace.source")
    adapter = module.GmailSourceAdapter(_RefusingAttachment(content))

    with pytest.raises(module.SourceError) as caught:
        await adapter.list_source_resources(ListSourceResources(connection_id="blackarc"))

    assert str(caught.value.code) == expected
    assert content[:40] in caught.value.detail
