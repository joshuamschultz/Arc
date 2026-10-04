"""Settings -> Maintenance -> Modules: per-agent module state, enable, disable, upgrade.

The browser's twin of ``arc module list`` / ``install`` and the live enable of
ADR-034. Every module reaches an agent as a signed bundle and nowhere else, so
there is no route here that installs from a name or a path:

* **install / upgrade** takes the bundle already waiting in the deployment's
  staging directory, verifies it at the deployment's tier against the operator key
  or a pinned issuer (:mod:`arcbundle`), writes the module, pins the issuer for
  that one agent, and enables it. A refusal writes nothing.
* **enable / disable** flips ``[modules.NAME] enabled`` for one agent. When that
  agent is running inside this process the change is applied live; otherwise it
  is saved and takes hold the next time the agent starts, and the answer says so.

Viewers may read. Every change is operator-only and audited.
"""

from __future__ import annotations

import ast
import asyncio
import logging
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

import arcagent
import arcbundle
from arctrust.paths import bundles_dir, config_file
from arctrust.policy import OperatorApprovalAuthority
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from arcui.audit import emit_mutation_audit, emit_read_audit, operator_audit_sink
from arcui.routes.agent_detail.config_files import BodyTooLargeError, _error, read_json_object
from arcui.routes.trust import operator_signer_for_request

logger = logging.getLogger("arcui.routes.maintenance_modules")

#: A module name is a single lowercase identifier — the folder it installs into.
MODULE_NAME = re.compile(r"^[a-z][a-z0-9_]{0,63}$")

_NOT_INSTALLED = "{name} is not installed on this computer. A signed bundle has to be added first."
_NEEDS_SETUP = (
    "{name} has not been set up for this agent yet. Install it from its signed bundle first."
)
_NO_BUNDLE = "No signed bundle for {name} is waiting to be installed."
_UNTRUSTED = "This bundle is not signed by a key Arc trusts, so it was not installed."
_ALTERED = "The bundle's files do not match its signature, so it was not installed."
_INVALID = "This bundle is not valid, so it was not installed."
_LIVE_FAILED = "Could not change {name} while {agent} is running. Nothing was changed."


def _agent_entries(request: Request) -> list[Any]:
    """Every native Arc agent on this deployment's roster."""
    provider = getattr(request.app.state, "roster_provider", None)
    entries = provider() if provider is not None else []
    return [e for e in entries if getattr(e, "harness", "arcagent") == "arcagent"]


def _entry_for(request: Request, agent_id: object) -> Any | None:
    if not isinstance(agent_id, str):
        return None
    return next((e for e in _agent_entries(request) if e.agent_id == agent_id), None)


def _live_agent(request: Request, entry: Any) -> Any | None:
    """The agent object when it runs inside this process, else ``None``."""
    cache = getattr(request.app.state, "embedded_agent_cache", None)
    return cache.get(entry.did) if cache is not None and entry.did else None


def _enabled_for(entry: Any) -> set[str] | None:
    """Module names this agent's config enables, or ``None`` when it cannot be read."""
    path = Path(entry.workspace_path) / "arcagent.toml"
    try:
        config = arcagent.load_config(path)
    except Exception:  # reason: one unreadable agent must not hide every other row
        logger.warning("modules: cannot read config for %s", entry.agent_id, exc_info=True)
        return None
    return {name for name, module in config.modules.items() if module.enabled}


def _description(name: str) -> str:
    """First line of the module's own description, read without importing it."""
    source = arcagent.module_root() / name / "__init__.py"
    try:
        doc = ast.get_docstring(ast.parse(source.read_text(encoding="utf-8")))
    except (OSError, SyntaxError, ValueError):
        return ""
    return doc.strip().splitlines()[0] if doc else ""


def _module_rows(request: Request) -> list[dict[str, Any]]:
    entries = _agent_entries(request)
    enabled_by_agent = {e.agent_id: _enabled_for(e) for e in entries}
    staged = arcbundle.staged_bundles(bundles_dir())
    installed = set(arcagent.discover_modules())
    names = sorted(
        installed | set(staged) | {n for names in enabled_by_agent.values() for n in names or ()}
    )
    root = arcagent.module_root()
    rows: list[dict[str, Any]] = []
    for name in names:
        bundle = staged.get(name)
        rows.append(
            {
                "name": name,
                "description": _description(name),
                "installed": name in installed,
                "staged": None
                if bundle is None
                else {
                    "version": bundle.version,
                    "issuer": bundle.issuer,
                    "update_available": not arcbundle.bundle_matches_installed(bundle, root),
                },
                "agents": {
                    agent_id: {"enabled": None if on is None else name in on}
                    for agent_id, on in enabled_by_agent.items()
                },
            }
        )
    return rows


