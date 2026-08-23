"""SPEC-064 T-021 — the connector routes: the web connects a deployment to a system.

The web re-derives nothing about how a connection is made (D-585, D-587). Every
verb here hands off to :mod:`arcagent.connections` — the one seam ``arc
connector`` and the TUI drive too. The ordering behind it (resolve, manifest,
host, verify, secrets, probe, persist) IS the security property: building an
attachment executes extension-declared code, so the signature gate has to close
first, and a credential written before a failed probe has to be rolled back. A
route that assembled its own sequence would be a second copy of that ordering to
keep in step, and the one that drifts is the one an attacker uses.

**Two scopes, and the URLs say which.** A *connection* belongs to the deployment:
one account, one credential, one bundle, connected once — so it lives under
``/api/connections`` with no agent in the path. A *grant* is the agent-scoped
thing, and it is the only thing that decides access: ``/api/connections/{name}/
grant`` adds agents, the same path with ``DELETE`` takes them away, and
``/api/agents/{id}/connectors`` answers the other direction — what this one agent
can reach. Deny by default falls out of that shape: an agent nobody has granted
appears in no grant list and attaches nothing, so no route here can widen access
except by naming an agent.

Two things the web adds, and only these:

* **The operator gate.** Every mutation is ``operator``-only and every body is
  capped, exactly as ``agent_detail/config_files`` does it. The probe verb is
  gated too — it opens a live outbound connection — and so are grant and revoke,
  which are access-control decisions.
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
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, cast

import arcagent
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from arcui.audit import emit_mutation_audit, operator_audit_sink
from arcui.routes.agent_detail._common import _agent_did, _agent_root
from arcui.routes.agent_detail.config_files import (
    BodyTooLargeError,
    _error,
    read_json_object,
)
from arcui.schemas import (
    AgentConnectorsResponse,
    ConnectionsResponse,
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

NOT_INSTALLED = arcagent.NOT_INSTALLED
AuditChain = arcagent.AuditChain
Authorization = arcagent.Authorization
CatalogEntry = arcagent.CatalogEntry
Connection = arcagent.Connection
Connections = arcagent.Connections
ConnectorPlan = arcagent.ConnectorPlan
ExtensionError = arcagent.ExtensionError
HostPrerequisiteDirector = arcagent.HostPrerequisiteDirector
HostVerdict = arcagent.HostVerdict
Tier = arcagent.Tier
ToolSpec = arcagent.ToolSpec
catalog = arcagent.catalog
resolve_roots = arcagent.resolve_roots

logger = logging.getLogger("arcui.routes.connectors")

#: Tier the fleet-wide catalog parses manifests at. The catalog answers "which
#: bundles exist on this machine", a question with no grantee and therefore no
#: tier; the tier verdict that matters is taken per connection by the seam, from
#: the agents it is about to be granted to. Reading the listing at the most
#: permissive tier means an operator sees a bundle and is told precisely why it is
#: refused, rather than seeing an empty page.
_CATALOG_TIER = Tier.PERSONAL


def _is_operator(request: Request) -> bool:
    return getattr(request.state, "role", None) == "operator"


def _connections(request: Request) -> Connections:
    """Bind this deployment's connector seam to the chain this process already holds.

    There is no agent to resolve: a connected account belongs to the deployment,
    and an agent enters only as a name in a grant.

    Raises:
        ExtensionError: The deployment's own config will not parse. Every caller
            already handles that refusal, so it is raised rather than swallowed —
            a route that answered "no connections" on an unreadable config would
            report an empty deployment as a healthy one.
    """
    backend = getattr(request.app.state, "arcstore_backend", None)

    async def state_opener() -> Any:
        return backend

    return Connections.for_deployment(
        audit=AuditChain.held(operator_audit_sink(request)),
        state_opener=state_opener if backend is not None else None,
        connector_control=_connector_control(request),
    )


class _InProcessConnectorControl:
    """Resolve dashboard-owned agents without teaching arcagent about arcui."""

    def __init__(self, request: Request) -> None:
        self._request = request

    async def reconcile(self, agent: str) -> arcagent.ConnectorReconcileResult | None:
        cache = getattr(self._request.app.state, "embedded_agent_cache", None)
        did = _agent_did(self._request, agent)
        live_agent = cache.get(did) if cache is not None and did is not None else None
        if live_agent is None:
            return None
        return cast(arcagent.ConnectorReconcileResult, await live_agent.reconcile_connectors())


def _connector_control(request: Request) -> arcagent.ConnectorControl:
    """Use an explicitly injected control in tests, else the embedded cache."""
    injected = getattr(request.app.state, "connector_control", None)
    return injected if injected is not None else _InProcessConnectorControl(request)


def _activation_payload(
    results: Sequence[arcagent.ConnectorReconcileResult],
) -> list[dict[str, Any]]:
    """Safe, operator-facing activation truth; tools/coordinates only."""
    return [
        {
            "agent": result.agent,
            "status": result.status,
            "revision": result.revision,
            "tools": list(result.tools),
            "detail": result.detail,
        }
        for result in results
    ]


def _not_found(exc: ExtensionError) -> bool:
    """A refusal about a connection this deployment has not made is a 404, not a 400."""
    return exc.code == NOT_INSTALLED


def _refused(exc: ExtensionError) -> JSONResponse:
    return _error(exc.message, 404 if _not_found(exc) else 400)


def _row(
    instance: str,
    connection: Connection,
    labels: Mapping[str, str] | None = None,
    knowledge: Mapping[str, tuple[str, str]] | None = None,
) -> ConnectorInstance:
    """One listing row, carrying the grant list that decides who may use it."""
    mode, reason = (knowledge or {}).get(connection.extension, ("", ""))
    return ConnectorInstance(
        instance=instance,
        extension=connection.extension,
        extension_display_name=(labels or {}).get(connection.extension, connection.extension),
        knowledge_mode=mode,
        knowledge_reason=reason,
        approval=connection.approval,
        agents=list(connection.agents),
    )


def _labels(connections: Connections) -> dict[str, str]:
    """Extension name to the name a person reads, read from the bundles themselves.

    A listing row names an extension the operator connected, and the only place
    that bundle's own spelling of itself lives is its manifest. A bundle that has
    since been removed from the search path simply has no entry, and ``_row``
    falls back to the coordinate rather than rendering a blank.
    """
    try:
        return {
            entry.name: entry.display_name or entry.name
            for entry in connections.catalog()
            if not entry.error
        }
    except ExtensionError:
        # A listing must not fail because a bundle directory is unreadable; the
        # coordinate is a correct, if plainer, answer.
        return {}


def _knowledge_metadata(connections: Connections) -> dict[str, tuple[str, str]]:
    """Return manifest-declared Knowledge mode and reason by extension coordinate."""
    try:
        return {
            entry.name: (entry.knowledge_mode, entry.knowledge_reason)
            for entry in connections.catalog()
            if not entry.error
        }
    except ExtensionError:
        return {}


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


def _submitted_agents(body: dict[str, Any]) -> list[str]:
    """The ``agents`` list, keeping only strings and dropping repeats.

    Names, never ids: a grant is matched against the agent's directory name when
    it starts, so anything else would be written, listed, and effective for no one.
    """
    raw = body.get("agents")
    if not isinstance(raw, list):
        return []
    ordered: dict[str, None] = {}
    for name in raw:
        if isinstance(name, str) and name:
            ordered.setdefault(name, None)
    return list(ordered)


def _missing_secrets(plan: ConnectorPlan, supplied: dict[str, str]) -> list[str]:
    """Declared credentials the operator did not fill in. Names only.

    An OAuth connector's refresh token is obtained by the authorize flow AFTER
    install, never typed here, so it is not a missing credential to refuse the
    install over — the operator supplies only the app key/secret.
    """
    managed = plan.manifest.oauth.refresh_token_secret if plan.manifest.oauth else None
    return [
        declared.name
        for declared in plan.secrets
        if declared.name != managed and not supplied.get(declared.name)
    ]


def _unknown_agents(request: Request, agents: Sequence[str]) -> list[str]:
    """Names this deployment has no agent for, when a roster can say.

    Not a security gate — a grant to a name nothing matches hands out nothing.
    It is the difference between a refusal an operator can act on and a grant
    that is written, shown in the listing, and silently effective for no one,
    which is the failure this whole surface exists to make visible.

    A server with no roster provider checks nothing and says so by returning
    empty: there is no third answer it could honestly give.
    """
    provider = getattr(request.app.state, "roster_provider", None)
    if provider is None:
        return []
    known = {_agent_name(entry) for entry in provider()}
    return [name for name in agents if name not in known]


def _agent_name(entry: Any) -> str:
    """The directory name a grant is matched against, from one roster row."""
    return Path(entry.workspace_path).name


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
    """One listing row: what connecting this bundle would give the agents granted it."""
    return ConnectorCatalogEntry(
        name=entry.name,
        display_name=entry.display_name or entry.name,
        version=entry.version,
        description=entry.description,
        knowledge_mode=entry.knowledge_mode,
        knowledge_reason=entry.knowledge_reason,
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
# The deployment's connections
# ---------------------------------------------------------------------------


async def get_connections(request: Request) -> JSONResponse:
    """GET /api/connections — every connected account, and who holds it.

    Readable by ``viewer``, and the one read that answers "who can reach my
    mail" for the whole fleet at once — the question no per-agent listing could
    answer without walking every agent and trusting the walk was complete.
    """
    try:
        connections = _connections(request)
        defined = connections.connections()
        labels = _labels(connections)
        knowledge = _knowledge_metadata(connections)
    except ExtensionError as exc:
        return _refused(exc)

    return JSONResponse(
        ConnectionsResponse(
            connections=[
                _row(name, cfg, labels, knowledge) for name, cfg in sorted(defined.items())
            ],
            extensions_roots=[str(root) for root in connections.world.extension_roots],
        ).model_dump(mode="json")
    )


async def get_agent_connectors(request: Request) -> JSONResponse:
    """GET /api/agents/{id}/connectors — what this one agent can reach.

    Readable by ``viewer``. The grant list read from the agent's side, which is
    exactly what the connector module will attach when that agent next starts —
    so an operator looking at an agent sees what it has, not what exists.
    """
    agent_dir = _agent_root(request, request.path_params["id"])
    if agent_dir is None:
        return _error("Agent not found", 404)

    try:
        connections = _connections(request)
        granted = connections.registry.granted_to(agent_dir.name)
        labels = _labels(connections)
        knowledge = _knowledge_metadata(connections)
    except ExtensionError as exc:
        return _refused(exc)

    return JSONResponse(
        AgentConnectorsResponse(
            instances=[
                _row(name, cfg, labels, knowledge) for name, cfg in sorted(granted.items())
            ],
            extensions_roots=[str(root) for root in connections.world.extension_roots],
        ).model_dump(mode="json")
    )


async def post_connection(request: Request) -> JSONResponse:
    """POST /api/connections — connect one account and hand it to its agents.

    Operator only. Refusals happen before anything is written: a name already in
    use is a 409, a credential the manifest declares and the operator left blank
    is a 422, and a missing host prerequisite is a 400 carrying the instruction
    the operator must run — arcui directs, it never installs (REQ-262).

    ``agents`` is collected here rather than in a second step because that is
    what makes one pass enough: a connection granted to nobody is proven to work
    and reaches no one, and an operator who is not asked will not discover that
    until they ask their agent to use it.
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

    agents = _submitted_agents(body)
    unknown = _unknown_agents(request, agents)
    if unknown:
        return _error(f"no agent named {', '.join(unknown)} on this deployment", 400)

    try:
        connections = _connections(request)
        if instance in connections.connections():
            return _error(f"connection {instance!r} already exists", 409)
        # Planned FOR its grantees: their tier decides how the manifest is parsed
        # and where the credential may be held, and ``install`` refuses a plan
        # taken at a laxer tier than the agents about to hold it.
        plan = connections.plan(extension, instance, agents=agents)
    except ExtensionError as exc:
        return _error(exc.message, 400)

    if plan.unsatisfied_host:
        return _host_blocked(plan)

    supplied = _submitted_secrets(body)
    missing = _missing_secrets(plan, supplied)
    if missing:
        return _error(f"missing required credential(s): {', '.join(missing)}", 422)

    return await _run_install(request, connections, plan, supplied, agents)


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
    agents: Sequence[str],
) -> JSONResponse:
    """Drive the shared install path and shape its report for the browser."""
    target = f"connector:{plan.instance}"
    try:
        report = await connections.install(plan, secrets, agents=agents)
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
            agents=list(agents),
        ).model_dump(mode="json")
    )


