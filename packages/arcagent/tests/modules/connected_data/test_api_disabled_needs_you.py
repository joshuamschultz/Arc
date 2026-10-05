"""A provider API the operator never enabled is a needs-you, not a retry storm.

DGX evidence (2026-10-05): Google answered 403 ``accessNotConfigured`` ("Google Drive
API has not been used in project ... Enable it by visiting <url>") and Arc retried it
as a transient outage. Only the operator can enable the API. The real service and the
real coordinator run; only the provider and the health record are doubles.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from arcstore.source_sync import InMemorySourceSyncStore
from packages.arcagent.tests.modules.connected_data.test_connections_sweep_sync_loop import (
    _Health,
    _Provider,
    _service,
    _until,
)

from arcagent.extension.connection_health import (
    HealthSignal,
    action_label,
    classify,
    next_health,
    reason_text,
)
from arcagent.extension.source import SourceError, SourceFailureCode, SyncSource, SyncSourcePage
from arcagent.extension.source_catalog import SourceCatalog
from arcagent.extension.state import ConnectionRecord

_LINK = "https://console.developers.google.com/apis/api/drive.googleapis.com/overview?project=1"
_NOW = datetime(2026, 10, 5, tzinfo=UTC)


class _ApiDisabledProvider(_Provider):
    async def sync_source(self, request: SyncSource) -> SyncSourcePage:
        self.checkpoints.append(request.checkpoint)
        raise SourceError(
            SourceFailureCode.API_DISABLED,
            "Google API error 403 accessNotConfigured: Google Drive API is not enabled",
            action_url=_LINK,
        )


async def test_a_disabled_api_reaches_the_health_record_with_its_link_and_is_not_retried() -> None:
    catalog, store, health = SourceCatalog(), InMemorySourceSyncStore(), _Health()
    provider = _ApiDisabledProvider()
    await catalog.register("drive", provider)
    service = _service(catalog, store, health=health, restart_backoff_seconds=0.01)
    await service.start()
    try:

        async def reported() -> bool:
            return any(not signal.ok for _, signal in health.signals)

        assert await _until(reported)
        # Long enough that a retry storm would have run the source again.
        assert not await _until(lambda: _more_than_one(provider), seconds=0.3)
    finally:
        await service.close()

    failing = [signal for _, signal in health.signals if not signal.ok]
    assert failing[0].reason_code == "api_disabled"
    assert failing[0].action_url == _LINK
    assert len(provider.checkpoints) == 1


async def _more_than_one(provider: _Provider) -> bool:
    return len(provider.checkpoints) > 1


def _record() -> ConnectionRecord:
    return ConnectionRecord(connection="drive", created_at=_NOW.isoformat())


def _api_disabled_signal(link: str | None = _LINK) -> HealthSignal:
    return HealthSignal(
        ok=False,
        source="sync",
        checked_by="did:agent",
        reason_code="api_disabled",
        detail="Google Drive API has not been used in project 1 before or it is disabled",
        provider="Google",
        action_url=link,
    )


def test_api_disabled_settles_at_once_as_needs_you_with_an_open_link_action() -> None:
    patch, transition = next_health(_record(), _api_disabled_signal(), _NOW)

    assert patch["status"] == "needs_you"
    assert patch["reason_code"] == "api_disabled"
    assert patch["action"] == "open_link"
    assert patch["action_url"] == _LINK
    assert patch["reason_text"] == "Enable the Google Drive API for your Google Cloud project"
    assert transition.notice == "needs_you"


def test_api_disabled_without_a_trusted_link_still_needs_you_but_carries_no_link() -> None:
    patch, _ = next_health(_record(), _api_disabled_signal(None), _NOW)

    assert patch["action"] == "open_link"
    assert patch["action_url"] is None


def test_a_link_off_google_s_console_never_reaches_the_record() -> None:
    patch, _ = next_health(_record(), _api_disabled_signal("https://evil.example/enable"), _NOW)

    assert patch["action_url"] is None


def test_the_api_disabled_wording_and_button() -> None:
    assert (
        reason_text("api_disabled", detail="Google Drive API")
        == "Enable the Google Drive API for your Google Cloud project"
    )
    assert action_label("open_link") == "Open Google Cloud console"


def test_a_sync_code_of_api_disabled_classifies_as_itself() -> None:
    assert classify("api_disabled", "") == "api_disabled"
    assert classify(None, "403 accessNotConfigured") == "api_disabled"


def test_a_good_sync_clears_the_link() -> None:
    patch, _ = next_health(_record(), _api_disabled_signal(), _NOW)
    settled = _record().model_copy(update=patch)
    ok = HealthSignal(ok=True, source="sync", checked_by="did:agent")

    cleared, _ = next_health(settled, ok, _NOW)

    assert cleared["status"] == "healthy"
    assert cleared["action"] == "none"
    assert cleared["action_url"] is None


@pytest.mark.parametrize("other", ["auth_required", "custody_unavailable"])
def test_a_different_settled_reason_does_not_inherit_a_stale_link(other: Any) -> None:
    patch, _ = next_health(_record(), _api_disabled_signal(), _NOW)
    settled = _record().model_copy(update=patch)
    again = HealthSignal(
        ok=False,
        source="operator",
        checked_by="did:agent",
        reason_code=other,
        provider="Google",
    )

    cleared, _ = next_health(settled, again, _NOW)

    assert cleared["action_url"] is None
