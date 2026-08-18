"""The gateway's concrete ``WorkflowProvider`` — workflows as slash commands.

``specs()`` reads the deployment's workflow bundles off disk (no runner needed),
so a workflow shows up in a surface's ``/`` menu whether or not the fleet runner
is up. ``run()`` fires one deterministically through the SAME
``WorkflowControlPlane`` the CLI and dashboard use, reusing the live runner's
store, run plane, and tier so a slash-command run and a ``arc workflow run`` land
on identical enforcement.

Nothing here may raise into the gateway: a broken store, an unavailable runner,
or a refused run all resolve to a plain reply line. A slash command that
exceptions would take a chat turn down with it.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, cast

from arcgateway.commands.base import CommandSpec

if TYPE_CHECKING:
    from arcteam.workflow.control_plane import ControlPlaneResult
    from arcteam.workflow.store import DefinitionStore

_logger = logging.getLogger("arcgateway.commands.workflow_provider")


class GatewayWorkflowProvider:
    """Turns the deployment's on-disk workflows into runnable ``/name`` commands."""

    def specs(self) -> list[CommandSpec]:
        """The runnable workflows as ``(name, description)`` specs.

        Resilient by construction: a missing store directory, or a bundle that
        will not load, yields what can be read rather than an exception — an
        empty list is a deployment with no workflows, never a crash.
        """
        store = self._read_store()
        if store is None:
            return []
        try:
            ids = store.list_ids(include_archived=False)
        except Exception:  # reason: a bad store must not break the command menu
            _logger.debug("workflow specs: list_ids failed", exc_info=True)
            return []
        return [CommandSpec(name=wid, description=self._describe(store, wid)) for wid in ids]

    async def run(self, workflow_id: str, *, actor_did: str, args: str) -> str:
        """Start ``workflow_id``; return the reply line (run id, or a refusal).

        Never raises: every failure — no runner, an unknown or archived
        workflow, a refused start — comes back as one line for the user.
        """
        try:
            return await self._run(workflow_id, actor_did=actor_did, args=args)
        except Exception:  # reason: a slash command must never take down the turn
            _logger.warning("workflow run %r failed unexpectedly", workflow_id, exc_info=True)
            return f"Couldn't start {workflow_id}: an unexpected error occurred."

    # -- internals ----------------------------------------------------------

    async def _run(self, workflow_id: str, *, actor_did: str, args: str) -> str:
        from arcgateway.workflow_runner_host import RunnerHost

        host = RunnerHost.active()
        if host is None:
            return "Workflows aren't running on this deployment yet."

        from arcteam.workflow import parse_definition, validate_definition
        from arcteam.workflow.control_plane import WorkflowControlPlane
        from arcteam.workflow.runner import WorkflowRunner
        from arctrust.paths import workflows_dir

        runner = cast(WorkflowRunner, host.runner)
        root = workflows_dir()

        def _parse(document: Any) -> Any:
            return parse_definition(dict(document))

        def _validate(definition: Any, *, pending_files: frozenset[str] = frozenset()) -> Any:
            return validate_definition(
                definition, bundle_root=root / definition.id, pending_files=pending_files
            )

        # Compose onto the runner's own store/run-plane/tier — never a second
        # set — so a slash-command run enforces exactly what the engine does.
        plane = WorkflowControlPlane(
            definitions=runner.definitions,
            parse=_parse,
            validate=_validate,
            runner=runner,
            runs=runner.runs,
            tier=runner.tier,
        )
        run_input = {"text": args} if args else {}
        result = await plane.run(workflow_id, input=run_input, actor_did=actor_did)
        if result.ok and result.run is not None:
            return f"Started {workflow_id} (run {result.run.run_id})."
        return self._refusal(workflow_id, result)

    @staticmethod
    def _refusal(workflow_id: str, result: ControlPlaneResult) -> str:
        """Render the control plane's refusal as one repairable line."""
        for issue in result.errors:
            return f"Couldn't start {workflow_id}: {issue.error}"
        return f"Couldn't start {workflow_id}."

    def _read_store(self) -> DefinitionStore | None:
        """The read-side definition store rooted at the deployment's bundles.

        Returns ``None`` — never raises — when arcteam is absent or the store
        cannot be constructed, so ``specs()`` degrades to "no workflows".
        """
        try:
            from arcteam.workflow import DefinitionStore
            from arctrust.paths import workflows_dir
        except ImportError:
            return None
        try:
            return DefinitionStore(workflows_dir())
        except Exception:  # reason: an unresolvable store means "no workflows", not a crash
            _logger.debug("workflow specs: store unavailable", exc_info=True)
            return None

    @staticmethod
    def _describe(store: DefinitionStore, workflow_id: str) -> str:
        """A workflow's own description, or a generic fallback line."""
        try:
            description = store.load(workflow_id).definition.description
        except Exception:  # reason: an unloadable bundle still deserves a menu row
            description = ""
        return description or f"Run the {workflow_id} workflow"


__all__ = ["GatewayWorkflowProvider"]
