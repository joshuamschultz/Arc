"""SPEC-064 T-021 — the connector routes: the web connects an agent to a system.

The web re-derives nothing about how a connection is made (D-585, D-587). Every
verb here resolves the agent, then hands off to :mod:`arcagent.connections` — the
one seam ``arc connector`` and the TUI drive too. The ordering behind it (resolve,
manifest, host, verify, secrets, probe, persist) IS the security property:
building an attachment executes extension-declared code, so the signature gate has
to close first, and a credential written before a failed probe has to be rolled
back. A route that assembled its own sequence would be a second copy of that
ordering to keep in step, and the one that drifts is the one an attacker uses.

Two things the web adds, and only these:

* **The operator gate.** Every mutation is ``operator``-only and every body is
  capped, exactly as ``agent_detail/config_files`` does it. The probe verb is
  gated too — it opens a live outbound connection.
* **A shape a browser can render.** The response models in :mod:`arcui.schemas`
  have no field able to hold a credential, so a secret an operator submits is
  consumed and dropped: it is not returned, not logged, and not part of any error
  this module raises. The ``ExtensionError`` messages that become 400s are
  written for operators and name coordinates, never values.

The chain these verbs record into is the one this process already holds open, not
a fresh one: a ``WormSink`` keeps an exclusive ``flock``, so a route opening its
own would contend with every later writer. ``AuditChain.held`` says exactly that,
and is why nothing here has a sink to close.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import Any

from arcagent.connections import (
    BUNDLES_DIRNAME,
    NOT_INSTALLED,
    AuditChain,
    Authorization,
    CatalogEntry,
    Connections,
    ConnectorPlan,
    ExtensionError,
    HostPrerequisiteDirector,
    HostVerdict,
    Tier,
    ToolSpec,
    catalog,
    resolve_roots,
)
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from arcui.audit import emit_mutation_audit, operator_audit_sink
from arcui.routes.agent_detail._common import _agent_root
from arcui.routes.agent_detail.config_files import (
    BodyTooLargeError,
    _error,
    read_json_object,
)
from arcui.schemas import (
    AgentConnectorsResponse,
    ConnectorApproveResponse,
    ConnectorAuthorizationResponse,
    ConnectorAuthResponse,
    ConnectorAuthStatusResponse,
    ConnectorCatalogEntry,
    ConnectorCatalogResponse,
    ConnectorDoctorCheck,
    ConnectorDoctorResponse,
    ConnectorHostAuthorization,
    ConnectorHostBlockedResponse,
    ConnectorHostRequirement,
    ConnectorHostSetupResponse,
    ConnectorInstallResponse,
    ConnectorInstance,
    ConnectorProbeResponse,
    ConnectorRemoveResponse,
    ConnectorSecretField,
    ConnectorTool,
    ConnectorUnreadableBundle,
)

logger = logging.getLogger("arcui.routes.connectors")

#: Tier the fleet-wide catalog parses manifests at. The catalog answers "which
#: bundles exist on this machine", a question with no agent and therefore no
#: tier; the tier verdict that matters is taken per agent by the connection seam,
#: where the agent's real tier is known. Reading the listing at the most permissive
#: tier means an operator sees a bundle and is told precisely why it is refused,
#: rather than seeing an empty page.
_CATALOG_TIER = Tier.PERSONAL


def _is_operator(request: Request) -> bool:
    return getattr(request.state, "role", None) == "operator"


def _connections(request: Request, agent_id: str) -> Connections | None:
    """Bind the agent's connector seam to the chain this process already holds."""
    agent_dir = _agent_root(request, agent_id)
    if agent_dir is None:
        return None
    try:
        return Connections.for_agent(
            agent_dir, audit=AuditChain.held(operator_audit_sink(request))
        )
    except ExtensionError as exc:
        logger.warning(
            "connectors: %s is not a usable agent directory — %s", agent_id, exc.message
        )
        return None


def _not_found(exc: ExtensionError) -> bool:
    """A refusal about an instance nothing has connected is a 404, not a 400."""
    return exc.code == NOT_INSTALLED


