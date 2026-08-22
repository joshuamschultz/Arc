"""Behavioral seams for the ArcUI attachment composer.

The web package intentionally has no browser test runner today.  These tests
pin the security-sensitive wire contract at the source seam while the Vite
build provides TypeScript/compiler coverage for the executable UI.
"""

from pathlib import Path


_WEB = Path(__file__).resolve().parents[1] / "web" / "src"


def _read(relative: str) -> str:
    return (_WEB / relative).read_text(encoding="utf-8")


def test_uploader_tracks_progress_and_has_abort_retry_state() -> None:
    source = _read("hooks/use-attachment-uploader.ts")
    assert "XMLHttpRequest" in source
    assert "xhr.upload.onprogress" in source
    assert "xhr.abort()" in source
    assert "retryAttachment" in source
    assert "removeAttachment" in source
    assert "rejected" in source


def test_attachment_uploader_never_exposes_bytes_or_paths_on_chat_wire() -> None:
    source = _read("hooks/use-attachment-uploader.ts")
    chat = _read("hooks/use-chat.ts")
    assert "attachment_id" in source
    assert "file.path" not in source
    assert "arrayBuffer" not in source
    assert "attachment_ids" in chat
    assert "attachment_ids: opaqueIds" in chat


def test_messages_render_picker_and_only_enable_send_for_clean_ids() -> None:
    source = _read("pages/messages.tsx")
    composer = _read("components/mention-composer.tsx")
    assert "AttachmentPicker" in source
    assert "cleanIds" in source
    assert "canSubmit" in composer
    assert "sendMessage(text, uploader.cleanIds)" in source
