"""A 5MB image adds kilobytes to the session jsonl, and re-opens next turn.

SPEC-065 T-934 (REQ-316, REQ-301; COMP-010 + COMP-009). This is the test the
whole media design exists to pass, so it runs against the real
:class:`SessionManager` writing a real workspace on disk with a real 5MiB
artefact. Nothing here is mocked: a threshold assertion against a mock would
prove only that the mock is small.

Three properties, each of which base64-in-the-envelope would break:

1. The turn costs the session log kilobytes, not megabytes.
2. The record survives ``resume_session`` — arcrun validates every line and
   silently skips what it cannot parse, so a history block shape the model
   layer rejects would vanish from replay without an error.
3. A later turn re-opens the file from the reference alone. The model API is
   stateless; a reference that only worked on the turn it arrived would make
   the agent unable to look at the image it was just sent.
"""

from __future__ import annotations

import base64
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import arcrun
from arcagent.parts import PartTranslator

from arcagent.core.config import ContextConfig, SessionConfig
from arcagent.core.session_internal.manager import SessionManager

_SESSION_KEY = "telegram-1001"
_REF = "inbox/2026-08-11/120000-alice-holiday.jpg"
_FIVE_MIB = 5 * 1024 * 1024
_KILOBYTE_CEILING = 64 * 1024


def _session_manager(workspace: Path) -> SessionManager:
    return SessionManager(
        config=SessionConfig(),
        context_config=ContextConfig(),
        telemetry=MagicMock(),
        workspace=workspace,
    )


def _five_megabyte_image(workspace: Path) -> bytes:
    """Write a real 5MiB artefact where MediaStore would have put it."""
    payload = b"\xff\xd8\xff\xe0" + b"ARCMEDIA" * ((_FIVE_MIB - 4) // 8)
    path = workspace / _REF
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return payload


def _inbound_parts() -> list[dict[str, Any]]:
    """A photo with a caption — the ordinary shape, not a special case."""
    return [
        {"kind": "text", "text": "what do you make of this?"},
        {
            "kind": "image",
            "mime": "image/jpeg",
            "declared_name": "holiday.jpg",
            "ref": _REF,
        },
    ]


async def test_a_five_megabyte_image_costs_the_jsonl_kilobytes(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    payload = _five_megabyte_image(workspace)
    translator = PartTranslator(workspace=workspace)
    session = _session_manager(workspace)
    await session.open_or_resume(_SESSION_KEY)

    await session.append_message(
        {"role": "user", "content": translator.to_history_content(_inbound_parts())}
    )

    jsonl = workspace / "sessions" / f"{_SESSION_KEY}.jsonl"
    assert (workspace / _REF).stat().st_size == len(payload) >= _FIVE_MIB - 8
    assert jsonl.stat().st_size < _KILOBYTE_CEILING, (
        f"the turn wrote {jsonl.stat().st_size} bytes of history for a "
        f"{len(payload)}-byte artefact — the bytes are in the log"
    )


async def test_the_jsonl_holds_the_reference_and_not_the_bytes(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    payload = _five_megabyte_image(workspace)
    translator = PartTranslator(workspace=workspace)
    session = _session_manager(workspace)
    await session.open_or_resume(_SESSION_KEY)

    await session.append_message(
        {"role": "user", "content": translator.to_history_content(_inbound_parts())}
    )

    written = (workspace / "sessions" / f"{_SESSION_KEY}.jsonl").read_text(encoding="utf-8")
    assert _REF in written
    assert base64.b64encode(payload[:512]).decode() not in written
    assert max(len(line) for line in written.splitlines()) < 8192


async def test_materialising_the_bytes_for_a_call_does_not_grow_the_history(
    tmp_path: Path,
) -> None:
    """The model block is built for the call and never written back."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _five_megabyte_image(workspace)
    translator = PartTranslator(workspace=workspace)
    session = _session_manager(workspace)
    await session.open_or_resume(_SESSION_KEY)
    content = translator.to_history_content(_inbound_parts())
    await session.append_message({"role": "user", "content": content})
    jsonl = workspace / "sessions" / f"{_SESSION_KEY}.jsonl"
    before = jsonl.stat().st_size

    translator.to_model_content(content)

    assert jsonl.stat().st_size == before


async def test_the_file_re_opens_from_the_reference_on_a_later_turn(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    payload = _five_megabyte_image(workspace)
    first_turn = _session_manager(workspace)
    await first_turn.open_or_resume(_SESSION_KEY)
    await first_turn.append_message(
        {
            "role": "user",
            "content": PartTranslator(workspace=workspace).to_history_content(_inbound_parts()),
        }
    )

    later_turn = _session_manager(workspace)
    messages = await later_turn.resume_session(_SESSION_KEY)

    assert len(messages) == 1, (
        "the media turn did not survive resume — arcrun rejected the stored "
        "content and SessionManager skipped the line"
    )
    blocks = PartTranslator(workspace=workspace).to_model_content(messages[0]["content"])
    images = [block for block in blocks if isinstance(block, arcrun.ImageBlock)]
    assert len(images) == 1
    assert base64.b64decode(images[0].source) == payload
