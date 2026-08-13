"""SPEC-066 T-972 — the module-capability trust class.

A ``module:*`` scan root used to be TRUSTED, which was defensible while modules
shipped inside the wheel and is not now that ``module_root()`` is a deployment
directory an install writes into. It is now VERIFIED: every artifact under it
passes the same signature + TOFU gate an agent-writable root does, and none of
it is contained.

Those two halves are separable and both are load-bearing, so both are measured:

* **Verified.** No sidecar, a sidecar signed by a key nobody pinned, or bytes
  edited after signing — each denies, with the loader's own verdict rather than
  a generic error. That is the whole point of the change.
* **Not contained.** The AST import allowlist and the ArcRun-isolated proxy
  exist to hold code the MODEL wrote. Applying them to a module was measured at
  17 of 18 modules registering ZERO tools, because a module uses ``@hook`` /
  ``@background_task`` / ``@capability`` — which the authored path does not
  register at all — and imports stdlib the allowlist blocks. Without the
  not-contained case here, this change silently reverts to that.

Everything installs the way an operator does:
:func:`arcbundle.build_bundle` → :func:`~arcbundle.verify_bundle` →
:func:`~arcbundle.materialize` → :func:`arcagent.trust_bundled_capabilities`.
Nothing is stubbed and no sidecar is hand-written, so the signatures under test
are the ones ``arc module install`` actually produces.
"""

from __future__ import annotations

from pathlib import Path

import arcbundle
import pytest
from arctrust import ValidatorsConfig, generate_keypair, load_validators

import arcagent
from arcagent.capabilities.capability_loader import (
    CapabilityLoader,
    RootTrust,
    is_untrusted_root,
    pin_name_for_path,
    root_trust,
)
from arcagent.capabilities.capability_registry import CapabilityRegistry
from arcagent.capabilities.inventory import resolve_trust_posture
from arcagent.core.config import CapabilitiesConfig, SecurityConfig

_ISSUER = "did:arc:module-trust-test-issuer"
_MODULE = "probe"

#: ``os`` is in the agent-authored allowlist's blocked "filesystem" group at
#: every tier. A tool that imports it is refused by the AST validator and never
#: reaches the isolated proxy, so its presence in the live registry is proof
#: that a module root skipped containment — not merely that something loaded.
_CAPABILITIES = """\
import os

from arcagent.tools._decorator import background_task, hook, tool


@tool(description="probe", version="1.0.0", classification="read_only")
async def probe_separator() -> str:
    return os.sep


@hook(event="agent:post_respond", name="probe_hook")
async def probe_hook(data: dict) -> None:
    return None


@background_task(interval=3600.0, name="probe_loop")
async def probe_loop() -> None:
    return None
"""

#: A module folder is one only if it ships both files; discovery reads both.
_RUNTIME = """\
def state() -> None:
    return None
"""

_CONFIG_TOML = """\
[agent]
name = "probe-agent"
org = "arc"
type = "exec"

[llm]
model = "test/model"

[security]
tier = "{tier}"

[modules.probe]
enabled = true
"""


# --------------------------------------------------------------------------
# Installing a module the way an operator does
# --------------------------------------------------------------------------


def _source(tmp_path: Path, *, capabilities: str = _CAPABILITIES) -> Path:
    source = tmp_path / "src" / _MODULE
    source.mkdir(parents=True)
    (source / "capabilities.py").write_text(capabilities, encoding="utf-8")
    (source / "_runtime.py").write_text(_RUNTIME, encoding="utf-8")
    return source


