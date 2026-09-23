"""Signed settings writer (SPEC-085 COMP-004, REQ-461/462).

``apply_setting_change()`` is the ONLY path a Settings-screen edit may take
to reach disk (SDD ADR-A): it validates a ``{field, value}`` change against
the schema emitted by :func:`arcagent.core.config_schema.emit_form_schema`,
then writes a signed overlay through the arcprompt pattern (signed content +
detached ``.arcsig`` sidecar, ADR-029) under
``<agent_root>/settings/<dotted.field.id>.toml``, and emits exactly one
``AuditEvent`` for the change.

Order of operations is the trust boundary (REQ-462, no partial write):

  1. look up the field in the schema and validate ``editable`` / ``type`` /
     ``ge``/``le`` constraints
  2. confine the overlay path under ``<agent_root>/settings/``
  3. render deterministic overlay bytes and sign them
  4. write the ``.toml`` and its ``.arcsig`` sidecar atomically
  5. emit the audit event

Any refusal in steps 1-2 raises :class:`SettingChangeRefused` before a single
byte is written — nothing is ever partially applied.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Any

import tomlkit
from arctrust.artifact import ArtifactSignature, sign_artifact_with_signer
from arctrust.audit import AuditEvent, AuditSink, emit
from arctrust.signer import Signer

from arcagent.core.config_schema import emit_form_schema
from arcagent.core.errors import ConfigError

_SETTINGS_DIRNAME = "settings"
_SIDECAR_SUFFIX = ".arcsig"
_FILE_MODE = 0o600


class SettingChangeRefused(ConfigError):  # noqa: N818 — pinned test-contract name, no Error suffix
    """A settings-screen change was refused before anything was written."""

    def __init__(self, message: str, *, field: str) -> None:
        super().__init__(
            code="SETTINGS_CHANGE_REFUSED",
            message=message,
            details={"field": field},
        )


class SettingChangeResult:
    """The signed overlay produced by a successful :func:`apply_setting_change`."""

    __slots__ = ("overlay_path", "signature")

    def __init__(self, overlay_path: Path, signature: ArtifactSignature) -> None:
        self.overlay_path = overlay_path
        self.signature = signature


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
    """Validate, sign, and durably write one settings-screen field change.

    Raises :class:`SettingChangeRefused` — and writes nothing — for a value
    that violates the field's schema constraints, a field that is not
    editable (schema denylist), or a field whose overlay path would escape
    ``<agent_root>/settings/``.
    """
    field_type = _validated_field_type(field, value)
    overlay_path = _confine_overlay_path(agent_root, field)
    sidecar_path = overlay_path.with_name(overlay_path.name + _SIDECAR_SUFFIX)

    content = _render_overlay(field, value, field_type).encode("utf-8")
    signature = sign_artifact_with_signer(content, signer_did=signer_did, signer=signer)

    overlay_path.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write(overlay_path, content)
    _atomic_write(sidecar_path, signature.to_json().encode("utf-8"))

    emit(
        AuditEvent(
            actor_did=signer_did,
            action="settings.write",
            target=field,
            outcome="applied",
            tier=tier,
        ),
        audit_sink,
    )
    return SettingChangeResult(overlay_path, signature)


def _validated_field_type(field: str, value: Any) -> str:
    """Look up ``field`` in the committed schema and enforce its constraints.

    Reuses ``config_schema.emit_form_schema()`` verbatim — the same
    editable/type/ge/le rules the Settings screen itself is generated from —
    so this writer can never accept a change the schema would refuse to show
    as editable.
    """
    schema_entry = _find_field_schema(field)
    if schema_entry is None or not schema_entry["editable"]:
        raise SettingChangeRefused(f"field {field!r} is not editable", field=field)
    type_name: str = schema_entry["type"]
    _validate_type(field, value, type_name)
    _validate_constraints(field, value, schema_entry["constraints"])
    return type_name


def _find_field_schema(field: str) -> dict[str, Any] | None:
    sections: list[dict[str, Any]] = emit_form_schema()["sections"]
    for section in sections:
        fields: list[dict[str, Any]] = section["fields"]
        for entry in fields:
            if entry["id"] == field:
                return entry
    return None


def _validate_type(field: str, value: Any, type_name: str) -> None:
    # bool is a subclass of int in Python — checked first so an int-typed
    # field never silently accepts a bool, and a bool-typed field never
    # accepts a bare int.
    if type_name == "bool":
        if not isinstance(value, bool):
            raise SettingChangeRefused(f"field {field!r} expects a bool value", field=field)
        return
    if isinstance(value, bool):
        raise SettingChangeRefused(f"field {field!r} does not accept a bool value", field=field)
    if type_name == "int" and not isinstance(value, int):
        raise SettingChangeRefused(f"field {field!r} expects an int value", field=field)
    elif type_name == "float" and not isinstance(value, (int, float)):
        raise SettingChangeRefused(f"field {field!r} expects a float value", field=field)
    elif type_name == "str" and not isinstance(value, str):
        raise SettingChangeRefused(f"field {field!r} expects a str value", field=field)


def _validate_constraints(field: str, value: Any, constraints: dict[str, Any] | None) -> None:
    if constraints is None:
        return
    min_value = constraints.get("min")
    max_value = constraints.get("max")
    if min_value is not None and value < min_value:
        raise SettingChangeRefused(
            f"field {field!r} value {value!r} is below the minimum {min_value!r}", field=field
        )
    if max_value is not None and value > max_value:
        raise SettingChangeRefused(
            f"field {field!r} value {value!r} is above the maximum {max_value!r}", field=field
        )


def _confine_overlay_path(agent_root: Path, field: str) -> Path:
    """Resolve ``field`` to a ``.toml`` path confined under ``<agent_root>/settings/``.

    Mirrors ``arcui/routes/agent_detail/files_write.py::_confine``: reject an
    absolute-looking field up front, then require the resolved (symlink/``..``
    collapsed) candidate to stay under the settings directory. A field that
    embeds ``/`` (e.g. ``"tools.policy../../../evil"``) is walked exactly
    like a filesystem path by ``Path.__truediv__``, so the same ``resolve()``
    + ``relative_to()`` check catches it regardless of where the ``..``
    segments fall.
    """
    if not field or field.startswith("/") or (len(field) > 1 and field[1] == ":"):
        raise SettingChangeRefused(f"field {field!r} is not a valid settings path", field=field)
    settings_dir = (agent_root / _SETTINGS_DIRNAME).resolve()
    candidate = (agent_root / _SETTINGS_DIRNAME / f"{field}.toml").resolve()
    try:
        candidate.relative_to(settings_dir)
    except ValueError:
        raise SettingChangeRefused(
            f"field {field!r} escapes the settings directory", field=field
        ) from None
    return candidate


def _render_overlay(field: str, value: Any, type_name: str) -> str:
    """Render ``field``/``value`` as a deterministic nested TOML fragment.

    ``tools.policy.timeout_seconds`` becomes ``[tools.policy]\ntimeout_seconds
    = 45\n`` — the same dotted-path-to-nested-table shape the config loader's
    sibling TOML files already use, so the overlay merges the way any other
    config layer does.
    """
    del type_name  # value was already type-checked against it in _validate_type
    document = tomlkit.document()
    table: Any = document
    parts = field.split(".")
    for part in parts[:-1]:
        nested = tomlkit.table()
        table[part] = nested
        table = nested
    table[parts[-1]] = value
    return tomlkit.dumps(document)


def _atomic_write(path: Path, content: bytes) -> None:
    """Write ``content`` to ``path`` via tempfile + ``os.replace`` + fsync.

    Mirrors ``config.py::_atomic_replace_config``, generalized for a target
    that may not yet exist: a reader can never observe a half-written
    overlay or sidecar. Mode is fixed at ``0600`` — these are new files, not
    a rewrite of an existing one whose mode must be preserved.
    """
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(fd, _FILE_MODE)
        with os.fdopen(fd, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


__all__ = ["SettingChangeRefused", "SettingChangeResult", "apply_setting_change"]
