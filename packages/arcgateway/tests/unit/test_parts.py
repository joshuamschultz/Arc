"""The normalised inbound envelope and its part vocabulary (SPEC-065 T-924, COMP-001).

REQ-296: a payload carrying any combination of text, images and files normalises
into ONE inbound envelope holding an ordered list of typed parts, in which text
is a part like any other.

The envelope is ``InboundEvent``. Its ``parts`` list is the canonical form of a
message; its ``message`` string is the *flattened text projection* the many
text-only consumers read (slash commands, the subprocess executor, the echo
stub). They are two views of one message, kept in step by a validator — which
is the property these tests pin down, because the failure mode of two
representations is that they quietly become two different messages.

The security property under test: a ``MediaPart`` carries a *workspace
reference*, never bytes. That is what keeps a 5MB image out of the session
jsonl, out of the queue, and out of the prompt.
"""

from __future__ import annotations

import pytest
from pydantic import TypeAdapter, ValidationError

from arcgateway.executor import InboundEvent
from arcgateway.parts import MediaPart, Part, TextPart, flatten_text

_ROUTING = {
    "platform": "telegram",
    "chat_id": "42",
    "user_did": "did:arc:user:x",
    "agent_did": "did:arc:agent:y",
    "session_key": "sess-1",
}


def _image() -> MediaPart:
    return MediaPart(
        kind="image",
        mime="image/png",
        declared_name="holiday.png",
        ref="inbox/2026-08-11/143000-op-holiday.png",
    )


def _file() -> MediaPart:
    return MediaPart(
        kind="file",
        mime="application/pdf",
        declared_name="invoice.pdf",
        ref="inbox/2026-08-11/143001-op-invoice.pdf",
    )


def _envelope(**overrides: object) -> InboundEvent:
    """An envelope with routing filled in; ``message`` omitted unless given."""
    fields: dict[str, object] = {**_ROUTING, "message": ""}
    fields.update(overrides)
    return InboundEvent(**fields)  # type: ignore[arg-type]  # reason: kwargs dict for a fixed field set


def _bytes_bearing_fields(model: type) -> list[str]:
    """Field names whose declared type could carry raw bytes."""
    return [
        name
        for name, field in model.model_fields.items()
        if "bytes" in str(field.annotation).lower()
    ]


def test_text_image_and_file_normalise_to_one_ordered_parts_list() -> None:
    """One payload with three kinds becomes one envelope, order exactly as sent."""
    message = _envelope(parts=[TextPart(text="look at this"), _image(), _file()])

    assert [part.kind for part in message.parts] == ["text", "image", "file"]
    assert isinstance(message.parts[0], TextPart)
    assert isinstance(message.parts[1], MediaPart)
    assert isinstance(message.parts[2], MediaPart)


def test_text_travels_as_a_part_not_as_the_scalar_field() -> None:
    """The words live in ``parts``; ``message`` only projects them.

    The scalar field survives because dozens of text-only consumers read it,
    but it must not be where the text *lives* — otherwise a caller reading
    ``message`` on a photo-plus-caption sees the caption and the photo is gone,
    which is the branch REQ-296 exists to remove.
    """
    message = _envelope(parts=[TextPart(text="look at this"), _image()])

    texts = [part for part in message.parts if isinstance(part, TextPart)]
    assert [part.text for part in texts] == ["look at this"]
    # The media survives alongside it rather than being flattened away.
    assert any(isinstance(part, MediaPart) for part in message.parts)


def test_a_caller_supplying_only_text_still_gets_a_parts_list() -> None:
    """The no-branch property from the text side: plain text becomes one part."""
    message = _envelope(message="just words")

    assert [part.kind for part in message.parts] == ["text"]
    assert message.parts[0].text == "just words"


