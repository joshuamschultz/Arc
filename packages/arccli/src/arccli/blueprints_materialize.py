"""Materialize a resolved blueprint into a concrete agent directory.

``arc blueprint apply`` merges only ``arcagent.toml``. Spinning up a *specialist*
agent from a v2 blueprint means writing the full surface into an agent's home:

  * ``arcagent.toml`` / ``arcllm.toml`` / ``arcrun.toml`` — deep-merged UNDER the
    agent's own values (the user always wins), tier floored by stringency-max.
  * ``workspace/identity.md`` — the persona, written only if absent (never clobber
    a customer-edited identity).
  * ``context/<package>/<name>.md`` (+ ``.arcsig``) — prompt overlays, secret-scanned
    then **operator-signed** so the agent's PromptResolver verifies them at run start.
  * ``capabilities/*.py`` and ``capabilities/skills/<name>/`` — copied from the
    blueprint and **signed with the agent's own pinned identity** (the same key
    ``arc agent create`` signs the scaffolded calculator with), so the TOFU gate at
    personal tier accepts them instead of treating them as unsigned agent-planted code.
  * ``workspace/<schedules.json>`` — declared schedules seeded via the scheduler store.

Signing on the deployment box with *its* keys means no key is ever distributed and
every overlay/capability verifies locally at run start.
"""

from __future__ import annotations

import shutil
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import arcagent
from arctrust import Signer as TrustSigner
from arctrust import SignerError

from arccli.blueprints import ResolvedBlueprint, apply_blueprint, dumps_toml

# (signer_did, ed25519_seed) — the raw 32-byte seed the signer holds.
Signer = tuple[str, bytes]


@dataclass(frozen=True)
class CapabilitySigner:
    """The operator's signer handle plus the DID its signatures are recorded under."""

    did: str
    signer: TrustSigner


_CONTEXT_DIRNAME = "context"
_SIDECAR_SUFFIX = ".arcsig"


@dataclass
class MaterializeResult:
    """What the materialize wrote — for the CLI summary and for tests to assert on."""

    agent_dir: Path
    wrote_identity: bool = False
    prompt_overlays: list[str] = field(default_factory=list)  # "package/name"
    capabilities: list[str] = field(default_factory=list)  # file names
    skills: list[str] = field(default_factory=list)  # skill folder names
    schedules: int = 0
    unsigned_warnings: list[str] = field(default_factory=list)


def materialize_blueprint(
    bp: ResolvedBlueprint,
    agent_dir: Path,
    *,
    deployment_tier: str,
    operator_signer: Signer | None = None,
    capability_signer: CapabilitySigner | None = None,
) -> MaterializeResult:
    """Write the full blueprint surface into ``agent_dir``. Returns what landed.

    ``operator_signer`` signs prompt overlays; if the blueprint ships overlays and no
    operator signer is available this raises (an unsigned overlay is refused by the
    resolver at run start — shipping one silently would be a dead feature).
    ``capability_signer`` is the OPERATOR signer handle that signs copied capabilities and
    skills (the agent key never signs under ``capabilities/``); when absent they are copied
    unsigned with a warning and stay gated until ``arc trust approve``.
    """
    result = MaterializeResult(agent_dir=agent_dir)
    merged = _merge_configs(bp, agent_dir, deployment_tier=deployment_tier)
    _write_persona(bp, agent_dir, result)
    _sign_persona(agent_dir, operator_signer, result)
    _author_prompt_overlays(bp, agent_dir, operator_signer, result)
    _install_capabilities(bp, agent_dir, capability_signer, result)
    _install_skills(bp, agent_dir, capability_signer, result)
    _seed_schedules(bp, agent_dir, merged, result)
    return result


# ---------------------------------------------------------------------------
# Config files
# ---------------------------------------------------------------------------


def _merge_configs(
    bp: ResolvedBlueprint, agent_dir: Path, *, deployment_tier: str
) -> dict[str, Any]:
    """Merge arcagent + sibling arcllm/arcrun tomls UNDER the agent's existing values."""
    agent_path = agent_dir / "arcagent.toml"
    base = _read_toml(agent_path)
    merged = apply_blueprint(bp, base, deployment_tier=deployment_tier)
    agent_path.write_text(dumps_toml(merged), encoding="utf-8")
    _merge_sibling(agent_dir / "arcllm.toml", bp.arcllm_overlay)
    _merge_sibling(agent_dir / "arcrun.toml", bp.arcrun_overlay)
    return merged


def _merge_sibling(path: Path, overlay: dict[str, Any]) -> None:
    """Deep-merge ``overlay`` UNDER ``path``'s existing config (user wins); no-op if empty."""
    if not overlay:
        return
    merged = arcagent.deep_merge(overlay, _read_toml(path))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(dumps_toml(merged), encoding="utf-8")


