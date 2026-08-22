"""Bounded child-process PDF extraction.

The pypdf parser expands compressed content streams before its visitor callback
runs.  It therefore cannot be treated as an in-process memory bound.  This
module runs it in a spawned worker with OS resource limits and a wall clock
deadline owned by the parent.
"""

from __future__ import annotations

import argparse
import io
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import psutil  # type: ignore[import-untyped]  # reason: psutil ships no type metadata

MAX_PDF_BYTES = 16 * 1024 * 1024
MAX_PDF_TEXT_CHARS = 32_000
MAX_PDF_PAGES = 100
MAX_PDF_WORKER_MEMORY_BYTES = 512 * 1024 * 1024
MAX_PDF_WORKER_CPU_SECONDS = 2
MAX_PDF_WORKER_WALL_SECONDS = 5.0
MAX_PDF_WORKER_OUTPUT_BYTES = MAX_PDF_TEXT_CHARS * 4 + 1024
MAX_PDF_WORKER_POLL_SECONDS = 0.05
_MAX_ERROR_BYTES = 512
_PDF_MAGIC = b"%PDF-"
_CONTROL_CHARS = frozenset(range(0, 9)) | frozenset((11, 12)) | frozenset(range(14, 32)) | {127}


class PdfWorkerError(RuntimeError):
    """The bounded parser worker could not produce safe output."""


class _TextLimitReachedError(Exception):
    """Internal control flow to stop extraction at the text budget."""


def extract_pdf(path: Path) -> str:
    """Extract bounded PDF text in a resource-limited subprocess."""
    if not _limits_available():
        raise PdfWorkerError("PDF extraction limits are unavailable on this platform")
    command = [sys.executable, "-I", str(Path(__file__).resolve()), str(path)]
    process: subprocess.Popen[bytes] | None = None
    try:
        process = subprocess.Popen(  # noqa: S603 -- fixed interpreter and argv, no shell
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            preexec_fn=_install_limits,
        )
        output = _communicate_with_limits(process)
        if process.returncode != 0:
            raise PdfWorkerError("PDF extraction worker failed")
        return _decode_result(output)
    except PdfWorkerError:
        raise
    except subprocess.TimeoutExpired as exc:
        raise PdfWorkerError("PDF extraction worker exceeded its wall-time limit") from exc
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        raise PdfWorkerError("PDF extraction worker failed") from exc
    finally:
        if process is not None and process.poll() is None:
            _kill_process_group(process)
            process.communicate()


def _kill_process_group(process: subprocess.Popen[bytes]) -> None:
    """Kill the worker process group, including any parser descendants."""
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except (OSError, ProcessLookupError):
        process.kill()


def _communicate_with_limits(process: subprocess.Popen[bytes]) -> bytes:
    """Drain bounded stdout while enforcing wall time and child RSS."""
    deadline = time.monotonic() + MAX_PDF_WORKER_WALL_SECONDS
    collected = bytearray()
    seen = 0
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            _kill_process_group(process)
            raise PdfWorkerError("PDF extraction worker exceeded its wall-time limit")
        try:
            output, _ = process.communicate(timeout=min(MAX_PDF_WORKER_POLL_SECONDS, remaining))
            if output:
                collected.extend(output[seen:])
            return bytes(collected)
        except subprocess.TimeoutExpired as exc:
            output = exc.output or b""
            if len(output) > seen:
                collected.extend(output[seen:])
                seen = len(output)
            if len(collected) > MAX_PDF_WORKER_OUTPUT_BYTES:
                _kill_process_group(process)
                raise PdfWorkerError("PDF extraction worker output exceeded the limit") from exc
            try:
                rss = psutil.Process(process.pid).memory_info().rss
            except (psutil.Error, OSError) as error:
                _kill_process_group(process)
                raise PdfWorkerError("PDF worker memory could not be measured") from error
            if rss > MAX_PDF_WORKER_MEMORY_BYTES:
                _kill_process_group(process)
                raise PdfWorkerError("PDF extraction worker exceeded its memory limit") from exc


def _limits_available() -> bool:
    """Return whether this host can install both mandatory OS limits."""
    if os.name != "posix":
        return False
    try:
        import resource

        limits = ("RLIMIT_CPU", "setrlimit")
        if sys.platform != "darwin":
            limits += ("RLIMIT_AS",)
        return all(hasattr(resource, name) for name in limits)
    except ImportError:
        return False


