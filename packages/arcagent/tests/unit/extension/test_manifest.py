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
* a third-party artifact without an exact version AND a per-platform sha256 is
  rejected (REQ-290) — a floating pin is a supply-chain hole, not a convenience,
  and one digest standing for every platform is a pin that matches one machine
  and silently describes the wrong bytes on all the others.

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

#: The whole ``[artifact]`` declaration, as one block. Tests swap this for a
#: broken variant, so a substitution that stops matching fails loudly rather
#: than silently testing the good manifest twice.
_ARTIFACT = f"""[artifact]
package = "acme-mcp-server"
version = "2.3.1"

[artifact.platforms."linux/arm64"]
url = "https://example.invalid/acme-2.3.1-linux-arm64.tar.gz"
sha256 = "{_SHA}"
member = "acme_2.3.1_linux_arm64/bin/acme"
"""

# A complete, well-formed manifest. Tests mutate a copy of this rather than each
# writing their own, so a failure points at the mutated clause and nothing else.
_FULL = f"""
requires = ["httpx>=0.27"]

[extension]
name = "acme_tickets"
version = "1.4.0"
attachment = "mcp"
tier_floor = "personal"

{_ARTIFACT}
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


def _platform_block(platform: str, sha: str = _SHA, url: str = "https://e.invalid/a.tgz") -> str:
    return f'[artifact.platforms."{platform}"]\nurl = "{url}"\nsha256 = "{sha}"\n'


@pytest.mark.parametrize(
    ("bad_artifact", "reason"),
    [
        (
            f'[artifact]\npackage = "acme-mcp-server"\n{_platform_block("linux/arm64")}',
            "no version",
        ),
        ('[artifact]\npackage = "acme-mcp-server"\nversion = "2.3.1"\n', "no platform pinned"),
        (
            f'[artifact]\npackage = "acme-mcp-server"\nversion = ">=2.3"\n'
            f"{_platform_block('linux/arm64')}",
            "range, not an exact version",
        ),
        (
            f'[artifact]\npackage = "acme-mcp-server"\nversion = "latest"\n'
            f"{_platform_block('linux/arm64')}",
            "floating tag",
        ),
        (
            f'[artifact]\npackage = "acme-mcp-server"\nversion = "2.3.1"\n'
            f"{_platform_block('linux/arm64', sha='deadbeef')}",
            "sha256 is not 64 hex chars",
        ),
        (
            f'[artifact]\npackage = "acme-mcp-server"\nversion = "2.3.1"\n'
            f'[artifact.platforms."linux/arm64"]\nsha256 = "{_SHA}"\n',
            "a digest with nothing to fetch",
        ),
        (
            f'[artifact]\npackage = "acme-mcp-server"\nversion = "2.3.1"\n'
            f"{_platform_block('linux/arm64', url='http://e.invalid/a.tgz')}",
            "plaintext download URL",
        ),
    ],
    ids=[
        "no-version",
        "no-platform",
        "version-range",
        "floating-tag",
        "short-hash",
        "no-url",
        "plaintext-url",
    ],
)
def test_third_party_artifact_without_an_exact_pin_is_rejected(
    bad_artifact: str, reason: str
) -> None:
    """REQ-290: an artifact Arc will execute must be pinned to one exact build.

    ``reason`` documents the case in the failure output; it is not asserted on.
    """
    text = _FULL.replace(_ARTIFACT, bad_artifact, 1)
    assert text != _FULL, f"fixture did not substitute the {reason} artifact block"

    with pytest.raises(ValidationError):
        load_manifest(text, tier=Tier.PERSONAL)


def test_manifest_without_an_artifact_is_valid() -> None:
    """A hosted or native attachment runs no third-party artifact — the pin is
    required only when there is something to pin."""
    text = _FULL.replace(_ARTIFACT, "", 1)

    manifest = load_manifest(text, tier=Tier.PERSONAL)

    assert manifest.artifact is None


def test_a_pin_answers_for_the_platform_it_covers_and_refuses_every_other() -> None:
    """The whole point of keying digests: a host with no pinned build gets ``None``.

    One ``sha256`` covering every platform was a pin that matched exactly one
    machine and described the wrong bytes on all the others — an install that
    honoured it would have to either skip verification or refuse everywhere but
    the pinned platform. ``None`` here is what lets the installer refuse the
    unpinned host by name instead.
    """
    pin = load_manifest(_FULL, tier=Tier.PERSONAL).artifact
    assert pin is not None

    covered = pin.for_host("linux/arm64")

    assert covered is not None
    assert covered.sha256 == _SHA
    assert covered.member == "acme_2.3.1_linux_arm64/bin/acme"
    assert pin.for_host("darwin/arm64") is None


def test_a_platform_independent_pin_answers_for_every_host() -> None:
    """An npm tarball or a PyPI sdist is the same bytes everywhere.

    Keyed under ``any`` so a manifest never has to repeat one digest per
    platform to say "this build names no machine".
    """
    text = _FULL.replace(
        _ARTIFACT, f'[artifact]\npackage = "a"\nversion = "1"\n{_platform_block("any")}', 1
    )
    pin = load_manifest(text, tier=Tier.PERSONAL).artifact
    assert pin is not None

    assert pin.for_host("linux/arm64") is not None
    assert pin.for_host("darwin/amd64") is not None


def test_a_host_requirement_says_whether_arc_can_finish_its_login() -> None:
    """SPEC-064 — a button that claims to authorise an interactive CLI is a lie.

    ``gh auth login`` opens a browser and waits for a person; ``gh auth login
    --with-token`` reads the token on stdin and exits. Only the manifest knows
    which of the two a binary has, so a surface that guessed would offer half the
    shipped connectors a button that hangs. An empty ``token_command`` is the
    honest "a person must run this on the host".
    """
    text = _FULL.replace(
        '[[host_requires]]\nname = "node"\nminimum_version = "20.0.0"\n',
        '[[host_requires]]\nname = "acme"\nauthorize_command = "acme auth login"\n'
        'token_command = "acme auth login --with-token"\n',
        1,
    )

    requirement = load_manifest(text, tier=Tier.PERSONAL).host_requires[0]

    assert requirement.authorize_command == "acme auth login"
    assert requirement.token_command == "acme auth login --with-token"


def test_a_host_requirement_defaults_to_no_non_interactive_login() -> None:
    """Silence must mean "Arc cannot finish this", never "try it and see"."""
    assert load_manifest(_FULL, tier=Tier.PERSONAL).host_requires[0].token_command == ""


def test_a_host_requirement_declares_what_proves_it_is_signed_in() -> None:
    """SPEC-064 — the probe answers "does this program run", which is not the
    question the operator is being shown the answer to.

    ``dbxcli version`` succeeds on a ``dbxcli`` with no saved credentials at all,
    and that success was rendered as "Signed in". So the command that proves an
    account is connected is declared separately, and its pattern says what the
    output must contain when the exit code alone cannot tell the two apart.
    """
    text = _FULL.replace(
        '[[host_requires]]\nname = "node"\nminimum_version = "20.0.0"\n',
        '[[host_requires]]\nname = "acme"\nauthorize_command = "acme auth login"\n'
        'verify_command = "acme auth list"\nverify_pattern = "@"\n',
        1,
    )

    requirement = load_manifest(text, tier=Tier.PERSONAL).host_requires[0]

    assert requirement.verify_command == "acme auth list"
    assert requirement.verify_pattern == "@"


def test_a_host_requirement_defaults_to_no_authorisation_check() -> None:
    """Silence means "Arc cannot tell", which a surface must render as unknown —
    never as signed in, and never as signed out."""
    requirement = load_manifest(_FULL, tier=Tier.PERSONAL).host_requires[0]

    assert requirement.verify_command == ""
    assert requirement.verify_pattern == ""


def test_an_uncompilable_verify_pattern_is_refused_at_load() -> None:
    """A broken pattern must not become a runtime verdict in either direction."""
    text = _FULL.replace(
        '[[host_requires]]\nname = "node"\nminimum_version = "20.0.0"\n',
        '[[host_requires]]\nname = "acme"\nverify_command = "acme auth list"\n'
        'verify_pattern = "(unclosed"\n',
        1,
    )

    with pytest.raises(ValidationError, match="verify_pattern"):
        load_manifest(text, tier=Tier.PERSONAL)


def test_a_verify_pattern_without_a_verify_command_is_refused() -> None:
    """A pattern nothing runs is a control the operator believes is in force."""
    text = _FULL.replace(
        '[[host_requires]]\nname = "node"\nminimum_version = "20.0.0"\n',
        '[[host_requires]]\nname = "acme"\nverify_pattern = "@"\n',
        1,
    )

    with pytest.raises(ValidationError, match="verify_pattern"):
        load_manifest(text, tier=Tier.PERSONAL)


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
    assert manifest.artifact.platforms["linux/arm64"].sha256 == _SHA

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

    def test_tier_floor_is_read_only_by_the_refusal_and_the_listing(self) -> None:
        # The structural guard, and the only one that survives a future edit: a
        # floor that reached a policy resolver would hand a third-party bundle
        # author control over operator policy, and no behavioural assertion here
        # would see it happen.
        #
        # ``connections.py`` is the second and last permitted reader: it copies
        # the floor onto a catalog listing entry so a surface can SHOW which
        # bundles this deployment could run ("enterprise+"), and takes no verdict
        # from it — the listing is a display record with no resolver behind it.
        # That read is not new; it lived in ``arcui/routes/connectors.py``, out of
        # this scan's reach, until the connection façade (D-587) made one seam of
        # it. The set stays exact, so a THIRD reader — or a policy resolver
        # growing inside either of these two — still fails here.
        root = Path(manifest_module.__file__).resolve().parents[1]
        readers = sorted(
            path.relative_to(root)
            for path in root.rglob("*.py")
            # Attribute access only: ``resolve_tier_floor`` is the unrelated
            # SPEC-047 security-knob helper and must not count as a reader.
            if re.search(r"\.tier_floor\b", path.read_text(encoding="utf-8"))
        )

        assert readers == [Path("connections.py"), Path("extension/manifest.py")]


# --- what a token_command may name --------------------------------------------


def _manifest_with(body: str) -> ExtensionManifest:
    return load_manifest(
        '[extension]\nname = "acme"\nversion = "1.0.0"\nattachment = "cli"\n' + body,
        tier=Tier.PERSONAL,
    )


_SITE_FIELD = (
    '[[secrets]]\nname = "site"\nprompt = "p"\nsensitive = false\n'
    '[[secrets]]\nname = "api_token"\nprompt = "p"\n'
)

_HOST = (
    '[[host_requires]]\nname = "acme"\nauthorize_command = "acme login"\n'
    'token_command = "acme login --site={site} --token"\n'
)


def test_a_token_command_may_name_a_non_sensitive_declared_field() -> None:
    """The seam that lets a three-input login be finished without a person."""
    manifest = _manifest_with(_SITE_FIELD + _HOST)

    assert manifest.host_requires[0].token_command.endswith("--token")


def test_a_token_command_naming_a_field_the_bundle_never_declares_is_refused() -> None:
    """It would run with a literal ``{site}`` and fail in the binary's own words."""
    with pytest.raises(ValidationError, match="region"):
        _manifest_with(
            _SITE_FIELD + '[[host_requires]]\nname = "acme"\nauthorize_command = "acme login"\n'
            'token_command = "acme login --region={region} --token"\n'
        )


