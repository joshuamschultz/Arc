"""Architecture test — OAuth providers Arc holds the credential for run no vendor CLI.

P18-3 (Q18-B): Google signs in through Arc's native OAuth flow and its tools call
the provider's REST API with a short-lived access token. A bundle that also
declared a host binary or a ``[config.cli]`` would put a second credential store
(the vendor CLI's keyring) back on the host — the outage class this removed. The
browser ``remote_login`` machinery is gone with it; nothing may import it back.
"""

from __future__ import annotations

import ast
import tomllib
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[2]

#: Bundles whose credential Arc holds through ``[oauth]``.
_NATIVE_OAUTH = ("google_workspace", "dropbox")


@pytest.mark.parametrize("bundle", _NATIVE_OAUTH)
def test_oauth_bundle_declares_no_host_binary_and_no_cli(bundle: str) -> None:
    manifest = tomllib.loads((_REPO / "extensions" / bundle / "extension.toml").read_text())
    assert "oauth" in manifest, f"{bundle} must connect through [oauth]"
    assert not manifest.get("host_requires"), f"{bundle} declares a host binary"
    assert "cli" not in manifest.get("config", {}), f"{bundle} declares [config.cli]"
    assert "artifact" not in manifest, f"{bundle} pins a vendor binary"
    assert manifest["extension"]["attachment"] == "native"


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module)
        elif isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
    return found


def test_nothing_imports_remote_login() -> None:
    offenders = [
        str(path.relative_to(_REPO))
        for path in (_REPO / "packages").rglob("*.py")
        if "node_modules" not in path.parts
        and any(name.endswith("remote_login") for name in _imports(path))
    ]
    assert offenders == []
    assert not (_REPO / "packages/arcagent/src/arcagent/extension/remote_login.py").exists()