def _read_toml(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        return tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError:
        return {}


# ---------------------------------------------------------------------------
# Persona
# ---------------------------------------------------------------------------


def _write_persona(bp: ResolvedBlueprint, agent_dir: Path, result: MaterializeResult) -> None:
    """Write the persona to ``workspace/identity.md`` unless a customer already edited it.

    The scaffold (``arc agent create``) writes a generic placeholder identity, so
    "already exists" is not the same as "customer-owned": a first apply over the fresh
    scaffold MUST install the persona. We overwrite when the file is absent or still equals
    the scaffold default, and never clobber an identity the customer has actually changed.
    """
    if not bp.persona:
        return
    identity = agent_dir / "workspace" / "identity.md"
    if identity.exists() and not _is_scaffold_default_identity(identity):
        return
    identity.parent.mkdir(parents=True, exist_ok=True)
    identity.write_text(bp.persona.rstrip() + "\n", encoding="utf-8")
    result.wrote_identity = True


def _sign_persona(
    agent_dir: Path, operator_signer: Signer | None, result: MaterializeResult
) -> None:
    """Operator-sign the persona just written: an unsigned ``identity.md`` is refused at run start.

    Without an operator key the persona cannot be signed, and the scaffold's earlier
    signature no longer matches the new text — say so, so the operator runs the one
    command that fixes it rather than meeting a refused run.
    """
    if not result.wrote_identity:
        return
    if operator_signer is None:
        result.unsigned_warnings.append(
            "identity.md was changed but no operator key is available to sign it; the agent "
            "will refuse to run until you run 'arc prompt sign-workspace --agent <dir>'."
        )
        return
    import arcagent
    from arctrust import ED25519, InProcessSigner

    did, seed = operator_signer
    arcagent.scaffold.sign_workspace_documents(
        agent_dir,
        arcagent.scaffold.OperatorSigning(did=did, signer=InProcessSigner(seed, ED25519)),
    )


def _is_scaffold_default_identity(path: Path) -> bool:
    """True when ``identity.md`` is still the untouched ``arc agent create`` placeholder."""
    import arcagent

    return path.read_text(encoding="utf-8").strip() == arcagent.scaffold.DEFAULT_IDENTITY.strip()


# ---------------------------------------------------------------------------
# Prompt overlays (operator-signed)
# ---------------------------------------------------------------------------


def _author_prompt_overlays(
    bp: ResolvedBlueprint,
    agent_dir: Path,
    operator_signer: Signer | None,
    result: MaterializeResult,
) -> None:
    """Author + operator-sign each shipped prompt overlay into ``context/<pkg>/<name>.md``."""
    if not bp.prompt_overlays:
        return
    if operator_signer is None:
        raise ValueError(
            f"blueprint {bp.name!r} ships {len(bp.prompt_overlays)} prompt overlay(s) but no "
            f"operator key is available to sign them; the resolver rejects unsigned overlays "
            f"at run start. Run 'arc init' to create the deployment operator key first."
        )
    from arcprompt import load_stock_document, render_prompt
    from arctrust.artifact import sign_artifact

    signer_did, seed = operator_signer
    context_root = agent_dir / _CONTEXT_DIRNAME
    for spec in bp.prompt_overlays:
        secret = arcagent.find_secret(spec.body)
        if secret is not None:
            raise ValueError(
                f"blueprint {bp.name!r} overlay {spec.package}/{spec.name} looks like a live "
                f"credential ({secret}); credentials never touch the filesystem."
            )
        overlay = _confine(context_root, spec.package, spec.name)
        stock = load_stock_document(spec.package, spec.name)
        overlay_bytes = render_prompt(spec.body, name=spec.name, description=stock.description)
        signature = sign_artifact(overlay_bytes, signer_did=signer_did, private_key=seed)
        overlay.parent.mkdir(parents=True, exist_ok=True)
        overlay.write_bytes(overlay_bytes)
        Path(f"{overlay}{_SIDECAR_SUFFIX}").write_text(signature.to_json(), encoding="utf-8")
        result.prompt_overlays.append(f"{spec.package}/{spec.name}")


def _confine(context_root: Path, package: str, name: str) -> Path:
    """Resolve the overlay path under ``context_root``; raise if it escapes (path injection)."""
    candidate = (context_root / package / f"{name}.md").resolve()
    try:
        candidate.relative_to(context_root.resolve())
    except ValueError as exc:
        raise ValueError(f"prompt overlay {package}/{name} escapes the context root") from exc
    return candidate


# ---------------------------------------------------------------------------
# Capabilities + skills (operator-signed)
# ---------------------------------------------------------------------------


def _install_capabilities(
    bp: ResolvedBlueprint,
    agent_dir: Path,
    capability_signer: CapabilitySigner | None,
    result: MaterializeResult,
) -> None:
    """Copy ``capabilities/*`` into the agent root, operator-signing each ``.py``."""
    if bp.capabilities_dir is None:
        return
    dest = agent_dir / "capabilities"
    dest.mkdir(parents=True, exist_ok=True)
    for item in sorted(bp.capabilities_dir.iterdir()):
        if item.name == "skills":  # skills are installed separately (different scan root)
            continue
        target = dest / item.name
        _copy(item, target)
        if item.is_file() and item.suffix == ".py":
            _sign_agent_artifact(target, agent_dir, capability_signer, result)
            result.capabilities.append(item.name)


def _install_skills(
    bp: ResolvedBlueprint,
    agent_dir: Path,
    capability_signer: CapabilitySigner | None,
    result: MaterializeResult,
) -> None:
    """Copy each ``skills/<name>/`` into ``capabilities/skills`` and operator-sign its SKILL.md."""
    if bp.skills_dir is None:
        return
    dest = agent_dir / "capabilities" / "skills"
    dest.mkdir(parents=True, exist_ok=True)
    for skill in sorted(bp.skills_dir.iterdir()):
        if not skill.is_dir():
            continue
        target = dest / skill.name
        _copy(skill, target)
        skill_md = target / "SKILL.md"
        if skill_md.is_file():
            _sign_agent_artifact(skill_md, agent_dir, capability_signer, result)
        result.skills.append(skill.name)


def _copy(src: Path, dst: Path) -> None:
    """Copy a file or directory tree, replacing any existing target."""
    if src.is_dir():
        shutil.copytree(src, dst, dirs_exist_ok=True)
    else:
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)


