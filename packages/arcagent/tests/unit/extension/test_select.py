"""SPEC-047 — the generalized select-one mechanism (``select_extension``).

This is the single home of the choice dispatch + BYO refuse-before-import gate +
dotted-path importer that ``brain/select.py`` and ``skilladapt/select.py`` used to
duplicate. The dispatch table is exercised here directly against a synthetic
``ExtensionPoint`` so the mechanism is proven independently of either seam.
"""

from __future__ import annotations

import importlib
import logging
import types
from typing import Any

import pytest

from arcagent.extension import ExtensionPoint, select_extension

_logger = logging.getLogger("test.extension.select")


class _Null:
    """Stand-in Null default."""


class _Builtin:
    """Stand-in builtin instance built from an imported module."""

    def __init__(self, module: Any) -> None:
        self.module = module


class _Byo:
    """Stand-in BYO class: constructed ``cls(ctx["workspace"])``."""

    def __init__(self, workspace: str) -> None:
        self.workspace = workspace


def _build_builtin(module: Any, context: dict[str, Any]) -> Any | None:
    """Builder that returns an instance unless the module signals unavailability."""
    if getattr(module, "_degrade", False):
        return None
    return _Builtin(module)


_POINT = ExtensionPoint(
    name="thing",
    null_factory=_Null,
    builtin_modules={"builtin": "builtin_mod", "auto": "builtin_mod"},
    builtin_builder=_build_builtin,
    byo_constructor=lambda cls, ctx: cls(ctx["workspace"]),
)


def _patch_import(monkeypatch: pytest.MonkeyPatch, *, degrade: bool = False) -> dict[str, int]:
    """Make any import name resolve to a module carrying ``_Byo``; count imports."""
    calls = {"imports": 0}
    mod = types.ModuleType("stub_mod")
    mod._Byo = _Byo  # type: ignore[attr-defined]
    mod._degrade = degrade  # type: ignore[attr-defined]

    def fake_import(name: str) -> types.ModuleType:
        calls["imports"] += 1
        return mod

    monkeypatch.setattr(importlib, "import_module", fake_import)
    return calls


@pytest.mark.parametrize("setting", ["none", "", "null", "  none  "])
def test_none_family_selects_null(setting: str) -> None:
    ctx: dict[str, Any] = {"workspace": "/ws"}
    got = select_extension(
        _POINT, setting, tier="personal", allowlist=(), context=ctx, logger=_logger
    )
    assert isinstance(got, _Null)


