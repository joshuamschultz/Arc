"""TOFU approval store — the ``[security.validators]`` block and its mutations.

The persistence surface for Trust-On-First-Use approvals (SPEC-021 R-042/R-043).
An operator pins a capability's source hash into an agent's ``arcagent.toml``
under ``[[security.validators.approved]]``; :class:`~arctrust.tofu.TofuLayer`
consults those pins at the enterprise and federal tiers. The models, the source
hash, and the tomlkit-backed read/write all live here in arctrust because
approval is a trust-store concern — arcagent owns capability *discovery* (which
artifacts are gated), not the *approval* of them.

The file mutations take the agent's ``arcagent.toml`` path as input and touch
only the ``[security.validators]`` table; the rest of the file (comments, key
order, other sections) round-trips untouched. Writes are atomic (temp file +
``os.replace``) so an interrupted write never leaves a half-serialized config.
"""

from __future__ import annotations

import hashlib
import os
import tempfile
import tomllib
from pathlib import Path
from typing import Any

import tomlkit
from pydantic import BaseModel, Field


class ValidatorEntry(BaseModel):
    """A single TOFU-approved validator script (R-042 / R-043).

    Persisted under ``[[security.validators.approved]]`` in ``arcagent.toml``.
    Written only by the human operator via ``arc trust approve`` (or arcui's
    operator-gated approve route) — the agent has no write access.

    ``hash`` is the sha256 digest of the validator source body, prefixed
    ``sha256:``. ``timestamp`` is RFC3339 UTC.
    """

    name: str = Field(description="Validator script logical name")
    hash: str = Field(description="sha256:<digest> of approved source")
    approver: str = Field(description="Identity that approved (email or DID)")
    timestamp: str = Field(description="RFC3339 UTC timestamp of approval")


class ValidatorsConfig(BaseModel):
    """``[security.validators]`` block — TOFU policy state.

    Lives at agent-root, never inside workspace (R-043). Default is
    federal-safe: ``auto_run_agent_code = False`` and zero approved entries.
    Personal-tier templates seed it to ``True``; enterprise + federal templates
    leave it ``False``.
    """

    auto_run_agent_code: bool = Field(
        default=False,
        description=(
            "Personal tier only — auto-run agent-authored Python after "
            "AST validation. Enterprise/federal must approve via TOFU."
        ),
    )
    approved: tuple[ValidatorEntry, ...] = Field(
        default=(),
        description="Persisted TOFU approvals; appended by `arc trust approve`",
    )


def hash_source(source: str) -> str:
    """Return the ``sha256:<hex>`` digest TOFU pins a capability source to.

    The single canonical hash for capability approval — the loader hashes the
    decoded artifact bytes here to match a pin, and the ``arc trust`` admin
    surfaces hash the same bytes when they record one, so both agree.
    """
    return "sha256:" + hashlib.sha256(source.encode("utf-8")).hexdigest()


def approve_source(
    validators: ValidatorsConfig,
    *,
    name: str,
    source: str,
    approver: str,
    timestamp: str,
) -> ValidatorsConfig:
    """Record a TOFU approval — pin ``name`` to the current source hash (D1).

    Returns a new :class:`ValidatorsConfig` with ``name``'s approval set to
    ``source``'s hash (replacing any prior approval for that name, so a
    re-approval after drift supersedes the stale hash). This is the pure,
    in-memory data operation behind :func:`approve`; the file-level function
    persists the result to ``arcagent.toml``. A subsequent byte change no longer
    matches the pinned hash, so :meth:`TofuLayer.evaluate` returns ``DENY`` (drift
    = hard stop) until the operator approves again.
    """
    entry = ValidatorEntry(
        name=name,
        hash=hash_source(source),
        approver=approver,
        timestamp=timestamp,
    )
    kept = tuple(e for e in validators.approved if e.name != name)
    return validators.model_copy(update={"approved": (*kept, entry)})


def load_validators(config_path: Path) -> ValidatorsConfig:
    """Read the ``[security.validators]`` block from an agent's ``arcagent.toml``.

    Parses the per-agent file directly (not the merged config) because approvals
    persist to this exact file — reading the same surface a mutation writes keeps
    the store self-consistent. A missing file or missing block yields the
    federal-safe default (``auto_run_agent_code=False``, no approvals).
    """
    if not config_path.exists():
        return ValidatorsConfig()
    doc = tomllib.loads(config_path.read_text(encoding="utf-8"))
    block = doc.get("security", {}).get("validators", {})
    return ValidatorsConfig.model_validate(block)


def approve(
    config_path: Path,
    *,
    name: str,
    source: str,
    approver: str,
    timestamp: str,
) -> ValidatorsConfig:
    """Pin ``name`` to ``source``'s hash in ``config_path`` and persist (D1).

    Reads the current approvals, records a ``[[security.validators.approved]]``
    entry (name → sha256 of ``source``) via :func:`approve_source` — superseding
    any prior pin for that name — writes it back atomically, and returns the
    updated config. ``name`` is the loader's pin key (a tool's file stem, a
    skill's folder name), not necessarily the capability's display name.
    """
    validators = load_validators(config_path)
    updated = approve_source(
        validators, name=name, source=source, approver=approver, timestamp=timestamp
    )
    persist_validators(config_path, updated)
    return updated


def disapprove(config_path: Path, *, name: str) -> bool:
    """Remove the pin for ``name`` from ``config_path`` and persist (drift / revoke).

    Returns True when an entry was removed, False when nothing was pinned under
    that name. ``name`` is the loader's pin key (a tool's file stem, a skill's
    folder name), not necessarily the capability's display name.
    """
    validators = load_validators(config_path)
    kept = tuple(entry for entry in validators.approved if entry.name != name)
    if len(kept) == len(validators.approved):
        return False
    persist_validators(config_path, validators.model_copy(update={"approved": kept}))
    return True


def persist_validators(config_path: Path, validators: ValidatorsConfig) -> None:
    """Rewrite only the ``[security.validators]`` block in ``arcagent.toml``.

    tomlkit round-trips the rest of the file (comments, key order, other
    sections) untouched. The write is atomic (temp file + ``os.replace``) so an
    interrupted write can never leave a half-serialized config on disk.
    """
    doc: Any = tomlkit.parse(config_path.read_text(encoding="utf-8"))
    security = doc.get("security")
    if security is None:
        security = tomlkit.table()
        doc["security"] = security
    validators_table = security.get("validators")
    if validators_table is None:
        validators_table = tomlkit.table()
        security["validators"] = validators_table

    validators_table["auto_run_agent_code"] = validators.auto_run_agent_code
    approved = tomlkit.aot()
    for entry in validators.approved:
        row = tomlkit.table()
        row["name"] = entry.name
        row["hash"] = entry.hash
        row["approver"] = entry.approver
        row["timestamp"] = entry.timestamp
        approved.append(row)
    validators_table["approved"] = approved

    _atomic_write(config_path, doc)


def _atomic_write(config_path: Path, doc: Any) -> None:
    """Write a tomlkit document atomically (temp file in-dir + ``os.replace``)."""
    fd, tmp_path = tempfile.mkstemp(dir=str(config_path.parent), suffix=".toml.tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            tomlkit.dump(doc, handle)
        os.replace(tmp_path, str(config_path))
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


__all__ = [
    "ValidatorEntry",
    "ValidatorsConfig",
    "approve",
    "approve_source",
    "disapprove",
    "hash_source",
    "load_validators",
    "persist_validators",
]
