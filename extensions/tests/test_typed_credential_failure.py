"""A missing or revoked credential stays typed from the broker to the sync outcome.

Drive and OneDrive run through the real coordinator and the real native clients;
only the provider wire and the credential broker's answer are faked. A credential
only a person can fix must end the run once as ``auth_required`` (the card flips to
needs_you) with zero retries. A real provider outage (503) must still be retried.
"""

from __future__ import annotations

import asyncio
import importlib
from typing import Any

import httpx
import pytest
from arcagent.connected_data import SyncError, SyncLimits, TransientSyncError
from arcagent.extension.credentials import CredentialRenewalError
from arcagent.extension.secrets import Secret
from arcagent.extension.source import SourceDescription
from arcagent.modules.connected_data import ConnectedDataCoordinator
from arcstore.source_sync import InMemorySourceSyncStore

from extensions.google_workspace.arc_ext_google_workspace.drive_source import DriveSourceAdapter
from extensions.google_workspace.arc_ext_google_workspace.native import build_native_attachment
from packages.arcagent.tests.unit.modules.connected_data.test_coordinator import FakeIngest

_ms_source = importlib.import_module("extensions.microsoft365.arc_ext_microsoft365.source")

SOURCE = SourceDescription(connection_id="conn", source_kind="test", account_id="account")
LIMITS = SyncLimits(retries=3, retry_backoff_seconds=0.0)


class _BrokenCredential:
    """A broker that raises the typed renewal error once every run has asked it."""

    def __init__(self, error_code: str, *, expect_arrivals: int = 1) -> None:
        self.error_code = error_code
        self.bearer_calls = 0
        self._expect = expect_arrivals
        self._arrived = asyncio.Event()

    async def bearer(self) -> Secret:
        self.bearer_calls += 1
        if self.bearer_calls >= self._expect:
            self._arrived.set()
        await self._arrived.wait()  # forces the runs to interleave
        raise CredentialRenewalError(
            error_code=self.error_code, message="'conn' has no stored credential; connect it again"
        )

    async def invalidate(self) -> None:
        return None


async def _no_sleep(_seconds: float) -> None:
    return None


def _drive(credential: Any, transport: httpx.AsyncBaseTransport | None = None) -> Any:
    attachment = build_native_attachment(
        {"credential": credential, "account": "me@example.com", "transport": transport}
    )
    return DriveSourceAdapter(attachment, account="me@example.com")


def _onedrive(credential: Any, transport: httpx.AsyncBaseTransport | None = None) -> Any:
    adapters = _ms_source.build_source_adapters(
        {"credential": credential, "cloud": "global", "transport": transport}
    )
    adapter = adapters["onedrive"]
    adapter._drive, adapter._folder = "me", "root"  # a folder the operator already selected
    return adapter


def _run(source: Any, did: str, store: InMemorySourceSyncStore) -> Any:
    return ConnectedDataCoordinator(source, FakeIngest(), store, sleep=_no_sleep).run(
        SOURCE, agent_did=did, owner_id="worker", limits=LIMITS
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("build", [_drive, _onedrive], ids=["google_drive", "microsoft365"])
async def test_missing_credential_is_one_auth_required_outcome_and_zero_retries(
    build: Any,
) -> None:
    credential = _BrokenCredential("credential_missing", expect_arrivals=2)
    store = InMemorySourceSyncStore()
    outcomes = await asyncio.gather(
        _run(build(credential), "did:a", store),
        _run(build(credential), "did:b", store),
        return_exceptions=True,
    )
    for outcome in outcomes:
        assert isinstance(outcome, SyncError)
        assert not isinstance(outcome, TransientSyncError)
        assert outcome.code == "auth_required"
    assert credential.bearer_calls == 2, "one credential ask per run: nothing was retried"
    state = await store.get_state("did:a", "conn")
    assert state.error_code == "auth_required"


@pytest.mark.asyncio
async def test_a_provider_outage_on_the_credential_exchange_still_retries() -> None:
    credential = _BrokenCredential("provider_unavailable")
    with pytest.raises(TransientSyncError):
        await _run(_drive(credential), "did:a", InMemorySourceSyncStore())
    assert credential.bearer_calls > 1, "a transient renewal failure is retried"


@pytest.mark.asyncio
async def test_a_503_from_google_still_retries() -> None:
    calls = 0

    def answer(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(503, json={"error": {"code": 503}})

    class Token:
        async def bearer(self) -> Secret:
            return Secret("t")

        async def invalidate(self) -> None:
            return None

    source = _drive(Token(), httpx.MockTransport(answer))
    with pytest.raises(TransientSyncError):
        await _run(source, "did:a", InMemorySourceSyncStore())
    assert calls > 1


_DRIVE_ENABLE_URL = (
    "https://console.developers.google.com/apis/api/drive.googleapis.com/overview?project=123"
)


def _api_disabled_answer(link: str) -> Any:
    """Google's real 403 for an API the project never enabled."""
    calls: list[httpx.Request] = []

    def answer(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(
            403,
            json={
                "error": {
                    "code": 403,
                    "message": (
                        "Google Drive API has not been used in project 123 before or it is "
                        f"disabled. Enable it by visiting {link} then retry. If you "
                        "enabled this API recently, wait a few minutes for the action to "
                        "propagate to our systems and retry."
                    ),
                    "status": "PERMISSION_DENIED",
                    "errors": [{"reason": "accessNotConfigured", "domain": "usageLimits"}],
                }
            },
        )

    answer.calls = calls  # type: ignore[attr-defined] # reason: test probe on a local function
    return answer


class _GoodToken:
    async def bearer(self) -> Secret:
        return Secret("t")

    async def invalidate(self) -> None:
        return None


@pytest.mark.asyncio
async def test_a_disabled_google_api_is_a_typed_needs_you_with_its_link_and_no_retry() -> None:
    answer = _api_disabled_answer(_DRIVE_ENABLE_URL)
    source = _drive(_GoodToken(), httpx.MockTransport(answer))

    with pytest.raises(SyncError) as caught:
        await _run(source, "did:a", InMemorySourceSyncStore())

    assert not isinstance(caught.value, TransientSyncError)
    assert caught.value.code == "api_disabled"
    assert caught.value.action_url == _DRIVE_ENABLE_URL
    assert len(answer.calls) == 1, "an API nobody enabled is not retried"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "link",
    [
        "https://evil.example.com/apis/api/drive.googleapis.com/overview?project=1",
        "https://console.developers.google.com.evil.example/overview",
        "http://console.developers.google.com/apis/api/drive.googleapis.com/overview",
        "javascript:alert(1)",
    ],
)
async def test_a_disabled_api_link_off_the_google_console_is_dropped(link: str) -> None:
    source = _drive(_GoodToken(), httpx.MockTransport(_api_disabled_answer(link)))

    with pytest.raises(SyncError) as caught:
        await _run(source, "did:a", InMemorySourceSyncStore())

    assert caught.value.code == "api_disabled"
    assert caught.value.action_url is None