def _install_limits() -> None:
    """Install hard memory and CPU limits, or fail closed."""
    if not _limits_available():
        raise PdfWorkerError("PDF extraction limits are unavailable on this platform")
    import resource

    try:
        if sys.platform != "darwin":
            resource.setrlimit(
                resource.RLIMIT_AS,
                (MAX_PDF_WORKER_MEMORY_BYTES, MAX_PDF_WORKER_MEMORY_BYTES),
            )
        resource.setrlimit(
            resource.RLIMIT_CPU,
            (MAX_PDF_WORKER_CPU_SECONDS, MAX_PDF_WORKER_CPU_SECONDS),
        )
    except (OSError, ValueError) as exc:
        raise PdfWorkerError("PDF extraction limits could not be installed") from exc


def _worker_main(path: str) -> int:
    """Import pypdf after pre-exec limits and write one bounded frame."""
    try:
        text = _parse_pdf(Path(path))
        _send(b"O" + text.encode("utf-8"))
    except PdfWorkerError as exc:
        _send(b"E" + str(exc).encode("utf-8")[:_MAX_ERROR_BYTES])
    except Exception as exc:
        _send(f"Epdf parser failed: {type(exc).__name__}".encode())
    return 0


def _send(payload: bytes) -> None:
    """Send a single bounded frame; never send parser objects or tracebacks."""
    if len(payload) > MAX_PDF_WORKER_OUTPUT_BYTES:
        payload = b"Epdf parser output exceeded the limit"
    sys.stdout.buffer.write(payload)
    sys.stdout.buffer.flush()


def _parse_pdf(path: Path) -> str:
    """Parse one custody file with page and text work limits."""
    try:
        with path.open("rb") as handle:
            payload = handle.read(MAX_PDF_BYTES + 1)
    except OSError as exc:
        raise PdfWorkerError("PDF file cannot be read") from exc
    if len(payload) > MAX_PDF_BYTES or not _has_pdf_magic(payload):
        raise PdfWorkerError("file is not a valid PDF")

    try:
        from pypdf import PdfReader

        reader = PdfReader(io.BytesIO(payload), strict=True)
        if reader.is_encrypted:
            raise PdfWorkerError("encrypted PDFs are not accepted")
        pages = reader.pages
        page_count = len(pages)
        if page_count > MAX_PDF_PAGES:
            raise PdfWorkerError("PDF exceeds the page extraction limit")
        chunks: list[str] = []
        used = 0
        for index in range(page_count):
            remaining = MAX_PDF_TEXT_CHARS - used
            if remaining <= 0:
                break
            page_text, exhausted = _extract_page(pages[index], remaining)
            if page_text:
                if chunks:
                    chunks.append("\n")
                    used += 1
                available = MAX_PDF_TEXT_CHARS - used
                chunks.append(page_text[:available])
                used += min(len(page_text), available)
            if exhausted or used >= MAX_PDF_TEXT_CHARS:
                break
        text = "".join(chunks)
    except PdfWorkerError:
        raise
    except Exception as exc:
        raise PdfWorkerError("PDF content could not be safely extracted") from exc
    clean = "".join(char for char in text if ord(char) not in _CONTROL_CHARS).strip()
    if used >= MAX_PDF_TEXT_CHARS:
        clean += "\n[content truncated]"
    return clean[: MAX_PDF_TEXT_CHARS + len("\n[content truncated]")]


def _extract_page(page: Any, remaining: int) -> tuple[str, bool]:
    """Use only the visitor API; unsupported parser APIs fail closed."""
    chunks: list[str] = []
    used = 0

    def visitor_text(text: str, *_args: Any) -> None:
        nonlocal used
        if not text:
            return
        available = remaining - used
        if available <= 0:
            raise _TextLimitReachedError
        chunks.append(text[:available])
        used += min(len(text), available)
        if len(text) >= available:
            raise _TextLimitReachedError

    try:
        page.extract_text(visitor_text=visitor_text)
    except _TextLimitReachedError:
        return "".join(chunks), True
    return "".join(chunks), used >= remaining


def _decode_result(payload: bytes) -> str:
    """Decode one bounded worker frame."""
    if not payload:
        raise PdfWorkerError("PDF extraction worker returned no output")
    if payload[:1] == b"E":
        message = payload[1:].decode("utf-8", errors="replace")[:_MAX_ERROR_BYTES]
        raise PdfWorkerError(message or "PDF extraction worker failed")
    if payload[:1] != b"O":
        raise PdfWorkerError("PDF extraction worker returned invalid output")
    try:
        text = payload[1:].decode("utf-8")
    except UnicodeDecodeError as exc:
        raise PdfWorkerError("PDF extraction worker returned invalid text") from exc
    if len(text) > MAX_PDF_TEXT_CHARS + len("\n[content truncated]"):
        raise PdfWorkerError("PDF extraction worker output exceeded the limit")
    return text


def _has_pdf_magic(payload: bytes) -> bool:
    return payload.startswith(_PDF_MAGIC) or payload.startswith(b"\xef\xbb\xbf%PDF-")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("path")
    raise SystemExit(_worker_main(parser.parse_args().path))
