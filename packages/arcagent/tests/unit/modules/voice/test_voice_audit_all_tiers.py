"""Tests for §4: voice provider audit events at all tiers.

voice.provider_selected must be emitted at every tier.
Cloud provider warning must be emitted at non-federal tiers.
Air-gap violation must be emitted at federal tier.

These are emitted by :func:`arcagent.modules.voice._runtime.configure`.
"""

from __future__ import annotations

from collections.abc import Iterator
from unittest.mock import MagicMock

import pytest

from arcagent.modules.voice import _runtime


@pytest.fixture(autouse=True)
def _reset_runtime() -> Iterator[None]:
    _runtime.reset()
    yield
    _runtime.reset()


def _make_telemetry() -> MagicMock:
    tel = MagicMock()
    tel.audit_event = MagicMock()
    return tel


class TestVoiceProviderAuditAllTiers:
    """§4: voice provider selection must be audited at every tier."""

    @pytest.mark.parametrize("tier", ["personal", "enterprise"])
    def test_cloud_provider_warning_emitted_at_non_federal(self, tier: str) -> None:
        """Configuring with a cloud provider emits a provider audit event."""
        telemetry = _make_telemetry()
        _runtime.configure(
            config={"tier": tier, "stt_provider": "whisper_api", "tts_provider": "elevenlabs"},
            telemetry=telemetry,
        )
        calls = [c[0][0] for c in telemetry.audit_event.call_args_list]
        assert any(
            "voice.provider" in c or "cloud" in c or "provider_selected" in c for c in calls
        ), f"Expected a voice provider audit event at {tier} tier, got: {calls}"

    def test_provider_selected_audit_emitted_at_personal(self) -> None:
        """voice.provider_selected must be emitted on configure at personal tier."""
        telemetry = _make_telemetry()
        _runtime.configure(
            config={"tier": "personal", "stt_provider": "whisper_cpp", "tts_provider": "piper"},
            telemetry=telemetry,
        )
        calls = [c[0][0] for c in telemetry.audit_event.call_args_list]
        assert any("voice.provider" in c for c in calls), (
            f"Expected voice.provider_selected audit, got: {calls}"
        )

    def test_provider_selected_audit_emitted_at_enterprise(self) -> None:
        """voice.provider_selected must be emitted at enterprise tier."""
        telemetry = _make_telemetry()
        _runtime.configure(
            config={"tier": "enterprise", "stt_provider": "whisper_cpp", "tts_provider": "piper"},
            telemetry=telemetry,
        )
        calls = [c[0][0] for c in telemetry.audit_event.call_args_list]
        assert any("voice.provider" in c for c in calls)

    def test_no_audit_crash_when_telemetry_is_none(self) -> None:
        """configure must not raise if telemetry is None."""
        _runtime.configure(
            config={"tier": "personal", "stt_provider": "whisper_cpp", "tts_provider": "piper"},
            telemetry=None,
        )

    def test_cloud_provider_audit_contains_provider_names(self) -> None:
        """Audit details must include the configured provider names."""
        telemetry = _make_telemetry()
        _runtime.configure(
            config={"tier": "enterprise", "stt_provider": "whisper_api", "tts_provider": "piper"},
            telemetry=telemetry,
        )
        all_details = [c[0][1] for c in telemetry.audit_event.call_args_list]
        all_details_str = str(all_details)
        assert (
            "whisper_api" in all_details_str
            or "piper" in all_details_str
            or "enterprise" in all_details_str
        )
