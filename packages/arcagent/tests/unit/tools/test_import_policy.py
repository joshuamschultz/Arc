"""Tier-resolved import policy for workspace-authored (untrusted) tools.

One :class:`ImportPolicy` is the single source of truth shared by the authoring
gate (``create_tool``/``update_tool``) and the load gate (``CapabilityLoader``).
Three modes: personal allows every import, enterprise blocks four privileged
groups (with operator exceptions), federal is a pure allowlist (deny by default).
Sandbox-escape protections (eval/exec, frame traversal) are never relaxable.
"""

from __future__ import annotations

import pytest

from arcagent.tools._dynamic_loader import (
    ASTValidationError,
    AstValidator,
    ImportPolicy,
    resolve_workspace_import_policy,
)

_USES_OS = "import os\n\n\nasync def t(x: str) -> str:\n    return os.getcwd()\n"
_USES_SYS = "import sys\n\n\nasync def t(x: str) -> str:\n    return sys.platform\n"
_USES_SUBPROCESS = "import subprocess\n\n\nasync def t() -> int:\n    return 1\n"
_USES_SOCKET = "import socket\n\n\nasync def t() -> int:\n    return 1\n"
_USES_REQUESTS = "import requests\n\n\nasync def t() -> int:\n    return 1\n"
_USES_JSON = "import json\n\n\nasync def t() -> str:\n    return json.dumps({})\n"
_USES_ARCAGENT = "from arcagent.tools._decorator import tool\n\n\nx = tool\n"
_USES_FUTURE = "from __future__ import annotations\n\nx = 1\n"
_USES_EXEC = "async def t(x: str) -> str:\n    exec(x)\n    return x\n"


def _validate(policy: ImportPolicy, source: str) -> None:
    AstValidator(policy=policy).validate(source)


def _rejects(policy: ImportPolicy, source: str, category: str) -> None:
    with pytest.raises(ASTValidationError) as exc:
        _validate(policy, source)
    assert category in str(exc.value)


# --- personal: allow everything ---------------------------------------------


def test_personal_allows_os_sys_requests() -> None:
    policy = resolve_workspace_import_policy("personal", allow_all_imports=False, allow_imports=[])
    _validate(policy, _USES_OS)
    _validate(policy, _USES_SYS)
    _validate(policy, _USES_REQUESTS)


def test_personal_still_blocks_exec() -> None:
    """Import relaxation must NOT relax sandbox-escape protections."""
    policy = resolve_workspace_import_policy("personal", allow_all_imports=False, allow_imports=[])
    _rejects(policy, _USES_EXEC, "exec")


# --- enterprise: blocklist of four groups -----------------------------------


def _enterprise(
    *, allow_all_imports: bool = False, allow_imports: list[str] | None = None
) -> ImportPolicy:
    return resolve_workspace_import_policy(
        "enterprise", allow_all_imports=allow_all_imports, allow_imports=allow_imports or []
    )


def test_enterprise_blocks_each_group() -> None:
    policy = _enterprise()
    _rejects(policy, _USES_OS, "import:os")  # filesystem
    _rejects(policy, _USES_SUBPROCESS, "import:subprocess")  # process/exec
    _rejects(policy, _USES_SYS, "import:sys")  # interpreter/serialization
    _rejects(policy, _USES_SOCKET, "import:socket")  # network


def test_enterprise_allows_ordinary_stdlib() -> None:
    _validate(_enterprise(), _USES_JSON)


def test_enterprise_honors_allow_imports_exception() -> None:
    policy = _enterprise(allow_imports=["os"])
    _validate(policy, _USES_OS)  # exception granted
    _rejects(policy, _USES_SUBPROCESS, "import:subprocess")  # others still blocked


def test_enterprise_allow_all_opt_out() -> None:
    policy = _enterprise(allow_all_imports=True)
    _validate(policy, _USES_OS)
    _validate(policy, _USES_SUBPROCESS)


# --- federal: pure allowlist ------------------------------------------------


def test_federal_rejects_unlisted_including_requests() -> None:
    policy = resolve_workspace_import_policy("federal", allow_all_imports=True, allow_imports=[])
    # allow_all_imports is IGNORED at federal.
    _rejects(policy, _USES_REQUESTS, "import:requests")
    _rejects(policy, _USES_OS, "import:os")
    _rejects(policy, _USES_JSON, "import:json")


def test_federal_accepts_seeded_and_listed() -> None:
    policy = resolve_workspace_import_policy(
        "federal", allow_all_imports=False, allow_imports=["requests"]
    )
    _validate(policy, _USES_FUTURE)  # seeded __future__
    _validate(policy, _USES_ARCAGENT)  # seeded arcagent
    _validate(policy, _USES_REQUESTS)  # explicitly listed


def test_federal_still_blocks_exec() -> None:
    policy = resolve_workspace_import_policy(
        "federal", allow_all_imports=False, allow_imports=["os"]
    )
    _rejects(policy, _USES_EXEC, "exec")


# --- fail-closed default -----------------------------------------------------


def test_default_policy_is_fail_closed_enterprise() -> None:
    """The bare-validator default policy blocks os (enterprise, no exceptions)."""
    from arcagent.tools._dynamic_loader import DEFAULT_IMPORT_POLICY

    assert DEFAULT_IMPORT_POLICY.tier == "enterprise"
    _rejects(DEFAULT_IMPORT_POLICY, _USES_OS, "import:os")


def test_bare_validator_blocks_os() -> None:
    with pytest.raises(ASTValidationError) as exc:
        AstValidator().validate(_USES_OS)
    assert "import:os" in str(exc.value)


# --- describe() teaches the policy ------------------------------------------


def test_describe_enterprise_names_groups_and_exceptions() -> None:
    text = _enterprise(allow_imports=["os"]).describe()
    assert "blocked import groups" in text
    assert "filesystem" in text
    assert "exceptions configured" in text
    assert "os" in text


def test_describe_federal_lists_permitted() -> None:
    text = resolve_workspace_import_policy(
        "federal", allow_all_imports=False, allow_imports=["requests"]
    ).describe()
    assert "only these imports are permitted" in text
    assert "requests" in text
    assert "arcagent" in text


def test_policy_carries_tier() -> None:
    assert resolve_workspace_import_policy(
        "federal", allow_all_imports=False, allow_imports=[]
    ).tier == "federal"
    assert resolve_workspace_import_policy(
        "personal", allow_all_imports=False, allow_imports=[]
    ).tier == "personal"
