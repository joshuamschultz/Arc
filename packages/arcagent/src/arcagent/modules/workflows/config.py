"""Configuration for the workflows module (SPEC-061 COMP-012).

Owned by the workflows module — not part of core config. Loaded from
``[modules.workflows.config]`` in arcagent.toml.
"""

from __future__ import annotations

from arcagent.core.module_config import ModuleConfig


class WorkflowsConfig(ModuleConfig):
    """Workflows module configuration.

    Inherits ``extra="forbid"`` from ModuleConfig for typo detection.
    """

    # Config-level enable mirrors the module-config convention (tasks et al.);
    # the load gate is ModuleEntry.enabled in the [modules.workflows] table.
    enabled: bool = False
    # Definition bundle root, relative to the DEPLOYMENT config dir
    # (``$ARC_CONFIG_DIR``, default ``~/.arc``) — the same root the operator
    # signs into and the fleet runner dispatches from. An absolute path is used
    # as given.
    #
    # Not the agent's workspace: a workflow is not agent state. It is signed by
    # the operator and executed by the fleet's single runner, which cannot reach
    # into five agents' private workspaces — an agent-authored definition parked
    # in its own workspace is invisible to the runner, the CLI, and the
    # dashboard, and can never run (ADR-029 governs an agent's OWN state, and a
    # workflow is a deployment artifact).
    workflows_dir: str = "workflows"
    # Forwarded to ``arcstore.config.resolve_data_dir`` for the shared run/task
    # plane — empty defers to that function's env > default precedence so this
    # module, the tasks module, and arcui always agree on one SQLite file.
    data_dir: str = ""
    # --- Quotas (LLM10), checked BEFORE any validation work ----------------
    # A whole-graph validation over a 200-node definition is real CPU; an agent
    # that can author unboundedly can spend it unboundedly. Both ceilings are
    # therefore refusals at the tool boundary, ahead of the control plane.
    max_workflows: int = 50
    max_nodes: int = 200
    # Ceiling on inline free text (description, node instruction summaries) an
    # agent may hand a builder tool. Prompt files are file-referenced and signed
    # with the bundle; inline text is the only unsigned instruction surface.
    max_inline_text_length: int = 2000
    # Bounded self-repair: after this many rejected attempts the agent must ask
    # the human to clarify rather than oscillate (rounds 1-2 capture 76-95% of
    # achievable repair; models regress beyond ~3).
    max_repair_attempts: int = 3
    # Ceiling on one companion file (a prompt or a JSON schema) an agent may
    # write with a definition. Prompts are the signed instruction surface, so
    # they are bounded like any other agent-authored artifact.
    max_file_bytes: int = 32_768