async def get_modules(request: Request) -> JSONResponse:
    """GET /api/maintenance/modules — every known module and its state on each agent."""
    agents = [
        {"agent_id": e.agent_id, "name": getattr(e, "display_name", "") or e.name}
        for e in _agent_entries(request)
    ]
    emit_read_audit(request, target="modules", operation="module.list", outcome="ok")
    return JSONResponse(
        {"agents": agents, "modules": await asyncio.to_thread(_module_rows, request)}
    )


# --------------------------------------------------------------------------- changes


class _Refused(Exception):  # noqa: N818  # reason: a refusal carries its own status and words
    def __init__(self, message: str, status: int, detail: str) -> None:
        super().__init__(message)
        self.message = message
        self.status = status
        self.detail = detail


def _gate(request: Request, operation: str, module: str) -> JSONResponse | None:
    """The operator check shared by every change; audits the refusal."""
    if getattr(request.state, "role", None) == "operator":
        return None
    emit_mutation_audit(
        request,
        target=f"module:{module[:64]}",
        operation=operation,
        outcome="denied",
        detail="not an operator",
    )
    return _error("Only an operator can change modules.", 403)


async def _target(request: Request, module: str) -> tuple[Any, str] | _Refused:
    """The roster entry and the module name for a change, or the refusal to send."""
    if not MODULE_NAME.fullmatch(module):
        return _Refused("That is not a module name.", 400, "bad module name")
    try:
        body = await read_json_object(request)
    except BodyTooLargeError:
        body = None
    entry = _entry_for(request, None if body is None else body.get("agent_id"))
    if entry is None:
        return _Refused("Choose which agent to change.", 404, "unknown agent")
    return entry, module


def _installed_dir(module: str) -> Path | None:
    path = arcagent.module_root() / module
    return path if path.is_dir() and not path.is_symlink() else None


def _has_agent_copy(agent_root: Path, module: str) -> bool:
    return arcbundle.capability_dir(agent_root, module).is_dir()


def _ships_capabilities(installed: Path) -> bool:
    return (installed / arcbundle.CAPABILITY_FILE).is_file() or (
        installed / arcbundle.SKILLS_DIR
    ).is_dir()


async def _apply_flag(
    request: Request, entry: Any, module: str, *, enabled: bool
) -> dict[str, Any]:
    """Save ``enabled`` for one agent, live when that agent runs here."""
    config_path = Path(entry.workspace_path) / "arcagent.toml"
    live = _live_agent(request, entry)
    if live is not None:
        change = live.enable_module_persisted if enabled else live.disable_module_persisted
        try:
            await change(module)
        except Exception:  # reason: the lifecycle already rolled disk and memory back
            logger.warning("modules: live change failed for %s", module, exc_info=True)
            raise _Refused(
                _LIVE_FAILED.format(name=module, agent=entry.agent_id), 503, "live change failed"
            ) from None
    else:
        persist = arcagent.persist_module_enabled if enabled else arcagent.persist_module_disabled
        await asyncio.to_thread(persist, config_path, module)
    return {
        "module": module,
        "agent_id": entry.agent_id,
        "enabled": enabled,
        "live": live is not None,
        "restart_needed": live is None,
    }


async def _respond(
    request: Request,
    operation: str,
    module: str,
    work: Callable[[], Any],
) -> JSONResponse:
    """Run one change, audit its outcome, and turn a refusal into plain words."""
    target = f"module:{module}"
    try:
        result = await work()
    except _Refused as refusal:
        emit_mutation_audit(
            request,
            target=target,
            operation=operation,
            outcome="denied",
            detail=refusal.detail,
        )
        return _error(refusal.message, refusal.status)
    emit_mutation_audit(request, target=target, operation=operation, outcome="applied")
    return JSONResponse(result)


def _early_refusal(
    request: Request, operation: str, module: str, refusal: _Refused
) -> JSONResponse:
    emit_mutation_audit(
        request,
        target=f"module:{module[:64]}",
        operation=operation,
        outcome="denied",
        detail=refusal.detail,
    )
    return _error(refusal.message, refusal.status)


async def _set_enabled(request: Request, *, enabled: bool) -> JSONResponse:
    module = request.path_params["module"]
    operation = "module.enable" if enabled else "module.disable"
    refused = _gate(request, operation, module)
    if refused is not None:
        return refused
    target = await _target(request, module)
    if isinstance(target, _Refused):
        return _early_refusal(request, operation, module, target)
    entry, module = target

    async def _work() -> dict[str, Any]:
        installed = _installed_dir(module)
        if enabled:
            if installed is None:
                raise _Refused(_NOT_INSTALLED.format(name=module), 404, "not installed")
            if _ships_capabilities(installed) and not _has_agent_copy(
                Path(entry.workspace_path), module
            ):
                raise _Refused(_NEEDS_SETUP.format(name=module), 409, "no agent copy")
        result = await _apply_flag(request, entry, module, enabled=enabled)
        result["message"] = _flag_message(result)
        return result

    return await _respond(request, operation, module, _work)


