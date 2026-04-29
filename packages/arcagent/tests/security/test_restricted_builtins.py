"""SPEC-017 R-054 — restricted builtins dict.

Dynamic tools execute with a scrubbed ``__builtins__`` dict. Anything
not on the allowlist raises ``NameError`` at runtime. Prevents
``__import__('os')`` and similar bypasses after AST validation.
"""

from __future__ import annotations

import pytest


class TestAllowedBuiltins:
    """Safe primitives remain available."""

    def test_len_available(self) -> None:
        from arcagent.tools._dynamic_loader import RESTRICTED_BUILTINS

        assert "len" in RESTRICTED_BUILTINS
        assert RESTRICTED_BUILTINS["len"] is len

    def test_common_safe_builtins_present(self) -> None:
        from arcagent.tools._dynamic_loader import RESTRICTED_BUILTINS

        for name in (
            "print",
            "len",
            "range",
            "str",
            "int",
            "float",
            "bool",
            "list",
            "dict",
            "set",
            "tuple",
            "sorted",
            "min",
            "max",
            "sum",
            "enumerate",
            "zip",
            "map",
            "filter",
            "any",
            "all",
            "isinstance",
        ):
            assert name in RESTRICTED_BUILTINS, f"missing safe builtin {name!r}"


class TestBlockedBuiltins:
    """Dangerous builtins must not be reachable."""

    def test_import_not_available(self) -> None:
        from arcagent.tools._dynamic_loader import RESTRICTED_BUILTINS

        assert "__import__" not in RESTRICTED_BUILTINS
        assert "eval" not in RESTRICTED_BUILTINS
        assert "exec" not in RESTRICTED_BUILTINS
        assert "compile" not in RESTRICTED_BUILTINS
        assert "open" not in RESTRICTED_BUILTINS

    def test_runtime_name_error_for_blocked(self) -> None:
        """Executing code that references a blocked builtin fails at runtime."""
        from arcagent.tools._dynamic_loader import RESTRICTED_BUILTINS

        ns: dict[str, object] = {"__builtins__": RESTRICTED_BUILTINS}
        src = compile("x = __import__('os')", "<test>", "exec")
        with pytest.raises(NameError):
            exec(src, ns)  # noqa: S102 — this is the test subject


class TestRestrictedImportAtRuntime:
    """SPEC-017 defense-in-depth — even if the AST validator is bypassed,
    the loader's wrapped ``__import__`` refuses non-allowlist modules."""

    def test_allowlist_imports_succeed(self) -> None:
        """The decorator module stays importable — it's the hook tools use."""
        from arcagent.tools._dynamic_loader import DynamicToolLoader

        loader = DynamicToolLoader()
        src = (
            "from arcagent.tools._decorator import tool\n"
            "\n"
            "@tool(description='ok', classification='read_only')\n"
            "async def ok() -> str:\n"
            "    return 'ok'\n"
        )
        registered = loader.load(src, name="ok")
        assert registered.name == "ok"

    def test_runtime_os_import_blocked_even_if_ast_validator_missed_it(
        self,
    ) -> None:
        """Regression guard: construct a namespace directly (bypass AST)
        and verify the wrapped ``__import__`` refuses."""
        from arcagent.tools._dynamic_loader import (
            RESTRICTED_BUILTINS,
            ASTValidationError,
            _make_restricted_import,
        )

        ns: dict[str, object] = {
            "__builtins__": {
                **RESTRICTED_BUILTINS,
                "__import__": _make_restricted_import(),
            },
        }
        src = compile("x = __import__('os')", "<test>", "exec")
        with pytest.raises(ASTValidationError):
            exec(src, ns)  # noqa: S102 — adversarial test

    def test_runtime_subprocess_import_blocked(self) -> None:
        from arcagent.tools._dynamic_loader import (
            RESTRICTED_BUILTINS,
            ASTValidationError,
            _make_restricted_import,
        )

        ns: dict[str, object] = {
            "__builtins__": {
                **RESTRICTED_BUILTINS,
                "__import__": _make_restricted_import(),
            },
        }
        src = compile("x = __import__('subprocess')", "<test>", "exec")
        with pytest.raises(ASTValidationError):
            exec(src, ns)  # noqa: S102
