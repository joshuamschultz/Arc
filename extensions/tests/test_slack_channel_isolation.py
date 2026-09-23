"""One unreadable Slack channel is that channel's problem, not the account's.

On a live box one channel answering ``channel_not_found`` from
``conversations.history`` ended the whole Slack source: the refusal surfaced
while the page was being listed, where every error is account-wide. A missing
or not-joined channel is now listed like any other and refused when fetched,
typed ``NOT_FOUND``, so the coordinator skips just that object (audited with
its reason) and syncs the rest. Refusals about the token or the workspace
still end the run.

Driven through the real adapter and the real coordinator; httpx is mocked at
the transport.
"""

from __future__ import annotations

import urllib.parse
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest
from arcagent.connected_data import MappingPlan, SyncError, SyncLimits, SyncStatus
from arcagent.core.tier import Tier
from arcagent.extension.manifest import load_manifest
from arcagent.extension.secrets import Secret
from arcagent.extension.source import SourceDescription
from arcagent.modules.connected_data import ConnectedDataCoordinator
from arcagent.modules.connectors.install import build_attachment
from arcstore.source_sync import InMemorySourceSyncStore

_BUNDLE = Path(__file__).resolve().parents[1] / "slack"
_SOURCE = SourceDescription(connection_id="ctgslack", source_kind="slack", account_id="T123")


def _slack(broken_channel: str, error: str) -> Callable[[httpx.Request], httpx.Response]:
    """A workspace where every call about ``broken_channel`` answers ``error``."""

    def handler(request: httpx.Request) -> httpx.Response:
        method = str(request.url).rsplit("/", 1)[-1]
        params = dict(urllib.parse.parse_qsl(request.content.decode("utf-8")))
        if method == "conversations.list":
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "channels": [
                        {"id": "C1", "name": "deals"},
                        {"id": "C2", "name": "archived-away"},
                        {"id": "C3", "name": "general"},
                    ],
                    "response_metadata": {"next_cursor": ""},
                },
            )
        if method == "conversations.history":
            if params.get("channel") == broken_channel:
                return httpx.Response(200, json={"ok": False, "error": error})
            channel = params.get("channel", "")
            return httpx.Response(
                200,
                json={"ok": True, "messages": [{"ts": "10.0", "user": "U1", "text": channel}]},
            )
        if method == "users.info":
            return httpx.Response(200, json={"ok": True, "user": {"real_name": "Ann"}})
        return httpx.Response(200, json={"ok": False, "error": f"unmapped_{method}"})

    return handler


class Ingest:
    def __init__(self) -> None:
        self.ingested: list[str] = []

    async def require_approved_mapping(self, source: SourceDescription) -> MappingPlan:
        return MappingPlan(mapping_id="m", homes=("document",), revision="r", content_hash="h")

    async def ingest(self, source: Any, source_object: Any, content: Any, mapping: Any) -> None:
        self.ingested.append(source_object.object_id)

    async def complete_snapshot(self, source: Any, object_ids: Any, mapping: Any) -> None:
        return None


async def _run(
    monkeypatch: pytest.MonkeyPatch, broken_channel: str, error: str
) -> tuple[Any, Ingest, list[tuple[str, dict[str, Any]]]]:
    real = httpx.AsyncClient
    handler = _slack(broken_channel, error)
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda *a, **k: real(*a, **{**k, "transport": httpx.MockTransport(handler)}),
    )
    manifest = load_manifest(
        (_BUNDLE / "extension.toml").read_text(encoding="utf-8"), tier=Tier.PERSONAL
    )
    wrapper: Any = build_attachment(manifest, _BUNDLE, {"user_token": Secret("xoxp-t")})
    adapter = wrapper._delegate
    ingest = Ingest()
    events: list[tuple[str, dict[str, Any]]] = []

    async def audit(action: str, payload: dict[str, Any]) -> None:
        events.append((action, payload))

    try:
        result = await ConnectedDataCoordinator(
            adapter, ingest, InMemorySourceSyncStore(), audit=audit
        ).run(
            _SOURCE,
            agent_did="did:a",
            owner_id="worker",
            limits=SyncLimits(retries=0, max_duty_fraction=1.0),
        )
    finally:
        await adapter.close_source()
    return result, ingest, events


@pytest.mark.parametrize("error", ["channel_not_found", "not_in_channel"])
async def test_a_missing_channel_is_skipped_and_the_rest_sync(
    monkeypatch: pytest.MonkeyPatch, error: str
) -> None:
    result, ingest, events = await _run(monkeypatch, "C2", error)

    assert result.status is SyncStatus.COMPLETE
    assert ingest.ingested == ["channel:C1", "channel:C3"]
    skips = [payload for action, payload in events if action.endswith("object_skipped")]
    assert [payload["reason"] for payload in skips] == ["object_not_found"]


@pytest.mark.parametrize(
    ("error", "code"),
    [
        ("invalid_auth", "auth_required"),
        ("token_revoked", "auth_required"),
        ("missing_scope", "transient"),
    ],
)
async def test_an_account_wide_refusal_still_fails_the_source(
    monkeypatch: pytest.MonkeyPatch, error: str, code: str
) -> None:
    with pytest.raises(SyncError) as caught:
        await _run(monkeypatch, "C2", error)

    assert caught.value.code == code


async def test_a_rate_limit_still_fails_the_source(monkeypatch: pytest.MonkeyPatch) -> None:
    import arc_ext_slack

    monkeypatch.setattr(arc_ext_slack, "_retry_after", lambda *_: 0.0)
    with pytest.raises(SyncError) as caught:
        await _run(monkeypatch, "C2", "ratelimited")

    assert caught.value.code == "rate_limited"