def _refused(exc: ExtensionError) -> JSONResponse:
    return _error(exc.message, 404 if _not_found(exc) else 400)


def _tools(specs: Sequence[ToolSpec]) -> list[ConnectorTool]:
    return [
        ConnectorTool(
            name=spec.name,
            description=spec.description,
            classification=spec.classification,
            capability_tags=list(spec.capability_tags),
        )
        for spec in specs
    ]


def _host_requirements(verdicts: Sequence[HostVerdict]) -> list[ConnectorHostRequirement]:
    return [
        ConnectorHostRequirement(name=verdict.name, instruction=verdict.instruction)
        for verdict in verdicts
    ]


def _submitted_secrets(body: dict[str, Any]) -> dict[str, str]:
    """The ``secrets`` mapping, keeping only string values. Never logged."""
    raw = body.get("secrets")
    if not isinstance(raw, dict):
        return {}
    return {str(name): value for name, value in raw.items() if isinstance(value, str)}


def _missing_secrets(plan: ConnectorPlan, supplied: dict[str, str]) -> list[str]:
    """Declared credentials the operator did not fill in. Names only."""
    return [declared.name for declared in plan.secrets if not supplied.get(declared.name)]


# ---------------------------------------------------------------------------
# Catalog
# ---------------------------------------------------------------------------


async def get_catalog(request: Request) -> JSONResponse:
    """GET /api/connectors/catalog — what this deployment could connect.

    Readable by ``viewer``. A directory that will not parse is reported under
    ``unreadable`` rather than dropped or allowed to 500 the whole listing: one
    broken bundle blanking the catalog is a far worse failure than a listed
    bundle an operator cannot install.
    """
    entries = catalog(
        roots=resolve_roots(None),
        tier=_CATALOG_TIER,
        audit_sink=operator_audit_sink(request),
    )
    director = _host_director(request)
    return JSONResponse(
        ConnectorCatalogResponse(
            available=[_catalog_entry(entry, director) for entry in entries if not entry.error],
            unreadable=[
                ConnectorUnreadableBundle(name=entry.name, reason=entry.error)
                for entry in entries
                if entry.error
            ],
        ).model_dump(mode="json")
    )


def _host_director(request: Request) -> HostPrerequisiteDirector:
    """The prerequisite checker, injectable so a test never depends on this machine."""
    injected = getattr(request.app.state, "host_director", None)
    if isinstance(injected, HostPrerequisiteDirector):
        return injected
    return HostPrerequisiteDirector()


def _catalog_entry(
    entry: CatalogEntry, director: HostPrerequisiteDirector
) -> ConnectorCatalogEntry:
    """One listing row: what connecting this bundle would give the agent."""
    return ConnectorCatalogEntry(
        name=entry.name,
        version=entry.version,
        description=entry.description,
        attachment=entry.attachment,
        tier_floor=entry.tier_floor,
        approval_default=entry.approval_default,
        secrets=[
            # No ``value`` here and there could not be one: the catalog describes a
            # bundle nobody has connected yet, so there is nothing configured to show.
            ConnectorSecretField(
                name=declared.name, prompt=declared.prompt, sensitive=declared.sensitive
            )
            for declared in entry.secrets
        ],
        host_requires=[
            ConnectorHostRequirement(
                name=verdict.name,
                # A met requirement keeps no instruction: the operator has nothing
                # left to do, and showing steps anyway is what made these cards read
                # as broken.
                instruction=verdict.instruction,
                satisfied=verdict.satisfied,
            )
            for verdict in director.check(entry.host_requires)
        ],
        tools=[
            ConnectorTool(
                name=tool.name,
                description=tool.description,
                classification=tool.classification,
                capability_tags=list(tool.capability_tags),
            )
            for tool in entry.tools
        ],
        root=str(entry.path.parent),
    )


# ---------------------------------------------------------------------------
# Agent-scoped verbs
# ---------------------------------------------------------------------------


