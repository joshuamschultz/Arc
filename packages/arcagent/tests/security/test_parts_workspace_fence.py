"""The workspace fence on a media reference, beyond plain ``..`` traversal.

SPEC-065 T-933 (REQ-301). ``PartTranslator`` reads a ``ref`` that rode in on a
remote sender's message and hands the bytes to a model provider. An unfenced
read is therefore an exfiltration primitive (LLM02 / ASI06), not merely a bug:
one crafted reference and the agent posts a private file to an external API.

The unit suite covers the obvious ``../`` escape. These are the two escapes a
prefix-style check also misses, and which ``.resolve()`` plus a genuine
ancestry test catches: an absolute path (which discards the workspace root
entirely when joined) and a symlink that lives inside the workspace but points
outside it.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from arcagent.parts import PartTranslator

_SECRET = b"api_key = 'SHOULD_NEVER_LEAK'"


def _workspace(tmp_path: Path) -> Path:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (tmp_path / "arcagent.toml").write_bytes(_SECRET)
    return workspace


def _part(ref: str) -> dict[str, str]:
    return {"kind": "image", "mime": "image/jpeg", "declared_name": "cat.jpg", "ref": ref}


def test_an_absolute_reference_is_refused(tmp_path: Path) -> None:
    """``workspace / "/etc/passwd"`` is ``/etc/passwd`` — joining is not a fence."""
    workspace = _workspace(tmp_path)
    translator = PartTranslator(workspace=workspace)
    content = translator.to_history_content([_part(str(tmp_path / "arcagent.toml"))])

    with pytest.raises(ValueError, match="workspace"):
        translator.to_model_content(content)


def test_a_symlink_pointing_out_of_the_workspace_is_refused(tmp_path: Path) -> None:
    """The link sits inside; its target does not. Ancestry is tested after resolve."""
    workspace = _workspace(tmp_path)
    (workspace / "inbox").mkdir()
    (workspace / "inbox" / "cat.jpg").symlink_to(tmp_path / "arcagent.toml")
    translator = PartTranslator(workspace=workspace)
    content = translator.to_history_content([_part("inbox/cat.jpg")])

    with pytest.raises(ValueError, match="workspace"):
        translator.to_model_content(content)


def test_the_workspace_root_itself_is_not_a_readable_artefact(tmp_path: Path) -> None:
    """An empty ref resolves to the workspace directory — ancestry excludes it."""
    translator = PartTranslator(workspace=_workspace(tmp_path))
    content = translator.to_history_content([_part("")])

    with pytest.raises(ValueError, match="workspace"):
        translator.to_model_content(content)