def test_a_caller_supplying_only_parts_still_gets_the_text_projection() -> None:
    """And from the parts side: the scalar view is derived, never left empty.

    A photo-only message whose ``message`` stayed ``""`` reads to every
    text-only consumer as an empty message and is dropped as noise.
    """
    message = _envelope(parts=[_image()])

    assert message.message == flatten_text([_image()])
    assert "holiday.png" in message.message
    assert message.message.strip(), "a media-only message projected to no text at all"


def test_the_two_views_cannot_describe_different_messages() -> None:
    """Whichever view a caller fills in, the other agrees with it.

    Two representations that can disagree are two messages. This is the whole
    justification for keeping ``message`` alongside ``parts``.
    """
    from_text = _envelope(message="hello")
    from_parts = _envelope(parts=[TextPart(text="hello")])

    assert from_text.parts == from_parts.parts
    assert from_text.message == from_parts.message == "hello"


def test_round_trip_preserves_part_order_and_types() -> None:
    """model_dump -> model_validate loses neither order nor part type."""
    original = _envelope(
        parts=[TextPart(text="one"), _image(), TextPart(text="two"), _file()],
    )

    restored = InboundEvent.model_validate(original.model_dump())

    assert restored == original
    assert [part.kind for part in restored.parts] == ["text", "image", "text", "file"]
    assert [type(part) for part in restored.parts] == [TextPart, MediaPart, TextPart, MediaPart]
    assert restored.parts[1].ref == "inbox/2026-08-11/143000-op-holiday.png"


def test_part_vocabulary_resolves_raw_payloads_to_typed_parts() -> None:
    """``Part`` is the vocabulary: a raw dict resolves to the right typed part."""
    adapter: TypeAdapter[Part] = TypeAdapter(Part)

    assert isinstance(adapter.validate_python({"kind": "text", "text": "hi"}), TextPart)
    assert isinstance(
        adapter.validate_python(
            {
                "kind": "image",
                "mime": "image/png",
                "declared_name": "a.png",
                "ref": "inbox/2026-08-11/143000-op-a.png",
            }
        ),
        MediaPart,
    )


def test_media_part_carries_kind_mime_declared_name_and_workspace_ref() -> None:
    """The four things a media part is allowed to know."""
    part = _image()

    assert part.kind == "image"
    assert part.mime == "image/png"
    assert part.declared_name == "holiday.png"
    assert part.ref == "inbox/2026-08-11/143000-op-holiday.png"


def test_media_part_never_carries_bytes() -> None:
    """A media part is a reference. Bytes must have nowhere to live on it."""
    assert _bytes_bearing_fields(MediaPart) == [], (
        "MediaPart declares a bytes-bearing field — media must travel as a "
        "workspace reference, never as bytes through the queue or session log"
    )

    payload = b"\x89PNG\r\n\x1a\n"
    try:
        part = MediaPart(
            kind="image",
            mime="image/png",
            declared_name="holiday.png",
            ref="inbox/2026-08-11/143000-op-holiday.png",
            data=payload,
        )
    except ValidationError:
        return  # bytes rejected outright — the strongest form of the property

    assert not hasattr(part, "data"), "MediaPart retained smuggled bytes"
    assert payload not in part.model_dump().values(), "bytes reached the serialised part"


def test_envelope_never_carries_bytes() -> None:
    """The whole envelope is byte-free, so nothing large enters the queue."""
    assert _bytes_bearing_fields(InboundEvent) == []


def test_envelope_keeps_the_routing_identity() -> None:
    """Routing identity is unchanged by the arrival of ``parts``."""
    message = _envelope(
        thread_id="t-9",
        parts=[TextPart(text="hi")],
        raw_payload={"update_id": 1},
    )

    assert message.platform == "telegram"
    assert message.chat_id == "42"
    assert message.thread_id == "t-9"
    assert message.user_did == "did:arc:user:x"
    assert message.agent_did == "did:arc:agent:y"
    assert message.session_key == "sess-1"
    assert message.raw_payload == {"update_id": 1}


