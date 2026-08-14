"""COMP-006 — one throwaway ArcAgent per question.

Replicates what ``arc agent create`` does locally — ``render_agent_config`` →
scaffold → ``load_config`` → ``ArcAgent(..., config_path=<absolute>)`` →
``startup()`` — and nothing it does remotely. The command itself is never
called: it mints a keypair into ``~/.arcagent/keys``, signs the scaffolded
capabilities under that identity and auto-registers the agent over NATS, none
of which a per-question throwaway should leave behind 500 times.
``[identity] did`` stays empty so the key mints lazily at startup instead.

The emitted TOML is where this component's requirements actually live
(REQ-183, REQ-185, REQ-186, REQ-198, REQ-199); ``test_eval_agent_config.py``
reads every one of them back, because a setting nothing reads is exactly how a
benchmark ends up measuring something other than what it claims to.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING

from arccli.commands.agent import _scaffold_workspace
from arccli.commands.agent._common import (
    _DEFAULT_ARCLLM_CONFIG,
    _DEFAULT_ARCRUN_CONFIG,
    render_agent_config,
)
from arcstore.config import ENV_DATA_DIR

from evaluations.ingest.limits import MAX_EVENT_CHARS, RECALL_BUDGET, RECALL_TOP_K
from evaluations.ingest.models import AGENT_MODEL, DISTILL_MODEL, DISTILL_PROVIDER

if TYPE_CHECKING:
    from arcagent.core.agent import ArcAgent

ARCSTORE_DIR_NAME = ".arcstore"
"""Per-run operational store, inside the run dir so teardown takes it too."""


class WorkspaceEscapeError(RuntimeError):
    """An agent's workspace resolved outside its own run directory."""


def assert_workspace_contained(*, workspace: Path, run_dir: Path) -> Path:
    """Return ``workspace`` normalized, or raise if it escapes ``run_dir``.

    ``ArcAgent`` resolves a relative ``[agent] workspace`` against the process
    CWD unless it is handed a ``config_path`` — and the workspace, the trace
    store, the audit chain and the capability scan root all hang off that one
    decision. Missing the ``config_path`` therefore writes an agent's brain into
    the Arc repository; this is the check that makes it loud.

    Both sides are resolved, so a symlinked run dir is not a false escape and a
    symlink pointing out of one is not a false pass. ``resolve()`` is
    non-strict, so the guard can run before the scaffold exists — it must, since
    its whole job is to refuse before anything is written.
    """
    resolved = workspace.resolve()
    root = run_dir.resolve()
    if not resolved.is_relative_to(root):
        raise WorkspaceEscapeError(
            f"workspace {workspace} resolves to {resolved}, outside run dir {root}"
        )
    return resolved


# ---------------------------------------------------------------------------
# The eval agent's config surface
# ---------------------------------------------------------------------------

