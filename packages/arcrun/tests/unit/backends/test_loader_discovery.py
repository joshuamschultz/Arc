"""Tests for load_backend() — 3-tier federal-aware discovery.

Covers:
- built-in lookup (local, docker)
- dotted config import path
- FederalBackendPolicyError when entry_points attempted at federal tier
- Signed manifest required at federal tier (SPEC-018 HIGH-3 fail-closed)
"""

from __future__ import annotations

import pytest

from arcrun.backends import (
    DockerBackend,
    ExecutorBackend,
    LocalBackend,
    load_backend,
)
from arcrun.backends.loader import (
    BackendSignatureError,
    FederalBackendPolicyError,
    _enforce_federal_manifest,
)


class TestBuiltinLookup:
    def test_load_local_builtin(self) -> None:
        backend = load_backend("local")
        assert isinstance(backend, LocalBackend)
        assert isinstance(backend, ExecutorBackend)

    def test_load_docker_builtin(self) -> None:
        backend = load_backend("docker")
        assert isinstance(backend, DockerBackend)
        assert isinstance(backend, ExecutorBackend)

    def test_builtin_allowed_at_all_tiers(self) -> None:
        for tier in ("personal", "enterprise", "federal"):
            b = load_backend("local", tier=tier)
            assert isinstance(b, LocalBackend)


class TestDottedPathLookup:
    def test_load_via_dotted_path_requires_manifest(self) -> None:
        """Phase C: dotted-path backends now require a signed manifest at all tiers.

        Pre-Phase-C this test verified a dotted path loaded without a manifest.
        Post-Phase-C the manifest gate fires before the import, so the test
        instead asserts BackendSignatureError is raised when manifest is absent.
        """
        with pytest.raises(BackendSignatureError, match="signed manifest"):
            load_backend("_arcrun_test_fake_backend:FakeBackend")

    def test_dotted_path_missing_module_raises_manifest_gate_first(self) -> None:
        """Phase C: manifest gate fires before import, so BackendSignatureError
        is raised instead of ValueError('Cannot import backend module').
        """
        with pytest.raises(BackendSignatureError, match="signed manifest"):
            load_backend("nonexistent_module_xyz:SomeClass")


class TestFederalPolicyGate:
    def test_entry_points_disabled_at_federal_tier(self) -> None:
        """Attempting entry_points at federal raises FederalBackendPolicyError."""
        # "unknown_backend" is not built-in; without ':' it would go to entry_points
        with pytest.raises(FederalBackendPolicyError):
            load_backend("unknown_backend", tier="federal")

    def test_entry_points_disabled_at_personal_tier(self) -> None:
        """Phase C: entry-points are permanently disabled at personal tier too.

        Pre-Phase-C this test expected ValueError after entry-point discovery
        returned None.  Post-Phase-C short aliases at non-federal tiers raise
        FederalBackendPolicyError (same as federal) because entry-points are
        disabled unconditionally.
        """
        with pytest.raises(FederalBackendPolicyError):
            load_backend("nonexistent_personal_backend", tier="personal")

    def test_federal_manifest_required_for_dotted_path_at_federal_no_manifest(self) -> None:
        """Dotted-path backend at federal tier with no manifest → hard fail (SPEC-018 HIGH-3).

        Phase C: all tiers now require a manifest.  The error message reflects
        the universal requirement rather than calling out federal specifically.
        """
        with pytest.raises(BackendSignatureError, match="signed manifest"):
            load_backend(
                "somepackage:SomeBackend",
                tier="federal",
                manifest_path=None,
                allowed_backends=None,
            )

    def test_federal_unsigned_dict_also_hard_fails(self) -> None:
        """Federal tier: unsigned allowed_backends dict is NOT accepted (SPEC-018 HIGH-3).

        Phase C: manifest_path=None triggers BackendSignatureError regardless
        of the allowed_backends dict content.
        """
        with pytest.raises(BackendSignatureError, match="signed manifest"):
            load_backend(
                "somepackage:SomeBackend",
                tier="federal",
                allowed_backends={"somepackage:SomeBackend": "somepackage:SomeBackend"},
                manifest_path=None,
            )

    def test_federal_unsigned_dict_with_unlisted_also_hard_fails(self) -> None:
        """Even with an unsigned dict, federal tier must fail if no signed manifest.

        Verifies that the unlisted-backend path is never reached — the manifest
        requirement fires first.
        """
        with pytest.raises(BackendSignatureError, match="signed manifest"):
            load_backend(
                "somepackage:SomeBackend",
                tier="federal",
                allowed_backends={"otherpackage": "other:Other"},
                manifest_path=None,
            )


class TestFederalManifestDirectly:
    def test_no_manifest_raises(self) -> None:
        with pytest.raises(BackendSignatureError):
            _enforce_federal_manifest("mybackend", allowed_backends=None)

    def test_name_not_in_manifest_raises(self) -> None:
        with pytest.raises(BackendSignatureError):
            _enforce_federal_manifest(
                "mybackend", allowed_backends={"other": "other:Other"}
            )

    def test_name_in_manifest_passes(self) -> None:
        # Should not raise — the helper itself still works for non-federal paths
        _enforce_federal_manifest(
            "mybackend", allowed_backends={"mybackend": "pkg:Cls"}
        )
