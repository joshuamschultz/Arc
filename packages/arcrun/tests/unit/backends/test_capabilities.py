"""Tests for BackendCapabilities field values and constraints."""

from __future__ import annotations

import pytest

from arcrun.backends import BackendCapabilities, DockerBackend, LocalBackend


class TestLocalCapabilities:
    def test_isolation_is_none(self) -> None:
        caps = LocalBackend().capabilities
        assert caps.isolation == "none"

    def test_cold_start_budget_is_small(self) -> None:
        caps = LocalBackend().capabilities
        # Local backend should cold-start in ≪10ms.
        assert caps.cold_start_budget_ms <= 10

    def test_supports_bind_mount(self) -> None:
        assert LocalBackend().capabilities.supports_bind_mount is True

    def test_does_not_support_persistent_workspace(self) -> None:
        assert LocalBackend().capabilities.supports_persistent_workspace is False

    def test_max_stdout_default(self) -> None:
        caps = LocalBackend().capabilities
        assert caps.max_stdout_bytes == 64 * 1024

    def test_custom_max_stdout(self) -> None:
        backend = LocalBackend(max_stdout_bytes=128 * 1024)
        assert backend.capabilities.max_stdout_bytes == 128 * 1024


class TestDockerCapabilities:
    def test_isolation_is_container(self) -> None:
        caps = DockerBackend().capabilities
        assert caps.isolation == "container"

    def test_cold_start_budget_is_larger(self) -> None:
        caps = DockerBackend().capabilities
        # Docker needs ≥800ms for first container start.
        assert caps.cold_start_budget_ms >= 800

    def test_supports_persistent_workspace(self) -> None:
        assert DockerBackend().capabilities.supports_persistent_workspace is True

    def test_supports_bind_mount(self) -> None:
        assert DockerBackend().capabilities.supports_bind_mount is True


class TestCapabilitiesValidation:
    def test_isolation_enum_values(self) -> None:
        for val in ("none", "container", "vm", "remote"):
            caps = BackendCapabilities(isolation=val)  # type: ignore[arg-type]
            assert caps.isolation == val

    def test_invalid_isolation_raises(self) -> None:
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            BackendCapabilities(isolation="hypervisor")  # type: ignore[arg-type]

    def test_defaults_are_sane(self) -> None:
        caps = BackendCapabilities()
        assert caps.cold_start_budget_ms >= 0
        assert caps.max_stdout_bytes > 0
        assert caps.isolation == "none"
