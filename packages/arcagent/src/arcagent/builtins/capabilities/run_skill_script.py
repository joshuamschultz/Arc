"""Built-in ``run_skill_script`` tool (J4 B3).

The only way the model executes a script bundled with a skill. Every run:

1. resolves the skill by NAME to an offered, loader-verified skill;
2. accepts only ``scripts/**.py`` paths (no traversal, no other file);
3. verifies the WHOLE bundle against the operator signature (revision manifest
   for an anchored skill) and copies only the verified bytes into a private
   folder — a script swapped after signing, or re-signed with the agent key,
   never runs, and a swap after this check cannot reach the copy (TOCTOU);
4. runs it through :class:`SkillScriptRunner` in the tier's isolation backend
   (federal VM fail-closed, enterprise container) with the agent's
   ``caller_did``, which emits ``code_exec.backend.selected``;
5. audits ``skill.script.run`` with the outcome and exit code.

The core tool registry wraps the call in the policy pipeline like every tool.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import tempfile
from pathlib import Path, PurePosixPath

import arcrun

from arcagent.builtins.capabilities import _runtime
from arcagent.builtins.capabilities._skill_access import offered_skill, record, refuse
from arcagent.capabilities.capability_loader import RootTrust, root_trust
from arcagent.capabilities.skill_files import SkillFileError
from arcagent.capabilities.skill_script_runner import SkillScriptError, SkillScriptRunner
from arcagent.tools._decorator import tool

_ACTION = "skill.script.run"
_MAX_OUTPUT_CHARS = 30_000
_MAX_TIMEOUT_SECONDS = 600


@tool(
    name="run_skill_script",
    description=(
        "Run a Python script bundled with a skill (scripts/*.py) in the tier's sandbox, "
        "after verifying it against the operator's signature. Returns exit_code, stdout, "
        "stderr and backend as JSON."
    ),
    classification="state_modifying",
    capability_tags=["subprocess"],
    when_to_use="When a skill's instructions tell you to run one of its scripts.",
    version="1.0.0",
    examples=('run_skill_script(skill="pdf", path="scripts/extract.py", args=["in.pdf"])',),
)
async def run_skill_script(
    skill: str, path: str, args: list[str] | None = None, timeout: int = 120
) -> str:
    """Verify, isolate, run and audit one skill script; return its typed result."""
    entry = offered_skill(skill, action=_ACTION, path=path)
    if root_trust(entry.scan_root) is RootTrust.TRUSTED:
        raise refuse(
            _ACTION, skill, path, "builtin_skill", "built-in skill scripts are not runnable here"
        )
    _require_script_path(skill, path)
    if not 0 < timeout <= _MAX_TIMEOUT_SECONDS:
        raise refuse(
            _ACTION, skill, path, "timeout", f"timeout must be 1..{_MAX_TIMEOUT_SECONDS}s"
        )
    files = _runtime.skill_files()
    scratch = Path(tempfile.mkdtemp(prefix="arc-skill-run-"))
    try:
        try:
            copy = await asyncio.to_thread(files.materialize, entry, scratch)
        except (SkillFileError, OSError) as exc:
            raise refuse(_ACTION, skill, path, "integrity", str(exc)) from exc
        runner = SkillScriptRunner(
            capabilities_root=scratch,
            tier=_runtime.tier(),
            trusted_public_keys=files.keys_for(entry.scan_root),
            relax=_runtime.isolation_relax(),
            caller_did=_runtime.caller_did(),
            audit_sink=_runtime.arcrun_audit_sink(),
            timeout=float(timeout),
        )
        try:
            result = await runner.run(copy.name, path, args=list(args or []))
        except (SkillScriptError, arcrun.ExecutionIsolationError) as exc:
            raise refuse(_ACTION, skill, path, type(exc).__name__, str(exc)) from exc
    finally:
        await asyncio.to_thread(shutil.rmtree, scratch, True)
    record(_ACTION, skill, path, "allow", exit_code=result.exit_code, backend=result.backend)
    return json.dumps(
        {
            "exit_code": result.exit_code,
            "stdout": result.stdout[:_MAX_OUTPUT_CHARS],
            "stderr": result.stderr[:_MAX_OUTPUT_CHARS],
            "backend": result.backend,
        }
    )


def _require_script_path(skill: str, path: str) -> None:
    """Only ``scripts/**.py``, as a clean relative path."""
    parts = PurePosixPath(path).parts
    if (
        "\\" in path
        or path.startswith("/")
        or len(parts) < 2
        or parts[0] != "scripts"
        or any(part in {"", ".", ".."} for part in parts)
        or not path.endswith(".py")
    ):
        raise refuse(_ACTION, skill, path, "not_a_script", f"{path!r} is not a scripts/*.py file")
