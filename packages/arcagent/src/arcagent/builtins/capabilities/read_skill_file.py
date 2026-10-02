"""Built-in ``read_skill_file`` tool (J4 B4/B5).

Progressive disclosure: ``use_skill`` lists a skill's files; this tool reads one
of them. The bytes are returned only after they verify against the operator's
signature (or, for an anchored skill, the signed revision manifest), through
no-follow handles jailed to the skill folder. A tampered or agent-re-signed file
is refused, so the operator's approval covers exactly what the model reads.
"""

from __future__ import annotations

import asyncio

from arcagent.builtins.capabilities import _runtime
from arcagent.builtins.capabilities._skill_access import offered_skill, record, refuse
from arcagent.capabilities.skill_files import SkillFileError
from arcagent.tools._decorator import tool

_ACTION = "skill.file.read"


@tool(
    name="read_skill_file",
    description=(
        "Read one file bundled with a skill (for example references/advanced.md), "
        "verified against the operator's signature. Paths are relative to the skill root "
        "listed by use_skill."
    ),
    classification="read_only",
    capability_tags=["file_read"],
    when_to_use="When a skill's instructions point to one of its own files.",
    version="1.0.0",
    examples=('read_skill_file(skill="pdf", path="references/advanced.md")',),
)
async def read_skill_file(skill: str, path: str) -> str:
    """Return the verified text of ``path`` inside ``skill``."""
    entry = offered_skill(skill, action=_ACTION, path=path)
    try:
        data = await asyncio.to_thread(_runtime.skill_files().read, entry, path)
    except SkillFileError as exc:
        raise refuse(_ACTION, skill, path, type(exc).__name__, str(exc)) from exc
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise refuse(
            _ACTION, skill, path, "binary_file", f"{path} is a binary file ({len(data)} bytes)"
        ) from exc
    record(_ACTION, skill, path, "allow", bytes=len(data))
    return text
