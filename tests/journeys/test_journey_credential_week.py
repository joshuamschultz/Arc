"""P18-2 §8.8 — an Arc-held OAuth connection stays alive for a week, untouched (G14).

Dropbox-shaped ``[oauth]`` fake; P18-3 re-points this at Google. The proactive
renewer and two agent processes race over one custody row while the clock walks
8 days in 15-minute steps. Every tool call sees a valid token, renewals land about
every 45 minutes (75 % of an hour), no refresh token is presented twice, the
operator is never asked for anything, and the card stays healthy.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from arcagent.extension.connection_health import StoreHealthReporter
from arcagent.extension.credential_broker import (
    AccessTokenBroker,
    AccessTokenHandle,
    CredentialPlan,
)
from arcagent.extension.credentials import RefreshRequest, RenewalPlanner, RenewedCredential
from arcagent.extension.custody import CredentialRowStore
from arcagent.extension.grants import Connection, ConnectionRegistry
from arcagent.extension.manifest import OAuthFlow
from arcagent.extension.secrets import Secret
from arcagent.extension.state import ConnectionRecord, ConnectionStateStore
from arcstore.backends.memory import FakeBackend

from packages.arcagent.tests.custody_fakes import make_cipher

FLOW = OAuthFlow(
    authorize_url="https://auth.example/authorize",
    token_url="https://auth.example/token",
    client_id_secret="app_key",
    client_secret_secret="app_secret",
    refresh_token_secret="refresh_token",
)
PLAN = CredentialPlan(
    oauth=FLOW,
    bearer_field=None,
    handle_fields=frozenset(),
    withheld=frozenset({"refresh_token", "app_secret"}),
)


class Week:
    def __init__(self, arc_dir: Path) -> None:
        self.backend = FakeBackend()
        self.now = datetime(2026, 10, 2, tzinfo=UTC)
        self.state = ConnectionStateStore(self.backend)
        self.arc_dir = arc_dir
        self.presented: list[str] = []
        self.expiry: dict[str, datetime] = {}

    def clock(self) -> datetime:
        return self.now

    async def opener(self) -> Any:
        return self.backend

    async def provider(self, request: RefreshRequest) -> RenewedCredential:
        token = request.refresh_token.reveal()
        assert token not in self.presented, "a refresh token was presented twice"
        self.presented.append(token)
        access = f"a{len(self.presented)}"
        self.expiry[access] = self.now + timedelta(hours=1)
        return RenewedCredential(
            access_token=Secret(access),
            expires_in=3600,
            refresh_token=Secret(f"r{len(self.presented)}"),
        )

    def rows(self) -> CredentialRowStore:
        return CredentialRowStore(self.backend, make_cipher(), clock=self.clock)

    def planner(self, owner: str) -> RenewalPlanner:
        async def no_sleep(_seconds: float) -> None:
            await asyncio.sleep(0)

        return RenewalPlanner(
            rows=self.rows(),
            refresh=self.provider,
            health=StoreHealthReporter(self.opener),
            owner_id=owner,
            state=self.state,
            clock=self.clock,
            sleep=no_sleep,
        )

    def handle(self, agent: str) -> AccessTokenHandle:
        did = f"did:arc:agent:{agent}"
        broker = AccessTokenBroker(
            self.rows(),
            registry=lambda: ConnectionRegistry(self.arc_dir),
            renewals=self.planner(f"proc-{agent}"),
            health=StoreHealthReporter(self.opener),
            clock=self.clock,
            bound_agent=agent,
            bound_did=did,
        )
        return broker.handle("blackarc", agent=agent, agent_did=did, plan=PLAN)


async def test_google_shaped_oauth_connection_renews_for_a_simulated_week(
    tmp_path: Path,
) -> None:
    week = Week(tmp_path / "arc")
    await week.state.create(
        ConnectionRecord(connection="blackarc", custody="arc"), actor_did="did:arc:op"
    )
    await week.rows().put_fields(
        "blackarc", {"app_key": "k", "app_secret": "s", "refresh_token": "r0"}, actor_did="op"
    )
    ConnectionRegistry(week.arc_dir).define(
        "blackarc", Connection(extension="box", approval="auto", agents=("josh", "olivia"))
    )
    renewer = week.planner("arcui-renewer")
    agents = [week.handle("josh"), week.handle("olivia")]

    for _step in range(8 * 24 * 4):
        results = await asyncio.gather(
            renewer.ensure_fresh("blackarc", flow=FLOW),
            *(agent.bearer() for agent in agents),
            return_exceptions=True,
        )
        for value in results:
            assert not isinstance(value, BaseException), value
        for token in results[1:]:
            assert isinstance(token, Secret)
            assert week.expiry[token.reveal()] > week.now, "a tool call saw an expired token"
        record = await week.state.get("blackarc")
        assert record is not None and record.status in ("unknown", "healthy")
        week.now += timedelta(minutes=15)

    assert 240 <= len(week.presented) <= 260  # one renewal per ~45 minutes over 8 days
    record = await week.state.get("blackarc")
    assert record is not None
    assert record.status == "healthy" and record.notice_seq == 0
