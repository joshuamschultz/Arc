"""Unit tests for the read-only team stream (SPEC-031 F1/F2).

``TeamStreamHub`` is a pure *view* fan-out: it renders arcteam flows for the
browser and never routes, signs, or coordinates. These tests pin the three
guarantees the spec cares about:

* handles are rendered, never raw DIDs (REQ-062);
* ``@mentions`` are surfaced as a distinct list (REQ-062);
* fan-out reaches every subscribed socket, scoped by channel (REQ-060).
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from arcui.team_stream import (
    TeamBusObserver,
    TeamStreamHub,
    default_handle_of,
    render_team_frame,
)


class _FakeWS:
    """Minimal socket stand-in — the hub only needs an identity key."""


def _msg(
    *,
    sender: str,
    channel: str,
    body: str,
    mentions: list[str] | None = None,
    seq: int = 0,
    action_required: bool = False,
) -> dict[str, Any]:
    return {
        "seq": seq,
        "id": f"msg-{seq}",
        "ts": "2026-07-05T00:00:00Z",
        "sender": sender,
        "to": [f"channel://{channel}"],
        "body": body,
        "mentions": mentions or [],
        "action_required": action_required,
        "priority": "high" if mentions else "normal",
    }


# ── handle rendering ───────────────────────────────────────────────────────


class TestDefaultHandleOf:
    def test_strips_uri_scheme(self) -> None:
        assert default_handle_of("agent://intake") == "intake"

    def test_strips_did_to_last_segment(self) -> None:
        assert default_handle_of("did:arc:local:agent/architect") == "architect"

    def test_strips_leading_at(self) -> None:
        assert default_handle_of("@builder") == "builder"

    def test_bare_handle_passthrough(self) -> None:
        assert default_handle_of("intake") == "intake"


class TestRenderTeamFrame:
    def test_renders_handle_not_did(self) -> None:
        frame = render_team_frame(
            _msg(sender="did:arc:local:agent/intake", channel="c1", body="hi")
        )
        assert frame["from"] == "intake"
        assert "did:" not in frame["from"]

    def test_mentions_rendered_as_distinct_handle_list(self) -> None:
        frame = render_team_frame(
            _msg(
                sender="agent://intake",
                channel="c1",
                body="ping @architect",
                mentions=["did:arc:local:agent/architect"],
            )
        )
        assert frame["mentions"] == ["architect"]
        assert all("did:" not in m for m in frame["mentions"])

    def test_channel_extracted_from_to(self) -> None:
        frame = render_team_frame(_msg(sender="agent://intake", channel="access", body="x"))
        assert frame["channel"] == "access"

    def test_frame_type_is_team_message(self) -> None:
        frame = render_team_frame(_msg(sender="agent://intake", channel="c", body="x"))
        assert frame["type"] == "team_message"

    def test_no_did_anywhere_in_frame(self) -> None:
        frame = render_team_frame(
            _msg(
                sender="did:arc:local:agent/intake",
                channel="c",
                body="x",
                mentions=["did:arc:local:agent/architect"],
            )
        )
        import json

        assert "did:" not in json.dumps(frame)


# ── fan-out ────────────────────────────────────────────────────────────────


class TestTeamStreamHub:
    async def test_publish_fans_out_to_all_unscoped_sockets(self) -> None:
        hub = TeamStreamHub()
        a, b = _FakeWS(), _FakeWS()
        hub.register(a)
        hub.register(b)
        breakdown = await hub.publish({"type": "team_message", "channel": "c1", "body": "hi"})
        assert breakdown["delivered"] == 2
        assert hub.queue_for(a).get_nowait()["body"] == "hi"
        assert hub.queue_for(b).get_nowait()["body"] == "hi"

    async def test_channel_scoped_socket_only_gets_its_channel(self) -> None:
        hub = TeamStreamHub()
        scoped, everyone = _FakeWS(), _FakeWS()
        hub.register(scoped, channels={"ops"})
        hub.register(everyone)
        await hub.publish({"type": "team_message", "channel": "hr", "body": "n"})
        assert hub.queue_for(scoped).empty()
        assert hub.queue_for(everyone).get_nowait()["channel"] == "hr"

    async def test_unregister_removes_socket(self) -> None:
        hub = TeamStreamHub()
        a = _FakeWS()
        hub.register(a)
        hub.unregister(a)
        breakdown = await hub.publish({"type": "team_message", "channel": "c", "body": "x"})
        assert breakdown["delivered"] == 0

    async def test_late_joiner_replays_recent_frames(self) -> None:
        hub = TeamStreamHub()
        await hub.publish({"type": "team_message", "channel": "c1", "body": "earlier"})
        late = _FakeWS()
        hub.register(late)  # joins after the frame was published
        assert hub.queue_for(late).get_nowait()["body"] == "earlier"

    async def test_replay_respects_channel_scope(self) -> None:
        hub = TeamStreamHub()
        await hub.publish({"type": "team_message", "channel": "hr", "body": "hr-only"})
        await hub.publish({"type": "team_message", "channel": "ops", "body": "ops-only"})
        scoped = _FakeWS()
        hub.register(scoped, channels={"ops"})
        q = hub.queue_for(scoped)
        assert q.get_nowait()["body"] == "ops-only"
        assert q.empty()

    async def test_backpressure_drops_oldest(self) -> None:
        hub = TeamStreamHub(queue_maxsize=1)
        a = _FakeWS()
        hub.register(a)
        await hub.publish({"type": "team_message", "channel": "c", "body": "first"})
        breakdown = await hub.publish({"type": "team_message", "channel": "c", "body": "second"})
        assert breakdown["dropped_backpressure"] == 1
        # Only the newest frame survives the 1-slot queue.
        assert hub.queue_for(a).get_nowait()["body"] == "second"


# ── observer (read-only bus subscription) ──────────────────────────────────


class _FakeChannel:
    def __init__(self, name: str) -> None:
        self.name = name


class _FakeMessage:
    def __init__(self, data: dict[str, Any]) -> None:
        self._data = data
        self.seq = data["seq"]

    def model_dump(self, *, mode: str = "python") -> dict[str, Any]:
        return dict(self._data)


class _FakeService:
    """Duck-typed MessagingService: only the read surface the observer uses."""

    def __init__(self, channels: dict[str, list[dict[str, Any]]]) -> None:
        self._channels = channels

    async def list_channels(self) -> list[_FakeChannel]:
        return [_FakeChannel(n) for n in self._channels]

    async def list_channel_messages(
        self, channel_name: str, after_seq: int = 0, limit: int = 100
    ) -> list[_FakeMessage]:
        return [
            _FakeMessage(m) for m in self._channels[channel_name] if m["seq"] > after_seq
        ]


class TestTeamBusObserver:
    async def test_poll_once_publishes_rendered_frames(self) -> None:
        service = _FakeService(
            {
                "ops": [
                    _msg(sender="agent://intake", channel="ops", body="one", seq=1),
                    _msg(
                        sender="did:arc:local:agent/architect",
                        channel="ops",
                        body="two @intake",
                        mentions=["did:arc:local:agent/intake"],
                        seq=2,
                    ),
                ]
            }
        )
        hub = TeamStreamHub()
        sock = _FakeWS()
        hub.register(sock)
        observer = TeamBusObserver(service, hub)

        published = await observer.poll_once()
        assert published == 2

        q = hub.queue_for(sock)
        f1 = q.get_nowait()
        f2 = q.get_nowait()
        assert f1["from"] == "intake"
        assert f2["mentions"] == ["intake"]
        assert "did:" not in f2["from"]

    async def test_poll_once_is_incremental(self) -> None:
        channels: dict[str, list[dict[str, Any]]] = {
            "ops": [_msg(sender="agent://intake", channel="ops", body="one", seq=1)]
        }
        service = _FakeService(channels)
        hub = TeamStreamHub()
        hub.register(_FakeWS())
        observer = TeamBusObserver(service, hub)

        assert await observer.poll_once() == 1
        # No new messages ⇒ nothing re-published.
        assert await observer.poll_once() == 0
        channels["ops"].append(
            _msg(sender="agent://intake", channel="ops", body="two", seq=2)
        )
        assert await observer.poll_once() == 1

    async def test_run_survives_a_failing_poll(self) -> None:
        class _Boom(_FakeService):
            def __init__(self) -> None:
                super().__init__({})
                self.calls = 0

            async def list_channels(self) -> list[_FakeChannel]:
                self.calls += 1
                raise RuntimeError("bus down")

        service = _Boom()
        observer = TeamBusObserver(service, TeamStreamHub())
        task = asyncio.create_task(observer.run(interval=0.001))
        await asyncio.sleep(0.02)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        # The loop kept trying rather than dying on the first exception.
        assert service.calls >= 1
