"""Architecture test — the OAuth redirect URI never comes from a request.

P18-3 O3: a ``Host`` header (or anything else on the request) is attacker
controlled. The redirect URI is fixed at startup from ``[ui] public_base_url`` and
the UI port (``arcagent.oauth_redirect_uri``) and read from ``app.state``. This
scans the modules that build, check or hand out a redirect URI for any read of the
request's URL or host.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[2]

_SCANNED = (
    "packages/arcui/src/arcui/routes/connectors.py",
    "packages/arcagent/src/arcagent/connections.py",
    "packages/arcagent/src/arcagent/extension/oauth.py",
)

#: Request attributes that carry the address the client used.
_BANNED_ATTRIBUTES = frozenset({"url", "base_url", "url_for"})

#: Header names that carry the address the client used.
_BANNED_HEADERS = frozenset({"host", "x-forwarded-host", "forwarded", "origin"})


def _request_address_reads(tree: ast.AST) -> list[str]:
    found: list[str] = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Attribute)
            and node.attr in _BANNED_ATTRIBUTES
            and isinstance(node.value, ast.Name)
            and node.value.id == "request"
        ):
            found.append(f"request.{node.attr} (line {node.lineno})")
        if (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and node.value.lower() in _BANNED_HEADERS
        ):
            found.append(f"header {node.value!r} (line {node.lineno})")
    return found


@pytest.mark.parametrize("relative", _SCANNED)
def test_redirect_builders_never_read_the_request_address(relative: str) -> None:
    path = _REPO / relative
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    assert _request_address_reads(tree) == []