def _sign_agent_artifact(
    path: Path,
    agent_dir: Path,
    capability_signer: CapabilitySigner | None,
    result: MaterializeResult,
) -> None:
    """Operator-sign an agent-root artifact (signature + key pin + TOFU pin), or warn.

    Fail closed: a signing failure leaves the artifact unsigned, hence gated.
    """
    if capability_signer is None:
        result.unsigned_warnings.append(str(path))
        return
    try:
        arcagent.sign_capability(
            path,
            signer_did=capability_signer.did,
            signer=capability_signer.signer,
            config_path=agent_dir / "arcagent.toml",
        )
    except (OSError, ValueError, SignerError):
        result.unsigned_warnings.append(str(path))


# ---------------------------------------------------------------------------
# Schedules
# ---------------------------------------------------------------------------


def _seed_schedules(
    bp: ResolvedBlueprint,
    agent_dir: Path,
    merged: dict[str, Any],
    result: MaterializeResult,
) -> None:
    """Seed declared ``[[schedules]]`` into the agent's scheduler store (workspace/<store>)."""
    if not bp.schedules:
        return
    store_path = (
        merged.get("modules", {})
        .get("scheduler", {})
        .get("config", {})
        .get("store_path", "schedules.json")
    )
    workspace = agent_dir / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    meta = arcagent.ScheduleMetadata(created_by="system", reason=f"seeded by blueprint {bp.name}")
    entries = [
        arcagent.ScheduleEntry(id=arcagent.generate_schedule_id(), metadata=meta, **dict(spec))
        for spec in bp.schedules
    ]
    arcagent.ScheduleStore(workspace / store_path).save(entries)
    result.schedules = len(entries)


# ---------------------------------------------------------------------------
# Signer resolution (shared by `arc init` and `arc blueprint apply --agent`)
# ---------------------------------------------------------------------------


def operator_signer_pair() -> Signer | None:
    """Resolve the deployment operator key as ``(did, seed)`` — None if unavailable.

    Prompt overlays are signed with the SAME operator key the agent's PromptResolver
    pins and re-verifies at run start (parity with ``arc prompt edit``). None here makes
    the materialize refuse a blueprint that ships overlays — fail loud, not silent.
    """
    try:
        from arctrust import OperatorKey, default_operator_key_path
        from arctrust.policy import OperatorApprovalAuthority

        op = OperatorKey.load(default_operator_key_path(), generate_if_absent=False)
    except (OSError, ValueError, RuntimeError):
        return None
    return OperatorApprovalAuthority(op.into_signer()).did, op.seed


def operator_capability_signer() -> CapabilitySigner | None:
    """Resolve the operator signer handle for capability signing — None if unavailable."""
    from arccli.commands.operator import operator_signer_and_did

    try:
        did, signer = operator_signer_and_did()
    except (OSError, ValueError, RuntimeError, SignerError):
        return None
    return CapabilitySigner(did=did, signer=signer)


__all__ = [
    "CapabilitySigner",
    "MaterializeResult",
    "Signer",
    "materialize_blueprint",
    "operator_capability_signer",
    "operator_signer_pair",
]