async def get_agent_connectors(request: Request) -> JSONResponse:
    """GET /api/agents/{id}/connectors — this agent's connected accounts."""
    connections = _connections(request, request.path_params["id"])
    if connections is None:
        return _error("Agent not found", 404)
    try:
        instances = connections.installed()
    except ExtensionError as exc:
        return _error(exc.message, 400)

    return JSONResponse(
        AgentConnectorsResponse(
            instances=[
                ConnectorInstance(
                    instance=name, extension=config.extension, approval=config.approval
                )
                for name, config in sorted(instances.items())
            ],
            extensions_root=str(connections.world.agent_dir / BUNDLES_DIRNAME),
        ).model_dump(mode="json")
    )


async def post_agent_connector(request: Request) -> JSONResponse:
    """POST /api/agents/{id}/connectors — install one connected account.

    Operator only. Refusals happen before anything is written: a name already in
    use is a 409, a credential the manifest declares and the operator left blank
    is a 422, and a missing host prerequisite is a 400 carrying the instruction
    the operator must run — arcui directs, it never installs (REQ-262).
    """
    if not _is_operator(request):
        return _error("Operator role required", 403)

    try:
        body = await read_json_object(request)
    except BodyTooLargeError:
        return _error("Request body too large", 413)
    if body is None:
        return _error("Body must be a JSON object", 400)

    extension = str(body.get("extension") or "")
    instance = str(body.get("instance") or "")
    if not extension or not instance:
        return _error("Both 'extension' and 'instance' are required", 400)

    connections = _connections(request, request.path_params["id"])
    if connections is None:
        return _error("Agent not found", 404)

    agent = connections.world.agent
    try:
        if instance in connections.installed():
            return _error(f"instance {instance!r} already exists on {agent}", 409)
        plan = connections.plan(extension, instance)
    except ExtensionError as exc:
        return _error(exc.message, 400)

    if plan.unsatisfied_host:
        return _host_blocked(plan)

    supplied = _submitted_secrets(body)
    missing = _missing_secrets(plan, supplied)
    if missing:
        return _error(f"missing required credential(s): {', '.join(missing)}", 422)

    return await _run_install(request, connections, plan, supplied)


def _host_blocked(plan: ConnectorPlan) -> JSONResponse:
    """400 naming every prerequisite the operator must install first."""
    return JSONResponse(
        ConnectorHostBlockedResponse(
            error=f"{plan.extension} needs prerequisites this machine lacks",
            unsatisfied_host=_host_requirements(plan.unsatisfied_host),
        ).model_dump(mode="json"),
        status_code=400,
    )


async def _run_install(
    request: Request,
    connections: Connections,
    plan: ConnectorPlan,
    secrets: dict[str, str],
) -> JSONResponse:
    """Drive the shared install path and shape its report for the browser."""
    target = f"connector:{connections.world.agent}/{plan.instance}"
    try:
        report = await connections.install(plan, secrets)
    except ExtensionError as exc:
        emit_mutation_audit(
            request,
            target=target,
            operation="connector.install",
            outcome="denied",
            detail=exc.code,
        )
        return _error(exc.message, 400)

    emit_mutation_audit(
        request,
        target=target,
        operation="connector.install",
        outcome="applied",
        detail=plan.extension,
    )
    return JSONResponse(
        ConnectorInstallResponse(
            instance=report.instance,
            extension=report.extension,
            tools=list(report.tools),
            detail=report.detail,
        ).model_dump(mode="json")
    )