def _install(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    capabilities: str = _CAPABILITIES,
) -> tuple[Path, bytes]:
    """Build, verify, and materialize a signed bundle; return the module dir + key.

    ``ARC_CONFIG_DIR`` is set first and the module lands under it, because the
    pin name a module capability is gated under is derived from the deployment
    module root — a module materialized somewhere else would be adjudicated
    under a different name than the one an operator approves.
    """
    monkeypatch.setenv("ARC_CONFIG_DIR", str(tmp_path / "arc"))
    keypair = generate_keypair()
    bundle = arcbundle.build_bundle(
        _source(tmp_path, capabilities=capabilities),
        module=_MODULE,
        version="1.0.0",
        private_key=keypair.private_key,
        issuer=_ISSUER,
        out=tmp_path / "bundles" / f"{_MODULE}.arcbundle",
    )
    verified = arcbundle.verify_bundle(
        bundle, tier="personal", trusted_issuers={_ISSUER: keypair.public_key}
    )
    installed = arcbundle.materialize(verified, arcagent.module_root())
    assert verified.issuer_key == keypair.public_key
    return installed, keypair.public_key


def _agent_config(tmp_path: Path, *, tier: str) -> Path:
    """A real ``arcagent.toml`` — the sole persistence surface for both pins."""
    config_path = tmp_path / "agent" / "arcagent.toml"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(_CONFIG_TOML.format(tier=tier), encoding="utf-8")
    return config_path


async def _scan(
    module_dir: Path, config_path: Path, *, tier: str
) -> tuple[CapabilityRegistry, dict[str, tuple[str, str]]]:
    """Scan the module root at ``tier``'s real posture; return registry + verdicts.

    The posture comes from :func:`resolve_trust_posture`, the same helper
    ``setup_capabilities`` uses, so a verdict here is the verdict a booted agent
    gets. No agent DID key is supplied: a module is signed by its issuer, never
    by the agent, and passing the agent's key would hide an unpinned issuer.
    """
    validators = load_validators(config_path)
    posture = resolve_trust_posture(
        SecurityConfig(tier=tier, validators=validators),
        CapabilitiesConfig(),
        trusted_public_key=None,
    )
    registry = CapabilityRegistry()
    loader = CapabilityLoader(
        scan_roots=[(f"module:{_MODULE}", module_dir)],
        registry=registry,
        tofu=posture.tofu,
        require_signature=posture.require_signature,
        trusted_public_keys=posture.trusted_public_keys,
        import_policy=posture.import_policy,
        spawn_background_tasks=False,
    )
    delta = await loader.scan_and_register()
    verdicts = {
        outcome.name: (outcome.status, outcome.status_detail) for outcome in delta.outcomes
    }
    return registry, verdicts


def _trust(module_dir: Path, config_path: Path, issuer_key: bytes) -> list[Path]:
    """The install-time trust grant: pin the issuer key, pin each artifact's hash."""
    return arcagent.trust_bundled_capabilities(
        module_dir,
        config_path=config_path,
        issuer_key=issuer_key,
        issuer_did=_ISSUER,
    )


# --------------------------------------------------------------------------
# The trust classes themselves
# --------------------------------------------------------------------------


def test_every_root_lands_in_a_named_trust_class() -> None:
    """No root is trusted by absence — the failure this task exists to close."""
    assert root_trust("builtins") is RootTrust.TRUSTED
    assert root_trust("builtins-skills") is RootTrust.TRUSTED
    assert root_trust("module:scheduler") is RootTrust.VERIFIED
    for name in ("workspace", "global", "agent", "agent-skills"):
        assert root_trust(name) is RootTrust.UNTRUSTED
    # An unclassified name costs privilege rather than granting it.
    assert root_trust("something-nobody-classified") is RootTrust.UNTRUSTED


def test_extension_roots_keep_their_untrusted_containment() -> None:
    """SPEC-062's classification is untouched: extensions stay contained."""
    for name in ("extension:reference_service", "extension:reference_service-skills"):
        assert root_trust(name) is RootTrust.UNTRUSTED
        assert is_untrusted_root(name)


