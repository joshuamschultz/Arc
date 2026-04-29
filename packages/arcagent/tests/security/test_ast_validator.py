"""SPEC-017 R-053 adversarial tests — AST validator rejects 9 bypass classes.

Every test demonstrates a known Python sandbox-escape technique and
asserts the validator refuses to load the offending source. These
are not theoretical threats — they correspond to real CVEs against
RestrictedPython and similar sandboxes (see SDD §5 for citations).

The rejection categories (R-053):
  1. Imports of privileged modules (ctypes, subprocess, socket, os, sys, pickle, marshal, shelve)
  2. Attribute access on frame / class internals (gi_frame, f_back, __subclasses__, __class__, __bases__, etc.)
  3. Calls to dynamic loaders (compile, eval, exec, __import__, getattr/setattr against builtins)
  4. Subscripting sys.modules
  5. Non-UTF-8 source encoding declarations
  6. string.Formatter.vformat with untrusted format string
  7. Assignment to __builtins__ / __loader__ / __spec__
  8. __init_subclass__ that mutates base class
  9. Starred unpacking of __builtins__
"""

from __future__ import annotations

import pytest


def _validate(source: str) -> None:
    """Raise :class:`ASTValidationError` if ``source`` is unsafe."""
    from arcagent.tools._dynamic_loader import AstValidator

    AstValidator().validate(source)


def _raises_validation(source: str, category: str) -> None:
    """Assert validator rejects ``source`` with ``category`` in reason."""
    from arcagent.tools._dynamic_loader import ASTValidationError

    with pytest.raises(ASTValidationError) as exc_info:
        _validate(source)
    assert category in str(exc_info.value), (
        f"Expected rejection category {category!r} in error; got: {exc_info.value}"
    )


class TestAllowedSource:
    """Simple declarative tools pass validation."""

    def test_plain_function_allowed(self) -> None:
        _validate("def add(a: int, b: int) -> int:\n    return a + b\n")

    def test_stdlib_safe_imports_allowed(self) -> None:
        _validate("import json\nimport math\n")


class TestRejectPrivilegedImports:
    """Category 1 — importing ctypes / subprocess / os / etc."""

    def test_ctypes_rejected(self) -> None:
        _raises_validation("import ctypes\n", "import:ctypes")

    def test_subprocess_rejected(self) -> None:
        _raises_validation("import subprocess\n", "import:subprocess")

    def test_os_rejected(self) -> None:
        _raises_validation("import os\n", "import:os")

    def test_sys_rejected(self) -> None:
        _raises_validation("import sys\n", "import:sys")

    def test_pickle_rejected(self) -> None:
        _raises_validation("import pickle\n", "import:pickle")

    def test_from_import_also_rejected(self) -> None:
        _raises_validation("from os import system\n", "import:os")


class TestRejectFrameTraversal:
    """Category 2 — gi_frame / f_back chain used by CVE-2023-37271."""

    def test_gi_frame_rejected(self) -> None:
        _raises_validation(
            "def bad():\n    (yield).gi_frame\n",
            "attribute:gi_frame",
        )

    def test_f_back_rejected(self) -> None:
        _raises_validation(
            "def bad(x):\n    return x.f_back\n",
            "attribute:f_back",
        )

    def test_f_globals_rejected(self) -> None:
        _raises_validation(
            "def bad(x):\n    return x.f_globals['__builtins__']\n",
            "attribute:f_globals",
        )

    def test_subclasses_rejected(self) -> None:
        _raises_validation(
            "def bad():\n    return ().__class__.__base__.__subclasses__()\n",
            "attribute:__subclasses__",
        )


class TestRejectDynamicExec:
    """Category 3 — compile / eval / exec / __import__."""

    def test_eval_rejected(self) -> None:
        _raises_validation("eval('1+1')\n", "call:eval")

    def test_exec_rejected(self) -> None:
        _raises_validation("exec('x=1')\n", "call:exec")

    def test_compile_rejected(self) -> None:
        _raises_validation("compile('1+1', '<s>', 'eval')\n", "call:compile")

    def test_import_function_rejected(self) -> None:
        _raises_validation("__import__('os')\n", "call:__import__")


class TestRejectSysModulesAccess:
    """Category 4 — sys.modules['os'] bypass."""

    def test_sys_modules_subscript_rejected(self) -> None:
        # Fails first at ``import sys`` — validates the outer layer
        _raises_validation("import sys\nsys.modules['os']\n", "import:sys")

    def test_sys_modules_via_getattr_rejected(self) -> None:
        """Even without direct import, accessing sys.modules is rejected."""
        _raises_validation(
            "def bad(m):\n    return m.modules['os']\n",
            "attribute:modules",
        )


class TestRejectEncodingAttack:
    """Category 5 — non-UTF-8 source encoding declarations.

    Codec-stage attacks run BEFORE the AST parser, so we reject the
    source text before handing it to ``ast.parse``. Regex-based check.
    """

    def test_utf7_coding_declaration_rejected(self) -> None:
        _raises_validation(
            "# -*- coding: utf-7 -*-\nx = 1\n",
            "encoding:non_utf8",
        )

    def test_latin1_coding_declaration_rejected(self) -> None:
        _raises_validation(
            "# coding: latin-1\nx = 1\n",
            "encoding:non_utf8",
        )


class TestRejectBuiltinsMutation:
    """Category 7 — assignment to ``__builtins__`` / ``__loader__``."""

    def test_assign_builtins_rejected(self) -> None:
        _raises_validation("__builtins__ = None\n", "assign:__builtins__")

    def test_assign_loader_rejected(self) -> None:
        _raises_validation("__loader__ = None\n", "assign:__loader__")


class TestRejectInitSubclassMutation:
    """Category 8 — ``__init_subclass__`` mutating base class."""

    def test_init_subclass_rejected(self) -> None:
        _raises_validation(
            "class Evil:\n    def __init_subclass__(cls, **kw):\n        pass\n",
            "method:__init_subclass__",
        )


class TestRejectStarredBuiltins:
    """Category 9 — ``*__builtins__`` unpacking."""

    def test_starred_builtins_rejected(self) -> None:
        _raises_validation("f(*__builtins__)\n", "starred:__builtins__")
