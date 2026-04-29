"""SPEC-021 Task 1.7 — OS sandbox wrapper.

Personal tier returns ``None`` (no sandbox). Enterprise / federal
return a platform-appropriate :class:`OsSandbox` —
:class:`SandboxExecSandbox` on macOS, :class:`SeccompSandbox` on
Linux. Other platforms raise ``NotImplementedError``.

The sandbox runs an arbitrary Python source string with file-system
access scoped to ``scope_path``. Anything outside the scope (or any
syscall outside the seccomp/sandbox-exec policy) raises a
``SandboxViolationError``.

For Phase 1 this exercises the factory routing and structural
contract. Full syscall-level integration tests (ctypes/CDLL escape
attempts) live in tests/security and tests/integration; they are
opt-in via env var because they require platform-specific binaries.
"""

from __future__ import annotations

import sys

import pytest

from arcagent.core.tier import Tier


class TestMakeSandbox:
    def test_personal_returns_none(self) -> None:
        from arcagent.core.os_sandbox import make_sandbox

        assert make_sandbox(Tier.PERSONAL) is None

    def test_enterprise_darwin_returns_sandboxexec(self) -> None:
        if sys.platform != "darwin":
            pytest.skip("darwin-only path")
        from arcagent.core.os_sandbox import (
            SandboxExecSandbox,
            make_sandbox,
        )

        sb = make_sandbox(Tier.ENTERPRISE)
        assert isinstance(sb, SandboxExecSandbox)

    def test_federal_routes_same_as_enterprise(self) -> None:
        from arcagent.core.os_sandbox import make_sandbox

        sb = make_sandbox(Tier.FEDERAL)
        assert sb is not None  # platform-specific impl

    def test_unknown_platform_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from arcagent.core import os_sandbox

        monkeypatch.setattr(os_sandbox, "_PLATFORM", "plan9")
        with pytest.raises(NotImplementedError, match="plan9"):
            os_sandbox.make_sandbox(Tier.ENTERPRISE)


class TestSandboxExecBuildProfile:
    def test_profile_denies_default_allows_scope(self) -> None:
        if sys.platform != "darwin":
            pytest.skip("darwin-only")
        from pathlib import Path

        from arcagent.core.os_sandbox import build_sandbox_exec_profile

        profile = build_sandbox_exec_profile(scope_path=Path("/tmp/scope"))
        assert "(version 1)" in profile
        assert "(deny default)" in profile
        # Allow file-read* under scope.
        assert "/tmp/scope" in profile


class TestSandboxViolationErrorPropagation:
    def test_violation_carries_category(self) -> None:
        from arcagent.core.os_sandbox import SandboxViolationError

        err = SandboxViolationError(category="syscall:execve", detail="execve outside policy")
        assert err.category == "syscall:execve"
        assert "execve" in str(err)
