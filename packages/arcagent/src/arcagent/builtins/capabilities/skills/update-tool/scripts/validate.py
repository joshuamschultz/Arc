"""Validate a candidate updated tool source file.

Same checks as create-tool/scripts/validate.py — AST validator only.
The version-mismatch rule is enforced by ``update_tool`` itself, not
this script.

Usage:
    python validate.py <path-to-tool.py>
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
    from arcagent.tools._dynamic_loader import (
        ASTValidationError,
        AstValidator,
    )

    source = path.read_text(encoding="utf-8")
    try:
        AstValidator().validate(source)
    except ASTValidationError as exc:
        print(f"REJECTED [{exc.category}] {exc}")
        return 1
    print("OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
