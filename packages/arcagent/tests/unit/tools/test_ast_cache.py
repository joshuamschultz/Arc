"""SPEC-021 Task 1.4 — AST validation cache (MD5+mtime).

The loader re-scans every reload, but most files don't change between
reloads. ``AstValidationCache.validate(path)`` short-circuits when the
file's md5 + mtime are unchanged so the AST visitor never runs again.

Cache invariants:
  * miss on first call → validate + store
  * hit on unchanged file → skip validation
  * mtime change → re-validate
  * content change (md5) → re-validate
  * validation failure → cache NOT polluted
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest


@pytest.fixture
def tmp_py(tmp_path: Path) -> Path:
    p = tmp_path / "tool.py"
    p.write_text("def add(a: int, b: int) -> int:\n    return a + b\n")
    return p


class TestAstValidationCacheMisses:
    def test_first_call_validates_and_stores(self, tmp_py: Path) -> None:
        from arcagent.tools._dynamic_loader import AstValidationCache

        cache = AstValidationCache()
        assert tmp_py not in cache
        cache.validate(tmp_py)
        assert tmp_py in cache

    def test_invalid_source_does_not_populate_cache(self, tmp_path: Path) -> None:
        from arcagent.tools._dynamic_loader import (
            AstValidationCache,
            ASTValidationError,
        )

        bad = tmp_path / "bad.py"
        bad.write_text("import os\n")  # blocked import
        cache = AstValidationCache()

        with pytest.raises(ASTValidationError):
            cache.validate(bad)
        assert bad not in cache


class TestAstValidationCacheHits:
    def test_second_call_skips_revalidation(
        self, tmp_py: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Second call with unchanged file must NOT re-invoke the visitor."""
        from arcagent.tools import _dynamic_loader

        call_count = {"n": 0}
        real_validator = _dynamic_loader.AstValidator

        class CountingValidator(real_validator):  # type: ignore[misc, valid-type]
            def validate(self, source: str) -> None:  # type: ignore[override]
                call_count["n"] += 1
                super().validate(source)

        monkeypatch.setattr(_dynamic_loader, "AstValidator", CountingValidator)

        cache = _dynamic_loader.AstValidationCache()
        cache.validate(tmp_py)
        cache.validate(tmp_py)  # cache hit — no second validation
        assert call_count["n"] == 1


class TestAstValidationCacheInvalidates:
    def test_mtime_change_triggers_revalidation(
        self, tmp_py: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from arcagent.tools import _dynamic_loader

        call_count = {"n": 0}
        real_validator = _dynamic_loader.AstValidator

        class CountingValidator(real_validator):  # type: ignore[misc, valid-type]
            def validate(self, source: str) -> None:  # type: ignore[override]
                call_count["n"] += 1
                super().validate(source)

        monkeypatch.setattr(_dynamic_loader, "AstValidator", CountingValidator)

        cache = _dynamic_loader.AstValidationCache()
        cache.validate(tmp_py)

        # Touch mtime forward by 1 second (filesystem resolution-safe).
        future = time.time() + 1
        os.utime(tmp_py, (future, future))

        cache.validate(tmp_py)
        assert call_count["n"] == 2

    def test_content_change_triggers_revalidation(
        self, tmp_py: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Same mtime but different content (rare; a script wrote bytes
        without changing mtime) — md5 difference re-validates."""
        from arcagent.tools import _dynamic_loader

        call_count = {"n": 0}
        real_validator = _dynamic_loader.AstValidator

        class CountingValidator(real_validator):  # type: ignore[misc, valid-type]
            def validate(self, source: str) -> None:  # type: ignore[override]
                call_count["n"] += 1
                super().validate(source)

        monkeypatch.setattr(_dynamic_loader, "AstValidator", CountingValidator)

        cache = _dynamic_loader.AstValidationCache()
        cache.validate(tmp_py)
        original_mtime = tmp_py.stat().st_mtime

        tmp_py.write_text("def sub(a: int, b: int) -> int:\n    return a - b\n")
        os.utime(tmp_py, (original_mtime, original_mtime))  # restore mtime

        cache.validate(tmp_py)
        assert call_count["n"] == 2

    def test_invalidate_drops_entry(self, tmp_py: Path) -> None:
        from arcagent.tools._dynamic_loader import AstValidationCache

        cache = AstValidationCache()
        cache.validate(tmp_py)
        cache.invalidate(tmp_py)
        assert tmp_py not in cache
