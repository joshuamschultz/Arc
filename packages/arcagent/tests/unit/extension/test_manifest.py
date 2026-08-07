"""SPEC-062 T-877 (RED) — ``ExtensionManifest`` is the only document Arc parses.

COMP-001. An extension is third-party, lower-trust code: the manifest is the one
place it gets to describe itself, so every claim it makes is adversarial input.
These tests pin the four things that keep it from being a privilege-escalation
document:

* ``extra="forbid"`` — a typo raises and NAMES the key rather than vanishing
  (the ``core/module_config.py`` precedent).
* denied keys are STRIPPED — a manifest cannot reach the vault backend, process
  tools, the tool preamble, the sandbox path floor, or identity key custody
  (the ``_DENIED_OVERLAY_PATHS`` / ``_strip_denied`` precedent in
  ``arccli/blueprints.py``).
* an unbounded tool allowlist is refused above personal (REQ-268).
* a third-party artifact without an exact version AND a sha256 is rejected
  (REQ-290) — a floating pin is a supply-chain hole, not a convenience.

Serves REQ-262, REQ-264, REQ-268, REQ-269, REQ-274, REQ-290.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from arcagent.core.errors import ExtensionError
from arcagent.core.tier import Tier
from arcagent.extension import manifest as manifest_module
from arcagent.extension.manifest import ExtensionManifest, load_manifest

_SHA = "a" * 64

# A complete, well-formed manifest. Tests mutate a copy of this rather than each
# writing their own, so a failure points at the mutated clause and nothing else.
_FULL = f"""
requires = ["httpx>=0.27"]

[extension]
name = "acme_tickets"
version = "1.4.0"
attachment = "mcp"
tier_floor = "personal"

[artifact]
package = "acme-mcp-server"
version = "2.3.1"
sha256 = "{_SHA}"

[[host_requires]]
name = "node"
minimum_version = "20.0.0"

[[secrets]]
name = "api_token"
prompt = "Acme API token"

[tools]
allow = ["list_issues", "create_issue"]

[[tools.declared]]
name = "list_issues"
classification = "read_only"
capability_tags = ["external_comms"]

[[tools.declared]]
name = "create_issue"
classification = "state_modifying"
capability_tags = ["external_comms", "untrusted_input"]

