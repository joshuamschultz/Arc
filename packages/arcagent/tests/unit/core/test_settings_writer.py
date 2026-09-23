"""Tests for SignedSettingsWriter (SPEC-085 COMP-004, REQ-461/462).

``arcagent.core.settings_writer.apply_setting_change()`` is the ONLY path a
Settings-screen edit may take to reach disk (ADR-A, SDD ADR-A): it validates
a ``{field, value}`` change against the schema emitted by
``arcagent.core.config_schema.emit_form_schema()``, then writes a signed
overlay through the arcprompt-pattern (signed content + detached ``.arcsig``
sidecar, ADR-029) under ``<agent_root>/settings/<dotted.field.id>.toml``, and
emits exactly one ``AuditEvent`` for the change.

Pinned API (this is what GREEN must match):

    def apply_setting_change(
        *,
        field: str,
        value: Any,
        agent_root: Path,
        signer: Signer,
        signer_did: str,
        audit_sink: AuditSink,
        tier: str = "personal",
    ) -> SettingChangeResult:
        ...

    class SettingChangeResult:
        overlay_path: Path       # <agent_root>/settings/<field>.toml
        signature: ArtifactSignature

    class SettingChangeRefused(arcagent.core.errors.ConfigError):
        ...  # raised on any refusal below; NOTHING is written

Refusal cases (REQ-462, no partial write) — each must raise
``SettingChangeRefused`` and leave ``<agent_root>/settings/`` exactly as it
was before the call (absent, or unchanged if already present):

  1. ``value`` violates the field's ``type``/``ge``/``le`` constraints.
  2. ``field`` is not editable per the schema (denylisted, e.g.
     ``identity.key_dir`` — see ``config_loading._ENV_DENYLIST_PREFIXES``).
  3. ``field`` attempts path traversal (contains ``..`` or is absolute) — the
     overlay path must stay confined under ``<agent_root>/settings/``.

Success (REQ-461):

  - the overlay ``.toml`` and its ``.arcsig`` sidecar both exist under
    ``<agent_root>/settings/``;
  - ``arctrust.artifact.verify_artifact(overlay_bytes, signature)`` is True,
    and stays True when pinned to the signer's own public key (personal-tier
    self-signed round trip);
  - exactly one ``AuditEvent`` was emitted to the audit sink, with
    ``action == "settings.write"``, ``outcome == "applied"``,
    ``actor_did == signer_did``, and ``target == field``.

``arcagent.core.settings_writer`` does not exist yet. Every failure below
must be a ``ModuleNotFoundError`` for the ABSENT module, not a bug in these
tests.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from arctrust.artifact import verify_artifact
from arctrust.audit import AuditEvent
from arctrust.operator import OperatorKey
from arctrust.policy import OperatorApprovalAuthority
from arctrust.signer import Signer

from arcagent.core.settings_writer import (  # type: ignore[import-not-found]
    SettingChangeRefused,
    apply_setting_change,
)

# A real, editable, constrained field: ToolConfig.timeout_seconds
# (Field(default=120, ge=1, le=600)) nested at tools.policy.timeout_seconds.
EDITABLE_FIELD = "tools.policy.timeout_seconds"

# Denylisted per config_loading._ENV_DENYLIST_PREFIXES ("identity__key_dir").
DENYLISTED_FIELD = "identity.key_dir"


class _CapturingSink:
    """Test audit sink — records every emitted event, does not discard."""

    def __init__(self) -> None:
        self.events: list[AuditEvent] = []

    def write(self, event: AuditEvent) -> None:
        self.events.append(event)


@pytest.fixture
def agent_root(tmp_path: Path) -> Path:
    """A tmp_path agent workspace with a stub personal-tier arcagent.toml."""
    root = tmp_path / "agent"
    root.mkdir()
    (root / "arcagent.toml").write_text(
        '[agent]\nname = "test-agent"\n\n'
        '[llm]\nmodel = "anthropic/claude-3-5-sonnet"\n\n'
        '[security]\ntier = "personal"\n',
        encoding="utf-8",
    )
    return root


@pytest.fixture
def operator_signer(tmp_path: Path) -> tuple[Signer, str, OperatorKey]:
    """A real, freshly-generated operator Ed25519 signer + its derived DID.

    Mirrors personal-tier custody: the seed is generated in-process (no vault),
    matching ``OperatorKey.generate()`` + ``into_signer()`` in
    ``packages/arctrust/src/arctrust/operator.py``.
    """
    key = OperatorKey.generate()
    signer = key.into_signer()
    did = OperatorApprovalAuthority(signer).did
    return signer, did, key


def _settings_dir(agent_root: Path) -> Path:
    return agent_root / "settings"


def _settings_files(agent_root: Path) -> list[Path]:
    """Every file currently under <agent_root>/settings/, or [] if absent."""
    settings_dir = _settings_dir(agent_root)
    if not settings_dir.exists():
        return []
    return sorted(p for p in settings_dir.rglob("*") if p.is_file())


def test_valid_change_is_signed_written_and_audited(
    agent_root: Path, operator_signer: tuple[Signer, str, OperatorKey]
) -> None:
    signer, did, key = operator_signer
    sink = _CapturingSink()

    result = apply_setting_change(
        field=EDITABLE_FIELD,
        value=45,
        agent_root=agent_root,
        signer=signer,
        signer_did=did,
        audit_sink=sink,
    )

    overlay_path = agent_root / "settings" / f"{EDITABLE_FIELD}.toml"
    sidecar_path = agent_root / "settings" / f"{EDITABLE_FIELD}.toml.arcsig"
    assert overlay_path.exists()
    assert sidecar_path.exists()
    assert result.overlay_path == overlay_path

    content = overlay_path.read_bytes()
    assert verify_artifact(content, result.signature) is True
    # Pinned to the signer's own public key — proves the self-signed round trip.
    assert verify_artifact(content, result.signature, trusted_public_key=key.public_key) is True

    assert len(sink.events) == 1
    event = sink.events[0]
    assert event.action == "settings.write"
    assert event.outcome == "applied"
    assert event.actor_did == did
    assert event.target == EDITABLE_FIELD


def test_invalid_value_is_refused_with_no_write(
    agent_root: Path, operator_signer: tuple[Signer, str, OperatorKey]
) -> None:
    signer, did, _key = operator_signer
    sink = _CapturingSink()
    before = _settings_files(agent_root)

    with pytest.raises(SettingChangeRefused):
        apply_setting_change(
            field=EDITABLE_FIELD,
            value=9999,  # violates le=600
            agent_root=agent_root,
            signer=signer,
            signer_did=did,
            audit_sink=sink,
        )

    assert _settings_files(agent_root) == before
    assert sink.events == []


def test_wrong_type_value_is_refused_with_no_write(
    agent_root: Path, operator_signer: tuple[Signer, str, OperatorKey]
) -> None:
    signer, did, _key = operator_signer
    sink = _CapturingSink()
    before = _settings_files(agent_root)

    with pytest.raises(SettingChangeRefused):
        apply_setting_change(
            field=EDITABLE_FIELD,
            value="not-an-int",
            agent_root=agent_root,
            signer=signer,
            signer_did=did,
            audit_sink=sink,
        )

    assert _settings_files(agent_root) == before
    assert sink.events == []


def test_denylisted_field_is_refused_with_no_write(
    agent_root: Path, operator_signer: tuple[Signer, str, OperatorKey]
) -> None:
    signer, did, _key = operator_signer
    sink = _CapturingSink()
    before = _settings_files(agent_root)

    with pytest.raises(SettingChangeRefused):
        apply_setting_change(
            field=DENYLISTED_FIELD,
            value="/tmp/evil-keys",
            agent_root=agent_root,
            signer=signer,
            signer_did=did,
            audit_sink=sink,
        )

    assert _settings_files(agent_root) == before
    assert sink.events == []


@pytest.mark.parametrize(
    "traversal_field",
    [
        "../../etc/passwd",
        "tools.policy../../../evil",
        "/etc/passwd",
    ],
)
def test_path_traversal_field_is_refused_with_no_write(
    traversal_field: str, agent_root: Path, operator_signer: tuple[Signer, str, OperatorKey]
) -> None:
    signer, did, _key = operator_signer
    sink = _CapturingSink()

    with pytest.raises(SettingChangeRefused):
        apply_setting_change(
            field=traversal_field,
            value=45,
            agent_root=agent_root,
            signer=signer,
            signer_did=did,
            audit_sink=sink,
        )

    # Nothing escaped the confinement boundary: no file landed outside
    # <agent_root>/settings/, and settings/ itself gained no content.
    assert _settings_files(agent_root) == []
    assert not (agent_root.parent / "etc").exists()
    assert not Path("/tmp/passwd").exists()
    assert sink.events == []


def test_personal_tier_self_signed_key_loaded_from_disk_round_trips(
    tmp_path: Path, agent_root: Path
) -> None:
    """A key persisted to disk (the real personal-tier flow, not an in-memory
    seed) signs a change whose signature verifies under that same on-disk
    key's public key — proves the round trip survives a real load(), not just
    a freshly-generated in-process key.
    """
    key_path = tmp_path / "operator" / "operator.key"
    key = OperatorKey.load(key_path, generate_if_absent=True)
    signer = key.into_signer()
    did = OperatorApprovalAuthority(signer).did
    sink = _CapturingSink()

    result = apply_setting_change(
        field=EDITABLE_FIELD,
        value=200,
        agent_root=agent_root,
        signer=signer,
        signer_did=did,
        audit_sink=sink,
    )

    reloaded = OperatorKey.load(key_path)
    content = result.overlay_path.read_bytes()
    assert (
        verify_artifact(content, result.signature, trusted_public_key=reloaded.public_key) is True
    )
    assert sink.events[0].actor_did == did


def test_apply_setting_change_signature_is_importable() -> None:
    """Sanity pin: the callable and refusal type exist with the intended names.

    Guards against a GREEN implementation that renames the public contract
    other tests here rely on (a red flag distinct from the behavioral tests
    above, which would fail with a confusing TypeError instead of a clear
    import-shape mismatch).
    """
    assert callable(apply_setting_change)
    assert issubclass(SettingChangeRefused, Exception)