def test_a_token_command_naming_a_credential_is_refused() -> None:
    """argv is the process table. A sensitive field may only ever cross on stdin."""
    with pytest.raises(ValidationError, match="api_token"):
        _manifest_with(
            _SITE_FIELD + '[[host_requires]]\nname = "acme"\nauthorize_command = "acme login"\n'
            'token_command = "acme login --token={api_token}"\n'
        )


# --- what a command's fixed argv may name --------------------------------------

_CLI_BLOCK = (
    '[config.cli]\nbinary = "acme"\n\n'
    '[[config.cli.commands]]\ntool = "acme_list"\nargv = ["list", "--vault={vault}"]\n'
)


def test_a_command_argv_may_name_a_non_sensitive_declared_field() -> None:
    """The seam that keeps a blast-radius value out of the model's hands."""
    manifest = _manifest_with(
        '[[secrets]]\nname = "vault"\nprompt = "p"\nsensitive = false\n' + _CLI_BLOCK
    )

    assert manifest.config["cli"]["commands"][0]["argv"][-1] == "--vault={vault}"


def test_a_command_argv_naming_a_credential_is_refused() -> None:
    """Every verb's argv is in the process table, not just the login's."""
    with pytest.raises(ValidationError, match="vault"):
        _manifest_with('[[secrets]]\nname = "vault"\nprompt = "p"\n' + _CLI_BLOCK)


def test_a_command_argv_naming_a_field_the_bundle_never_declares_is_refused() -> None:
    """It would reach the binary literally and read as a vault named '{vault}'."""
    with pytest.raises(ValidationError, match="vault"):
        _manifest_with('[[secrets]]\nname = "other"\nprompt = "p"\nsensitive = false\n' + _CLI_BLOCK)