async def get_connector_auth(request: Request) -> JSONResponse:
    """GET /api/agents/{id}/connectors/{instance}/auth — how this connection is authorised.

    Readable by ``viewer``: it names credential FIELDS and host commands, never a
    value. The panel needs it because half the shipped bundles declare no
    ``[[secrets]]`` at all — their binary holds its own token — and a form drawn
    from an empty credential list leaves a non-technical operator with nothing to
    fill in and no idea that ``gh auth login`` is the actual next step.
    """
    instance = request.path_params["instance"]
    connections = _connections(request, request.path_params["id"])
    if connections is None:
        return _error("Agent not found", 404)

    try:
        auth = await connections.authorization(instance)
    except ExtensionError as exc:
        return _refused(exc)

    return JSONResponse(
        ConnectorAuthorizationResponse(
            instance=auth.instance,
            extension=auth.extension,
            credentials=[
                # ``value`` is whatever the seam resolved, which is the empty string
                # for every sensitive field — this route neither decides that nor can
                # ask for anything else.
                ConnectorSecretField(
                    name=required.name,
                    prompt=required.prompt,
                    sensitive=required.sensitive,
                    value=required.value,
                )
                for required in auth.credentials
            ],
            hosts=[
                ConnectorHostAuthorization(
                    binary=host.binary,
                    command=host.command,
                    instruction=host.instruction,
                    token_command=host.token_command,
                )
                for host in auth.hosts
            ],
            reachable=auth.reachable,
            detail=auth.detail,
        ).model_dump(mode="json")
    )


async def put_connector_auth(request: Request) -> JSONResponse:
    """PUT /api/agents/{id}/connectors/{instance}/auth — re-supply credentials.

    Operator only. Answers with the field NAMES that were rewritten; the values
    go to the secret store and nowhere else.
    """
    if not _is_operator(request):
        return _error("Operator role required", 403)

    try:
        body = await read_json_object(request)
    except BodyTooLargeError:
        return _error("Request body too large", 413)
    if body is None:
        return _error("Body must be a JSON object", 400)

    instance = request.path_params["instance"]
    connections = _connections(request, request.path_params["id"])
    if connections is None:
        return _error("Agent not found", 404)

    try:
        plan = connections.plan_for(instance)
        updated = await connections.reauth(plan, _submitted_secrets(body))
    except ExtensionError as exc:
        return _refused(exc)

    emit_mutation_audit(
        request,
        target=f"connector:{connections.world.agent}/{instance}",
        operation="connector.auth",
        outcome="applied",
        detail=",".join(updated),
    )
    return JSONResponse(
        ConnectorAuthResponse(instance=instance, updated=list(updated)).model_dump(mode="json")
    )


async def post_connector_probe(request: Request) -> JSONResponse:
    """POST /api/agents/{id}/connectors/{instance}/probe — is it live right now?

    Operator only: probing opens an outbound connection to the external system.
    """
    if not _is_operator(request):
        return _error("Operator role required", 403)

    instance = request.path_params["instance"]
    connections = _connections(request, request.path_params["id"])
    if connections is None:
        return _error("Agent not found", 404)

    try:
        result = await connections.probe(instance)
    except ExtensionError as exc:
        return _refused(exc)
    except Exception:  # reason: an unbuildable attachment must not 500 with detail
        logger.exception("connectors: probe failed for %s/%s", connections.world.agent, instance)
        return _error("probe failed; see the server log", 500)

    return JSONResponse(
        ConnectorProbeResponse(
            reachable=result.reachable, detail=result.detail, tools=_tools(result.tools)
        ).model_dump(mode="json")
    )


def _auth_status(auth: Authorization) -> JSONResponse:
    """The sign-in as the panel renders it: two live answers and the honest next step.

    ``sign_in`` is the manifest's own authorisation check, run now — never the
    probe. The probe said "the binary ran", the panel drew "Signed in", and a
    ``dbxcli`` with no saved credentials was reported to its operator as a
    connected account. ``reachable`` still carries the probe, because a panel
    needs to distinguish "not signed in" from "not answering at all".

    ``detail`` is the sign-in evidence and only that: the command that was run and
    what it answered, empty when nothing was checked. It is NOT the probe's line —
    the probe's was rendered beside "Signed in" and for a connector Arc cannot log
    in read "signs in on this host; Arc ran nothing", which is a green tick citing
    a check nobody performed.

    ``command`` is :attr:`~arcagent.connections.Authorization.manual_command`,
    which is empty whenever Arc could finish the login itself.
    """
    return JSONResponse(
        ConnectorAuthStatusResponse(
            sign_in=auth.sign_in,
            reachable=auth.reachable,
            detail=auth.sign_in_detail,
            command=auth.manual_command,
        ).model_dump(mode="json")
    )


