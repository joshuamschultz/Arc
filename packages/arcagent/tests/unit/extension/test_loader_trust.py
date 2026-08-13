"""SPEC-062 T-882 (RED) — the extension root is UNTRUSTED and the gate runs at load.

COMP-003, serving REQ-281/282/283. These are the tests the whole spec rests on:
an extension is third-party code, so the one thing that must never happen is it
arriving through a door that skips the trust gate.

The trusted-root inversion is subtle and silent. ``CapabilityLoader`` runs the
AST validator + the isolated execution path only for a root that classifies as
``RootTrust.UNTRUSTED``. When this suite was written, module roots were trusted
by absence — they fell off the end of a membership test — and a bundle loaded
through a module-shaped root would have inherited that trust with every test
still green. SPEC-066 T-972 closed that: ``root_trust`` now names three classes
and defaults to UNTRUSTED, and ``module:*`` is VERIFIED (signature gate, no
isolation). An extension root is untrusted on both counts and must stay so. Hence:

* the extension root must classify as untrusted (asserted at the classifier, the
  single source of truth — not at a copy);
* an unsigned bundle must be DENIED above personal and allowed-with-audit at
  personal;
* requiring a signature without a PINNED key is no requirement at all
  (``capability_loader.py:334-337``: unpinned, arctrust accepts any
  self-consistent signature) — so it must refuse;
* verification runs at LOAD, independently of any install-time check, and ANY
  exception denies.

Nothing here patches loader internals. The end-to-end tests drive the real
``CapabilityLoader`` into a real ``CapabilityRegistry`` and assert on what did or
did not register — a correct-looking predicate with dead wiring cannot pass.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest
from arctrust.audit import AuditEvent
from arctrust.identity import AgentIdentity

from arcagent.capabilities import artifact_signing
from arcagent.capabilities.capability_loader import is_untrusted_root
from arcagent.capabilities.capability_registry import CapabilityRegistry
from arcagent.core.errors import ExtensionError
from arcagent.core.tier import Tier
from arcagent.extension.loader import ExtensionLoader

_MANIFEST = """
[extension]
name = "{name}"
version = "1.0.0"
attachment = "native"
tier_floor = "personal"

[tools]
allow = ["{name}_ping"]

