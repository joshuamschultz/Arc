"""P18-2 §8.7 — a native attachment's build context never carries a sensitive value.

Parametrised off the filesystem: every ``extensions/*`` bundle with a sensitive
``[[secrets]]`` entry is built through :func:`build_attachment` with EVERY field
supplied, and the context handed to the bundle's factory is inspected. Sensitive
values reach the bundle only through ``context["credential"]`` at call time.

Allowlist: ``attachment = "mcp"`` bundles. C7: an MCP header/env is resolved at
session start; a reconcile push follows every credential change.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from arcagent.core.tier import Tier
from arcagent.extension.manifest import ExtensionManifest, load_manifest
from arcagent.extension.secrets import Secret
from arcagent.modules.connectors import attachments

EXTENSIONS = Path(__file__).resolve().parents[2] / "extensions"
MCP_REASON = "C7: MCP header/env resolved at session start; reconcile push on change"


def _sensitive_bundles() -> list[tuple[Path, ExtensionManifest]]:
    found = []
    for path in sorted(EXTENSIONS.glob("*/extension.toml")):
        manifest = load_manifest(path.read_text(), tier=Tier.PERSONAL)
        if any(declared.sensitive for declared in manifest.secrets):
            found.append((path.parent, manifest))
    return found


BUNDLES = _sensitive_bundles()


def test_sensitive_bundles_are_found() -> None:
    assert BUNDLES


@pytest.mark.parametrize(
    ("bundle", "manifest"), BUNDLES, ids=lambda value: getattr(value, "name", "")
)
def test_native_context_holds_no_sensitive_value(
    bundle: Path, manifest: ExtensionManifest, monkeypatch: pytest.MonkeyPatch
) -> None:
    kind = manifest.extension.attachment
    if kind == "mcp":
        pytest.skip(MCP_REASON)
    if kind != "native":
        pytest.skip("cli: sensitive placements are resolved per spawn, never at build")
    captured: dict[str, Any] = {}

    class Capture:
        def __init__(self, entrypoint: str, context: dict[str, Any]) -> None:
            captured.update(context)

    monkeypatch.setattr(attachments, "NativeAttachment", Capture)
    values = {declared.name: Secret(f"value-of-{declared.name}") for declared in manifest.secrets}
    marker = object()
    attachments.build_attachment(manifest, bundle, values, credential=marker)  # type: ignore[arg-type]

    sensitive = {declared.name for declared in manifest.secrets if declared.sensitive}
    assert not sensitive & set(captured)
    rendered = repr(captured)
    for name in sensitive:
        assert f"value-of-{name}" not in rendered
    assert captured["credential"] is marker
