"""Config-only agent blueprints — discover / verify / merge / apply.

A blueprint is just a **folder** — ``<name>/blueprint.toml`` (a ``[blueprint]``
metadata header + a config overlay of the same shape as ``arcagent.toml``) plus
optional prompt markdowns (``persona.md`` — the agent identity written to
``workspace/identity.md`` at scaffold time). No package, no code: hand someone the
folder and they spin it up with ``arc init --blueprint <path>``.

**Not in arcagent.** The agent runtime flat-reads its own ``arcagent.toml``; it
never resolves or merges blueprints. Blueprints are a scaffold/distribution concern
consumed only by ``arc init`` / ``arc blueprint``, so the loader lives here in the
CLI and reuses arcagent's config-merge, tier, and signing primitives.

**Materialize-to-disk, not a runtime layer.** A blueprint is rendered under the
user's values and written to the concrete ``arcagent.toml`` the runtime reads
(precedence: packaged-defaults < blueprint < user; the user's explicit keys win).

**Tier floor (AC-4):** ``effective_tier = stringency-max(deployment, blueprint)`` —
a blueprint can only RAISE stringency; the ``SecurityConfig`` validator forces every
federal floor at load, so "a personal blueprint cannot weaken federal" holds by
construction.

**Trust (AC-5):** built-in presets under the repo-root ``blueprints/`` are
provenance-trusted. A user/shared preset (``~/.arc/blueprints/`` or a ``--blueprint
<path>`` folder) is verified fail-closed via its ``.arcsig`` sidecar, PINNED to the
deployment operator's public key; above ``personal`` an unsigned/invalid/wrong-key
preset is refused, and an unresolvable operator key DENIES fail-closed. ``personal``
may apply an unsigned preset with an audit-warn (a shared folder is untrusted
provenance — treated like a user preset, never as a built-in).
"""

from __future__ import annotations

import copy
import hashlib
import logging
import os
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from arcagent.capabilities.artifact_signing import load_signature, verify_file
from arcagent.core.config import _deep_merge
from arcagent.tiers import stricter_tier, tier_rank

_logger = logging.getLogger("arccli.blueprints")

_USER_DIR = Path("~/.arc/blueprints").expanduser()
_BLUEPRINT_TOML = "blueprint.toml"
_PERSONA_MD = "persona.md"

# Trusted-admin-only keys a blueprint overlay must never set (mirror of the
# env-override denylist in core/config.py). A lower-trust preset must not touch the
# vault backend, native tool execution, the tool preamble, identity key custody, or the
# operator-key / federal-witness custody paths.
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


def builtin_blueprints_dir() -> Path:
    """Resolve the repo-root ``blueprints/`` directory (the built-in presets).

    Discovery: ``ARC_BLUEPRINTS_DIR`` override, else the first ``blueprints/`` found
    walking up from this file. Blueprints are a root-level, shareable data folder —
    not packaged code — so a path-discovered directory is the contract.
    """
    override = os.environ.get("ARC_BLUEPRINTS_DIR")
    if override:
        return Path(override).expanduser()
    here = Path(__file__).resolve()
    for parent in here.parents:
        candidate = parent / "blueprints"
        if candidate.is_dir():
            return candidate
    return here.parents[4] / "blueprints"  # fallback: repo-root guess by layout


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
    # The agent persona (identity.md body), loaded from the blueprint's persona.md.
    # The scaffold step writes it to workspace/identity.md. None when config-only.
    persona: str | None = None


def _looks_like_path(name: str) -> bool:
    """True when ``name`` should be treated as a filesystem path, not a bare name."""
    return os.sep in name or name.startswith((".", "~")) or Path(name).is_absolute()