def test_an_envelope_needs_words_or_parts() -> None:
    """``message`` is required, so no caller can construct a contentless envelope."""
    with pytest.raises(ValidationError):
        InboundEvent(**_ROUTING)  # type: ignore[arg-type]  # reason: the missing field is the point


def test_supplying_both_views_inconsistently_reconciles_rather_than_diverging() -> None:
    """Both fields supplied, disagreeing: ``parts`` wins — they never diverge.

    The original validator reconciled only when one side was MISSING, so a
    caller passing both inconsistently kept two contradictory views of one
    message. That is the failure its own docstring warns about: a media
    message delivered as its caption alone. Nothing in production hit it,
    because every call site happened to pass consistent values — the
    invariant held by convention, which is not the same as being enforced.
    """
    from arcgateway.executor import InboundEvent

    event = InboundEvent(
        platform="telegram",
        chat_id="42",
        user_did="did:arc:user:a",
        agent_did="did:arc:agent:b",
        message="a stale caption",
        parts=[TextPart(text="the real text")],
    )

    assert event.message == "the real text", (
        "message and parts diverged: the richer view (parts) must win, or a "
        "media message can be delivered as its caption alone"
    )


def test_dropping_a_divergent_message_is_logged_not_silent(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Reconciling discards the caller's text — say so.

    Deriving ``message`` from ``parts`` is right, but it throws away words the
    parts never held. A caption that vanishes with no trace is far harder to
    diagnose from the symptom than from a warning naming the event, so the
    degrade is loud. Refusing the whole message would cost the turn instead of
    one field, which is the worse trade on an inbound path.
    """
    from arcgateway.executor import InboundEvent

    with caplog.at_level("WARNING", logger="arcgateway.executor"):
        InboundEvent(
            platform="telegram",
            chat_id="42",
            user_did="did:arc:user:a",
            agent_did="did:arc:agent:b",
            message="a caption no part holds",
            parts=[
                MediaPart(
                    kind="image",
                    mime="image/png",
                    declared_name="holiday.png",
                    ref="inbox/2026-08-11/143000-op-holiday.png",
                )
            ],
        )

    assert "a caption no part holds" in caplog.text, (
        "the dropped text must be named in the log, or it vanishes without trace"
    )


def test_consistent_views_reconcile_without_a_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The production path supplies both, consistently — it must stay quiet.

    Every in-tree producer passes ``message=flatten_text(parts)``. If that
    tripped the warning, the log would fill on every media message and the
    signal would be worthless.
    """
    from arcgateway.executor import InboundEvent
    from arcgateway.parts import flatten_text

    parts = [
        TextPart(text="look"),
        MediaPart(
            kind="image",
            mime="image/png",
            declared_name="chart.png",
            ref="inbox/2026-08-11/143000-op-chart.png",
        ),
    ]

    with caplog.at_level("WARNING", logger="arcgateway.executor"):
        InboundEvent(
            platform="telegram",
            chat_id="42",
            user_did="did:arc:user:a",
            agent_did="did:arc:agent:b",
            message=flatten_text(parts),
            parts=parts,
        )

    assert not caplog.text, f"the consistent path warned: {caplog.text!r}"


def test_a_media_events_text_projection_names_the_artefact() -> None:
    """The flattened text names the file, so no surface sees only the caption."""
    from arcgateway.executor import InboundEvent

    event = InboundEvent(
        platform="telegram",
        chat_id="42",
        user_did="did:arc:user:a",
        agent_did="did:arc:agent:b",
        message="look at this",
        parts=[
            TextPart(text="look at this"),
            MediaPart(
                kind="image",
                mime="image/png",
                declared_name="chart.png",
                ref="inbox/2026-08-11/143000-op-chart.png",
            ),
        ],
    )

    assert "look at this" in event.message
    assert "chart.png" in event.message, "the text projection drops the artefact"