[approval]
default = "outbound"
"""

# Trusted-admin-only config paths a manifest must never set. Mirrors
# ``_DENIED_OVERLAY_PATHS`` (arccli/blueprints.py:73-83): the vault backend,
# native process tools, the tool preamble, the sandbox filesystem floor, and
# identity key custody. Hard-coded here on purpose — a green implementation that
# quietly shrinks the denylist must fail this test.
_DENIED: tuple[tuple[str, ...], ...] = (
    ("vault", "backend"),
    ("tools", "process"),
    ("tools", "preamble"),
    ("tools", "policy", "allowed_paths"),
    ("identity", "key_dir"),
)


def _config_block(path: tuple[str, ...], value: str) -> str:
    """Render ``[config.<a>.<b>] <leaf> = <value>`` for a denied path."""
    table = ".".join(("config", *path[:-1]))
    return f"\n[{table}]\n{path[-1]} = {value}\n"


def _dig(node: Any, path: tuple[str, ...]) -> Any:
    for part in path:
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node


def test_unknown_key_raises_and_names_the_offending_key() -> None:
    """A typo must raise a validation error that says WHICH key was wrong."""
    text = _FULL.replace('version = "1.4.0"', 'verison = "1.4.0"', 1)

    with pytest.raises(ValidationError) as excinfo:
        load_manifest(text, tier=Tier.PERSONAL)

    assert "verison" in str(excinfo.value)


@pytest.mark.parametrize("path", _DENIED, ids=lambda p: ".".join(p))
def test_denied_config_keys_are_stripped_not_accepted(path: tuple[str, ...]) -> None:
    """A lower-trust manifest cannot self-grant an admin-only setting."""
    value = '["/"]' if path[-1] in {"allowed_paths"} else '"attacker-controlled"'
    text = _FULL + _config_block(path, value)

    manifest = load_manifest(text, tier=Tier.PERSONAL)

    assert _dig(manifest.config, path) is None


def test_stripping_is_surgical_and_leaves_sibling_config_intact() -> None:
    """Stripping removes the denied leaf only — not its parent table."""
    text = _FULL + '\n[config.tools]\nprocess = "curl"\ntimeout_seconds = 30\n'

    manifest = load_manifest(text, tier=Tier.PERSONAL)

    assert _dig(manifest.config, ("tools", "process")) is None
    assert _dig(manifest.config, ("tools", "timeout_seconds")) == 30


@pytest.mark.parametrize("tier", [Tier.ENTERPRISE, Tier.FEDERAL])
def test_wildcard_tool_allowlist_refused_above_personal(tier: Tier) -> None:
    """``allow = ["*"]`` is an unbounded grant — refused above personal (REQ-268)."""
    text = _FULL.replace('allow = ["list_issues", "create_issue"]', 'allow = ["*"]', 1)

    with pytest.raises(ExtensionError):
        load_manifest(text, tier=tier)


def test_wildcard_tool_allowlist_permitted_at_personal() -> None:
    """Personal tier may take the unbounded grant — the refusal is tier-gated, not absolute."""
    text = _FULL.replace('allow = ["list_issues", "create_issue"]', 'allow = ["*"]', 1)

    manifest = load_manifest(text, tier=Tier.PERSONAL)

    assert manifest.tools.allow == ["*"]


@pytest.mark.parametrize("tier", [Tier.ENTERPRISE, Tier.FEDERAL])
def test_omitted_tool_allowlist_is_unbounded_and_refused_above_personal(tier: Tier) -> None:
    """No declared bound is the same unbounded grant as ``*`` — refusal must not be
    escapable by simply omitting the key."""
    text = _FULL.replace('allow = ["list_issues", "create_issue"]\n', "", 1)

    with pytest.raises(ExtensionError):
        load_manifest(text, tier=tier)


@pytest.mark.parametrize(
    ("bad_artifact", "reason"),
    [
        (f'[artifact]\npackage = "acme-mcp-server"\nsha256 = "{_SHA}"\n', "no version"),
        ('[artifact]\npackage = "acme-mcp-server"\nversion = "2.3.1"\n', "no sha256"),
        (
            f'[artifact]\npackage = "acme-mcp-server"\nversion = ">=2.3"\nsha256 = "{_SHA}"\n',
            "range, not an exact version",
        ),
        (
            f'[artifact]\npackage = "acme-mcp-server"\nversion = "latest"\nsha256 = "{_SHA}"\n',
            "floating tag",
        ),
        (
            '[artifact]\npackage = "acme-mcp-server"\nversion = "2.3.1"\nsha256 = "deadbeef"\n',
            "sha256 is not 64 hex chars",
        ),
    ],
    ids=["no-version", "no-sha256", "version-range", "floating-tag", "short-hash"],
)
def test_third_party_artifact_without_an_exact_pin_is_rejected(
    bad_artifact: str, reason: str
) -> None:
    """REQ-290: an artifact Arc will execute must be pinned to one exact build.

    ``reason`` documents the case in the failure output; it is not asserted on.
    """
    good = f'[artifact]\npackage = "acme-mcp-server"\nversion = "2.3.1"\nsha256 = "{_SHA}"\n'
    text = _FULL.replace(good, bad_artifact, 1)
    assert text != _FULL, f"fixture did not substitute the {reason} artifact block"

    with pytest.raises(ValidationError):
        load_manifest(text, tier=Tier.PERSONAL)


def test_manifest_without_an_artifact_is_valid() -> None:
    """A hosted or native attachment runs no third-party artifact — the pin is
    required only when there is something to pin."""
    good = f'[artifact]\npackage = "acme-mcp-server"\nversion = "2.3.1"\nsha256 = "{_SHA}"\n'
    text = _FULL.replace(good, "", 1)

    manifest = load_manifest(text, tier=Tier.PERSONAL)

    assert manifest.artifact is None


def test_manifest_parses_the_full_declaration() -> None:
    """Every clause the loader, bridge, launcher, and CLI depend on round-trips."""
    manifest = load_manifest(_FULL, tier=Tier.PERSONAL)

    assert isinstance(manifest, ExtensionManifest)
    assert manifest.extension.name == "acme_tickets"
    assert manifest.extension.version == "1.4.0"
    assert manifest.extension.attachment == "mcp"
    assert manifest.extension.tier_floor == Tier.PERSONAL

    assert manifest.artifact is not None
    assert manifest.artifact.package == "acme-mcp-server"
    assert manifest.artifact.version == "2.3.1"
    assert manifest.artifact.sha256 == _SHA

    assert [req.name for req in manifest.host_requires] == ["node"]
    assert manifest.host_requires[0].minimum_version == "20.0.0"

    assert [secret.name for secret in manifest.secrets] == ["api_token"]
    assert manifest.requires == ["httpx>=0.27"]

    assert manifest.tools.allow == ["list_issues", "create_issue"]
    declared = {tool.name: tool for tool in manifest.tools.declared}
    assert declared["list_issues"].classification == "read_only"
    assert declared["list_issues"].capability_tags == ["external_comms"]
    assert declared["create_issue"].classification == "state_modifying"
    assert declared["create_issue"].capability_tags == ["external_comms", "untrusted_input"]

    assert manifest.approval.default == "outbound"


def test_tool_without_a_classification_defaults_to_the_restrictive_value() -> None:
    """REQ-269: an absent declaration defaults to the MORE restrictive value.

    Silence must never buy a tool the cheap ``read_only`` treatment.
    """
    text = _FULL.replace('classification = "read_only"\n', "", 1)

    manifest = load_manifest(text, tier=Tier.PERSONAL)

    declared = {tool.name: tool for tool in manifest.tools.declared}
    assert declared["list_issues"].classification == "state_modifying"


def test_approval_defaults_to_outbound_requires_approval() -> None:
    """REQ-274: the default admits inbound reads and gates every outbound call."""
    text = _FULL.replace('[approval]\ndefault = "outbound"\n', "", 1)

    manifest = load_manifest(text, tier=Tier.PERSONAL)

    assert manifest.approval.default == "outbound"


class TestTierFloorRefusesButCannotRaise:
    """D-579: a tier floor declines to run below itself and does nothing else.

    The last test is the load-bearing one. A bundle author who could raise the
    deployment's stringency would be writing operator policy — so the floor must
    reach no policy resolver at all, which only a structural check can hold.
    """

    def test_a_floor_above_the_deployment_refuses_to_load(self) -> None:
        text = _FULL.replace('tier_floor = "personal"', 'tier_floor = "federal"', 1)

        with pytest.raises(ExtensionError) as excinfo:
            load_manifest(text, tier=Tier.ENTERPRISE)

        assert excinfo.value.details["reason"] == "tier_floor"

    def test_a_floor_at_or_below_the_deployment_loads(self) -> None:
        text = _FULL.replace('tier_floor = "personal"', 'tier_floor = "enterprise"', 1)

        manifest = load_manifest(text, tier=Tier.FEDERAL)

        assert manifest.extension.tier_floor == Tier.ENTERPRISE

    def test_the_other_tier_gate_still_keys_off_the_deployment_tier(self) -> None:
        # The unbounded-allowlist refusal is the one other tier-sensitive rule
        # here. Raising the floor to the deployment's own tier must not change
        # its verdict — if the floor fed an "effective tier" anywhere, a
        # personal deployment with a personal floor could stop admitting what
        # personal admits.
        unbounded = _FULL.replace('allow = ["list_issues", "create_issue"]\n', "", 1)

        assert load_manifest(unbounded, tier=Tier.PERSONAL).tools.is_unbounded is True

    def test_tier_floor_is_read_nowhere_but_the_refusal(self) -> None:
        # The structural guard, and the only one that survives a future edit: a
        # floor that reached a policy resolver would hand a third-party bundle
        # author control over operator policy, and no behavioural assertion here
        # would see it happen.
        root = Path(manifest_module.__file__).resolve().parents[1]
        readers = sorted(
            path.relative_to(root)
            for path in root.rglob("*.py")
            # Attribute access only: ``resolve_tier_floor`` is the unrelated
            # SPEC-047 security-knob helper and must not count as a reader.
            if re.search(r"\.tier_floor\b", path.read_text(encoding="utf-8"))
        )

        assert readers == [Path("extension/manifest.py")]
