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
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import arcagent

_logger = logging.getLogger("arccli.blueprints")

_USER_DIR = Path("~/.arc/blueprints").expanduser()
_BLUEPRINT_TOML = "blueprint.toml"
_PERSONA_MD = "persona.md"

# v2 sibling-config tables + declarative arrays, peeled out of the arcagent overlay.
# Everything NOT in this set stays in the arcagent.toml overlay (unchanged semantics).
_SIBLING_TABLES: tuple[str, ...] = ("arcllm", "arcrun")


def dumps_toml(data: dict[str, Any]) -> str:
    """Render TOML through ArcAgent's public configuration serializer."""
    return arcagent.dumps_toml(data)


_DECLARATIVE_ARRAYS: tuple[str, ...] = ("schedules", "questions")
# v2 file-tree sub-directories under the blueprint folder.
_PROMPTS_DIRNAME = "prompts"
_CAPABILITIES_DIRNAME = "capabilities"
_SKILLS_DIRNAME = "skills"

# Trusted-admin-only keys a blueprint overlay must never set (mirror of the
# env-override denylist in core/config.py). A lower-trust preset must not touch the
# vault backend, native tool execution, the tool preamble, identity key custody, the
# operator-key / federal-witness custody paths, or the sandbox boundary itself.
#
# `tools.policy.allowed_paths` is the sandbox floor: a blueprint that set it would
# self-grant filesystem access outside the workspace (SEC-18). Filesystem grants come
# only from the operator's own toml or the folder-trust prompt — never a shared/unsigned
# preset. (`operate_in_launch_dir` is deliberately NOT denied: on its own it grants
# nothing — the working_dir gate still requires the dir be inside allowed_paths, i.e.
# already user-trusted — and the built-in coding blueprint needs to set it.)
_DENIED_OVERLAY_PATHS: tuple[tuple[str, ...], ...] = (
    ("vault", "backend"),
    ("tools", "process"),
    ("tools", "preamble"),
    ("tools", "policy", "allowed_paths"),
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
class PromptOverlaySpec:
    """One prompt overlay a blueprint ships (``prompts/<package>/<name>.md``).

    ``(package, name)`` is validated against the arcprompt catalog at resolve time,
    so a typo is a hard error, never a silent no-op. ``body`` is authored + operator
    signed into ``<agent_root>/context/<package>/<name>.md`` at materialize time.
    """

    package: str
    name: str
    body: str


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
    # --- v2 additions (all default-empty so v1 blueprints resolve unchanged) ---
    root: Path | None = None  # the blueprint folder (source of the file trees)
    arcllm_overlay: dict[str, Any] = field(default_factory=dict)
    arcrun_overlay: dict[str, Any] = field(default_factory=dict)
    prompt_overlays: tuple[PromptOverlaySpec, ...] = ()
    schedules: tuple[dict[str, Any], ...] = ()
    questions: tuple[dict[str, Any], ...] = ()
    capabilities_dir: Path | None = None  # <root>/capabilities, copied + agent-signed
    skills_dir: Path | None = None  # <root>/skills, copied + agent-signed


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
    merged = arcagent.deep_merge(overlay, base)  # base (user) overrides the blueprint overlay
    floor = arcagent.stricter_tier(deployment_tier, blueprint.tier)
    user_tier = str(merged.get("security", {}).get("tier", floor))
    effective = arcagent.stricter_tier(floor, user_tier)
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
            content, meta, overlay, v2 = _parse(toml_path)
            out.append(
                _make(
                    meta,
                    overlay,
                    "packaged",
                    signed=True,
                    content=content,
                    signer_did="",
                    persona=_read_persona(toml_path.parent),
                    root=toml_path.parent,
                    v2=v2,
                )
            )
    udir = user_dir if user_dir is not None else _USER_DIR
    if udir.is_dir():
        for toml_path in sorted(udir.glob(f"*/{_BLUEPRINT_TOML}")):
            content, meta, overlay, v2 = _parse(toml_path)
            signed = arcagent.verify_file(
                toml_path, content, trusted_public_key=operator_public_key
            )
            out.append(
                _make(
                    meta,
                    overlay,
                    "user",
                    signed=signed,
                    content=content,
                    signer_did=_signer_did(toml_path) if signed else "",
                    persona=_read_persona(toml_path.parent),
                    root=toml_path.parent,
                    v2=v2,
                )
            )
    return out


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
    content, meta, overlay, v2 = _parse(toml_path)
    root = toml_path.parent
    persona = _read_persona(root)
    prompt_overlays = _discover_prompt_overlays(root)
    name = str(meta.get("name", "?"))
    if source == "packaged":
        return _make(
            meta,
            overlay,
            "packaged",
            signed=True,
            content=content,
            signer_did="",
            persona=persona,
            root=root,
            v2=v2,
            prompt_overlays=prompt_overlays,
        )

    above_personal = arcagent.tier_rank(tier) > arcagent.tier_rank("personal")
    if above_personal and operator_public_key is None:
        raise ValueError(
            f"blueprint {name!r} requires the deployment operator's public key to pin its "
            f"signature against, but it could not be resolved; refusing above the personal tier "
            f"(fail-closed — an unpinned signature gate accepts any self-signed preset, LLM03)"
        )
    signed = arcagent.verify_file(toml_path, content, trusted_public_key=operator_public_key)
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
        root=root,
        v2=v2,
        prompt_overlays=prompt_overlays,
    )