# ---------------------------------------------------------------------------
# Grants — the only thing that decides access
# ---------------------------------------------------------------------------


async def post_connection_grant(request: Request) -> JSONResponse:
    """POST /api/connections/{instance}/grant — let these agents use this account.

    Operator only: this is an access-control decision, and the one that matters.
    Nothing is copied — the credential stays at its single coordinate and every
    grantee reads it through the same store, so a rotation is one write and a
    revoke cannot leave a copy behind.

    Takes effect at the granted agent's next start, which the answer says,
    because an operator who grants and then watches an agent fail to use it has
    no way to tell a restart from a broken grant.
    """
    return await _change_grant(request, granting=True)


async def delete_connection_grant(request: Request) -> JSONResponse:
    """DELETE /api/connections/{instance}/grant — take this account back.

    Operator only. Revoking from an agent that never held it is not an error: an
    operator making sure nobody has something must not be stopped by a name that
    already does not.
    """
    return await _change_grant(request, granting=False)


async def _change_grant(request: Request, *, granting: bool) -> JSONResponse:
    """Grant or revoke, once — the two verbs differ only in direction.

    Both answer with the connection's whole grant list afterwards rather than
    with what changed, so a surface renders who holds it now instead of
    reconstructing that from a request it sent and an answer it half-believes.
    """
    if not _is_operator(request):
        return _error("Operator role required", 403)

    try:
        body = await read_json_object(request)
    except BodyTooLargeError:
        return _error("Request body too large", 413)
    if body is None:
        return _error("Body must be a JSON object", 400)

    agents = _submitted_agents(body)
    if not agents:
        return _error("'agents' must be a non-empty list of agent names", 400)
    if granting and (unknown := _unknown_agents(request, agents)):
        return _error(f"no agent named {', '.join(unknown)} on this deployment", 400)

    instance = request.path_params["instance"]
    operation = "connector.grant" if granting else "connector.revoke"
    try:
        connections = _connections(request)
        mutation = (
            await connections.grant_and_reconcile(instance, agents)
            if granting
            else await connections.revoke_and_reconcile(instance, agents)
        )
    except ExtensionError as exc:
        emit_mutation_audit(
            request,
            target=f"connector:{instance}",
            operation=operation,
            outcome="denied",
            detail=exc.code,
        )
        return _refused(exc)

    emit_mutation_audit(
        request,
        target=f"connector:{instance}",
        operation=operation,
        outcome="applied",
        detail=",".join(agents),
    )
    connection = mutation.connection
    if connection is None:
        return _error("connector grant did not produce a connection", 500)
    current = _connections(request)
    body = _row(
        instance,
        connection,
        _labels(current),
        _knowledge_metadata(current),
    ).model_dump(mode="json")
    body["activations"] = _activation_payload(mutation.activations)
    return JSONResponse(body)


