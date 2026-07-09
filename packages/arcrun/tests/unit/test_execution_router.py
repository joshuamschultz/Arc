"""Unit tests for resolve_execution_backend — the single, pure tier→backend router.

Pure function: no host probe, no audit, no side effects. ``platform_supports_vm``
is injected so the decision is fully unit-testable and TOCTOU-free.
"""

from __future__ import annotations

import pytest

from arcrun.builtins.execute import (
    IsolationRelaxationError,
    IsolationUnavailableError,
    resolve_execution_backend,
)


class TestFederalRouting:
    def test_federal_routes_to_vm(self) -> None:
        assert resolve_execution_backend("federal", relax=None, platform_supports_vm=True) == "vm"

    def test_federal_refuses_without_vm_support(self) -> None:
        # Fail closed — never a silent downgrade to container/local (REQ-003/030).
        with pytest.raises(IsolationUnavailableError):
            resolve_execution_backend("federal", relax=None, platform_supports_vm=False)

    def test_federal_rejects_any_relax(self) -> None:
        # Federal cannot be relaxed below its floor by any config (REQ-021).
        with pytest.raises(IsolationRelaxationError):
            resolve_execution_backend("federal", relax="local", platform_supports_vm=True)


class TestEnterpriseRouting:
    def test_enterprise_routes_to_container(self) -> None:
        assert (
            resolve_execution_backend("enterprise", relax=None, platform_supports_vm=True)
            == "docker"
        )

    def test_enterprise_container_relax_is_noop(self) -> None:
        assert (
            resolve_execution_backend("enterprise", relax="container", platform_supports_vm=True)
            == "docker"
        )

    @pytest.mark.parametrize("relax", ["local", "off", "none"])
    def test_enterprise_rejects_below_floor_relax(self, relax: str) -> None:
        with pytest.raises(IsolationRelaxationError):
            resolve_execution_backend("enterprise", relax=relax, platform_supports_vm=True)

    def test_enterprise_no_kvm_still_container(self) -> None:
        # Container is the enterprise floor; KVM absence is irrelevant, no refusal.
        assert (
            resolve_execution_backend("enterprise", relax=None, platform_supports_vm=False)
            == "docker"
        )


class TestPersonalRouting:
    def test_personal_defaults_to_container(self) -> None:
        assert (
            resolve_execution_backend("personal", relax=None, platform_supports_vm=True) == "docker"
        )

    def test_personal_container_relax(self) -> None:
        assert (
            resolve_execution_backend("personal", relax="container", platform_supports_vm=True)
            == "docker"
        )

    @pytest.mark.parametrize("relax", ["off", "none", "local"])
    def test_personal_off_routes_to_local(self, relax: str) -> None:
        # Sandbox OFF is a first-class personal mode (REQ-020) → LocalBackend.
        assert (
            resolve_execution_backend("personal", relax=relax, platform_supports_vm=True) == "local"
        )

    def test_personal_unknown_relax_rejected(self) -> None:
        with pytest.raises(IsolationRelaxationError):
            resolve_execution_backend("personal", relax="wormhole", platform_supports_vm=True)


class TestRouterPurity:
    def test_unknown_tier_raises(self) -> None:
        with pytest.raises(ValueError, match="tier"):
            resolve_execution_backend("galactic", relax=None, platform_supports_vm=True)

    def test_tier_is_case_insensitive(self) -> None:
        assert (
            resolve_execution_backend("PERSONAL", relax="OFF", platform_supports_vm=True) == "local"
        )