@dataclass(frozen=True)
class _V2Sections:
    """The v2 tables/arrays peeled out of a blueprint.toml (empty for a v1 blueprint)."""

    arcllm: dict[str, Any]
    arcrun: dict[str, Any]
    schedules: tuple[dict[str, Any], ...]
    questions: tuple[dict[str, Any], ...]


def _parse(path: Path) -> tuple[bytes, dict[str, Any], dict[str, Any], _V2Sections]:
    """Return (raw bytes, ``[blueprint]`` meta, arcagent overlay, v2 sections).

    The arcagent overlay is *everything that is not* the ``[blueprint]`` header, the
    ``[arcllm]``/``[arcrun]`` sibling tables, or the ``[[schedules]]``/``[[questions]]``
    arrays — so a v1 blueprint (top level == arcagent.toml) is unchanged.
    """
    content = path.read_bytes()
    data = tomllib.loads(content.decode("utf-8"))
    meta = data.pop("blueprint", {})
    v2 = _V2Sections(
        arcllm=dict(data.pop("arcllm", {})),
        arcrun=dict(data.pop("arcrun", {})),
        schedules=tuple(data.pop("schedules", [])),
        questions=tuple(data.pop("questions", [])),
    )
    return content, meta, data, v2


def _discover_prompt_overlays(root: Path) -> tuple[PromptOverlaySpec, ...]:
    """Discover + validate ``root/prompts/<package>/<name>.md`` overlays.

    Each ``(package, name)`` is checked against the arcprompt catalog; an unknown
    prompt raises ``ValueError`` (a typo'd override must fail loud, never no-op).
    """
    prompts_dir = root / _PROMPTS_DIRNAME
    if not prompts_dir.is_dir():
        return ()
    from arcprompt import PromptCatalog

    catalog = PromptCatalog()
    overlays: list[PromptOverlaySpec] = []
    for md in sorted(prompts_dir.glob("*/*.md")):
        package, name = md.parent.name, md.stem
        if catalog.stock_path(package, name) is None:
            raise ValueError(
                f"blueprint ships a prompt overlay for unknown stock prompt "
                f"{package}/{name} (no such packaged prompt); refusing (a typo'd "
                f"override silently no-ops — the failure mode we fail loud on)"
            )
        overlays.append(
            PromptOverlaySpec(package=package, name=name, body=md.read_text(encoding="utf-8"))
        )
    return tuple(overlays)


def _v2_dir(root: Path, name: str) -> Path | None:
    """Return ``root/<name>`` if it is a non-empty directory, else None."""
    candidate = root / name
    if candidate.is_dir() and any(candidate.iterdir()):
        return candidate
    return None


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
    root: Path,
    v2: _V2Sections,
    prompt_overlays: tuple[PromptOverlaySpec, ...] = (),
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
        root=root,
        arcllm_overlay=v2.arcllm,
        arcrun_overlay=v2.arcrun,
        prompt_overlays=prompt_overlays,
        schedules=v2.schedules,
        questions=v2.questions,
        capabilities_dir=_v2_dir(root, _CAPABILITIES_DIRNAME),
        skills_dir=_v2_dir(root, _SKILLS_DIRNAME),
    )


def _signer_did(path: Path) -> str:
    manifest = arcagent.load_signature(path)
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


__all__ = [
    "PromptOverlaySpec",
    "ResolvedBlueprint",
    "apply_blueprint",
    "builtin_blueprints_dir",
    "dumps_toml",
    "list_blueprints",
    "resolve_blueprint",
]