[[tools.declared]]
name = "{name}_ping"
classification = "read_only"
capability_tags = ["external_comms"]
"""

_TOOL = (
    "from arcagent.tools._decorator import tool\n"
    "@tool(description='pings the upstream', version='1.0.0')\n"
    "async def {name}_ping() -> str:\n"
    "    return 'pong'\n"
)


class RecordingSink:
    """Audit sink keeping every event. ``write`` only — the arctrust protocol."""

    def __init__(self) -> None:
        self.events: list[AuditEvent] = []

    def write(self, event: AuditEvent) -> None:
        self.events.append(event)

    def actions(self) -> list[str]:
        return [event.action for event in self.events]


def _sign(path: Path, signer: AgentIdentity) -> None:
    artifact_signing.write_signature(
        path, path.read_bytes(), signer_did=signer.did, private_key=signer.signing_seed
    )


def _write_bundle(root: Path, name: str, *, sign_with: AgentIdentity | None) -> Path:
    """Materialise a bundle: manifest + one capability tool, each signed or not."""
    bundle = root / name
    bundle.mkdir(parents=True, exist_ok=True)
    manifest = bundle / "extension.toml"
    manifest.write_text(_MANIFEST.format(name=name), encoding="utf-8")
    tool_py = bundle / f"{name}_tools.py"
    tool_py.write_text(_TOOL.format(name=name), encoding="utf-8")
    if sign_with is not None:
        _sign(manifest, sign_with)
        _sign(tool_py, sign_with)
    return bundle


def _loader(
    root: Path,
    *,
    tier: Tier,
    sink: RecordingSink,
    registry: CapabilityRegistry | None = None,
    trusted_public_key: bytes | None = None,
) -> ExtensionLoader:
    return ExtensionLoader(
        roots=[root],
        registry=registry if registry is not None else CapabilityRegistry(),
        tier=tier,
        audit_sink=sink,
        trusted_public_key=trusted_public_key,
    )


# --- REQ-281: the extension root is untrusted -------------------------------


def test_an_extension_root_classifies_as_untrusted() -> None:
    """The classifier the loader gates on must call an extension root untrusted.

    Asserted against ``is_untrusted_root`` itself so this cannot be satisfied by a
    second, parallel notion of trust living in the extension package.
    """
    assert is_untrusted_root("extension:acme_tickets") is True


def test_a_module_root_remains_trusted() -> None:
    """Guards the other direction: shipped module code must not start failing the
    gate, or the fix for REQ-281 would break every module."""
    assert is_untrusted_root("module:scheduler") is False


@pytest.mark.parametrize(
    "root_name",
    ["workspace", "global", "agent", "workspace-skills", "global-skills", "agent-skills"],
)
def test_every_agent_writable_root_stays_untrusted(root_name: str) -> None:
    """Pins the set itself, not just the prefix rule.

    ``is_untrusted_root`` is one ``or`` over a frozenset and a prefix. The prefix
    half is covered above; without this, a name could be dropped from the set and
    the only signal would be third-party code silently loading trusted.
    """
    assert is_untrusted_root(root_name) is True


@pytest.mark.parametrize("root_name", ["builtins", "builtins-skills"])
def test_shipped_package_roots_stay_trusted(root_name: str) -> None:
    """The complement: package code Arc ships is the only skills root left trusted."""
    assert is_untrusted_root(root_name) is False


@pytest.mark.asyncio
async def test_bundle_under_a_module_root_is_refused(tmp_path: Path) -> None:
    """A bundle sitting in a module-shaped root never loads: module roots are trusted.

    This is the inversion REQ-281 exists to prevent — third-party code inheriting
    the trust that shipped package code has.
    """
    modules_dir = tmp_path / "modules"
    bundle = _write_bundle(modules_dir, "acme_tickets", sign_with=None)
    sink = RecordingSink()
    loader = _loader(tmp_path / "extensions", tier=Tier.PERSONAL, sink=sink)

    with pytest.raises(ExtensionError):
        await loader.load_path(bundle)

    assert "extension.blocked" in sink.actions()
    blocked = next(e for e in sink.events if e.action == "extension.blocked")
    assert blocked.outcome == "deny"


@pytest.mark.asyncio
async def test_an_unsigned_bundle_registers_no_capability_above_personal(
    tmp_path: Path,
) -> None:
    """The end-to-end proof the gate is live: nothing from an unsigned bundle
    reaches the registry at enterprise.

    If the extension root were registered as TRUSTED, ``CapabilityLoader`` would
    skip the gate entirely and this tool WOULD register — which is precisely the
    dead-wiring failure a boolean-only assertion would miss.
    """
    root = tmp_path / "extensions"
    _write_bundle(root, "acme_tickets", sign_with=None)
    identity = AgentIdentity.generate(org="arc", agent_type="exec")
    registry = CapabilityRegistry()
    sink = RecordingSink()
    loader = _loader(
        root,
        tier=Tier.ENTERPRISE,
        sink=sink,
        registry=registry,
        trusted_public_key=identity.public_key,
    )

    with pytest.raises(ExtensionError):
        await loader.load("acme_tickets")

    assert await registry.get_tool("acme_tickets_ping") is None


@pytest.mark.asyncio
async def test_a_signed_bundle_registers_its_capability_above_personal(
    tmp_path: Path,
) -> None:
    """The positive control: with a valid signature under the pinned key the same
    bundle loads and its tool registers. Without this, the denial tests could pass
    against a loader that simply never loads anything."""
    root = tmp_path / "extensions"
    identity = AgentIdentity.generate(org="arc", agent_type="exec")
    _write_bundle(root, "acme_tickets", sign_with=identity)
    registry = CapabilityRegistry()
    sink = RecordingSink()
    loader = _loader(
        root,
        tier=Tier.ENTERPRISE,
        sink=sink,
        registry=registry,
        trusted_public_key=identity.public_key,
    )

    await loader.load("acme_tickets")

    assert await registry.get_tool("acme_tickets_ping") is not None


# --- REQ-282: signature verified at load, above personal --------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("tier", [Tier.ENTERPRISE, Tier.FEDERAL])
async def test_unsigned_bundle_is_denied_above_personal(tmp_path: Path, tier: Tier) -> None:
    """Above personal an unsigned bundle refuses to load and the refusal is audited."""
    root = tmp_path / "extensions"
    _write_bundle(root, "acme_tickets", sign_with=None)
    identity = AgentIdentity.generate(org="arc", agent_type="exec")
    sink = RecordingSink()
    loader = _loader(root, tier=tier, sink=sink, trusted_public_key=identity.public_key)

    with pytest.raises(ExtensionError):
        await loader.load("acme_tickets")

    blocked = next(e for e in sink.events if e.action == "extension.blocked")
    assert blocked.outcome == "deny"
    assert blocked.extra["reason"] == "unsigned"


@pytest.mark.asyncio
async def test_unsigned_bundle_loads_at_personal_with_an_audit_warning(
    tmp_path: Path,
) -> None:
    """Personal is a stringency dial, not a different code path: the load succeeds
    but the unverified state is recorded rather than passing silently."""
    root = tmp_path / "extensions"
    _write_bundle(root, "acme_tickets", sign_with=None)
    registry = CapabilityRegistry()
    sink = RecordingSink()
    loader = _loader(root, tier=Tier.PERSONAL, sink=sink, registry=registry)

    await loader.load("acme_tickets")

    unverified = next(e for e in sink.events if e.action == "extension.unverified")
    assert unverified.outcome == "allow"
    assert unverified.extra["reason"] == "unsigned"


@pytest.mark.asyncio
async def test_a_bundle_signed_by_the_wrong_key_is_denied(tmp_path: Path) -> None:
    """A self-consistent signature from an unpinned key is a forgery, not a signature."""
    root = tmp_path / "extensions"
    attacker = AgentIdentity.generate(org="evil", agent_type="exec")
    operator = AgentIdentity.generate(org="arc", agent_type="exec")
    _write_bundle(root, "acme_tickets", sign_with=attacker)
    sink = RecordingSink()
    loader = _loader(root, tier=Tier.ENTERPRISE, sink=sink, trusted_public_key=operator.public_key)

    with pytest.raises(ExtensionError):
        await loader.load("acme_tickets")


@pytest.mark.asyncio
async def test_verification_runs_at_load_not_only_at_install(tmp_path: Path) -> None:
    """Tampering AFTER a clean install must still be caught.

    The bundle is signed, loaded successfully once (standing in for the
    install-time check having passed), then its bytes are edited without
    re-signing. The next load must refuse — proving load-time verification is
    independent of any recorded install-time verdict, and is not cached.
    """
    root = tmp_path / "extensions"
    identity = AgentIdentity.generate(org="arc", agent_type="exec")
    bundle = _write_bundle(root, "acme_tickets", sign_with=identity)
    sink = RecordingSink()
    loader = _loader(root, tier=Tier.ENTERPRISE, sink=sink, trusted_public_key=identity.public_key)
    await loader.load("acme_tickets")  # install-time verification passes

    tampered = bundle / "acme_tickets_tools.py"
    tampered.write_text(
        tampered.read_text(encoding="utf-8").replace("'pong'", "'exfiltrated'"),
        encoding="utf-8",
    )

    registry = CapabilityRegistry()
    fresh = _loader(
        root,
        tier=Tier.ENTERPRISE,
        sink=sink,
        registry=registry,
        trusted_public_key=identity.public_key,
    )
    with pytest.raises(ExtensionError):
        await fresh.load("acme_tickets")
    assert await registry.get_tool("acme_tickets_ping") is None


# --- REQ-283: an unpinned signature requirement is no requirement -----------


@pytest.mark.asyncio
@pytest.mark.parametrize("tier", [Tier.ENTERPRISE, Tier.FEDERAL])
async def test_signature_required_without_a_pinned_key_refuses(tmp_path: Path, tier: Tier) -> None:
    """Requiring a signature with no pinned key must NOT count as satisfied.

    Unpinned, arctrust accepts any self-consistent signature — so an attacker
    signs with their own key and passes. The bundle here is validly signed by an
    unrelated identity precisely to prove the missing PIN, not a missing
    signature, is what denies.
    """
    root = tmp_path / "extensions"
    anyone = AgentIdentity.generate(org="whoever", agent_type="exec")
    _write_bundle(root, "acme_tickets", sign_with=anyone)
    registry = CapabilityRegistry()
    sink = RecordingSink()
    loader = _loader(root, tier=tier, sink=sink, registry=registry, trusted_public_key=None)

    with pytest.raises(ExtensionError):
        await loader.load("acme_tickets")

    assert await registry.get_tool("acme_tickets_ping") is None
    blocked = next(e for e in sink.events if e.action == "extension.blocked")
    assert blocked.extra["reason"] == "no_pinned_key"


# --- Fail-closed: any exception denies --------------------------------------


@pytest.mark.asyncio
@pytest.mark.skipif(
    hasattr(os, "geteuid") and os.geteuid() == 0,
    reason="root bypasses file permissions, so the read cannot be made to raise",
)
async def test_an_unreadable_bundle_denies_rather_than_propagating(tmp_path: Path) -> None:
    """Any exception inside verification is a DENY, never a crash and never a pass.

    The manifest is made unreadable so the real read-and-verify path raises
    ``PermissionError``. Fail-closed means an ``ExtensionError`` denial with an
    audit event — not the raw OS error escaping to the caller.
    """
    root = tmp_path / "extensions"
    identity = AgentIdentity.generate(org="arc", agent_type="exec")
    bundle = _write_bundle(root, "acme_tickets", sign_with=identity)
    manifest = bundle / "extension.toml"
    manifest.chmod(stat.S_IWUSR)  # write-only: any read raises PermissionError
    sink = RecordingSink()
    loader = _loader(root, tier=Tier.ENTERPRISE, sink=sink, trusted_public_key=identity.public_key)

    try:
        with pytest.raises(ExtensionError):
            await loader.load("acme_tickets")
    finally:
        manifest.chmod(stat.S_IRUSR | stat.S_IWUSR)

    assert "extension.blocked" in sink.actions()
