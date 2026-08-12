"""PartTranslator — a workspace reference becomes a model block for one call.

SPEC-065 T-933 (REQ-301, COMP-009). The agent, not the gateway, is where a
media reference meets a model content block, because that is the lowest layer
that is allowed to know model types at all: arcgateway imports no model
package, and arcagent reaches them through the ``arcrun`` facade.

The property under test is narrow and load-bearing: bytes are materialised for
the provider call that needs them and for nothing else. They do not enter the
session log, they are not cached on the translator, and the part the caller
handed in still carries only its reference afterwards. Inlining base64 into the
envelope was rejected in the SDD's Alternatives Considered precisely because a
stateless model API would then re-ride the bytes every turn and the agent could
never re-open the file.
"""

from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any

import arcrun
import pytest
from arcagent.parts import PartTranslator

_IMAGE_REF = "inbox/2026-08-11/120000-alice-cat.jpg"
_FILE_REF = "inbox/2026-08-11/120001-alice-quarterly.pdf"


def _write(workspace: Path, ref: str, payload: bytes) -> Path:
    """Write an artefact into the workspace exactly as MediaStore would."""
    path = workspace / ref
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return path


def _image_part(ref: str = _IMAGE_REF) -> dict[str, Any]:
    """The wire shape of ``arcgateway.parts.MediaPart`` — four fields, no bytes."""
    return {
        "kind": "image",
        "mime": "image/jpeg",
        "declared_name": "cat.jpg",
        "ref": ref,
    }


def _file_part() -> dict[str, Any]:
    return {
        "kind": "file",
        "mime": "application/pdf",
        "declared_name": "quarterly.pdf",
        "ref": _FILE_REF,
    }


def _retained_leaves(obj: Any) -> list[bytes | str]:
    """Every str/bytes the translator still holds, however nested.

    A top-level scan is not enough — a cache is usually a dict, which is where
    the bytes would hide.
    """
    leaves: list[bytes | str] = []
    seen: set[int] = set()

    def walk(value: Any) -> None:
        if id(value) in seen:
            return
        seen.add(id(value))
        if isinstance(value, (bytes, str)):
            leaves.append(value)
        elif isinstance(value, dict):
            for key, item in value.items():
                walk(key)
                walk(item)
        elif isinstance(value, (list, tuple, set, frozenset)):
            for item in value:
                walk(item)
        elif hasattr(value, "__dict__"):
            walk(vars(value))

    walk(vars(obj))
    return leaves


def _rendered(blocks: list[Any]) -> str:
    """Serialise model blocks so byte-leak assertions can search one string."""
    return json.dumps([block.model_dump() for block in blocks])


class TestReferenceToModelBlock:
    def test_an_image_reference_becomes_an_arcrun_image_block_carrying_the_bytes(
        self, tmp_path: Path
    ) -> None:
        payload = b"\xff\xd8\xff\xe0" + b"jpeg-bytes" * 100
        _write(tmp_path, _IMAGE_REF, payload)
        translator = PartTranslator(workspace=tmp_path)

        blocks = translator.to_model_content(translator.to_history_content([_image_part()]))

        assert len(blocks) == 1
        assert isinstance(blocks[0], arcrun.ImageBlock)
        assert blocks[0].media_type == "image/jpeg"
        assert base64.b64decode(blocks[0].source) == payload

    def test_a_text_part_travels_the_same_path_and_stays_a_text_block(
        self, tmp_path: Path
    ) -> None:
        """Text is a part like any other (REQ-296) — one path, not two."""
        translator = PartTranslator(workspace=tmp_path)
        parts = [{"kind": "text", "text": "look at this"}]

        blocks = translator.to_model_content(translator.to_history_content(parts))

        assert len(blocks) == 1
        assert isinstance(blocks[0], arcrun.TextBlock)
        assert blocks[0].text == "look at this"

    def test_a_non_image_artefact_is_named_to_the_model_not_inlined(self, tmp_path: Path) -> None:
        """A PDF is not an image block — naming it costs tokens, inlining it costs megabytes."""
        payload = b"%PDF-1.7" + b"pdf-bytes" * 5000
        _write(tmp_path, _FILE_REF, payload)
        translator = PartTranslator(workspace=tmp_path)

        rendered = _rendered(
            translator.to_model_content(translator.to_history_content([_file_part()]))
        )

        assert "quarterly.pdf" in rendered
        assert base64.b64encode(payload[:256]).decode() not in rendered
        assert len(rendered) < 2000


class TestBytesAreForOneCallOnly:
    def test_the_translator_retains_no_bytes_after_the_call(self, tmp_path: Path) -> None:
        payload = b"\xff\xd8\xff\xe0" + b"first" * 2000
        _write(tmp_path, _IMAGE_REF, payload)
        translator = PartTranslator(workspace=tmp_path)

        translator.to_model_content(translator.to_history_content([_image_part()]))

        assert all(len(value) < 4096 for value in _retained_leaves(translator)), (
            "the translator is holding artefact-sized state between calls"
        )

    def test_a_second_call_re_reads_the_file_rather_than_serving_a_cache(
        self, tmp_path: Path
    ) -> None:
        """A cache would make the reference a lie the next time the file changes."""
        _write(tmp_path, _IMAGE_REF, b"\xff\xd8\xff\xe0" + b"first" * 100)
        translator = PartTranslator(workspace=tmp_path)
        history = translator.to_history_content([_image_part()])
        translator.to_model_content(history)

        replaced = b"\xff\xd8\xff\xe0" + b"second" * 100
        _write(tmp_path, _IMAGE_REF, replaced)
        blocks = translator.to_model_content(history)

        assert base64.b64decode(blocks[0].source) == replaced

    def test_the_stored_content_is_unchanged_by_building_the_model_blocks(
        self, tmp_path: Path
    ) -> None:
        """The materialised block never travels back into what gets logged."""
        payload = b"\xff\xd8\xff\xe0" + b"jpeg" * 1000
        _write(tmp_path, _IMAGE_REF, payload)
        translator = PartTranslator(workspace=tmp_path)
        history = translator.to_history_content([_image_part()])
        before = json.dumps(history)

        translator.to_model_content(history)

        assert json.dumps(history) == before
        assert base64.b64encode(payload[:64]).decode() not in before

    def test_the_part_handed_in_still_carries_only_its_reference(self, tmp_path: Path) -> None:
        payload = b"\xff\xd8\xff\xe0" + b"jpeg" * 1000
        _write(tmp_path, _IMAGE_REF, payload)
        translator = PartTranslator(workspace=tmp_path)
        part = _image_part()

        translator.to_model_content(translator.to_history_content([part]))

        assert part == _image_part()


class TestWorkspaceFence:
    def test_a_reference_escaping_the_workspace_is_refused(self, tmp_path: Path) -> None:
        """Same fail-closed containment rule as ``SessionManager._session_jsonl_path``."""
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        (tmp_path / "secret.txt").write_bytes(b"not the agent's to read")
        translator = PartTranslator(workspace=workspace)
        part = _image_part(ref="../secret.txt")

        with pytest.raises(ValueError, match="workspace"):
            translator.to_model_content(translator.to_history_content([part]))
