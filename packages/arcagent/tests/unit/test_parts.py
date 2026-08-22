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

from arcagent.parts import MAX_PDF_PAGES, MAX_PDF_TEXT_CHARS, PartTranslator

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


def _minimal_pdf(text: str) -> bytes:
    """Build a tiny real, uncompressed PDF without relying on a shell/tool."""
    stream = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET\n".encode()
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"endstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    body = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for number, payload in enumerate(objects, 1):
        offsets.append(len(body))
        body.extend(f"{number} 0 obj\n".encode())
        body.extend(payload)
        body.extend(b"\nendobj\n")
    xref = len(body)
    body.extend(f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode())
    body.extend(b"".join(f"{offset:010d} 00000 n \n".encode() for offset in offsets[1:]))
    body.extend(
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    )
    return bytes(body)


def _multi_page_pdf(texts: list[str]) -> bytes:
    """Build a tiny real PDF with one uncompressed text stream per page."""
    streams = [f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET\n".encode() for text in texts]
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids ["
        + b" ".join(f"{3 + index * 2} 0 R".encode() for index in range(len(streams)))
        + f"] /Count {len(streams)} >>".encode(),
    ]
    for index, stream in enumerate(streams):
        page_number = 3 + index * 2
        content_number = page_number + 1
        objects.extend(
            [
                f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents {content_number} 0 R /Resources << /Font << /F1 {3 + len(streams) * 2} 0 R >> >> >>".encode(),
                b"<< /Length "
                + str(len(stream)).encode()
                + b" >>\nstream\n"
                + stream
                + b"endstream",
            ]
        )
    objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    body = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for number, payload in enumerate(objects, 1):
        offsets.append(len(body))
        body.extend(f"{number} 0 obj\n".encode())
        body.extend(payload)
        body.extend(b"\nendobj\n")
    xref = len(body)
    body.extend(f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode())
    body.extend(b"".join(f"{offset:010d} 00000 n \n".encode() for offset in offsets[1:]))
    body.extend(
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    )
    return bytes(body)


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

    def test_a_pdf_is_extracted_from_magic_bytes_even_when_name_has_no_extension(
        self, tmp_path: Path
    ) -> None:
        pytest.importorskip("pypdf")
        ref = "inbox/2026-08-11/sha256-deadbeef"
        _write(tmp_path, ref, _minimal_pdf("Hello PDF"))
        part = {
            "kind": "file",
            "mime": "application/octet-stream",
            "declared_name": "download",
            "ref": ref,
        }

        blocks = PartTranslator(workspace=tmp_path).to_model_content(
            PartTranslator(workspace=tmp_path).to_history_content([part])
        )

        assert isinstance(blocks[0], arcrun.TextBlock)
        assert "Hello PDF" in blocks[0].text
        assert "begin untrusted attachment content" in blocks[0].text

    def test_pdf_extraction_is_bounded_and_sanitizes_controls(self, tmp_path: Path) -> None:
        pytest.importorskip("pypdf")
        ref = "inbox/2026-08-11/sha256-bounded"
        payload = _minimal_pdf("A" * (MAX_PDF_TEXT_CHARS + 500))
        _write(tmp_path, ref, payload)
        part = {**_file_part(), "mime": "application/pdf", "ref": ref}

        blocks = PartTranslator(workspace=tmp_path).to_model_content(
            PartTranslator(workspace=tmp_path).to_history_content([part])
        )

        assert isinstance(blocks[0], arcrun.TextBlock)
        assert len(blocks[0].text) < MAX_PDF_TEXT_CHARS + 500
        assert "content truncated" in blocks[0].text

    def test_pdf_extraction_stops_before_a_later_page_after_budget_is_consumed(
        self, tmp_path: Path
    ) -> None:
        pytest.importorskip("pypdf")
        ref = "inbox/2026-08-11/many-pages.pdf"
        _write(tmp_path, ref, _multi_page_pdf(["A" * MAX_PDF_TEXT_CHARS, "LATER_PAGE"]))
        part = {**_file_part(), "ref": ref}

        blocks = PartTranslator(workspace=tmp_path).to_model_content(
            PartTranslator(workspace=tmp_path).to_history_content([part])
        )

        assert isinstance(blocks[0], arcrun.TextBlock)
        assert "LATER_PAGE" not in blocks[0].text
        assert "content truncated" in blocks[0].text

    def test_pdf_page_count_limit_returns_metadata_without_extracting_pages(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        pypdf = pytest.importorskip("pypdf")

        class TrackingPage:
            extract_calls = 0

            def extract_text(self, **_kwargs: Any) -> str:
                self.extract_calls += 1
                return "must not run"

        pages = [TrackingPage() for _ in range(MAX_PDF_PAGES + 1)]

        class FakeReader:
            is_encrypted = False

            def __init__(self, *_args: Any, **_kwargs: Any) -> None:
                self.pages = pages

        monkeypatch.setattr(pypdf, "PdfReader", FakeReader)
        ref = "inbox/2026-08-11/too-many-pages.pdf"
        _write(tmp_path, ref, b"%PDF-1.7\nnot parsed by fake reader")
        part = {**_file_part(), "ref": ref}

        blocks = PartTranslator(workspace=tmp_path).to_model_content(
            PartTranslator(workspace=tmp_path).to_history_content([part])
        )

        assert isinstance(blocks[0], arcrun.TextBlock)
        assert "page extraction limit" in blocks[0].text
        assert all(page.extract_calls == 0 for page in pages)

    def test_expanding_page_stops_before_following_page(self, tmp_path: Path) -> None:
        pytest.importorskip("pypdf")

        class ExpandingPage:
            def __init__(self, text: str) -> None:
                self.text = text
                self.calls = 0

            def extract_text(self, **kwargs: Any) -> str:
                self.calls += 1
                if kwargs:
                    raise TypeError
                return self.text

        first = ExpandingPage("X" * (MAX_PDF_TEXT_CHARS + 1))
        later = ExpandingPage("LATER_EXPANSION")

        class FakeReader:
            is_encrypted = False

            def __init__(self, *_args: Any, **_kwargs: Any) -> None:
                self.pages = [first, later]

        monkeypatch = pytest.MonkeyPatch()
        monkeypatch.setattr("pypdf.PdfReader", FakeReader)
        try:
            ref = "inbox/2026-08-11/expanded.pdf"
            _write(tmp_path, ref, b"%PDF-1.7\nnot parsed by fake reader")
            part = {**_file_part(), "ref": ref}
            blocks = PartTranslator(workspace=tmp_path).to_model_content(
                PartTranslator(workspace=tmp_path).to_history_content([part])
            )
        finally:
            monkeypatch.undo()

        assert isinstance(blocks[0], arcrun.TextBlock)
        assert "LATER_EXPANSION" not in blocks[0].text
        assert first.calls == 2
        assert later.calls == 0


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
