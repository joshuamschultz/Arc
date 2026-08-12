"""Broker absent: the messaging surface says unavailable, loudly (T-943 / REQ-307).

The route half of REQ-307 is already guarded in ``test_embedded_messaging.py``
(``TestExplicitUnavailable``): with no service wired, ``/api/team/channels``
answers 503 ``team_messaging_unavailable`` rather than ``{"channels": []}``.
This module covers the two things that guard does not:

* the **log**. REQ-307 says the failure is reported loudly. Today the only trace
  of an unreachable broker is a ``warning`` inside ``_connect_backend``; the
  builder that decides the whole deployment has no messaging says nothing at
  all. An operator scanning for errors sees a clean log and an empty inbox.
* the **other side of the distinction**. An unavailable surface is only
  meaningful if a healthy-but-empty surface looks different. A wired service
  holding zero channels must answer 200 with an empty list, so "nothing to
  show" and "cannot see" are two answers, not one.

No NATS. ``_connect_backend`` is the established monkeypatch seam and stands in
for an unreachable broker by returning ``None``, exactly as the live code does
when the port refuses.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from arcteam.audit import AuditLogger
from arcteam.registry import EntityRegistry
from arcteam.storage import MemoryBackend
from arctrust.signer import InProcessSigner
from starlette.testclient import TestClient

import arcui.messaging as messaging
from arcui.auth import AuthConfig
from arcui.server import create_app

_SIGNER_SEED = b"\x11" * 32


async def _empty_backend() -> MemoryBackend:
    """A healthy backend with an initialised audit chain and no channels."""
    backend = MemoryBackend()
    audit = AuditLogger(backend, InProcessSigner(_SIGNER_SEED))
    await audit.initialize()
    EntityRegistry(backend, audit)
    return backend


def _app(tmp_path: Path) -> Any:
    team_root = tmp_path / "team"
    team_root.mkdir()
    auth = AuthConfig({"viewer_token": "viewer-tok", "operator_token": "operator-tok"})
    return create_app(
        auth_config=auth,
        team_root=team_root,
        data_dir=tmp_path / "data",
    )


class TestLoudLog:
    async def test_unreachable_broker_is_logged_loudly(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """An ERROR record naming messaging as unavailable, from the builder.

        Degrading to ``(None, None, None)`` in silence is what makes the empty
        inbox believable: the routes report it, but nothing in the log tells the
        operator their messaging is down.
        """
        monkeypatch.setattr(messaging, "_operator_signer", lambda: InProcessSigner(_SIGNER_SEED))

        async def _broker_down() -> None:
            return None

        monkeypatch.setattr(messaging, "_connect_backend", _broker_down)

        with caplog.at_level("ERROR"):
            service, registry, backend = await messaging.build_messaging_service()

        assert (service, registry, backend) == (None, None, None)
        loud = [r for r in caplog.records if r.levelno >= 40]
        assert loud, "an unreachable broker produced no ERROR record"
        assert any("unavailable" in r.getMessage().lower() for r in loud)

    async def test_missing_audit_authority_is_logged_loudly(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The other degrade path lands the operator in the same blind spot."""
        monkeypatch.setattr(messaging, "_operator_signer", lambda: None)

        with caplog.at_level("ERROR"):
            service, registry, backend = await messaging.build_messaging_service()

        assert (service, registry, backend) == (None, None, None)
        loud = [r for r in caplog.records if r.levelno >= 40]
        assert loud, "a missing audit authority produced no ERROR record"
        assert any("unavailable" in r.getMessage().lower() for r in loud)


class TestEmptyIsNotUnavailable:
    def test_wired_service_with_no_channels_answers_200_and_an_empty_list(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Healthy and empty must not look like unreachable."""

        async def _fake_connect() -> MemoryBackend:
            return backend

        backend = MemoryBackend()
        monkeypatch.setattr(messaging, "_operator_signer", lambda: InProcessSigner(_SIGNER_SEED))
        monkeypatch.setattr(messaging, "_connect_backend", _fake_connect)

        with TestClient(_app(tmp_path)) as client:
            resp = client.get(
                "/api/team/channels",
                headers={"Authorization": "Bearer viewer-tok"},
            )

        assert resp.status_code == 200
        assert resp.json() == {"channels": []}
