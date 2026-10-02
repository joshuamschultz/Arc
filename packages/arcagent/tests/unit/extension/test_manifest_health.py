"""``[health]`` — the manifest's declared probe and its parse-time refusals (P18-1, 5.1)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from arcagent.core.tier import Tier
from arcagent.extension.manifest import ExtensionManifest, load_manifest

_NATIVE = """
[extension]
name = "svc"
version = "1.0.0"
attachment = "{attachment}"
{health}
[tools]
allow = ["svc_ping", "svc_send"]

[[tools.declared]]
name = "svc_ping"
classification = "read_only"

[[tools.declared]]
name = "svc_send"
classification = "state_modifying"
{host}
"""

_HOST = """
[[host_requires]]
name = "svc"
instruction = "install svc"
{verify}
"""


def _load(health: str, *, attachment: str = "native", verify: bool = False) -> ExtensionManifest:
    host = _HOST.format(verify='verify_command = "svc whoami"' if verify else "") if verify else ""
    text = _NATIVE.format(attachment=attachment, health=health, host=host)
    return load_manifest(text, tier=Tier.PERSONAL)


def test_attachment_probe_parses_for_a_native_bundle() -> None:
    manifest = _load('[health]\nprobe = "attachment"')

    assert manifest.health is not None
    assert manifest.health.probe == "attachment"


def test_tool_probe_names_a_read_only_allowed_tool() -> None:
    manifest = _load('[health]\nprobe = "tool:svc_ping"\nargs = { limit = "1" }')

    assert manifest.health.tool == "svc_ping"


def test_cli_bundle_with_attachment_probe_is_refused_at_parse() -> None:
    with pytest.raises(ValidationError, match="proves nothing for a cli bundle"):
        _load('[health]\nprobe = "attachment"', attachment="cli")


def test_tool_probe_must_be_read_only() -> None:
    with pytest.raises(ValidationError, match="read_only"):
        _load('[health]\nprobe = "tool:svc_send"')


def test_tool_probe_must_be_allowed() -> None:
    with pytest.raises(ValidationError, match="does not list"):
        _load('[health]\nprobe = "tool:svc_other"')


def test_host_verify_needs_a_verify_command() -> None:
    with pytest.raises(ValidationError, match="verify_command"):
        _load('[health]\nprobe = "host_verify"')
    assert _load('[health]\nprobe = "host_verify"', attachment="cli", verify=True)


def test_mode_none_needs_a_reason() -> None:
    with pytest.raises(ValidationError, match="needs a reason"):
        _load('[health]\nprobe = "attachment"\nmode = "none"')
    assert _load('[health]\nprobe = "attachment"\nmode = "none"\nreason = "stateless"')


def test_unknown_probe_shape_is_refused() -> None:
    with pytest.raises(ValidationError, match="must be"):
        _load('[health]\nprobe = "ping"')


def test_args_only_apply_to_a_tool_probe() -> None:
    with pytest.raises(ValidationError, match="only apply to a tool"):
        _load('[health]\nprobe = "attachment"\nargs = { a = "b" }')
