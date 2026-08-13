"""Operator-gated capability-trust surface — the arcui half of ``arc trust``.

``GET  /api/trust/gated``       — list gated (non-loaded) capabilities across the
                                  server's agents (any authed role).
                                  ``?include_loaded=1`` adds the loaded ones.
``GET  /api/trust/source``      — one capability's artifact text (any authed
                                  role) so it can be read before it is trusted.
``POST /api/trust/approve``     — sign a capability (operator).
``POST /api/trust/disapprove``  — withdraw that signature (operator).

Signing is not only for what the loader currently refuses. An operator who
hand-edits a skill that loads fine at personal tier, pre-signs an artifact
before promoting it to a stricter deployment, or re-signs one they just
modified is doing the same operator action on a capability whose status is
``loaded``. So the mutations resolve their target from the FULL inventory while
the default listing stays gated-only — widening the default would bury "what
needs my attention" under everything that is already fine.

This module is a pure view: it DISCOVERS gated capabilities via
``arcagent.capabilities.inventory`` (arcagent owns loading) and MUTATES trust
through ``arcagent``'s signing seam (SPEC-066 COMP-010) — the same one
``arc trust approve`` drives, so a browser approval and a CLI approval are one
code path. Approval SIGNS: a source-hash pin alone leaves the capability behind
the loader's signature floor, which is the gap SPEC-066 exists to close. It
resolves agents from the roster, gates mutations on the operator role, records
the approver as the on-box operator DID, and audits every mutation — mirroring
``routes/approvals.py`` exactly. ``arcagent`` is reached through its root facade
only (SPEC-023 §2.2, enforced by ``test_arcui_imports_arcagent_only_via_
approved_seams`` and ``test_arcui_uses_only_public_arcagent_names``), so a view
module cannot quietly widen that surface. ``arctrust`` (a lower layer that
imports no siblings) is imported at module top.

REQ-321: approval is operator-authenticated HTTP only. It is not registered as a
tool on any registry and is not routed on ``/ws/chat`` — an agent must never be
able to authorize its own capability.
"""

from __future__ import annotations

import logging
import tomllib
from pathlib import Path
from typing import Any

import arcagent
from arctrust import (
    FileNotaryTransit,
    OperatorKey,
    Signer,
    SignerConfig,
    arc_home,
    build_signer,
    default_operator_key_path,
)
from arctrust import disapprove as _disapprove_pin
from arctrust.policy import OperatorApprovalAuthority
from arctrust.signer import VAULT_TRANSIT
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from arcui.audit import emit_mutation_audit, operator_actor_did, operator_audit_sink
from arcui.schemas import ErrorResponse

logger = logging.getLogger("arcui.routes.trust")

#: The transit key reference the deployment operator key is provisioned under —
#: the same one ``arccli.commands.operator`` uses, so a notary provisioned for
#: the CLI serves this route unchanged.
_OPERATOR_KEY_REF = "operator"


def _error(message: str, status: int) -> JSONResponse:
    return JSONResponse(ErrorResponse(error=message).model_dump(mode="json"), status_code=status)


def _flag(raw: str | None) -> bool:
    """Read an opt-in query flag. Anything but a truthy token is off."""
    return raw is not None and raw.lower() in {"1", "true", "yes", "on"}


def _row(item: Any) -> dict[str, Any]:
    """One inventory item as a wire row, plus WHO signed the artifact on disk.

    ``status`` alone cannot answer "is this signed, and by whom" — at personal
    tier an unsigned capability loads, and an agent may sign its own artifact
    with its own DID (SPEC-033) which loads too. Reading the sidecar is what
    lets an operator tell an operator-signed capability from a self-signed one
    before deciding to re-sign it. ``signer_did`` is empty when unsigned; only
    the signer's DID is surfaced — never the public key, never key material.
    """
    row: dict[str, Any] = item.model_dump(mode="json")
    manifest = arcagent.load_signature(Path(item.path))
    row["signer_did"] = manifest.signer_did if manifest is not None else ""
    return row


def _is_operator(request: Request) -> bool:
    return getattr(request.state, "role", None) == "operator"