# ---------------------------------------------------------------------------
# One connection
# ---------------------------------------------------------------------------


async def get_connector_auth(request: Request) -> JSONResponse:
    """GET /api/connections/{instance}/auth — how this connection is authorised.

    Readable by ``viewer``: it names credential FIELDS and host commands, never a
    value. The panel needs it because half the shipped bundles declare no
    ``[[secrets]]`` at all — their binary holds its own token — and a form drawn
    from an empty credential list leaves a non-technical operator with nothing to
    fill in and no idea that ``gh auth login`` is the actual next step.
    """
    try:
        auth = await _connections(request).authorization(request.path_params["instance"])
    except ExtensionError as exc:
        return _refused(exc)

    return JSONResponse(
        ConnectorAuthorizationResponse(
            instance=auth.instance,
            extension=auth.extension,
            extension_display_name=_labels(_connections(request)).get(
                auth.extension, auth.extension
            ),
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
            oauth=auth.oauth,
            authorize_url=auth.authorize_url,
        ).model_dump(mode="json")
    )


async def put_connector_auth(request: Request) -> JSONResponse:
    """PUT /api/connections/{instance}/auth — re-supply credentials.

    Operator only. Answers with the field NAMES that were rewritten; the values
    go to the secret store and nowhere else. One write serves every agent granted
    this connection, because there is only ever one copy of the credential.
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
    try:
        connections = _connections(request)
        plan = connections.plan_for(instance)
        updated = await connections.reauth(plan, _submitted_secrets(body))
    except ExtensionError as exc:
        return _refused(exc)

    emit_mutation_audit(
        request,
        target=f"connector:{instance}",
        operation="connector.auth",
        outcome="applied",
        detail=",".join(updated),
    )
    return JSONResponse(
        ConnectorAuthResponse(instance=instance, updated=list(updated)).model_dump(mode="json")
    )


async def post_connector_probe(request: Request) -> JSONResponse:
    """POST /api/connections/{instance}/probe — is it live right now?

    Operator only: probing opens an outbound connection to the external system.
    """
    if not _is_operator(request):
        return _error("Operator role required", 403)

    instance = request.path_params["instance"]
    try:
        result = await _connections(request).probe(instance)
    except ExtensionError as exc:
        return _refused(exc)
    except Exception:  # reason: an unbuildable attachment must not 500 with detail
        logger.exception("connectors: probe failed for %s", instance)
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
    """GET /api/connections/{instance}/auth-status — is its binary signed in?

    Readable by ``viewer``: it probes and names commands, never a credential.
    """
    try:
        return _auth_status(
            await _connections(request).authorization(request.path_params["instance"])
        )
    except ExtensionError as exc:
        return _refused(exc)


async def post_connector_authorize(request: Request) -> JSONResponse:
    """POST /api/connections/{instance}/authorize — sign the binary in.

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
    submitted = body.get("token")
    try:
        auth = await _connections(request).authorize(
            instance, token=submitted if isinstance(submitted, str) else ""
        )
    except ExtensionError as exc:
        return _refused(exc)

    emit_mutation_audit(
        request,
        target=f"connector:{instance}",
        operation="connector.authorize",
        outcome="applied" if auth.working else "denied",
        detail=auth.extension,
    )
    return _auth_status(auth)