def test_module_pin_names_are_qualified_by_their_module(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two modules' ``capabilities.py`` cannot collide on one TOFU pin.

    Every module ships a file with that name, so a bare stem would give them all
    one pin: approving the second would supersede the first's hash and the
    loader would then read the first as drifted — a hard DENY for no reason but
    the name.
    """
    monkeypatch.setenv("ARC_CONFIG_DIR", str(tmp_path / "arc"))
    root = arcagent.module_root()
    assert pin_name_for_path(root / "scheduler" / "capabilities.py") == "scheduler/capabilities"
    assert pin_name_for_path(root / "tasks" / "capabilities.py") == "tasks/capabilities"
    # Outside the module root the rule is unchanged (SPEC-033 pins keep working).
    assert pin_name_for_path(tmp_path / "caps" / "echo.py") == "echo"
    assert pin_name_for_path(tmp_path / "caps" / "skills" / "reporter" / "SKILL.md") == "reporter"


# --------------------------------------------------------------------------
# Verified: a valid signature loads, everything else does not
# --------------------------------------------------------------------------


@pytest.mark.parametrize("tier", ["personal", "enterprise", "federal"])
async def test_a_validly_signed_module_capability_loads_and_registers(
    tier: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The bundle's own sidecar clears the gate at every tier, federal included."""
    module_dir, issuer_key = _install(tmp_path, monkeypatch)
    config_path = _agent_config(tmp_path, tier=tier)
    assert _trust(module_dir, config_path, issuer_key), "install trusted nothing"

    registry, verdicts = await _scan(module_dir, config_path, tier=tier)

    assert verdicts["probe_separator"] == ("loaded", "")
    assert await registry.get_tool("probe_separator") is not None


@pytest.mark.parametrize("tier", ["enterprise", "federal"])
async def test_an_unsigned_module_capability_is_denied_as_unsigned(
    tier: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No sidecar → the verdict is ``unsigned``, not a generic failure.

    An operator reading ``arc trust list`` has to be able to tell "nobody signed
    this" from "this was tampered with"; they lead to different actions.
    """
    module_dir, issuer_key = _install(tmp_path, monkeypatch)
    config_path = _agent_config(tmp_path, tier=tier)
    _trust(module_dir, config_path, issuer_key)
    _unsign(module_dir / "capabilities.py")

    registry, verdicts = await _scan(module_dir, config_path, tier=tier)

    status, detail = verdicts[f"{_MODULE}/capabilities"]
    assert status == "unsigned", f"expected unsigned, got {status}: {detail}"
    assert await registry.get_tool("probe_separator") is None


@pytest.mark.parametrize("tier", ["personal", "enterprise", "federal"])
async def test_a_module_capability_edited_after_signing_is_denied(
    tier: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The whole point: edited bytes stop loading, at every tier.

    Personal is included deliberately. It is the tier where the deployment root
    is most likely to be poked at by hand, and a change that only bites at
    federal would leave every developer box running unverified module code.
    """
    module_dir, issuer_key = _install(tmp_path, monkeypatch)
    config_path = _agent_config(tmp_path, tier=tier)
    _trust(module_dir, config_path, issuer_key)
    _edit(module_dir / "capabilities.py", _CAPABILITIES + "\nEDITED = True\n")

    registry, verdicts = await _scan(module_dir, config_path, tier=tier)

    status, _ = verdicts[f"{_MODULE}/capabilities"]
    assert status != "loaded", "an edited module capability still loaded"
    assert await registry.get_tool("probe_separator") is None


async def test_a_sidecar_from_an_unpinned_issuer_does_not_load(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A signature nobody pinned is attribution to a stranger, not authority.

    Installing without the trust grant is exactly the state a hand-copied module
    tree is in, so this is also the regression gate on ``arc module install``
    pinning the issuer at all.
    """
    module_dir, _ = _install(tmp_path, monkeypatch)
    config_path = _agent_config(tmp_path, tier="enterprise")

    registry, verdicts = await _scan(module_dir, config_path, tier="enterprise")

    assert verdicts[f"{_MODULE}/capabilities"][0] == "unsigned"
    assert await registry.get_tool("probe_separator") is None


# --------------------------------------------------------------------------
# Not contained: the half that keeps 17 of 18 modules alive
# --------------------------------------------------------------------------


async def test_a_module_capability_is_verified_but_not_isolated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A module tool importing blocked stdlib registers AND runs.

    ``import os`` is refused by the agent-authored import allowlist at every
    tier, so a module root that routed through containment could not produce
    this tool at all. Calling it matters as much as finding it: an isolated
    proxy would register the name and then fail to execute.
    """
    module_dir, issuer_key = _install(tmp_path, monkeypatch)
    config_path = _agent_config(tmp_path, tier="federal")
    _trust(module_dir, config_path, issuer_key)

    registry, _ = await _scan(module_dir, config_path, tier="federal")

    entry = await registry.get_tool("probe_separator")
    assert entry is not None, "a module tool importing blocked stdlib did not register"
    assert await entry.execute() == "/"


async def test_a_module_registers_hooks_and_background_tasks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``@hook`` and ``@background_task`` survive the gate.

    The authored path parses inert ``@tool`` metadata and nothing else, so these
    two decorators register only on an imported module. They are how most module
    behaviour reaches an agent — the scheduler engine, the tasks dispatch loop —
    which is why "17 of 18 modules registered zero tools" understated the damage.
    """
    module_dir, issuer_key = _install(tmp_path, monkeypatch)
    config_path = _agent_config(tmp_path, tier="federal")
    _trust(module_dir, config_path, issuer_key)

    registry, _ = await _scan(module_dir, config_path, tier="federal")

    hooks = await registry.get_hooks("agent:post_respond")
    assert [entry.meta.name for entry in hooks] == ["probe_hook"]
    assert await registry.get_task("probe_loop") is not None


# --------------------------------------------------------------------------
# The operator loop: gated → listed → re-signed → loading
# --------------------------------------------------------------------------


async def test_a_gated_module_capability_is_visible_to_list_gated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``arc trust list`` reads this seam, and it is what unblocks a gated module.

    The inventory used to resolve four scan roots and no module roots, so a
    module capability the loader refused was invisible to the only surface that
    could approve it.

    It lists the artifact the LOADER adjudicates — the agent's own copy — because
    that is the file an operator has to read and re-sign to unblock the module.
    Approving the shared original would leave this agent gated forever.
    """
    module_dir, _ = _install(tmp_path, monkeypatch)
    config_path = _agent_config(tmp_path, tier="enterprise")
    copy_dir = arcbundle.copy_capabilities(module_dir, config_path.parent, module=_MODULE)
    _edit(copy_dir / "capabilities.py", _CAPABILITIES + "\nEDITED = True\n")

    gated = await arcagent.list_gated(config_path.parent, agent_id="probe-agent")

    names = {item.name for item in gated}
    assert f"{_MODULE}/capabilities" in names, f"module capability not listed; saw {sorted(names)}"
    item = next(item for item in gated if item.name == f"{_MODULE}/capabilities")
    assert Path(item.path) == copy_dir / "capabilities.py"


@pytest.mark.parametrize("tier", ["personal", "enterprise", "federal"])
async def test_an_edited_module_capability_loads_again_after_re_signing(
    tier: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Josh's re-sign requirement, proven through the seam ``arc trust approve`` uses.

    The operator who modified the file signs it with THEIR key: the sidecar is
    rewritten, their key is pinned alongside the issuer's, and the new bytes are
    approved. Both keys must keep working — an operator re-signing one file must
    not gate every other module the issuer signed.

    Note the mode dance this exercises without saying so: a materialized module
    is ``0444`` inside ``0555``, so writing the sidecar in place is impossible
    unless the signing path borrows write permission and hands it back.
    """
    module_dir, issuer_key = _install(tmp_path, monkeypatch)
    config_path = _agent_config(tmp_path, tier=tier)
    _trust(module_dir, config_path, issuer_key)
    artifact = module_dir / "capabilities.py"
    _edit(artifact, _CAPABILITIES + "\nEDITED = True\n")

    _, before = await _scan(module_dir, config_path, tier=tier)
    assert before[f"{_MODULE}/capabilities"][0] != "loaded", "the edit was not caught at all"

    operator = generate_keypair()
    arcagent.sign_capability(
        artifact,
        signer_did="did:arc:operator:test",
        signer=_InProcessOperator(operator.private_key),
        config_path=config_path,
    )

    registry, after = await _scan(module_dir, config_path, tier=tier)

    # A file that loads is reported per registered TOOL; the pin name only ever
    # names a candidate that was refused, so its absence here is the pass.
    assert f"{_MODULE}/capabilities" not in after
    assert after["probe_separator"] == ("loaded", "")
    assert await registry.get_tool("probe_separator") is not None
    validators = load_validators(config_path)
    assert issuer_key.hex() in validators.trusted_keys, "re-signing evicted the issuer's key"
    assert operator.public_key.hex() in validators.trusted_keys


def test_install_records_both_pins_where_an_operator_can_see_them(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The trust an install grants is written into the agent's own config.

    A trust anchor added silently is one nobody can audit or withdraw, so the
    key and every approved artifact land in ``[security.validators]`` — the same
    block ``arc trust`` reads and writes.
    """
    module_dir, issuer_key = _install(tmp_path, monkeypatch)
    config_path = _agent_config(tmp_path, tier="federal")

    trusted = _trust(module_dir, config_path, issuer_key)

    validators = load_validators(config_path)
    assert issuer_key.hex() in validators.trusted_keys
    approved = {entry.name for entry in validators.approved}
    assert {f"{_MODULE}/capabilities", f"{_MODULE}/_runtime"} <= approved
    assert module_dir / "capabilities.py" in trusted


def test_install_refuses_to_pin_an_artifact_the_issuer_did_not_sign(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A planted sidecar cannot nominate its own trust anchor.

    ``trust_bundled_capabilities`` pins the key the MANIFEST verified under and
    approves only artifacts that verify beneath it, so a sidecar signed by
    anyone else is left gated instead of being approved into the config.
    """
    module_dir, issuer_key = _install(tmp_path, monkeypatch)
    config_path = _agent_config(tmp_path, tier="federal")
    stranger = generate_keypair()
    artifact = module_dir / "capabilities.py"
    _edit(artifact, _CAPABILITIES + "\nEDITED = True\n")
    arcagent.write_signature(
        artifact,
        artifact.read_bytes(),
        signer_did="did:arc:stranger",
        private_key=stranger.private_key,
    )

    trusted = _trust(module_dir, config_path, issuer_key)

    assert artifact not in trusted
    approved = {entry.name for entry in load_validators(config_path).approved}
    assert f"{_MODULE}/capabilities" not in approved
    assert stranger.public_key.hex() not in load_validators(config_path).trusted_keys


def test_the_default_validators_config_pins_nothing(tmp_path: Path) -> None:
    """Control: the pins asserted above are written, not inherited from a default."""
    del tmp_path
    assert ValidatorsConfig().trusted_keys == ()
    assert ValidatorsConfig().approved == ()


# --------------------------------------------------------------------------
# Helpers that have to defeat the deployment root's read-only modes
# --------------------------------------------------------------------------


def _unsign(artifact: Path) -> None:
    """Delete ``artifact``'s sidecar, defeating the deployment tree's 0555 dirs."""
    artifact.parent.chmod(0o755)
    arcagent.sidecar_path(artifact).unlink()


def _edit(artifact: Path, content: str) -> None:
    """Modify a materialized artifact in place, the way a person on the box would.

    ``chmod`` on the FILE only, exactly what ``chmod u+w capabilities.py``
    gives you: the deployment tree is deliberately read-only, and rewriting a
    file in place never needs its directory writable. Leaving the ``0555`` on
    the parent is what makes the re-sign case prove the signing path can write
    a sidecar there at all.
    """
    artifact.chmod(0o644)
    artifact.write_text(content, encoding="utf-8")


class _InProcessOperator:
    """A minimal ``arctrust.Signer`` over a raw seed — the personal custody model."""

    def __init__(self, seed: bytes) -> None:
        from arctrust import ED25519, InProcessSigner

        self._signer = InProcessSigner(seed, ED25519)
        self.algorithm = ED25519

    @property
    def public_key(self) -> bytes:
        key: bytes = self._signer.public_key
        return key

    def sign(self, payload: bytes) -> bytes:
        signature: bytes = self._signer.sign(payload)
        return signature
