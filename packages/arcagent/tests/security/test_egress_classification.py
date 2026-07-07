"""SPEC-038 REQ-025 — egress no-exfil (Bell-LaPadula, external ceiling)."""

from __future__ import annotations

from typing import Any

import pytest

from arcagent.tools._egress import EgressClassificationDenied, EgressProxy


class _Resp:
    status_code = 200


async def _send(url: str, method: str, **_kwargs: Any) -> _Resp:
    return _Resp()


class TestNoExfil:
    async def test_secret_data_to_unclassified_destination_refused(self) -> None:
        proxy = EgressProxy(allowlist={"https://api.example.com"}, send_fn=_send)
        with pytest.raises(EgressClassificationDenied):
            await proxy.request(
                "https://api.example.com/x", method="POST", data_classification="SECRET"
            )

    async def test_unclassified_data_proceeds(self) -> None:
        proxy = EgressProxy(allowlist={"https://api.example.com"}, send_fn=_send)
        resp = await proxy.request("https://api.example.com/x", method="POST")
        assert resp.status_code == 200

    async def test_cleared_origin_override_allows(self) -> None:
        proxy = EgressProxy(
            allowlist={"https://secure.example.com"},
            send_fn=_send,
            origin_clearances={"https://secure.example.com": "SECRET"},
        )
        resp = await proxy.request(
            "https://secure.example.com/x", method="POST", data_classification="SECRET"
        )
        assert resp.status_code == 200

    async def test_refusal_is_audited(self) -> None:
        events: list[tuple[str, dict]] = []
        proxy = EgressProxy(
            allowlist={"https://api.example.com"},
            send_fn=_send,
            audit_sink=lambda e, p: events.append((e, p)),
        )
        with pytest.raises(EgressClassificationDenied):
            await proxy.request(
                "https://api.example.com/x", method="POST", data_classification="SECRET"
            )
        assert any(e == "egress.classification_refused" for e, _ in events)


class TestSessionDataClassifier:
    """SPEC-038 F2 — the data label is RESOLVED from the session, not hardcoded.

    The security-review defect was that no caller ever passed
    ``data_classification`` so the gate always saw UNCLASSIFIED and always
    allowed. The proxy now defaults the label from the injected resolver.
    """

    async def test_resolved_secret_read_refuses_egress_without_explicit_label(self) -> None:
        # A session that has read SECRET data — resolver reports it; the caller
        # omits data_classification entirely (the real dynamic-tool path).
        proxy = EgressProxy(
            allowlist={"https://api.example.com"},
            send_fn=_send,
            data_classifier=lambda: "SECRET",
        )
        with pytest.raises(EgressClassificationDenied):
            await proxy.request("https://api.example.com/x", method="POST")

    async def test_resolved_unclassified_session_allows(self) -> None:
        proxy = EgressProxy(
            allowlist={"https://api.example.com"},
            send_fn=_send,
            data_classifier=lambda: "UNCLASSIFIED",
        )
        resp = await proxy.request("https://api.example.com/x", method="POST")
        assert resp.status_code == 200


pytestmark = pytest.mark.asyncio
