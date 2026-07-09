"""SPEC-047 — signed, versioned preset-config blueprints (discover / verify / merge).

A blueprint is a ``[blueprint]``-headed TOML carrying a config overlay of the same
shape as ``arcagent.toml``. It bootstraps a deployment in one command.

**Materialize-to-disk, not a runtime layer (DC-8b).** The agent runtime entrypoint
(``arcagent/__main__.py``) flat-reads its per-agent ``arcagent.toml`` — it does NOT
call ``load_config``, so there is no runtime merge hook a blueprint could live in. A
blueprint is therefore *rendered under the user's values and written to the concrete
file the runtime reads* (``arc init --blueprint`` / ``arc blueprint apply``).

**Precedence (REQ-012):** packaged-defaults < blueprint < user. The user's explicit
keys always win — a blueprint is a *starting point*. This is a write-time deep-merge,
never a destructive template overwrite (unlike ``arc agent build``).

**Tier floor (REQ-013, AC-4):** ``effective_tier = stringency-max(deployment, blueprint)``
— a blueprint can only RAISE stringency. The written ``[security].tier`` is that maximum,
and the real ``SecurityConfig`` model_validator then forces every federal floor at load,
so "a personal blueprint cannot weaken federal" is true by construction, not a second check.

**Trust (REQ-014, AC-5):** packaged presets ship read-only inside the verified wheel
(provenance-trusted, no ``.arcsig``). Any user preset from ``~/.arc/blueprints/`` is
verified fail-closed via the existing ``.arcsig`` sidecar — above ``personal`` an
unsigned/invalid one is refused before merge; ``personal`` may apply it with an audit-warn.

**Pinning is ENFORCED, not optional (SPEC-047 HIGH-1).** Above personal the sidecar is
verified against the DEPLOYMENT OPERATOR's public key (``operator_public_key``, the same
key ``arc blueprint sign`` signs with) — an unpinned signature gate accepts ANY
self-consistent signature (an attacker self-signs a malicious preset with a random
keypair), so an unpinned floor is no floor. When the operator key cannot be resolved above
personal, resolution DENIES fail-closed (mirrors the capability loader's
require-signature-with-no-pinned-key deny). Personal may verify unpinned (audit-warn).
"""

from __future__ import annotations

import copy
import hashlib
import logging
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from arcagent.capabilities.artifact_signing import load_signature, verify_file
from arcagent.core.config import _deep_merge
from arcagent.tiers import stricter_tier, tier_rank

_logger = logging.getLogger("arcagent.blueprints")

_PACKAGED_DIR = Path(__file__).parent
_USER_DIR = Path("~/.arc/blueprints").expanduser()

# Trusted-admin-only keys a blueprint overlay must never set (mirror of the
# env-override denylist in core/config.py). A lower-trust preset must not touch the
# vault backend, native tool execution, the tool preamble, identity key custody, or the
# operator-key / federal-witness custody paths — redirecting the latter would let a preset
# co-locate the witness with the operator key (making rollback detection illusory) or
# point the audit authority at attacker-controlled storage (SPEC-047 LOW-3, SPEC-053 AU-9).
_DENIED_OVERLAY_PATHS: tuple[tuple[str, ...], ...] = (
    ("vault", "backend"),
    ("tools", "process"),
    ("tools", "preamble"),
    ("identity", "key_dir"),
    ("security", "operator_key_dir"),
    ("security", "operator_vault_path"),
    ("security", "notary_keystore"),
    ("security", "witness_medium_path"),
)


@dataclass(frozen=True)
class ResolvedBlueprint:
    """A discovered, verified blueprint ready to merge (``[blueprint]`` header stripped)."""

    name: str
    version: str
    tier: str
    overlay: dict[str, Any]
    source: str  # "packaged" | "user"
    signed: bool
    sha256: str
    signer_did: str


def resolve_blueprint(
    name: str,
    *,
    tier: str,
    user_dir: Path | None = None,
    operator_public_key: bytes | None = None,
) -> ResolvedBlueprint:
    """Find + verify a blueprint by ``name`` for a deployment at ``tier`` (fail-closed).

    Packaged presets (shipped read-only in the wheel) are provenance-trusted. A user
    preset from ``~/.arc/blueprints/`` is verified via its ``.arcsig`` sidecar, PINNED to
    ``operator_public_key`` (the deployment operator's key, the same one ``arc blueprint
    sign`` signs with). Above the personal tier the pin is mandatory: an unsigned, invalid,
    or wrong-key preset is refused before merge, and when the operator key cannot be
    resolved resolution DENIES fail-closed — an unpinned floor is no floor (HIGH-1).
    """
    packaged = _PACKAGED_DIR / f"{name}.toml"
    if packaged.is_file():
        content, meta, overlay = _parse(packaged)
        return _make(meta, overlay, "packaged", signed=True, content=content, signer_did="")

    udir = user_dir if user_dir is not None else _USER_DIR
    upath = udir / f"{name}.toml"
    if not upath.is_file():
        raise FileNotFoundError(
            f"blueprint {name!r} not found (looked in {_PACKAGED_DIR} and {udir})"
        )

    content, meta, overlay = _parse(upath)
    above_personal = tier_rank(tier) > tier_rank("personal")
    if above_personal and operator_public_key is None:
        raise ValueError(
            f"user blueprint {name!r} requires the deployment operator's public key to pin its "
            f"signature against, but it could not be resolved; refusing above the personal tier "
            f"(fail-closed — an unpinned signature gate accepts any self-signed preset, "
            f"LLM03/ASI04)"
        )
    signed = verify_file(upath, content, trusted_public_key=operator_public_key)
    if above_personal and not signed:
        raise ValueError(
            f"user blueprint {name!r} is unsigned or not signed by the deployment operator key; "
            f"refusing to apply above the personal tier (fail-closed, LLM03/ASI04)"
        )
    if not signed:
        _logger.warning(
            "user blueprint %r applied unsigned at the personal tier (audit-warn)", name
        )
    signer_did = _signer_did(upath) if signed else ""
    return _make(meta, overlay, "user", signed=signed, content=content, signer_did=signer_did)