async def get_connector_auth_status(request: Request) -> JSONResponse:
    """GET /api/agents/{id}/connectors/{instance}/auth-status — is its binary signed in?

    Readable by ``viewer``: it probes and names commands, never a credential.
    """
    connections = _connections(request, request.path_params["id"])
    if connections is None:
        return _error("Agent not found", 404)

    try:
        return _auth_status(await connections.authorization(request.path_params["instance"]))
    except ExtensionError as exc:
        return _refused(exc)


async def post_connector_authorize(request: Request) -> JSONResponse:
    """POST /api/agents/{id}/connectors/{instance}/authorize — sign the binary in.

    Operator only: it runs a program on the host and opens an outbound
    connection. The optional ``token`` is a credential — it goes to the binary's
    stdin and to nothing else, and appears in no response, log line, or audit
    event this route produces. A connector whose login only a person can finish
    is not attempted; the answer carries the command instead (SPEC-064).
    """
    if not _is_operator(request):
        return _error("Operator role required", 403)

    try:
        body = await read_json_object(request)
    except BodyTooLargeError:
        return _error("Request body too large", 413)
    if body is None:
        return _error("Body must be a JSON object", 400)

    instance = request.path_params["instance"]
    connections = _connections(request, request.path_params["id"])
    if connections is None:
        return _error("Agent not found", 404)

    submitted = body.get("token")
    try:
        auth = await connections.authorize(
            instance, token=submitted if isinstance(submitted, str) else ""
        )
    except ExtensionError as exc:
        return _refused(exc)

    emit_mutation_audit(
        request,
        target=f"connector:{connections.world.agent}/{instance}",
        operation="connector.authorize",
        outcome="applied" if auth.working else "denied",
        detail=auth.extension,
    )
    return _auth_status(auth)


async def post_connector_host_setup(request: Request) -> JSONResponse:
    """POST /api/agents/{id}/connectors/{extension}/host-setup — install what it needs.

    Operator only. Keyed by BUNDLE, not instance: the prerequisite is missing
    before any instance exists.

    REQ-262 is intact — Arc still never runs the manifest's instruction. The
    manifest names bytes and the digest they must hash to for this platform; the
    bytes are verified before anything is unpacked and land in the operator's own
    ``~/.local/bin``, never a system path and never with sudo. A refusal is a 200
    carrying the reason and the bundle's own manual steps, because an operator
    left with an error and no next step is exactly the wall this removes.
    """
    if not _is_operator(request):
        return _error("Operator role required", 403)

    extension = request.path_params["instance"]
    connections = _connections(request, request.path_params["id"])
    if connections is None:
        return _error("Agent not found", 404)

    try:
        report = await connections.setup_host(extension)
    except ExtensionError as exc:
        return _error(exc.message, 400)

    emit_mutation_audit(
        request,
        target=f"connector:{connections.world.agent}/{extension}",
        operation="connector.host_setup",
        outcome="applied" if report.installed else "denied",
        detail=extension,
    )
    return JSONResponse(
        ConnectorHostSetupResponse(
            installed=report.installed, detail=report.detail, manual_steps=report.manual_steps
        ).model_dump(mode="json")
    )


async def get_connector_doctor(request: Request) -> JSONResponse:
    """GET /api/agents/{id}/connectors/{instance}/doctor — everything that is wrong.

    Readable by ``viewer``, and the same rows ``arc connector doctor`` prints:
    unmet prerequisites, whether each declared credential is present (never its
    value), and whether the connection answers.
    """
    instance = request.path_params["instance"]
    connections = _connections(request, request.path_params["id"])
    if connections is None:
        return _error("Agent not found", 404)

    try:
        checks = await connections.doctor(instance)
    except ExtensionError as exc:
        return _refused(exc)

    return JSONResponse(
        ConnectorDoctorResponse(
            checks=[
                ConnectorDoctorCheck(check=row.check, status=row.status, detail=row.detail)
                for row in checks
            ]
        ).model_dump(mode="json")
    )