def _flag_message(result: dict[str, Any]) -> str:
    state = "on" if result["enabled"] else "off"
    base = f"{result['module']} is now {state} for {result['agent_id']}."
    if result["live"]:
        return base
    return f"{base} It takes effect when Arc restarts."


async def enable_module(request: Request) -> JSONResponse:
    """POST /api/maintenance/modules/{module}/enable — body ``{"agent_id"}``. Operator only."""
    return await _set_enabled(request, enabled=True)


async def disable_module(request: Request) -> JSONResponse:
    """POST /api/maintenance/modules/{module}/disable — body ``{"agent_id"}``. Operator only."""
    return await _set_enabled(request, enabled=False)


def _operator_identity(request: Request) -> tuple[str, bytes]:
    signer = operator_signer_for_request(request)
    return OperatorApprovalAuthority(signer).did, signer.public_key


def _verify_and_install(request: Request, module: str, entry: Any) -> tuple[Path, bytes, str]:
    """Verify the staged bundle in full, then write it for one agent. Blocking."""
    bundle = arcbundle.staged_bundles(bundles_dir()).get(module)
    if bundle is None:
        raise _Refused(_NO_BUNDLE.format(name=module), 404, "no staged bundle")
    agent_root = Path(entry.workspace_path)
    try:
        tier = arcbundle.verification_tier(
            config_file("arcagent.toml"), agent_root / "arcagent.toml"
        )
        operator_did, operator_key = _operator_identity(request)
    except arcbundle.BundleTierError:
        raise _Refused(
            "Arc could not read this deployment's security level.", 503, "tier"
        ) from None
    except Exception:  # reason: no operator key means no verification authority
        raise _Refused("Arc's operator key is unavailable.", 503, "operator key") from None
    sink = operator_audit_sink(request)
    try:
        verified = arcbundle.verify_bundle(
            bundle.path,
            tier=tier,
            trusted_issuers=arcbundle.trusted_issuers(
                bundle.path, operator=(operator_did, operator_key)
            ),
            sink=sink,
            actor_did=operator_did,
        )
    except arcbundle.BundleSignatureError:
        raise _Refused(_UNTRUSTED, 409, "untrusted signature") from None
    except arcbundle.BundleContentHashError:
        raise _Refused(_ALTERED, 409, "content mismatch") from None
    except arcbundle.BundleError:
        raise _Refused(_INVALID, 409, "invalid bundle") from None
    installed = arcbundle.install_verified(
        verified,
        modules_root=arcagent.module_root(),
        agent_root=agent_root,
        sink=sink,
        actor_did=operator_did,
    )
    arcagent.trust_bundled_capabilities(
        installed,
        config_path=agent_root / "arcagent.toml",
        issuer_key=verified.issuer_key,
        issuer_did=verified.manifest.issuer,
        audit_sink=sink,
    )
    return installed, operator_key, verified.manifest.version


async def install_module(request: Request) -> JSONResponse:
    """POST /api/maintenance/modules/{module}/install — install or upgrade from the staged one."""
    module = request.path_params["module"]
    refused = _gate(request, "module.install", module)
    if refused is not None:
        return refused
    target = await _target(request, module)
    if isinstance(target, _Refused):
        return _early_refusal(request, "module.install", module, target)
    entry, module = target

    async def _work() -> dict[str, Any]:
        was_enabled = module in (_enabled_for(entry) or set())
        _, _, version = await asyncio.to_thread(_verify_and_install, request, module, entry)
        result = await _apply_flag(request, entry, module, enabled=True)
        # New code only replaces old code in a running agent when it starts again.
        result["restart_needed"] = was_enabled or result["restart_needed"]
        result["version"] = version
        result["message"] = f"{module} {version} is installed for {entry.agent_id}." + (
            " Restart Arc to start using it." if result["restart_needed"] else ""
        )
        return result

    return await _respond(request, "module.install", module, _work)


routes = [
    Route("/api/maintenance/modules", get_modules, methods=["GET"]),
    Route("/api/maintenance/modules/{module}/enable", enable_module, methods=["POST"]),
    Route("/api/maintenance/modules/{module}/disable", disable_module, methods=["POST"]),
    Route("/api/maintenance/modules/{module}/install", install_module, methods=["POST"]),
]

__all__ = ["disable_module", "enable_module", "get_modules", "install_module", "routes"]
