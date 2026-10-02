"""P18-2 — nothing executes from the operator tree (``~/arc``), at any tier.

A code-bearing bundle planted in ``~/arc/extensions`` or ``~/arc/state/extensions``
is never imported or spawned: the first is refused (and audited) by name, the
second is not on the search path at all. Only an operator-signed, config-only MCP
bundle may live in the operator tree. Code-bearing connectors are installed into
``~/.arc/extensions`` by ``install-bundle``, which verifies every signature first;
the loader verifies them again on every load.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

import pytest
from arctrust import bootstrap_operator_signer
from arctrust.audit import AuditEvent
from arctrust.paths import installed_extensions_dir

from arcagent.capabilities.capability_registry import CapabilityRegistry
from arcagent.connections import Connections
from arcagent.core.errors import ExtensionError
from arcagent.core.tier import Tier
from arcagent.extension.catalog import (
    ExtensionCatalog,
    in_operator_tree,
    resolve_extension_roots,
)
from arcagent.extension.loader import ExtensionLoader

NATIVE = """
[extension]
name = "planted"
version = "1.0.0"
attachment = "native"

[config.native]
entrypoint = "planted_entry"

[health]
probe = "attachment"
"""

MCP = """
[extension]
name = "configonly"
version = "1.0.0"
attachment = "mcp"

[config.mcp]
transport = "http"
url = "https://mcp.example.com/mcp"

[health]
probe = "attachment"
"""


class ListSink:
    def __init__(self) -> None:
        self.events: list[AuditEvent] = []

    def write(self, event: AuditEvent) -> None:
        self.events.append(event)


@pytest.fixture
def deployment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    arc_dir = tmp_path / "arc"
    monkeypatch.setenv("ARC_TEAM_ROOT", str(arc_dir))
    monkeypatch.setenv("ARC_CONFIG_DIR", str(tmp_path / "dot-arc"))
    monkeypatch.delenv("ARC_EXTENSIONS_ROOT", raising=False)
    return arc_dir


def _plant(root: Path, marker: Path) -> Path:
    bundle = root / "planted"
    bundle.mkdir(parents=True)
    (bundle / "extension.toml").write_text(NATIVE)
    (bundle / "planted_entry.py").write_text(
        f"from pathlib import Path\nPath({str(marker)!r}).write_text('executed')\n"
        "def build_native_attachment(context):\n    raise SystemExit('executed')\n"
    )
    return bundle


@pytest.mark.parametrize("tier", list(Tier))
@pytest.mark.parametrize("where", ["extensions", "state/extensions"])
async def test_planted_code_bundle_in_operator_tree_never_loads(
    deployment: Path, tmp_path: Path, tier: Tier, where: str
) -> None:
    marker = tmp_path / "executed.txt"
    bundle = _plant(deployment / where, marker)
    assert in_operator_tree(bundle)
    sink = ListSink()
    loader = ExtensionLoader(
        roots=resolve_extension_roots(deployment),
        registry=CapabilityRegistry(),
        tier=tier,
        audit_sink=sink,
        trusted_public_key=bytes(32),
    )
    with pytest.raises(ExtensionError):
        await loader.load("planted")
    assert not marker.exists()
    assert any(event.outcome == "deny" for event in sink.events)
    if where == "extensions":
        reasons = [event.extra.get("reason") for event in sink.events]
        assert "code_in_operator_tree" in reasons


async def test_state_extensions_is_not_on_the_search_path(deployment: Path) -> None:
    (deployment / "state" / "extensions").mkdir(parents=True)
    (deployment / "extensions").mkdir(parents=True)
    roots = resolve_extension_roots(deployment)
    assert deployment / "state" / "extensions" not in roots
    assert roots[-1] == deployment / "extensions"


async def test_config_only_mcp_bundle_may_live_in_operator_tree_but_must_be_signed(
    deployment: Path,
) -> None:
    bundle = deployment / "extensions" / "configonly"
    bundle.mkdir(parents=True)
    (bundle / "extension.toml").write_text(MCP)
    catalog = ExtensionCatalog(
        roots=resolve_extension_roots(deployment), tier=Tier.PERSONAL, audit_sink=ListSink()
    )
    assert catalog.locate("configonly") == bundle
    loader = ExtensionLoader(
        roots=resolve_extension_roots(deployment),
        registry=CapabilityRegistry(),
        tier=Tier.PERSONAL,
        audit_sink=ListSink(),
        trusted_public_key=bytes(32),
    )
    with pytest.raises(ExtensionError) as caught:
        await loader.load("configonly")
    assert caught.value.details.get("reason") == "unsigned"
    # Adding one code file makes it code-bearing: refused before anything is read.
    (bundle / "sneaky.py").write_text("raise SystemExit")
    with pytest.raises(ExtensionError) as refused:
        catalog.locate("configonly")
    assert refused.value.details.get("reason") == "code_in_operator_tree"


def _connections(deployment: Path) -> Connections:
    async def opener() -> Any:
        from arcstore.backends.memory import FakeBackend

        return FakeBackend()

    return Connections.for_deployment(arc_dir=deployment, state_opener=opener)


async def test_install_bundle_verifies_then_installs_into_the_arc_home(
    deployment: Path, tmp_path: Path
) -> None:
    bootstrap_operator_signer(base=deployment)
    source = _plant(tmp_path / "src", tmp_path / "never.txt")
    connections = _connections(deployment)

    with pytest.raises(ExtensionError) as unsigned:
        connections.install_bundle(source)
    assert unsigned.value.code == "BUNDLE_UNSIGNED"

    connections.sign_bundle(source)
    target = connections.install_bundle(source)
    assert target == installed_extensions_dir() / "planted"
    assert not in_operator_tree(target)
    with pytest.raises(ExtensionError) as again:
        connections.install_bundle(source)
    assert again.value.code == "BUNDLE_EXISTS"

    # Tampered after install: the loader re-verifies at load, at personal tier too.
    (target / "planted_entry.py").write_text("print('tampered')\n")
    loader = ExtensionLoader(
        roots=resolve_extension_roots(deployment),
        registry=CapabilityRegistry(),
        tier=Tier.PERSONAL,
        audit_sink=ListSink(),
        trusted_public_key=connections._pinned_key(),
    )
    with pytest.raises(ExtensionError):
        await loader.load("planted")
    shutil.rmtree(target)