_EVAL_ARCAGENT_OVERRIDES: Mapping[str, Mapping[str, str]] = {
    "security": {
        # Empty routes the WORM policy chain into the SHARED arcstore worm dir,
        # where 500 throwaway agents contend on one exclusive flock and leave
        # their audit behind after their run dir is deleted.
        "policy_audit_log": '"audit/policy-chain.jsonl"',
    },
    "modules.memory.config": {
        # Stated rather than inherited: these two are already the template's
        # defaults, and a benchmark that silently measured a NullBrain or a
        # no-op distiller because a default moved would look like a memory
        # result rather than a dead seam.
        "brain": '"arcmemory"',
        "distill_provider": f'"{DISTILL_PROVIDER}"',
        "distill_model": f'"{DISTILL_MODEL}"',
        # The harness fires every consolidation pass itself, once per session
        # boundary (COMP-008), because the module's poll interval is a constant
        # and per-session cadence is unreachable by config. The outer trigger
        # must therefore never be the thing that declines.
        "consolidate_event_threshold": "1",
        "consolidate_idle_seconds": "0.0",
        "consolidate_interval_seconds": "0.0",
        # At the default budget of 1024 exactly one 2000-char recall survives
        # enforce_budget whatever top_k says, so the run would measure the
        # budget rather than the memory.
        "top_k": str(RECALL_TOP_K),
        "budget": str(RECALL_BUDGET),
    },
    "modules.memory.config.dynamics": {
        # arcmemory's OWN cadence gate, read from a persisted last-run stamp.
        # Left at its 60-minute default, every harness-driven pass after the
        # first returns an empty result and the five settings above are theatre.
        "consolidate_interval_minutes": "0.0",
        # The sanitize cap, and the ONLY key path that reaches it: arcagent's
        # MemoryConfig forbids extra keys, so `max_event_chars` one table up is
        # a validation error, while `dynamics` is the opaque dict arcmemory's
        # build_brain re-validates as its own MemoryConfig. At arcmemory's 2000
        # default a third of this corpus's turns overflow and their questions
        # void, so this is the number the corpus is ingestible under at all.
        "max_event_chars": str(MAX_EVENT_CHARS),
    },
    # NATS is deliberately not integrated (SDD External Integrations): there is
    # no broker for a local benchmark, and a per-question throwaway must not
    # announce itself to a fleet.
    "modules.messaging": {"enabled": "false"},
    "modules.tasks": {"enabled": "false"},
}

_EVAL_ARCLLM_TELEMETRY = """
# Raw request/response capture persists every haystack chunk verbatim as
# plaintext JSONL inside the repo tree — several GB across a full run.
[llm.modules.telemetry]
store_raw_bodies = false
"""


ARCRUN_EVAL_CONFIG = _DEFAULT_ARCRUN_CONFIG
"""The eval agent's ``arcrun.toml`` — the scaffold's loop controls, unchanged."""


def _apply_toml_overrides(text: str, overrides: Mapping[str, Mapping[str, str]]) -> str:
    """Rewrite ``key = value`` lines in a rendered config, table by table.

    TOML refuses a re-declared table, so eval settings cannot simply be appended
    to ``render_agent_config``'s output — each has to land on the line already
    carrying its default. A key the template ships commented out (the whole
    dynamics block) is inserted under its header instead. Trailing comments are
    kept: the emitted file is what an operator reads when a number looks wrong.
    """
    lines = text.splitlines()
    pending = {(t, k): v for t, kv in overrides.items() for k, v in kv.items()}
    table = ""
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            table = stripped[1:-1]
            continue
        key, sep, rest = stripped.partition("=")
        if not sep:
            continue
        value = pending.pop((table, key.strip()), None)
        if value is not None:
            _, hash_sign, comment = rest.partition("#")
            trailer = f"  #{comment}" if hash_sign else ""
            lines[i] = f"{key.strip()} = {value}{trailer}"
    for (header, key), value in reversed(list(pending.items())):
        lines.insert(lines.index(f"[{header}]") + 1, f"{key} = {value}")
    return "\n".join(lines) + "\n"


ARCLLM_EVAL_CONFIG = _apply_toml_overrides(
    _DEFAULT_ARCLLM_CONFIG + _EVAL_ARCLLM_TELEMETRY,
    {"llm": {"model": f'"{AGENT_MODEL}"'}},
)
"""The eval agent's ``arcllm.toml``: the scaffold's, with raw capture off."""


def eval_agent_name(question_id: str) -> str:
    """The unique ``[agent] name`` for one question's throwaway agent."""
    return f"lme-{question_id}"


def render_eval_agent_config(*, question_id: str, run_dir: Path, tier: str = "personal") -> str:
    """Render one question's ``arcagent.toml`` text — no filesystem side effects.

    ``run_dir`` must be absolute: every path this config carries is resolved
    either against it or against the config file next to it, and a relative run
    dir reintroduces the CWD dependence the whole component exists to remove.
    """
    if not run_dir.is_absolute():
        raise ValueError(f"run_dir must be absolute, got {run_dir}")
    overrides = {
        **_EVAL_ARCAGENT_OVERRIDES,
        "arcstore": {"data_dir": f'"{run_dir / ARCSTORE_DIR_NAME}"'},
    }
    base = render_agent_config(name=eval_agent_name(question_id), tier=tier)
    return _apply_toml_overrides(base, overrides)