def _resolve_agent(request: Request, agent_id: str) -> tuple[Path, str] | None:
    """Map ``agent_id`` to ``(agent_root, label)`` via the injected roster provider."""
    provider = getattr(request.app.state, "roster_provider", None)
    if provider is None:
        return None
    for entry in provider():
        if entry.agent_id == agent_id:
            return Path(entry.workspace_path), entry.display_name
    return None


def _machine_security() -> Any:
    """The deployment's ``[security]`` block, validated by ``SecurityConfig``.

    Read from the same arc home the operator key itself resolves under, so the
    tier crypto floor applies (federal implies ``vault_transit`` +
    ``ecdsa-p256``). An absent or unparseable config is the personal default,
    matching ``arccli.commands.operator._machine_security``.
    """
    config = arc_home() / "arcagent.toml"
    block: dict[str, Any] = {}
    if config.exists():
        try:
            block = tomllib.loads(config.read_text(encoding="utf-8")).get("security", {})
        except (OSError, tomllib.TOMLDecodeError):
            block = {}
    return arcagent.SecurityConfig(**block)


def _operator_signer() -> Signer:
    """The operator signer that signs an approved capability, at the machine's custody.

    ``in_process`` signs with the on-disk operator key — a read-only load that
    never bootstraps one, because an unpinned operator is no operator.
    ``vault_transit`` signs by reference through the notary/HSM and the seed
    never enters this process; the on-disk key is deliberately NOT consulted
    there, or a stale file left behind by a custody change would mint an
    authority the deployment moved to a vault.

    Anything unresolvable raises and the caller fails the mutation with 500,
    exactly like ``routes/approvals.py._operator_authority`` — never a silent
    downgrade to whatever key can be found (NFR-3).
    """
    security = _machine_security()
    if security.custody == VAULT_TRANSIT:
        return build_signer(
            SignerConfig(
                custody=VAULT_TRANSIT,
                algorithm=security.signing_algorithm,
                key_ref=_OPERATOR_KEY_REF,
            ),
            vault_transit=_transit(security),
        )
    key = OperatorKey.load(default_operator_key_path(), generate_if_absent=False)
    return key.into_signer(security.signing_algorithm)


def _transit(security: Any) -> FileNotaryTransit:
    """The out-of-process transit, proven able to serve the operator key first.

    Mirrors ``arccli.commands.operator._resolve_transit``: the same keystore
    convention, so a notary provisioned for the CLI serves this route unchanged.
    Probing the key here turns a missing keystore into a refusal at resolution
    time rather than a partial signing later.
    """
    keystore = (
        Path(security.notary_keystore).expanduser()
        if security.notary_keystore
        else Path(security.operator_key_dir).expanduser() / "notary"
    )
    transit = FileNotaryTransit(keystore, algorithm=security.signing_algorithm)
    transit.public_key(_OPERATOR_KEY_REF)
    return transit


def _operator_did(signer: Signer) -> str:
    """The deployment operator DID recorded as signer and approver."""
    return OperatorApprovalAuthority(signer).did


async def list_gated(request: Request) -> JSONResponse:
    """GET /api/trust/gated[?include_loaded=1] — capabilities across the server's agents.

    Gated-only by DEFAULT. This list is the operator's "what needs my attention"
    queue, and on a healthy fleet the loaded capabilities outnumber the gated
    ones by an order of magnitude — folding them in unconditionally would turn a
    short actionable queue into a full inventory dump. ``include_loaded=1`` is
    the explicit ask from a surface that offers re-signing, so the operator
    chooses the noise rather than inheriting it.
    """
    include_loaded = _flag(request.query_params.get("include_loaded"))
    provider = getattr(request.app.state, "roster_provider", None)
    entries = provider() if provider is not None else []
    gated: list[dict[str, Any]] = []
    for entry in entries:
        agent_root = Path(entry.workspace_path)
        try:
            items = await arcagent.list_gated(
                agent_root,
                agent_id=entry.agent_id,
                agent_label=entry.display_name,
                include_loaded=include_loaded,
            )
        except Exception:  # reason: fleet resilience — one bad agent never sinks the list
            logger.warning("trust inventory failed for %s; contributing none", entry.agent_id)
            continue
        gated.extend(_row(item) for item in items)
    return JSONResponse({"gated": gated})


