"""Tests for the VaultBackend Protocol and VaultUnreachable exception."""

from __future__ import annotations

import pytest

from arcagent.modules.vault.protocol import VaultBackend, VaultUnreachable


class _ConcreteBackend:
    """Minimal concrete implementation for runtime_checkable test."""

    async def get_secret(self, path: str) -> str | None:
        return "value"


class _MissingMethod:
    """Does NOT implement get_secret — should fail isinstance check."""

    def other_method(self) -> None:
        pass


def test_protocol_is_runtime_checkable_with_valid_impl() -> None:
    backend = _ConcreteBackend()
    assert isinstance(backend, VaultBackend)


def test_protocol_fails_isinstance_without_required_method() -> None:
    obj = _MissingMethod()
    assert not isinstance(obj, VaultBackend)


def test_vault_unreachable_is_exception() -> None:
    exc = VaultUnreachable("could not connect")
    assert isinstance(exc, Exception)
    assert "could not connect" in str(exc)


def test_vault_unreachable_can_be_raised_and_caught() -> None:
    with pytest.raises(VaultUnreachable, match="timeout"):
        raise VaultUnreachable("vault timeout")


def test_vault_unreachable_is_not_key_error() -> None:
    # VaultUnreachable signals connectivity failure, not missing secret
    exc = VaultUnreachable("down")
    assert not isinstance(exc, KeyError)