def resolve_blueprint(
    name: str,
    *,
    tier: str,
    user_dir: Path | None = None,
    builtin_dir: Path | None = None,
    operator_public_key: bytes | None = None,
) -> ResolvedBlueprint:
    """Find + verify a blueprint by ``name`` (or a shared-folder ``path``) at ``tier``.

    Resolution order: an explicit path (``--blueprint ./shared/coding``) → a built-in
    under the repo-root ``blueprints/`` → a user preset under ``~/.arc/blueprints/``.
    Built-ins are provenance-trusted; everything else is verified fail-closed, pinned to
    the deployment operator's key above the personal tier.
    """
    if _looks_like_path(name):
        candidate = Path(name).expanduser()
        toml_path = candidate if candidate.suffix == ".toml" else candidate / _BLUEPRINT_TOML
        if not toml_path.is_file():
            raise FileNotFoundError(f"no blueprint.toml at {toml_path}")
        return _resolve_from_toml(
            toml_path, tier, source="user", operator_public_key=operator_public_key
        )

    bdir = builtin_dir if builtin_dir is not None else builtin_blueprints_dir()
    packaged = bdir / name / _BLUEPRINT_TOML
    if packaged.is_file():
        return _resolve_from_toml(
            packaged, tier, source="packaged", operator_public_key=operator_public_key
        )

    udir = user_dir if user_dir is not None else _USER_DIR
    upath = udir / name / _BLUEPRINT_TOML
    if upath.is_file():
        return _resolve_from_toml(
            upath, tier, source="user", operator_public_key=operator_public_key
        )

    raise FileNotFoundError(f"blueprint {name!r} not found (looked in {bdir} and {udir})")


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
    *,
    user_dir: Path | None = None,
    builtin_dir: Path | None = None,
    operator_public_key: bytes | None = None,
) -> list[ResolvedBlueprint]:
    """Enumerate available blueprints (built-in + user) for ``arc blueprint list``.

    Informational: reports each user preset's signed status (PINNED to
    ``operator_public_key``) without refusing an unsigned one — that gate fires at
    :func:`resolve_blueprint`/apply time.
    """
    out: list[ResolvedBlueprint] = []
    bdir = builtin_dir if builtin_dir is not None else builtin_blueprints_dir()
    if bdir.is_dir():
        for toml_path in sorted(bdir.glob(f"*/{_BLUEPRINT_TOML}")):
            content, meta, overlay = _parse(toml_path)
            out.append(
                _make(
                    meta,
                    overlay,
                    "packaged",
                    signed=True,
                    content=content,
                    signer_did="",
                    persona=_read_persona(toml_path.parent),
                )
            )
    udir = user_dir if user_dir is not None else _USER_DIR
    if udir.is_dir():
        for toml_path in sorted(udir.glob(f"*/{_BLUEPRINT_TOML}")):
            content, meta, overlay = _parse(toml_path)
            signed = verify_file(toml_path, content, trusted_public_key=operator_public_key)
            out.append(
                _make(
                    meta,
                    overlay,
                    "user",
                    signed=signed,
                    content=content,
                    signer_did=_signer_did(toml_path) if signed else "",
                    persona=_read_persona(toml_path.parent),
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


def _resolve_from_toml(
    toml_path: Path, tier: str, *, source: str, operator_public_key: bytes | None
) -> ResolvedBlueprint:
    """Build a ResolvedBlueprint from a blueprint.toml, applying the trust gate.

    ``source="packaged"`` (repo-root built-ins) is provenance-trusted. ``source="user"``
    (``~/.arc/blueprints/`` or a shared ``--blueprint <path>`` folder) is verified
    fail-closed and pinned above the personal tier.
    """
    content, meta, overlay = _parse(toml_path)
    persona = _read_persona(toml_path.parent)
    name = str(meta.get("name", "?"))
    if source == "packaged":
        return _make(
            meta, overlay, "packaged", signed=True, content=content, signer_did="", persona=persona
        )

    above_personal = tier_rank(tier) > tier_rank("personal")
    if above_personal and operator_public_key is None:
        raise ValueError(
            f"blueprint {name!r} requires the deployment operator's public key to pin its "
            f"signature against, but it could not be resolved; refusing above the personal tier "
            f"(fail-closed — an unpinned signature gate accepts any self-signed preset, LLM03)"
        )
    signed = verify_file(toml_path, content, trusted_public_key=operator_public_key)
    if above_personal and not signed:
        raise ValueError(
            f"blueprint {name!r} is unsigned or not signed by the deployment operator key; "
            f"refusing to apply above the personal tier (fail-closed, LLM03/ASI04)"
        )
    if not signed:
        _logger.warning("blueprint %r applied unsigned at the personal tier (audit-warn)", name)
    signer_did = _signer_did(toml_path) if signed else ""
    return _make(
        meta,
        overlay,
        "user",
        signed=signed,
        content=content,
        signer_did=signer_did,
        persona=persona,
    )


def _parse(path: Path) -> tuple[bytes, dict[str, Any], dict[str, Any]]:
    """Return (raw bytes, ``[blueprint]`` metadata, config overlay) for a blueprint file."""
    content = path.read_bytes()
    data = tomllib.loads(content.decode("utf-8"))
    meta = data.pop("blueprint", {})
    return content, meta, data


def _read_persona(blueprint_dir: Path) -> str | None:
    """Read the blueprint's ``persona.md`` (the identity body), or None if absent."""
    try:
        text = (blueprint_dir / _PERSONA_MD).read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return text or None


def _make(
    meta: dict[str, Any],
    overlay: dict[str, Any],
    source: str,
    *,
    signed: bool,
    content: bytes,
    signer_did: str,
    persona: str | None = None,
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
        persona=persona,
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
    "builtin_blueprints_dir",
    "dumps_toml",
    "list_blueprints",
    "resolve_blueprint",
]