async def read_source(request: Request) -> JSONResponse:
    """GET /api/trust/source?agent_id&name — the artifact text behind a row.

    Any authed role may read: reviewing a capability is what the approve gate
    demands first, and a viewer who cannot read it cannot review it. Only the
    operator may act on what they read.

    Resolves against the FULL inventory, loaded included. The approve gate
    unlocks only when this hash matches the row's hash, so a loaded capability
    whose source could not be fetched here could never be re-signed — the gate
    would be permanently locked and the operator would be pushed toward some
    ungated path to do the same thing.

    Fetched per row on demand rather than folded into ``/api/trust/gated``: that
    list polls every few seconds, so bundling artifact text would re-ship every
    capability's executable source to every open dashboard continuously.

    The ``hash`` returned is the same sha256 the row carries, so a surface can
    prove the text it rendered is the text an approval would sign.

    The artifact path comes from the inventory, never from the caller — a client
    names a capability, not a file, so no request can steer this read outside
    the agent's own capability tree.
    """
    agent_id = request.query_params.get("agent_id", "")
    name = request.query_params.get("name", "")
    if not agent_id or not name:
        return _error("agent_id and name are required", 400)

    resolved = _resolve_agent(request, agent_id)
    if resolved is None:
        return _error("agent_not_found", 404)
    agent_root, label = resolved
    try:
        gated = await arcagent.list_gated(
            agent_root, agent_id=agent_id, agent_label=label, include_loaded=True
        )
    except Exception:  # reason: an artifact bad enough to break the scan is unreviewable
        logger.warning("trust inventory failed for %s; source unavailable", agent_id)
        return _error(f"cannot read capability source for {name!r}", 404)
    item = next((entry for entry in gated if entry.name == name), None)
    if item is None:
        return _error(f"no gated capability named {name!r} for this agent", 404)

    source = arcagent.read_capability_source(Path(item.path))
    if source is None:
        return _error(f"cannot read capability source at {item.path}", 404)
    return JSONResponse(
        {
            "agent_id": agent_id,
            "name": name,
            "kind": item.kind,
            "path": item.path,
            "hash": item.hash,
            "source": source,
        }
    )


async def _read_body(request: Request) -> tuple[str, str] | JSONResponse:
    """Parse ``{agent_id, name}`` from the request body, or return a 400."""
    import json

    try:
        body = json.loads(await request.body())
    except (json.JSONDecodeError, ValueError):
        return _error("invalid JSON body", 400)
    if not isinstance(body, dict):
        return _error("body must be a JSON object", 400)
    agent_id = body.get("agent_id")
    name = body.get("name")
    if not isinstance(agent_id, str) or not isinstance(name, str) or not agent_id or not name:
        return _error("agent_id and name are required", 400)
    return agent_id, name


