"""Tests for load_backend() — 3-tier federal-aware discovery.

Covers:
- built-in lookup (local, docker)
- dotted config import path
- FederalBackendPolicyError when entry_points attempted at federal tier
- Signed manifest required at federal tier (SPEC-018 HIGH-3 fail-closed)
"""

from __future__ import annotations

import sys
import types
from unittest.mock import MagicMock, patch

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
    def test_load_via_dotted_path(self) -> None:
        """Inject a fake module and class, then load via dotted path."""
        from typing import AsyncIterator

        from arcrun.backends.base import BackendCapabilities, ExecHandle

        class FakeBackend:
            name = "fake"
            capabilities = BackendCapabilities()

            async def run(  # type: ignore[override]
                self, cmd: str, **kw: object
            ) -> ExecHandle:
                return ExecHandle(handle_id="x", backend_name="fake")

            async def stream(  # type: ignore[override]
                self, h: ExecHandle
            ) -> AsyncIterator[bytes]:
                yield b""

            async def cancel(  # type: ignore[override]
                self, h: ExecHandle, *, grace: float = 5.0
            ) -> None:
                pass

            async def close(self) -> None:
                pass

        # Inject fake module
        fake_mod = types.ModuleType("_arcrun_test_fake_backend")
        fake_mod.FakeBackend = FakeBackend  # type: ignore[attr-defined]
        sys.modules["_arcrun_test_fake_backend"] = fake_mod

        try:
            backend = load_backend("_arcrun_test_fake_backend:FakeBackend")
            assert isinstance(backend, ExecutorBackend)
        finally:
            del sys.modules["_arcrun_test_fake_backend"]

    def test_dotted_path_non_conforming_raises(self) -> None:
        """A class that doesn't implement the Protocol raises ValueError."""
        fake_mod = types.ModuleType("_arcrun_bad_backend")

        class NotABackend:
            pass

        fake_mod.NotABackend = NotABackend  # type: ignore[attr-defined]
        sys.modules["_arcrun_bad_backend"] = fake_mod

        try:
            with pytest.raises(ValueError, match="does not implement ExecutorBackend"):
                load_backend("_arcrun_bad_backend:NotABackend")
        finally:
            del sys.modules["_arcrun_bad_backend"]

    def test_dotted_path_missing_module_raises(self) -> None:
        with pytest.raises(ValueError, match="Cannot import backend module"):
            load_backend("nonexistent_module_xyz:SomeClass")


class TestFederalPolicyGate:
    def test_entry_points_disabled_at_federal_tier(self) -> None:
        """Attempting entry_points at federal raises FederalBackendPolicyError."""
        # "unknown_backend" is not built-in; without ':' it would go to entry_points
        with pytest.raises(FederalBackendPolicyError):
            load_backend("unknown_backend", tier="federal")

    def test_entry_points_allowed_at_personal_tier(self) -> None:
        """At personal tier unknown backends attempt entry_points, then raise ValueError."""
        # Entry_points discovery returns None for unknown names; raises ValueError
        with pytest.raises(ValueError):
            load_backend("nonexistent_personal_backend", tier="personal")

    def test_federal_manifest_required_for_dotted_path_at_federal_no_manifest(self) -> None:
        """Dotted-path backend at federal tier with no manifest → hard fail (SPEC-018 HIGH-3).

        Federal tier is fail-closed: both manifest_path=None and allowed_backends=None
        raise BackendSignatureError with the 'signed manifest' message.
        """
        with pytest.raises(BackendSignatureError, match="Federal tier requires signed manifest"):
            load_backend(
                "somepackage:SomeBackend",
                tier="federal",
                manifest_path=None,
                allowed_backends=None,
            )

    def test_federal_unsigned_dict_also_hard_fails(self) -> None:
        """Federal tier: unsigned allowed_backends dict is NOT accepted (SPEC-018 HIGH-3).

        The old warning-and-proceed branch has been removed.  Passing an unsigned
        dict at federal tier now raises BackendSignatureError immediately.
        """
        with pytest.raises(BackendSignatureError, match="Federal tier requires signed manifest"):
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
        with pytest.raises(BackendSignatureError, match="Federal tier requires signed manifest"):
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