def write_eval_agent_config(*, question_id: str, run_dir: Path, tier: str = "personal") -> Path:
    """Emit the three sibling TOMLs plus the workspace; return the config path."""
    text = render_eval_agent_config(question_id=question_id, run_dir=run_dir, tier=tier)
    agent_dir = run_dir / "agent"
    agent_dir.mkdir(parents=True, exist_ok=True)

    config_path = agent_dir / "arcagent.toml"
    config_path.write_text(text, encoding="utf-8")
    (agent_dir / "arcllm.toml").write_text(ARCLLM_EVAL_CONFIG, encoding="utf-8")
    (agent_dir / "arcrun.toml").write_text(ARCRUN_EVAL_CONFIG, encoding="utf-8")

    _scaffold_workspace(agent_dir, eval_agent_name(question_id))
    install_enabled_modules(config_path)
    return config_path


def install_enabled_modules(config_path: Path) -> list[str]:
    """Materialize every module this agent's config enables; return their names.

    Modules ship as signed bundles installed at the *deployment* module root, not
    inside the wheel, so an agent config that merely enables one gets nothing on a
    home where ``arc install`` has never run — and this harness deliberately
    builds each agent in a throwaway home. The agent then starts fine and simply
    has no memory, which for a memory benchmark means measuring the absence of
    the thing under test.

    Uses the installer ``arc install`` itself calls, so the eval agent is
    provisioned by the same code path as a real deployment rather than a second
    one that could drift from it.
    """
    import arcagent
    from arccli.commands.module import install_module_for_agent

    agent_dir = config_path.parent
    config = arcagent.load_config(config_path)
    installed: list[str] = []
    for name, entry in config.modules.items():
        if not entry.enabled:
            continue
        install_module_for_agent(name, agent_root=agent_dir, agent_id=config.agent.name)
        installed.append(name)
    return installed


def pin_arcstore_data_dir(run_dir: Path) -> Path:
    """Point this process's arcstore at ``run_dir`` and return the directory.

    ``ARCSTORE_DATA_DIR`` outranks the ``[arcstore] data_dir`` written above
    (``arcstore.config.resolve_data_dir``), so an operator shell that exports it
    would pool all 500 runs — and the developer's own ``~/.arc/state/store`` — into one
    database. Setting it here is what makes the emitted value hold.
    """
    data_dir = (run_dir / ARCSTORE_DIR_NAME).resolve()
    data_dir.mkdir(parents=True, exist_ok=True)
    os.environ[ENV_DATA_DIR] = str(data_dir)
    return data_dir


async def build_eval_agent(*, question_id: str, run_dir: Path, tier: str = "personal") -> ArcAgent:
    """Build and start one question's throwaway agent inside ``run_dir``."""
    from arcagent.core.agent import ArcAgent
    from arcagent.core.config import load_config

    pin_arcstore_data_dir(run_dir)
    config_path = write_eval_agent_config(question_id=question_id, run_dir=run_dir, tier=tier)
    agent = ArcAgent(load_config(config_path), config_path=config_path)
    assert_workspace_contained(workspace=agent._workspace, run_dir=run_dir)
    await agent.startup()
    return agent


__all__ = [
    "ARCLLM_EVAL_CONFIG",
    "ARCRUN_EVAL_CONFIG",
    "ARCSTORE_DIR_NAME",
    "WorkspaceEscapeError",
    "assert_workspace_contained",
    "build_eval_agent",
    "eval_agent_name",
    "install_enabled_modules",
    "pin_arcstore_data_dir",
    "render_eval_agent_config",
    "write_eval_agent_config",
]