async def approve(request: Request) -> JSONResponse:
    """POST /api/trust/approve — sign a capability (operator).

    Resolves the target from the full inventory, so an already-loaded capability
    can be signed. Signing one that is already signed re-signs its CURRENT bytes
    and re-pins the hash — the re-sign flow after a hand edit, never a no-op.
    """
    parsed = await _read_body(request)
    if isinstance(parsed, JSONResponse):
        return parsed
    agent_id, name = parsed
    target = f"trust:{agent_id}:{name}"

    if not _is_operator(request):
        emit_mutation_audit(
            request,
            target=target,
            operation="trust.approve",
            outcome="denied",
            detail="viewer role",
        )
        return _error("operator_role_required", 403)

    resolved = _resolve_agent(request, agent_id)
    if resolved is None:
        return _error("agent_not_found", 404)
    agent_root, label = resolved

    try:
        signer = _operator_signer()
    except (OSError, RuntimeError, ValueError) as exc:
        # ``SignerError`` (a RuntimeError) covers an unresolvable transit; OSError
        # a missing/unreadable key file. Both are "no authority here" — refuse
        # rather than sign with anything else.
        logger.exception("operator signer unavailable for trust approval")
        emit_mutation_audit(
            request,
            target=target,
            operation="trust.approve",
            outcome="denied",
            detail="operator key unavailable",
        )
        return _error(f"operator_key_unavailable: {type(exc).__name__}", 500)
    approver = _operator_did(signer)

    # Discover the capability (arcagent), sign it (arcagent), re-scan.
    gated = await arcagent.list_gated(
        agent_root, agent_id=agent_id, agent_label=label, include_loaded=True
    )
    item = next((entry for entry in gated if entry.name == name), None)
    if item is None:
        detail = f"no capability named {name!r} for this agent"
        emit_mutation_audit(
            request, target=target, operation="trust.approve", outcome="denied", detail=detail
        )
        return _error(detail, 404)
    if arcagent.read_capability_source(Path(item.path)) is None:
        detail = f"cannot read capability source at {item.path}"
        emit_mutation_audit(
            request, target=target, operation="trust.approve", outcome="denied", detail=detail
        )
        return _error(detail, 404)

    # Read BEFORE signing: afterwards every artifact has a sidecar, so this is
    # the only moment that can tell a first signature from a re-signature.
    resigned = arcagent.sidecar_path(Path(item.path)).exists()

    try:
        # ``audit_sink`` is the chain this process already holds, so the
        # capability-level ``capability.signed`` record (REQ-323) lands beside
        # the HTTP-level mutation record below rather than opening a second one.
        arcagent.sign_capability(
            Path(item.path),
            signer_did=approver,
            signer=signer,
            config_path=agent_root / "arcagent.toml",
            audit_sink=operator_audit_sink(request),
        )
    except (OSError, ValueError) as exc:
        # A half-applied signing is a capability the operator believes is
        # trusted and is not — so it is audited, never silently 500'd.
        logger.exception("signing failed for %s", item.path)
        emit_mutation_audit(
            request,
            target=target,
            operation="trust.approve",
            outcome="error",
            detail=f"signing failed: {type(exc).__name__}",
        )
        return _error(f"capability_signing_failed: {type(exc).__name__}", 500)
    after = await arcagent.list_gated(
        agent_root, agent_id=agent_id, agent_label=label, include_loaded=True
    )
    resolved_item = next((entry for entry in after if entry.name == name), item)
    emit_mutation_audit(
        request,
        target=target,
        operation="trust.approve",
        outcome="applied",
        detail="re-signed" if resigned else "signed",
    )
    return JSONResponse({**_row(resolved_item), "resigned": resigned})


async def disapprove(request: Request) -> JSONResponse:
    """POST /api/trust/disapprove — withdraw a capability's signature (operator)."""
    parsed = await _read_body(request)
    if isinstance(parsed, JSONResponse):
        return parsed
    agent_id, name = parsed
    target = f"trust:{agent_id}:{name}"

    if not _is_operator(request):
        emit_mutation_audit(
            request,
            target=target,
            operation="trust.disapprove",
            outcome="denied",
            detail="viewer role",
        )
        return _error("operator_role_required", 403)

    resolved = _resolve_agent(request, agent_id)
    if resolved is None:
        return _error("agent_not_found", 404)
    agent_root, label = resolved

    # Revoke against the artifact when the inventory still sees it — that removes
    # the signature, the trusted key, and the pin together. When the artifact is
    # gone there is nothing to unsign, so clear the orphaned pin by name alone.
    inventory = await arcagent.list_gated(
        agent_root, agent_id=agent_id, agent_label=label, include_loaded=True
    )
    item = next((entry for entry in inventory if entry.name == name), None)
    config_path = agent_root / "arcagent.toml"
    try:
        if item is None:
            _disapprove_pin(config_path, name=name)
        else:
            arcagent.revoke_capability(
                Path(item.path),
                config_path=config_path,
                operator_did=operator_actor_did(request),
                audit_sink=operator_audit_sink(request),
            )
    except (OSError, ValueError) as exc:
        logger.exception("revocation failed for %s on %s", name, agent_id)
        emit_mutation_audit(
            request,
            target=target,
            operation="trust.disapprove",
            outcome="error",
            detail=f"revocation failed: {type(exc).__name__}",
        )
        return _error(f"capability_revocation_failed: {type(exc).__name__}", 500)
    emit_mutation_audit(request, target=target, operation="trust.disapprove", outcome="applied")
    return JSONResponse({"ok": True})


routes = [
    Route("/api/trust/gated", list_gated, methods=["GET"]),
    Route("/api/trust/source", read_source, methods=["GET"]),
    Route("/api/trust/approve", approve, methods=["POST"]),
    Route("/api/trust/disapprove", disapprove, methods=["POST"]),
]

__all__ = ["approve", "disapprove", "list_gated", "read_source", "routes"]
