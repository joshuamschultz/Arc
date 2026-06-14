"""Tier-gated import allowlist for workspace-authored (untrusted) tools.

Lets an operator permit specific privileged imports (e.g. `sys`) for tools in
the protected workspace/capabilities/ root WITHOUT moving them out — the AST
gate still runs, only the import blocklist is relaxed per tier.
"""

from __future__ import annotations

import pytest

from arcagent.tools._dynamic_loader import (
    ASTValidationError,
    AstValidator,
    resolve_workspace_import_policy,
)

_USES_SYS = "import sys\n\n\nasync def t(x: str) -> str:\n    return sys.platform\n"
_USES_OS = "import os\n\n\nasync def t(x: str) -> str:\n    return os.getcwd()\n"
_USES_EXEC = "async def t(x: str) -> str:\n    exec(x)\n    return x\n"


# --- tier resolution --------------------------------------------------------


def test_personal_allows_everything() -> None:
    allow_all, allowed = resolve_workspace_import_policy(
        "personal", allow_all_imports=False, allow_imports=[]
    )
    assert allow_all is True
    assert allowed == frozenset()


def test_federal_only_explicit_allowlist_ignores_allow_all() -> None:
    # allow_all_imports must NOT grant a blanket relaxation at federal.
    allow_all, allowed = resolve_workspace_import_policy(
        "federal", allow_all_imports=True, allow_imports=["sys"]
    )
    assert allow_all is False
    assert allowed == frozenset({"sys"})


def test_enterprise_deny_by_default() -> None:
    allow_all, allowed = resolve_workspace_import_policy(
        "enterprise", allow_all_imports=False, allow_imports=[]
    )
    assert allow_all is False
    assert allowed == frozenset()


def test_enterprise_allow_all_toggle() -> None:
    allow_all, allowed = resolve_workspace_import_policy(
        "enterprise", allow_all_imports=True, allow_imports=[]
    )
    assert allow_all is True


def test_enterprise_array_allowlist() -> None:
    allow_all, allowed = resolve_workspace_import_policy(
        "enterprise", allow_all_imports=False, allow_imports=["sys", "subprocess"]
    )
    assert allow_all is False
    assert allowed == frozenset({"sys", "subprocess"})


# --- validator honours the policy ------------------------------------------


def test_bare_validator_still_blocks_sys() -> None:
    """Default construction is fail-closed (federal-safe)."""
    with pytest.raises(ASTValidationError) as exc:
        AstValidator().validate(_USES_SYS)
    assert "import:sys" in str(exc.value)


def test_allow_all_permits_sys() -> None:
    AstValidator(allow_all_imports=True).validate(_USES_SYS)  # no raise


def test_allowlist_permits_listed_but_blocks_others() -> None:
    v = AstValidator(allowed_imports=frozenset({"sys"}))
    v.validate(_USES_SYS)  # sys allowed
    with pytest.raises(ASTValidationError) as exc:
        AstValidator(allowed_imports=frozenset({"sys"})).validate(_USES_OS)
    assert "import:os" in str(exc.value)


def test_allow_all_still_blocks_exec() -> None:
    """Import relaxation must NOT relax sandbox-escape protections."""
    with pytest.raises(ASTValidationError) as exc:
        AstValidator(allow_all_imports=True).validate(_USES_EXEC)
    assert "exec" in str(exc.value)