def test_builtin_name_builds_builtin(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_import(monkeypatch)
    got = select_extension(
        _POINT,
        "builtin",
        tier="personal",
        allowlist=(),
        context={"workspace": "/ws"},
        logger=_logger,
    )
    assert isinstance(got, _Builtin)


def test_builtin_not_importable_degrades_to_null(monkeypatch: pytest.MonkeyPatch) -> None:
    def raising_import(name: str) -> types.ModuleType:
        raise ImportError(name)

    monkeypatch.setattr(importlib, "import_module", raising_import)
    got = select_extension(
        _POINT,
        "builtin",
        tier="personal",
        allowlist=(),
        context={"workspace": "/ws"},
        logger=_logger,
    )
    assert isinstance(got, _Null)


def test_auto_degrades_silently_when_unavailable(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    _patch_import(monkeypatch, degrade=True)
    with caplog.at_level(logging.WARNING, logger="test.extension.select"):
        got = select_extension(
            _POINT,
            "auto",
            tier="personal",
            allowlist=(),
            context={"workspace": "/ws"},
            logger=_logger,
        )
    assert isinstance(got, _Null)
    assert caplog.records == [], "auto must degrade silently — no warning"


def test_explicit_builtin_warns_when_unavailable(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    _patch_import(monkeypatch, degrade=True)
    with caplog.at_level(logging.WARNING, logger="test.extension.select"):
        got = select_extension(
            _POINT,
            "builtin",
            tier="personal",
            allowlist=(),
            context={"workspace": "/ws"},
            logger=_logger,
        )
    assert isinstance(got, _Null)
    assert caplog.records, "explicit builtin must warn when it degrades"


def test_byo_loads_at_personal(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _patch_import(monkeypatch)
    got = select_extension(
        _POINT,
        "stub_mod:_Byo",
        tier="personal",
        allowlist=(),
        context={"workspace": "/ws"},
        logger=_logger,
    )
    assert isinstance(got, _Byo)
    assert got.workspace == "/ws"
    assert calls["imports"] == 1


@pytest.mark.parametrize("tier", ["enterprise", "federal"])
def test_byo_refused_before_import_above_personal(
    monkeypatch: pytest.MonkeyPatch, tier: str
) -> None:
    calls = _patch_import(monkeypatch)
    with pytest.raises(ValueError, match="allowlist"):
        select_extension(
            _POINT,
            "stub_mod:_Byo",
            tier=tier,
            allowlist=(),
            context={"workspace": "/ws"},
            logger=_logger,
        )
    assert calls["imports"] == 0, "must fail closed BEFORE importing an unverified class-path"


@pytest.mark.parametrize("tier", ["enterprise", "federal"])
def test_byo_allowed_when_allowlisted_above_personal(
    monkeypatch: pytest.MonkeyPatch, tier: str
) -> None:
    _patch_import(monkeypatch)
    got = select_extension(
        _POINT,
        "stub_mod:_Byo",
        tier=tier,
        allowlist=("stub_mod:_Byo",),
        context={"workspace": "/ws"},
        logger=_logger,
    )
    assert isinstance(got, _Byo)


# -- generic provider-entrypoint resolution (Brain's decoupled path) --------


class _Provider:
    """Stand-in provider instance a backend module's ``build_it(context)`` returns."""

    def __init__(self, context: dict[str, Any]) -> None:
        self.context = context


_PROVIDER_POINT = ExtensionPoint(
    name="provided",
    null_factory=_Null,
    byo_constructor=lambda cls, ctx: cls(ctx["workspace"]),
    provider_entrypoint="build_it",
)


def _patch_provider_import(
    monkeypatch: pytest.MonkeyPatch, *, entrypoint: bool = True
) -> dict[str, int]:
    calls = {"imports": 0}
    mod = types.ModuleType("backend_pkg")
    if entrypoint:
        mod.build_it = lambda context: _Provider(context)  # type: ignore[attr-defined]

    def fake_import(name: str) -> types.ModuleType:
        calls["imports"] += 1
        return mod

    monkeypatch.setattr(importlib, "import_module", fake_import)
    return calls


@pytest.mark.parametrize("tier", ["personal", "enterprise", "federal"])
def test_provider_bare_name_builds_via_entrypoint_ungated(
    monkeypatch: pytest.MonkeyPatch, tier: str
) -> None:
    """A bare backend name imports + calls the entrypoint at EVERY tier (ungated, like a
    builtin) — this keeps ``brain='arcmemory'`` working without a hardcoded builtin."""
    _patch_provider_import(monkeypatch)
    got = select_extension(
        _PROVIDER_POINT,
        "backend_pkg",
        tier=tier,
        allowlist=(),
        context={"workspace": "/ws"},
        logger=_logger,
    )
    assert isinstance(got, _Provider)
    assert got.context["workspace"] == "/ws"


def test_provider_missing_package_degrades_with_warning(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    def raising_import(name: str) -> types.ModuleType:
        raise ImportError(name)

    monkeypatch.setattr(importlib, "import_module", raising_import)
    with caplog.at_level(logging.WARNING, logger="test.extension.select"):
        got = select_extension(
            _PROVIDER_POINT,
            "backend_pkg",
            tier="personal",
            allowlist=(),
            context={"workspace": "/ws"},
            logger=_logger,
        )
    assert isinstance(got, _Null)
    assert caplog.records, "an uninstalled provider must warn as it degrades"


def test_provider_without_entrypoint_degrades_to_null(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_provider_import(monkeypatch, entrypoint=False)
    got = select_extension(
        _PROVIDER_POINT,
        "backend_pkg",
        tier="personal",
        allowlist=(),
        context={"workspace": "/ws"},
        logger=_logger,
    )
    assert isinstance(got, _Null)


def test_provider_colon_path_is_still_byo_and_gated(monkeypatch: pytest.MonkeyPatch) -> None:
    """A colon class-path stays BYO — allowlist-gated above personal — even on a
    provider point (the two resolution modes coexist)."""
    calls = _patch_import(monkeypatch)
    with pytest.raises(ValueError, match="allowlist"):
        select_extension(
            _PROVIDER_POINT,
            "stub_mod:_Byo",
            tier="federal",
            allowlist=(),
            context={"workspace": "/ws"},
            logger=_logger,
        )
    assert calls["imports"] == 0