async def post_connector_oauth(request: Request) -> JSONResponse:
    """POST /api/connections/{instance}/oauth — finish a native OAuth connection.

    Operator only. ``code`` is the one-time authorization code the provider showed;
    Arc exchanges it for a DURABLE refresh token, stores it, and probes. The code is
    a short-lived credential: it goes to the provider's token endpoint and to
    nothing else, and appears in no response, log line, or audit event this route
    produces (LLM02, LLM07). A dead code refuses with the provider's reason so the
    operator authorizes again rather than staring at a silent failure.
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
    code = body.get("code")
    if not isinstance(code, str) or not code.strip():
        return _error("A one-time authorization code is required", 400)

    try:
        auth = await _connections(request).complete_oauth(instance, code=code)
    except ExtensionError as exc:
        return _refused(exc)

    emit_mutation_audit(
        request,
        target=f"connector:{instance}",
        operation="connector.oauth",
        outcome="applied" if auth.working else "denied",
        detail=auth.extension,
    )
    return _auth_status(auth)


async def post_connector_host_setup(request: Request) -> JSONResponse:
    """POST /api/connections/{extension}/host-setup — install what it needs.

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
    try:
        report = await _connections(request).setup_host(extension)
    except ExtensionError as exc:
        return _error(exc.message, 400)

    emit_mutation_audit(
        request,
        target=f"connector:{extension}",
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
    """GET /api/connections/{instance}/doctor — everything that is wrong.

    Readable by ``viewer``, and the same rows ``arc connector doctor`` prints:
    unmet prerequisites, whether each declared credential is present (never its
    value), and whether the connection answers.
    """
    try:
        checks = await _connections(request).doctor(request.path_params["instance"])
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
    """POST /api/connections/{instance}/approve — pin the tool contract.

    Operator only. Records the contract the connection serves RIGHT NOW as
    approved, which is the rug-pull defence (REQ-291): a tool whose shape changes
    afterwards is suspended until an operator approves it again.
    """
    if not _is_operator(request):
        return _error("Operator role required", 403)

    instance = request.path_params["instance"]
    try:
        approved = await _connections(request).approve(instance)
    except ExtensionError as exc:
        return _refused(exc)

    emit_mutation_audit(
        request,
        target=f"connector:{instance}",
        operation="connector.approve",
        outcome="applied",
        detail=",".join(approved),
    )
    return JSONResponse(
        ConnectorApproveResponse(instance=instance, approved=list(approved)).model_dump(
            mode="json"
        )
    )


async def delete_connection(request: Request) -> JSONResponse:
    """DELETE /api/connections/{instance} — disconnect one account entirely.

    Operator only, and it takes the credential, the definition and every grant on
    it together: a connection removed for one agent and left standing for another
    would be a credential nobody thinks is live.

    Removing something that was never connected is reported, not raised: an
    operator cleaning up after a failed install must not be blocked by a step
    that already has nothing to do.
    """
    if not _is_operator(request):
        return _error("Operator role required", 403)

    instance = request.path_params["instance"]
    try:
        mutation = await _connections(request).remove_and_reconcile(instance)
    except ExtensionError as exc:
        return _error(exc.message, 400)

    removal = mutation.removal
    if removal is None:
        return _error("connector removal did not produce a report", 500)

    emit_mutation_audit(
        request,
        target=f"connector:{instance}",
        operation="connector.remove",
        outcome="applied" if removal.removed_config else "denied",
    )
    body = ConnectorRemoveResponse(
        instance=removal.instance,
        removed_secrets=list(removal.removed_secrets),
        removed_config=removal.removed_config,
        removed_state=removal.removed_state,
    ).model_dump(mode="json")
    body["activations"] = _activation_payload(mutation.activations)
    return JSONResponse(body)


routes = [
    Route("/api/connectors/catalog", get_catalog, methods=["GET"]),
    Route("/api/agents/{id}/connectors", get_agent_connectors, methods=["GET"]),
    Route("/api/connections", get_connections, methods=["GET"]),
    Route("/api/connections", post_connection, methods=["POST"]),
    Route("/api/connections/{instance}/grant", post_connection_grant, methods=["POST"]),
    Route("/api/connections/{instance}/grant", delete_connection_grant, methods=["DELETE"]),
    Route("/api/connections/{instance}/auth", get_connector_auth, methods=["GET"]),
    Route("/api/connections/{instance}/auth", put_connector_auth, methods=["PUT"]),
    Route("/api/connections/{instance}/auth-status", get_connector_auth_status, methods=["GET"]),
    Route("/api/connections/{instance}/authorize", post_connector_authorize, methods=["POST"]),
    Route("/api/connections/{instance}/oauth", post_connector_oauth, methods=["POST"]),
    Route("/api/connections/{instance}/host-setup", post_connector_host_setup, methods=["POST"]),
    Route("/api/connections/{instance}/probe", post_connector_probe, methods=["POST"]),
    Route("/api/connections/{instance}/doctor", get_connector_doctor, methods=["GET"]),
    Route("/api/connections/{instance}/approve", post_connector_approve, methods=["POST"]),
    Route("/api/connections/{instance}", delete_connection, methods=["DELETE"]),
]

__all__ = [
    "delete_connection",
    "delete_connection_grant",
    "get_agent_connectors",
    "get_catalog",
    "get_connections",
    "get_connector_auth",
    "get_connector_auth_status",
    "get_connector_doctor",
    "post_connection",
    "post_connection_grant",
    "post_connector_approve",
    "post_connector_authorize",
    "post_connector_host_setup",
    "post_connector_oauth",
    "post_connector_probe",
    "put_connector_auth",
    "routes",
]
