"""MediaStore — inbound artefact custody at the gateway boundary (SPEC-065 COMP-002).

Covers T-925 / T-926 / T-927 (REQ-297, REQ-298, REQ-299, REQ-300).

The sender controls the declared filename; the *gateway* composes the path. These
tests exist mostly to prove that separation holds under attack (LLM05 improper
output handling, ASI02 tool misuse, classic path traversal): a hostile declared
name must never influence where bytes land, and the declared name survives only
as metadata.

ADR-029: an inbound attachment is agent state, so it is written with direct
filesystem I/O to the agent workspace — never through the LLM-facing tools. The
workspace here is a real ``tmp_path`` tree; only the audit sink is a spy, so that
"exactly one event" can be counted rather than merely observed.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from arctrust.audit import AuditEvent, NullSink

from arcgateway import audit as gw_audit
from arcgateway.media_store import MediaStore, MediaTooLargeError

ACTOR_DID = "did:arc:org:user/alice"
AGENT_DID = "did:arc:org:agent/olivia"
CHANNEL = "telegram:9001"
SENDER = "alice"

# inbox/<YYYY-MM-DD>/<hhmmss>-<sender>-<stem>.<ext>
DATE_DIR = re.compile(r"^\d{4}-\d{2}-\d{2}$")
FILENAME = re.compile(r"^\d{6}-alice-holiday\.jpg$")

HOSTILE_NAMES = [
    pytest.param("../../../etc/passwd", id="posix-traversal"),
    pytest.param("..\\..\\windows\\system32\\x", id="windows-traversal"),
    pytest.param("sub/dir/photo.png", id="embedded-separators"),
    pytest.param("/etc/passwd", id="absolute-path"),
    pytest.param("...", id="dots-only"),
    pytest.param("", id="empty"),
    pytest.param("bad\x00name.png", id="null-byte"),
    pytest.param("\r\n\tname.png", id="control-chars"),
    pytest.param("A" * 500 + ".png", id="over-long"),
]


class _CapturingSink:
    """Test sink — records every event written, satisfies the AuditSink Protocol."""

    def __init__(self) -> None:
        self.events: list[AuditEvent] = []

    def write(self, event: AuditEvent) -> None:
        self.events.append(event)

    def actions(self) -> list[str]:
        return [e.action for e in self.events]


@pytest.fixture
def captured() -> _CapturingSink:
    """Replace the gateway audit sink with a capturing one for the test."""
    sink = _CapturingSink()
    gw_audit.configure_sink(sink, actor_did="did:arc:gateway:test")
    yield sink
    gw_audit.configure_sink(NullSink(), actor_did="did:arc:gateway:daemon")


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    """A real agent workspace root — MediaStore writes here directly (ADR-029)."""
    root = tmp_path / "team" / "olivia_agent" / "workspace"
    root.mkdir(parents=True)
    return root


@pytest.fixture
def store(workspace: Path) -> MediaStore:
    return MediaStore(workspace=workspace, max_bytes=1024)


def store_image(store: MediaStore, declared_name: str, data: bytes = b"jpegbytes"):
    """Store one inbound image with the standard test identity."""
    return store.store(
        data=data,
        declared_name=declared_name,
        mime="image/jpeg",
        kind="image",
        sender=SENDER,
        channel=CHANNEL,
        actor_did=ACTOR_DID,
    )


def files_under(root: Path) -> list[Path]:
    return sorted(p for p in root.rglob("*") if p.is_file())


# ---------------------------------------------------------------------------
# T-925 / REQ-297, REQ-298 — gateway-composed path
# ---------------------------------------------------------------------------


class TestGatewayComposedPath:
    def test_bytes_land_at_the_dated_inbox_path(self, store: MediaStore, workspace: Path) -> None:
        stored = store_image(store, "holiday.jpg", data=b"jpegbytes")

        path = stored.path.resolve()
        assert path.is_file()
        assert path.read_bytes() == b"jpegbytes"

        relative = path.relative_to((workspace / "inbox").resolve())
        assert len(relative.parts) == 2, f"expected inbox/<date>/<file>, got {relative}"
        date_dir, filename = relative.parts
        assert DATE_DIR.match(date_dir), date_dir
        assert FILENAME.match(filename), filename

    def test_declared_name_survives_as_metadata(self, store: MediaStore) -> None:
        stored = store_image(store, "holiday.jpg")

        assert stored.declared_name == "holiday.jpg"
        assert stored.kind == "image"
        assert stored.mime == "image/jpeg"
        assert stored.size_bytes == len(b"jpegbytes")

    @pytest.mark.parametrize("declared_name", HOSTILE_NAMES)
    def test_hostile_declared_name_cannot_influence_the_path(
        self, store: MediaStore, workspace: Path, declared_name: str
    ) -> None:
        """The sender names the file; the gateway decides where it goes."""
        stored = store_image(store, declared_name, data=b"payload")

        path = stored.path.resolve()
        inbox = (workspace / "inbox").resolve()

        # Genuine containment: resolve() first, then check ancestry. A string
        # prefix check would pass for `inbox/../../etc/passwd`.
        assert inbox in path.parents, f"{path} escaped {inbox}"
        assert path.is_file()
        assert path.read_bytes() == b"payload"

        # Exactly inbox/<date>/<file> — no extra segments smuggled in.
        relative = path.relative_to(inbox)
        assert len(relative.parts) == 2, f"{declared_name!r} added path segments: {relative}"
        assert DATE_DIR.match(relative.parts[0])

        # A usable single filename component, within the filesystem's NAME_MAX.
        name = path.name
        assert name not in {"", ".", ".."}
        assert "/" not in name and "\\" not in name
        assert "\x00" not in name
        assert len(name.encode("utf-8")) <= 255

    @pytest.mark.parametrize("declared_name", HOSTILE_NAMES)
    def test_hostile_declared_name_is_still_kept_verbatim_as_metadata(
        self, store: MediaStore, declared_name: str
    ) -> None:
        """Sanitising the path must not rewrite the record of what was claimed."""
        stored = store_image(store, declared_name)

        assert stored.declared_name == declared_name

    @pytest.mark.parametrize("declared_name", HOSTILE_NAMES)
    def test_nothing_is_written_outside_the_inbox(
        self, store: MediaStore, workspace: Path, declared_name: str
    ) -> None:
        store_image(store, declared_name)

        inbox = (workspace / "inbox").resolve()
        written = files_under(workspace)
        assert written, "expected the artefact to be written"
        for path in written:
            assert inbox in path.resolve().parents, f"{path} written outside {inbox}"


# ---------------------------------------------------------------------------
# T-926 / REQ-299 — size ceiling
# ---------------------------------------------------------------------------


class TestSizeCeiling:
    def test_over_ceiling_artefact_is_refused(self, workspace: Path) -> None:
        store = MediaStore(workspace=workspace, max_bytes=8)

        with pytest.raises(MediaTooLargeError):
            store_image(store, "huge.jpg", data=b"x" * 9)

    def test_refusal_writes_nothing_at_all(self, workspace: Path) -> None:
        """Never-written, not written-then-deleted: no file may appear."""
        store = MediaStore(workspace=workspace, max_bytes=8)

        with pytest.raises(MediaTooLargeError):
            store_image(store, "huge.jpg", data=b"x" * 9)

        assert files_under(workspace) == []

    def test_refusal_carries_what_the_sender_must_be_told(self, workspace: Path) -> None:
        """Typed refusal — the caller replies on the channel it arrived from."""
        store = MediaStore(workspace=workspace, max_bytes=8)

        with pytest.raises(MediaTooLargeError) as exc_info:
            store_image(store, "huge.jpg", data=b"x" * 9)

        refusal = exc_info.value
        assert refusal.channel == CHANNEL
        assert refusal.declared_name == "huge.jpg"
        assert refusal.size_bytes == 9
        assert refusal.limit_bytes == 8

    def test_refusal_emits_no_stored_event(
        self, captured: _CapturingSink, workspace: Path
    ) -> None:
        store = MediaStore(workspace=workspace, max_bytes=8)

        with pytest.raises(MediaTooLargeError):
            store_image(store, "huge.jpg", data=b"x" * 9)

        assert "media.received" not in captured.actions()

    def test_artefact_at_the_ceiling_is_accepted(self, workspace: Path) -> None:
        """The ceiling is a maximum, not an exclusive bound."""
        store = MediaStore(workspace=workspace, max_bytes=8)

        stored = store_image(store, "exact.jpg", data=b"x" * 8)

        assert stored.path.resolve().is_file()


# ---------------------------------------------------------------------------
# T-927 / REQ-300 — exactly one audit event per artefact
# ---------------------------------------------------------------------------


class TestAuditExactlyOnce:
    def test_storing_emits_exactly_one_event(
        self, captured: _CapturingSink, store: MediaStore
    ) -> None:
        store_image(store, "holiday.jpg")

        assert len(captured.events) == 1, captured.actions()
        assert captured.events[0].action == "media.received"

    def test_stored_event_names_actor_channel_kind_and_size(
        self, captured: _CapturingSink, store: MediaStore
    ) -> None:
        stored = store_image(store, "holiday.jpg", data=b"jpegbytes")
        event = captured.events[0]

        assert event.actor_did == ACTOR_DID
        assert event.extra.get("channel") == CHANNEL
        assert event.extra.get("kind") == "image"
        assert event.extra.get("size_bytes") == len(b"jpegbytes")
        assert stored.path.name in event.target

    def test_stored_event_does_not_carry_the_bytes(
        self, captured: _CapturingSink, store: MediaStore
    ) -> None:
        """Audit records custody, not content."""
        store_image(store, "holiday.jpg", data=b"secretjpegbytes")

        assert "secretjpegbytes" not in captured.events[0].model_dump_json()

    def test_sending_emits_exactly_one_event(
        self, captured: _CapturingSink, store: MediaStore
    ) -> None:
        stored = store_image(store, "holiday.jpg")
        captured.events.clear()

        store.record_sent(
            path=stored.path,
            mime="image/jpeg",
            kind="image",
            channel=CHANNEL,
            actor_did=AGENT_DID,
        )

        assert len(captured.events) == 1, captured.actions()
        assert captured.events[0].action == "media.sent"

    def test_sent_event_names_actor_channel_kind_and_size(
        self, captured: _CapturingSink, store: MediaStore
    ) -> None:
        stored = store_image(store, "holiday.jpg", data=b"jpegbytes")
        captured.events.clear()

        store.record_sent(
            path=stored.path,
            mime="image/jpeg",
            kind="image",
            channel=CHANNEL,
            actor_did=AGENT_DID,
        )
        event = captured.events[0]

        assert event.actor_did == AGENT_DID
        assert event.extra.get("channel") == CHANNEL
        assert event.extra.get("kind") == "image"
        assert event.extra.get("size_bytes") == len(b"jpegbytes")

    def test_two_artefacts_emit_two_events(
        self, captured: _CapturingSink, store: MediaStore
    ) -> None:
        """One event per artefact — not one per message, not one per batch."""
        store_image(store, "one.jpg")
        store_image(store, "two.jpg")

        assert captured.actions() == ["media.received", "media.received"]