async def post_connector_approve(request: Request) -> JSONResponse:
    """POST /api/agents/{id}/connectors/{instance}/approve — pin the tool contract.

    Operator only. Records the contract the connection serves RIGHT NOW as
    approved, which is the rug-pull defence (REQ-291): a tool whose shape changes
    afterwards is suspended until an operator approves it again.
    """
    if not _is_operator(request):
        return _error("Operator role required", 403)

    instance = request.path_params["instance"]
    connections = _connections(request, request.path_params["id"])
    if connections is None:
        return _error("Agent not found", 404)

    try:
        approved = await connections.approve(instance)
    except ExtensionError as exc:
        return _refused(exc)

    emit_mutation_audit(
        request,
        target=f"connector:{connections.world.agent}/{instance}",
        operation="connector.approve",
        outcome="applied",
        detail=",".join(approved),
    )
    return JSONResponse(
        ConnectorApproveResponse(instance=instance, approved=list(approved)).model_dump(
            mode="json"
        )
    )


async def delete_agent_connector(request: Request) -> JSONResponse:
    """DELETE /api/agents/{id}/connectors/{instance} — drop one connected account.

    Operator only. Removing something that was never installed is reported, not
    raised: an operator cleaning up after a failed install must not be blocked by
    a step that already has nothing to do.
    """
    if not _is_operator(request):
        return _error("Operator role required", 403)

    instance = request.path_params["instance"]
    connections = _connections(request, request.path_params["id"])
    if connections is None:
        return _error("Agent not found", 404)

    try:
        report = await connections.remove(instance)
    except ExtensionError as exc:
        return _error(exc.message, 400)

    emit_mutation_audit(
        request,
        target=f"connector:{connections.world.agent}/{instance}",
        operation="connector.remove",
        outcome="applied" if report.removed_config else "denied",
    )
    return JSONResponse(
        ConnectorRemoveResponse(
            instance=report.instance,
            removed_secrets=list(report.removed_secrets),
            removed_config=report.removed_config,
            removed_state=report.removed_state,
        ).model_dump(mode="json")
    )


routes = [
    Route("/api/connectors/catalog", get_catalog, methods=["GET"]),
    Route("/api/agents/{id}/connectors", get_agent_connectors, methods=["GET"]),
    Route("/api/agents/{id}/connectors", post_agent_connector, methods=["POST"]),
    Route("/api/agents/{id}/connectors/{instance}/auth", get_connector_auth, methods=["GET"]),
    Route("/api/agents/{id}/connectors/{instance}/auth", put_connector_auth, methods=["PUT"]),
    Route(
        "/api/agents/{id}/connectors/{instance}/auth-status",
        get_connector_auth_status,
        methods=["GET"],
    ),
    Route(
        "/api/agents/{id}/connectors/{instance}/authorize",
        post_connector_authorize,
        methods=["POST"],
    ),
    Route(
        "/api/agents/{id}/connectors/{instance}/host-setup",
        post_connector_host_setup,
        methods=["POST"],
    ),
    Route(
        "/api/agents/{id}/connectors/{instance}/probe",
        post_connector_probe,
        methods=["POST"],
    ),
    Route(
        "/api/agents/{id}/connectors/{instance}/doctor",
        get_connector_doctor,
        methods=["GET"],
    ),
    Route(
        "/api/agents/{id}/connectors/{instance}/approve",
        post_connector_approve,
        methods=["POST"],
    ),
    Route(
        "/api/agents/{id}/connectors/{instance}",
        delete_agent_connector,
        methods=["DELETE"],
    ),
]

__all__ = [
    "delete_agent_connector",
    "get_agent_connectors",
    "get_catalog",
    "get_connector_auth",
    "get_connector_auth_status",
    "get_connector_doctor",
    "post_agent_connector",
    "post_connector_approve",
    "post_connector_authorize",
    "post_connector_host_setup",
    "post_connector_probe",
    "put_connector_auth",
    "routes",
]
