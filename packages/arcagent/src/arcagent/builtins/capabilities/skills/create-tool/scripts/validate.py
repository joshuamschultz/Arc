"""Validate a candidate tool source file.

Usage:
    python validate.py <path-to-tool.py>

Exits 0 on success. Exits non-zero with a clear error category on
AST-validator rejection. Used by the LLM (or developer) to verify a
tool source before calling ``create_tool``.
"""
# ruff: noqa: T201 — CLI tool; print is the right primitive here.

from __future__ import annotations

import sys
from pathlib import Path


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: validate.py <path>")
        return 2
    path = Path(sys.argv[1])
    if not path.is_file():
        print(f"not a file: {path}")
        return 2
    source = path.read_text(encoding="utf-8")
    from arcagent.tools._dynamic_loader import (
        ASTValidationError,
        AstValidator,
    )

    # Standalone CLI subprocess: it has no access to the in-agent runtime's
    # tier-resolved import policy (a ContextVar in the agent process), so it
    # validates against the fail-closed enterprise default. That is the correct
    # conservative pre-check — a source that passes here still faces the agent's
    # real (possibly stricter federal) policy inside create_tool.
    try:
        AstValidator().validate(source)
    except ASTValidationError as exc:
        print(f"REJECTED [{exc.category}] {exc}")
        return 1
    print("OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
