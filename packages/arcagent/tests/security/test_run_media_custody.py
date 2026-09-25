"""Signed-run media cannot escape its claimed session or change before model use."""

import hashlib
from pathlib import Path

import arcrun
import pytest

from arcagent.parts import PartTranslator


def _part(payload: bytes) -> dict[str, str | int]:
    return {
        "kind": "image",
        "mime": "image/png",
        "declared_name": "plot.png",
        "ref": "attachments/objects/plot.png",
        "sha256": f"sha256:{hashlib.sha256(payload).hexdigest()}",
        "size_bytes": len(payload),
        "session_key": "session-1",
        "owner_did": "did:arc:user/1",
        "agent_did": "did:arc:agent/1",
    }


def _translator(workspace: Path) -> PartTranslator:
    return PartTranslator(
        workspace=workspace,
        session_key="session-1",
        owner_did="did:arc:user/1",
        agent_did="did:arc:agent/1",
        require_proof=True,
    )


def test_claimed_png_becomes_provider_image_block(tmp_path: Path) -> None:
    payload = b"\x89PNG\r\n\x1a\n" + b"image-bytes"
    path = tmp_path / "attachments/objects/plot.png"
    path.parent.mkdir(parents=True)
    path.write_bytes(payload)
    translator = _translator(tmp_path)

    blocks = translator.to_model_content(translator.to_history_content([_part(payload)]))

    assert isinstance(blocks[0], arcrun.ImageBlock)
    assert blocks[0].media_type == "image/png"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("session_key", "other-session"),
        ("owner_did", "did:arc:user/other"),
        ("agent_did", "did:arc:agent/other"),
    ],
)
def test_cross_scope_media_is_refused_before_history(
    tmp_path: Path, field: str, value: str
) -> None:
    part = _part(b"\x89PNG\r\n\x1a\nimage")
    part[field] = value
    with pytest.raises(ValueError, match="scope mismatch"):
        _translator(tmp_path).to_history_content([part])


def test_media_without_custody_proof_is_refused(tmp_path: Path) -> None:
    part = _part(b"\x89PNG\r\n\x1a\nimage")
    del part["sha256"]
    with pytest.raises(ValueError, match="custody proof"):
        _translator(tmp_path).to_history_content([part])


def test_mime_spoof_and_ref_tamper_are_refused(tmp_path: Path) -> None:
    payload = b"\x89PNG\r\n\x1a\nimage"
    path = tmp_path / "attachments/objects/plot.png"
    path.parent.mkdir(parents=True)
    path.write_bytes(payload)
    part = _part(payload)
    translator = _translator(tmp_path)
    content = translator.to_history_content([part])
    path.write_bytes(b"\xff\xd8\xffchanged")
    with pytest.raises(ValueError, match="changed after custody"):
        translator.to_model_content(content)

    path.write_bytes(payload)
    part["mime"] = "image/jpeg"
    with pytest.raises(ValueError, match="MIME"):
        translator.to_model_content(translator.to_history_content([part]))