def apply_blueprint(
    blueprint: ResolvedBlueprint, base: dict[str, Any], *, deployment_tier: str
) -> dict[str, Any]:
    """Deep-merge ``blueprint`` UNDER ``base`` (user wins) and floor the tier by stringency-max.

    Returns the concrete config dict to materialize to disk. The written ``[security].tier``
    is ``max(deployment_tier, blueprint.tier, user_tier)`` — a blueprint can only raise it.
    """
    overlay = _strip_denied(blueprint.overlay)
    merged = _deep_merge(overlay, base)  # base (user) overrides the blueprint overlay
    floor = stricter_tier(deployment_tier, blueprint.tier)
    user_tier = str(merged.get("security", {}).get("tier", floor))
    effective = stricter_tier(floor, user_tier)
    merged.setdefault("security", {})["tier"] = effective
    return merged


def list_blueprints(
    *, user_dir: Path | None = None, operator_public_key: bytes | None = None
) -> list[ResolvedBlueprint]:
    """Enumerate available blueprints (packaged + user) for ``arc blueprint list``.

    Informational: reports each user preset's signed status (PINNED to
    ``operator_public_key`` so a wrong-key self-signed preset reads as unsigned) without
    refusing an unsigned one — that gate fires at :func:`resolve_blueprint`/apply time.
    """
    out: list[ResolvedBlueprint] = []
    for path in sorted(_PACKAGED_DIR.glob("*.toml")):
        content, meta, overlay = _parse(path)
        out.append(_make(meta, overlay, "packaged", signed=True, content=content, signer_did=""))
    udir = user_dir if user_dir is not None else _USER_DIR
    if udir.is_dir():
        for path in sorted(udir.glob("*.toml")):
            content, meta, overlay = _parse(path)
            signed = verify_file(path, content, trusted_public_key=operator_public_key)
            out.append(
                _make(
                    meta,
                    overlay,
                    "user",
                    signed=signed,
                    content=content,
                    signer_did=_signer_did(path) if signed else "",
                )
            )
    return out


def dumps_toml(data: dict[str, Any]) -> str:
    """Serialize a nested config dict to TOML the flat loader round-trips.

    Handles the config shape (tables, ``[a.b]`` sub-tables, scalar/list values) — there
    is no ``tomli_w`` dependency in-tree, and the materialized file must parse back to the
    same dict via ``tomllib`` + ``ArcAgentConfig``.
    """
    lines: list[str] = []
    _emit_table(data, [], lines)
    return "\n".join(lines).rstrip("\n") + "\n"


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _parse(path: Path) -> tuple[bytes, dict[str, Any], dict[str, Any]]:
    """Return (raw bytes, ``[blueprint]`` metadata, config overlay) for a blueprint file."""
    content = path.read_bytes()
    data = tomllib.loads(content.decode("utf-8"))
    meta = data.pop("blueprint", {})
    return content, meta, data


def _make(
    meta: dict[str, Any],
    overlay: dict[str, Any],
    source: str,
    *,
    signed: bool,
    content: bytes,
    signer_did: str,
) -> ResolvedBlueprint:
    return ResolvedBlueprint(
        name=str(meta.get("name", "?")),
        version=str(meta.get("version", "0")),
        tier=str(meta.get("tier", "personal")),
        overlay=overlay,
        source=source,
        signed=signed,
        sha256=hashlib.sha256(content).hexdigest(),
        signer_did=signer_did,
    )


def _signer_did(path: Path) -> str:
    manifest = load_signature(path)
    return manifest.signer_did if manifest is not None else ""


def _strip_denied(overlay: dict[str, Any]) -> dict[str, Any]:
    """Drop trusted-admin-only keys a blueprint must not set (see ``_DENIED_OVERLAY_PATHS``)."""
    result = copy.deepcopy(overlay)
    for path in _DENIED_OVERLAY_PATHS:
        node: dict[str, Any] | None = result
        for part in path[:-1]:
            nxt = node.get(part) if node is not None else None
            node = nxt if isinstance(nxt, dict) else None
        if node is not None and path[-1] in node:
            node.pop(path[-1])
            _logger.warning(
                "blueprint overlay set trusted-admin key %s; ignoring it", ".".join(path)
            )
    return result


def _emit_table(table: dict[str, Any], path: list[str], lines: list[str]) -> None:
    scalars = [(k, v) for k, v in table.items() if not isinstance(v, dict)]
    subtables = [(k, v) for k, v in table.items() if isinstance(v, dict)]
    if path:
        lines.append(f"[{'.'.join(path)}]")
    for key, val in scalars:
        lines.append(f"{key} = {_toml_scalar(val)}")
    if path:
        lines.append("")
    for key, val in subtables:
        _emit_table(val, [*path, key], lines)


def _toml_scalar(val: Any) -> str:
    if isinstance(val, bool):
        return "true" if val else "false"
    if isinstance(val, str):
        return '"' + val.replace("\\", "\\\\").replace('"', '\\"') + '"'
    if isinstance(val, (int, float)):
        return str(val)
    if isinstance(val, list):
        return "[" + ", ".join(_toml_scalar(v) for v in val) + "]"
    raise ValueError(f"unsupported TOML value type: {type(val).__name__}")


__all__ = [
    "ResolvedBlueprint",
    "apply_blueprint",
    "dumps_toml",
    "list_blueprints",
    "resolve_blueprint",
]
