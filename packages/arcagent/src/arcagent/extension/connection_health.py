"""Connection health authority — one durable, truthful status per connection (P18-1).

Every connection has ONE health record in the ``connections`` collection. Five
kinds of writer may change it (scheduled probe, sync run end, credential
lifecycle, operator action, tool-contract ledger) and every one of them goes
through :func:`next_health` — the single pure state machine — and one
compare-and-set on ``revision``. A page view reads the record and nothing else,
so it makes no secret read and spawns no process.

A transition into ``needs_you`` or ``error`` earns exactly one operator notice.
The notice is claimed with a lease on the record and finished durably, so a
restart, a second process, or five agents reporting the same failure send one,
never five. The protocol is a compare-and-set on ``revision`` everywhere, which
both arcstore backends perform as a single atomic statement; no session lock a
crashed process could hold is involved.

Module layout: the data types live in :mod:`arcagent.extension.state` (the store
needs them and must not import this module); this module owns the rules and the
only writer.
"""

from __future__ import annotations

import logging
import random
import re
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Final, Literal, Protocol

from arctrust.audit import AuditEvent, AuditSink, emit
from arctrust.secrets import SECRET_PATTERNS

from arcagent.extension.manifest import ExtensionManifest, HealthProbe
from arcagent.extension.source import SourceFailureCode, classify_cli_failure
from arcagent.extension.state import (
    ConnectionAction,
    ConnectionRecord,
    ConnectionStateStore,
    ConnectionStatus,
    CredentialCustody,
    ReasonCode,
)

_logger = logging.getLogger("arcagent.extension.connection_health")

#: Actor for the probe loop and notice delivery — never the page view in flight.
PROBE_DID = "did:arc:system:connection-health"

NoticeKind = Literal["needs_you", "error", "recovered"]
SignalSource = Literal["probe", "sync", "credential", "operator", "contract"]

#: Counted failures escalate to ``error`` only past BOTH bounds: enough failures
#: and enough elapsed time. The time floor stops N agents failing in the same
#: minute from tripping the ceiling in one cycle.
ERROR_FAILURE_CEILING: Final = 3
ERROR_MIN_DURATION: Final = timedelta(minutes=10)
#: Or, with no success at all for this long, the first failure is already an error.
ERROR_NO_SUCCESS_WINDOW: Final = timedelta(hours=24)

NOTICE_CLAIM_TTL: Final = timedelta(minutes=2)
NOTICE_MAX_ATTEMPTS: Final = 3
NOTICE_RETRY_BACKOFF: Final = (timedelta(minutes=1), timedelta(minutes=5))
#: A flapping connection must not turn the operator's phone into a siren.
NOTICE_HOURLY_CAP: Final = 6
_NOTICE_WINDOW: Final = timedelta(hours=1)

_MAX_REASON_CHARS = 200


@dataclass(frozen=True)
class ReasonSpec:
    """How one reason code behaves: its class, resulting status/action, and wording."""

    kind: Literal["terminal", "sticky", "counted"]
    status: ConnectionStatus
    action: ConnectionAction
    template: str


#: The single source of status, action and plain-language reason. ``{provider}``
#: is the bundle's display name; ``{detail}`` the redacted provider text.
REASONS: Final[Mapping[ReasonCode, ReasonSpec]] = {
    "auth_required": ReasonSpec(
        "terminal", "needs_you", "reconnect", "{provider} sign-in expired or was revoked"
    ),
    "invalid_grant": ReasonSpec(
        "terminal", "needs_you", "reconnect", "{provider} sign-in expired or was revoked"
    ),
    "consent_required": ReasonSpec(
        "terminal", "needs_you", "reconnect", "{provider} needs you to approve access again"
    ),
    "token_revoked": ReasonSpec(
        "terminal", "needs_you", "reconnect", "{provider} access was revoked"
    ),
    "credential_missing": ReasonSpec("terminal", "needs_you", "reconnect", "Not connected yet"),
    "credential_unreadable": ReasonSpec(
        "terminal",
        "needs_you",
        "reconnect",
        "The stored credential could not be read; connect again",
    ),
    "scope_missing": ReasonSpec(
        "terminal", "needs_you", "reconnect", "Reconnect {provider} and allow {detail}"
    ),
    "account_mismatch": ReasonSpec(
        "terminal", "needs_you", "reconnect", "Signed in as a different account than {detail}"
    ),
    "token_expiring": ReasonSpec(
        "terminal", "needs_you", "reconnect", "Token expires {detail}; paste a new one"
    ),
    "contract_changed": ReasonSpec(
        "sticky", "needs_you", "approve", "{detail} tool(s) changed; approve them"
    ),
    "host_missing": ReasonSpec(
        "terminal", "needs_you", "install_host", "{detail} is not installed on this computer"
    ),
    "renewer_unavailable": ReasonSpec(
        "counted", "error", "wait", "Credential renewal is not running"
    ),
    "provider_unavailable": ReasonSpec(
        "counted", "error", "wait", "{provider} is not answering: {detail}"
    ),
    "rate_limited": ReasonSpec("counted", "error", "wait", "{provider} is rate-limiting Arc"),
    "sync_failed": ReasonSpec("counted", "error", "wait", "Sync keeps failing: {detail}"),
    "repeated_failures": ReasonSpec(
        "counted", "error", "wait", "Failing for {duration}: {detail}"
    ),
}

#: Reasons that mean the credential itself is dead. An Arc-held credential in one of
#: these is not probed again until its custody generation changes.
AUTH_REASONS: Final[frozenset[str]] = frozenset(
    {"auth_required", "invalid_grant", "consent_required", "token_revoked"}
)


def effective_probe(manifest: ExtensionManifest) -> HealthProbe | None:
    """The probe to run: the declared one, else the honest default for the bundle's shape.

    A bundle shipped in this repo always declares ``[health]`` (an architecture test
    holds that). A third-party bundle may not, and "Not checked yet" forever is not
    an answer, so: a non-CLI attachment's own ``probe()``, or the host sign-in
    check. A bare CLI with neither has nothing that proves an account works (its
    ``probe()`` is ``--version``), so it stays unchecked rather than guessed healthy.
    """
    if manifest.health is not None:
        return manifest.health
    if manifest.extension.attachment != "cli":
        return HealthProbe(probe="attachment")
    if any(required.verify_command for required in manifest.host_requires):
        return HealthProbe(probe="host_verify")
    return None


def custody_of(manifest: ExtensionManifest) -> CredentialCustody:
    """Who holds this bundle's credential, read from the manifest and nothing else.

    ``arc``: Arc stores it (an OAuth flow, or declared sensitive secrets on a
    non-CLI bundle). ``host``: the vendor binary's own store holds it. ``none``: no
    credential at all.
    """
    if manifest.oauth is not None:
        return "arc"
    if manifest.extension.attachment == "cli" and manifest.host_requires:
        return "host"
    return "arc" if any(secret.sensitive for secret in manifest.secrets) else "none"


#: Producer codes that name a reason directly (the column "input" of design 1.2).
_CODE_ALIASES: Final[Mapping[str, ReasonCode]] = {
    SourceFailureCode.AUTH_REQUIRED.value: "auth_required",
    SourceFailureCode.RATE_LIMITED.value: "rate_limited",
    "interaction_required": "consent_required",
    "sync_stalled": "sync_failed",
    "sync_error": "sync_failed",
    "lease_lost": "sync_failed",
    "credential_missing": "credential_missing",
}

_TOKEN_REVOKED_MARKERS = ("token_revoked", "invalid_auth", "account_inactive")
_SYNC_MARKERS = (
    "sync_stalled",
    "leaselost",
    "lease lost",
    "syncerror",
    "sync_error",
    "time limit exceeded",
)
_URL = re.compile(r"https?://\S+")
_QUERY = re.compile(r"\?[^\s]*")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]+")


def classify(code: str | None, text: str) -> ReasonCode:
    """Map any existing producer's code and text onto the closed reason table.

    The fall-through is **counted**, not terminal: an unknown failure escalates
    after a bound but one unknown blip never pages.
    """
    if code is not None:
        lowered_code = code.lower()
        if lowered_code in REASONS:
            return lowered_code  # type: ignore[return-value] # reason: membership proves the Literal
        alias = _CODE_ALIASES.get(lowered_code)
        if alias is not None:
            return alias
    lowered = text.lower()
    if any(marker in lowered for marker in _TOKEN_REVOKED_MARKERS):
        return "token_revoked"
    if any(marker in lowered for marker in _SYNC_MARKERS):
        return "sync_failed"
    verdict = classify_cli_failure(text)
    if verdict is SourceFailureCode.AUTH_REQUIRED:
        return "auth_required"
    if verdict is SourceFailureCode.RATE_LIMITED:
        return "rate_limited"
    return "provider_unavailable"


def _clean_detail(text: str) -> str:
    """Make provider text safe for a browser, a chat message and the audit chain.

    OAuth error URLs carry codes and tokens in their queries, and a hostile
    provider can put anything in an error body, so: control characters and
    newlines go, whole URLs go, secret shapes are redacted, and ``@`` is dropped
    so no chat mention syntax survives.
    """
    cleaned = _CONTROL.sub(" ", text)
    cleaned = _URL.sub("[link]", cleaned)
    cleaned = _QUERY.sub("?…", cleaned)
    for _name, pattern in SECRET_PATTERNS:
        cleaned = pattern.sub("[redacted]", cleaned)
    cleaned = cleaned.replace("@", "")
    return " ".join(cleaned.split())


def reason_text(
    code: ReasonCode, *, provider: str = "", detail: str = "", duration: str = ""
) -> str:
    """The plain, redacted, bounded sentence the card and the notice show."""
    spec = REASONS[code]
    rendered = spec.template.format(
        provider=_clean_detail(provider) or "The service",
        detail=_clean_detail(detail) or "an error",
        duration=duration or "a while",
    )
    return rendered[:_MAX_REASON_CHARS]


def action_label(action: ConnectionAction, *, provider: str = "", detail: str = "") -> str:
    """The one primary button's wording, computed server side."""
    if action == "reconnect":
        return f"Reconnect {provider}".strip()
    if action == "approve":
        return "Approve changed tools"
    if action == "install_host":
        return f"Install {detail}".strip() if detail else "Show install steps"
    return ""


@dataclass(frozen=True)
class HealthSignal:
    """One observation about a connection, from one of the five writers."""

    ok: bool
    source: SignalSource
    checked_by: str
    reason_code: ReasonCode | None = None
    detail: str = ""
    provider: str = ""
    credential_generation: int | None = None

    def __post_init__(self) -> None:
        if not self.ok and self.reason_code is None:
            raise ValueError("a failing signal names its reason_code")


@dataclass(frozen=True)
class HealthTransition:
    """What one signal did to the record."""

    before: ConnectionStatus
    after: ConnectionStatus
    before_action: ConnectionAction
    after_action: ConnectionAction
    seq: int
    notice: NoticeKind | None

    @property
    def changed(self) -> bool:
        return (self.before, self.before_action) != (self.after, self.after_action)


class HealthReporter(Protocol):
    """What a writer outside the authority holds: report a signal, never raise.

    ``statuses`` is the cheap bulk read a sync loop needs to notice that an
    operator reconnected a connection it had backed off. It returns an empty
    mapping when the store is unreachable, which a caller reads as "unknown", not
    as "healthy".
    """

    async def report(self, connection: str, signal: HealthSignal) -> None: ...

    async def statuses(self) -> dict[str, ConnectionStatus]: ...


# --- the state machine ------------------------------------------------------


def parse_time(raw: str) -> datetime:
    """An ISO timestamp as an aware UTC datetime."""
    parsed = datetime.fromisoformat(raw)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _humanize(span: timedelta) -> str:
    minutes = int(span.total_seconds() // 60)
    if minutes < 60:
        return f"{max(minutes, 1)} min"
    if minutes < 60 * 48:
        return f"{minutes // 60} h"
    return f"{minutes // (60 * 24)} days"


def next_health(
    record: ConnectionRecord, signal: HealthSignal, now: datetime
) -> tuple[dict[str, Any], HealthTransition]:
    """Pure: the CAS patch and the transition one signal produces. No I/O, no clock."""
    stamp = now.isoformat()
    patch: dict[str, Any] = {
        "revision": record.revision + 1,
        "last_checked_at": stamp,
        "checked_by": signal.checked_by,
        "updated_at": stamp,
    }
    if signal.credential_generation is not None:
        patch["credential_generation"] = signal.credential_generation
    if signal.ok:
        patch.update(_on_success(record, signal, stamp))
    else:
        patch.update(_on_failure(record, signal, now))
    return _with_transition(record, signal, patch)


def _sticky_hold(record: ConnectionRecord, signal: HealthSignal) -> bool:
    """A changed tool contract is not fixed by the credential working."""
    return (
        record.status == "needs_you"
        and record.reason_code is not None
        and REASONS[record.reason_code].kind == "sticky"
        and signal.source not in ("contract", "operator")
    )


def _on_success(record: ConnectionRecord, signal: HealthSignal, stamp: str) -> dict[str, Any]:
    cleared: dict[str, Any] = {
        "consecutive_failures": 0,
        "failing_since": None,
        "last_success_at": stamp,
    }
    if _sticky_hold(record, signal):
        return cleared
    return {
        **cleared,
        "status": "healthy",
        "reason_code": None,
        "reason_text": None,
        "action": "none",
    }


def _reason_of(signal: HealthSignal) -> ReasonCode:
    """The failing signal's reason; ``HealthSignal`` refuses to exist without one."""
    if signal.reason_code is None:
        raise ValueError("a failing signal names its reason_code")
    return signal.reason_code


def _on_failure(record: ConnectionRecord, signal: HealthSignal, now: datetime) -> dict[str, Any]:
    spec = REASONS[_reason_of(signal)]
    since = record.failing_since or now.isoformat()
    counters: dict[str, Any] = {
        "consecutive_failures": record.consecutive_failures + 1,
        "failing_since": since,
    }
    if spec.kind != "counted":
        return {**counters, **_settled(signal, spec, now, since)}
    if record.status == "needs_you":
        return counters
    # An operator's own check is a deliberate look at this moment, not one sample of
    # many to average: "Check now" that answers green while the provider is down is
    # the lie this authority exists to stop. (It sends no notice; they are watching.)
    if (
        record.status == "error"
        or signal.source == "operator"
        or _past_error_ceiling(record, counters, now, since)
    ):
        return {**counters, **_settled(signal, spec, now, since)}
    return counters


def _settled(signal: HealthSignal, spec: ReasonSpec, now: datetime, since: str) -> dict[str, Any]:
    code = _reason_of(signal)
    return {
        "status": spec.status,
        "reason_code": code,
        "reason_text": reason_text(
            code,
            provider=signal.provider,
            detail=signal.detail,
            duration=_humanize(now - parse_time(since)),
        ),
        "action": spec.action,
    }


def _past_error_ceiling(
    record: ConnectionRecord, counters: dict[str, Any], now: datetime, since: str
) -> bool:
    """True once a counted failure has lasted long enough, or success is long gone."""
    if (
        counters["consecutive_failures"] >= ERROR_FAILURE_CEILING
        and now - parse_time(since) >= ERROR_MIN_DURATION
    ):
        return True
    baseline = record.last_success_at or record.created_at
    return baseline is not None and now - parse_time(baseline) >= ERROR_NO_SUCCESS_WINDOW


def _with_transition(
    record: ConnectionRecord, signal: HealthSignal, patch: dict[str, Any]
) -> tuple[dict[str, Any], HealthTransition]:
    status: ConnectionStatus = patch.get("status", record.status)
    action: ConnectionAction = patch.get("action", record.action)
    changed = (status, action) != (record.status, record.action)
    seq = record.transition_seq + 1 if changed else record.transition_seq
    if changed:
        patch["transition_seq"] = seq
    notice = _notice_for(record, signal, status, changed)
    if notice is not None:
        patch["notice_seq"] = seq
    return patch, HealthTransition(
        before=record.status,
        after=status,
        before_action=record.action,
        after_action=action,
        seq=seq,
        notice=notice,
    )


def _outage_was_reported(record: ConnectionRecord) -> bool:
    last = record.last_notice
    return last is not None and last.delivered and last.kind != "recovered"


def _notice_for(
    record: ConnectionRecord, signal: HealthSignal, status: ConnectionStatus, changed: bool
) -> NoticeKind | None:
    """The operator is told about what they did not cause, and about what they were told."""
    if not changed or signal.source == "operator":
        return None
    if status == "needs_you":
        return "needs_you"
    if status == "error":
        return "error"
    if (
        status == "healthy"
        and record.status in ("needs_you", "error")
        and record.notified_seq > 0
        and _outage_was_reported(record)
    ):
        return "recovered"
    return None


# --- scheduling -------------------------------------------------------------


def probe_interval(status: ConnectionStatus) -> tuple[timedelta, timedelta]:
    """(base interval, max jitter) for the next probe of a connection in ``status``."""
    if status == "needs_you":
        return timedelta(minutes=5), timedelta(seconds=60)
    if status == "error":
        return timedelta(minutes=10), timedelta(seconds=60)
    return timedelta(minutes=30), timedelta(minutes=5)


def next_check_time(
    status: ConnectionStatus, now: datetime, rng: Callable[[], float] = random.random
) -> datetime:
    base, spread = probe_interval(status)
    return now + base + spread * rng()


# --- notices ---------------------------------------------------------------


@dataclass(frozen=True)
class PendingNotice:
    """A notice this caller holds the lease to deliver."""

    connection: str
    seq: int
    kind: NoticeKind
    attempt: int
    owner: str
    reason_text: str
    action: ConnectionAction

    @property
    def idempotency_key(self) -> str:
        return f"connection-health:{self.connection}:{self.seq}"


def notice_text(pending: PendingNotice, *, ui_base: str = "") -> str:
    """One plain line. Never tokens, codes, provider URLs or an account email."""
    link = f" {ui_base.rstrip('/')}/connections?focus={pending.connection}" if ui_base else ""
    if pending.kind == "recovered":
        return f"Connection '{pending.connection}' is working again."
    if pending.kind == "error":
        return (
            f"Connection '{pending.connection}' is failing: {pending.reason_text}. "
            f"Arc keeps retrying.{link}"
        )
    label = action_label(pending.action)
    click = f' and click "{label}"' if label else ""
    return (
        f"Connection '{pending.connection}' needs you: {pending.reason_text}. "
        f"Open Connections in ArcUI{click}.{link}"
    )


def _notice_kind(record: ConnectionRecord) -> NoticeKind | None:
    if record.status == "needs_you":
        return "needs_you"
    if record.status == "error":
        return "error"
    return "recovered" if record.status == "healthy" else None


#: Delivers one notice; returns the channel that took it, or ``None`` if none did.
NoticeDeliverer = Callable[[PendingNotice, str], Awaitable[str | None]]


class ConnectionHealthAuthority:
    """The only writer of connection health fields, over one store.

    Constructible in any process from an arcstore opener: every method is a
    compare-and-set on the shared row, so any number of authorities may run
    against one database.
    """

    def __init__(
        self,
        store: ConnectionStateStore,
        *,
        sink: AuditSink | None = None,
        rng: Callable[[], float] = random.random,
    ) -> None:
        self._store = store
        self._sink = sink
        self._rng = rng

    @property
    def store(self) -> ConnectionStateStore:
        return self._store

    async def get(self, connection: str) -> ConnectionRecord | None:
        return await self._store.get(connection)

    async def statuses(self) -> dict[str, ConnectionStatus]:
        return await self._store.statuses()

    async def record(
        self, connection: str, signal: HealthSignal, *, now: datetime | None = None
    ) -> HealthTransition | None:
        """Apply one signal. ``None`` when the connection has no record."""
        moment = now or datetime.now(UTC)

        def decide(record: ConnectionRecord) -> tuple[dict[str, Any], HealthTransition] | None:
            return next_health(record, signal, moment)

        transition = await self._store.cas_update(connection, decide, actor_did=signal.checked_by)
        if transition is not None and transition.changed:
            self._audit_changed(connection, signal, transition)
        return transition

    def audit_checked(
        self,
        connection: str,
        *,
        checked_by: str,
        ok: bool,
        reason_code: str | None,
        duration_ms: int,
        probe: str,
        skipped: str | None = None,
    ) -> None:
        """One ``connection.health.checked`` row per probe or operator check."""
        extra: dict[str, Any] = {
            "ok": ok,
            "reason_code": reason_code,
            "duration_ms": duration_ms,
            "probe": probe,
        }
        if skipped:
            extra["skipped"] = skipped
        self._emit("connection.health.checked", connection, checked_by, extra)

    # --- probe claims ---

    async def claim_due_checks(self, now: datetime, *, limit: int = 8) -> list[ConnectionRecord]:
        """Claim the connections due a probe; return only the ones this caller won.

        The claim IS the schedule: one CAS moves ``next_check_at`` forward, so two
        monitors (or a restarted one) can never probe a connection twice in an
        interval.
        """
        won: list[ConnectionRecord] = []
        for record in await self._store.list():
            if len(won) >= limit:
                break
            if record.next_check_at is not None and parse_time(record.next_check_at) > now:
                continue
            claimed = await self._claim_check(record, now)
            if claimed is not None:
                won.append(claimed)
        return won

    async def _claim_check(
        self, record: ConnectionRecord, now: datetime
    ) -> ConnectionRecord | None:
        next_at = next_check_time(record.status, now, self._rng).isoformat()
        patch = {
            "next_check_at": next_at,
            "revision": record.revision + 1,
            "updated_at": now.isoformat(),
        }
        if await self._store.compare_and_set(
            record.connection, patch, record.revision, actor_did=PROBE_DID
        ):
            return record.model_copy(update=patch)
        return None

    async def reschedule(self, connection: str, now: datetime) -> None:
        """Re-time the next probe from the status the check just produced.

        A connection that just became ``needs_you`` is looked at again in five
        minutes, not at the 30 its claim assumed.
        """
        record = await self._store.get(connection)
        if record is None:
            return
        next_at = next_check_time(record.status, now, self._rng).isoformat()
        await self._store.schedule_check(connection, next_at, actor_did=PROBE_DID)

    # --- notice claims ---

    async def pending_notices(self) -> list[str]:
        """Connections with a notice not yet finished."""
        return [
            record.connection
            for record in await self._store.list()
            if record.notice_seq > record.notified_seq
        ]

    async def claim_notice(
        self, connection: str, owner: str, ttl: timedelta, now: datetime
    ) -> PendingNotice | None:
        """Take the lease to deliver this connection's due notice, or ``None``.

        Returns ``None`` when nothing is due, another owner holds a live lease, or
        the notice has nothing left to say (an outage that recovered before anyone
        was told, or one over the hourly cap): those finish silently here.
        """

        def decide(record: ConnectionRecord) -> tuple[dict[str, Any], PendingNotice | None] | None:
            return self._decide_claim(record, owner, ttl, now)

        return await self._store.cas_update(connection, decide, actor_did=PROBE_DID)

    def _decide_claim(
        self, record: ConnectionRecord, owner: str, ttl: timedelta, now: datetime
    ) -> tuple[dict[str, Any], PendingNotice | None] | None:
        claim = record.notice_claim
        if record.notice_seq <= record.notified_seq:
            return None
        if claim is not None and parse_time(claim.expires_at) > now:
            return None
        seq = record.notice_seq
        kind = _notice_kind(record)
        if kind is None or (kind == "recovered" and not _outage_was_reported(record)):
            return {"notified_seq": record.transition_seq, "notice_claim": None}, None
        attempt = claim.attempt + 1 if claim is not None and claim.seq == seq else 1
        window = self._window_patch(record, now, attempt)
        if window.get("suppressed"):
            window.pop("suppressed")
            window.update(
                notified_seq=record.transition_seq,
                notice_claim=None,
                last_notice={
                    "seq": seq,
                    "kind": kind,
                    "delivered": False,
                    "channel": "suppressed",
                    "at": now.isoformat(),
                },
            )
            return window, None
        patch = {
            **window,
            "notice_claim": {
                "owner": owner,
                "expires_at": (now + ttl).isoformat(),
                "seq": seq,
                "attempt": attempt,
            },
        }
        pending = PendingNotice(
            connection=record.connection,
            seq=seq,
            kind=kind,
            attempt=attempt,
            owner=owner,
            reason_text=record.reason_text or "",
            action=record.action,
        )
        return patch, pending

    @staticmethod
    def _window_patch(record: ConnectionRecord, now: datetime, attempt: int) -> dict[str, Any]:
        """Count a NEW notice against the hourly cap; a retry is not a new notice."""
        if attempt > 1:
            return {}
        start = record.notice_window_start
        fresh = start is None or now - parse_time(start) >= _NOTICE_WINDOW
        count = 0 if fresh else record.notice_window_count
        if count >= NOTICE_HOURLY_CAP:
            return {"suppressed": True}
        return {
            "notice_window_start": now.isoformat() if fresh else start,
            "notice_window_count": count + 1,
        }

    async def finish_notice(
        self,
        connection: str,
        owner: str,
        seq: int,
        *,
        delivered: bool,
        channel: str,
        kind: NoticeKind,
        now: datetime,
    ) -> bool:
        """Finish a claimed notice, delivered or given up. ``False`` if not the owner."""

        def decide(record: ConnectionRecord) -> tuple[dict[str, Any], bool] | None:
            claim = record.notice_claim
            if claim is None or claim.owner != owner or claim.seq != seq:
                return None
            patch: dict[str, Any] = {
                "notified_seq": max(seq, record.notified_seq),
                "notice_claim": None,
                "last_notice": {
                    "seq": seq,
                    "kind": kind,
                    "delivered": delivered,
                    "channel": channel,
                    "at": now.isoformat(),
                },
            }
            # The operator was told it broke while it was already mending. They
            # are owed the "working again" the suppressed recovery would have said.
            if (
                delivered
                and kind != "recovered"
                and record.status == "healthy"
                and record.transition_seq > seq
                and record.notice_seq <= seq
            ):
                patch["notice_seq"] = record.transition_seq
            return patch, True

        return bool(await self._store.cas_update(connection, decide, actor_did=PROBE_DID))

    async def defer_notice(
        self, connection: str, owner: str, seq: int, retry_at: datetime
    ) -> bool:
        """Keep the lease but make it expire at ``retry_at``: the backoff between tries."""

        def decide(record: ConnectionRecord) -> tuple[dict[str, Any], bool] | None:
            claim = record.notice_claim
            if claim is None or claim.owner != owner or claim.seq != seq:
                return None
            return {
                "notice_claim": {**claim.model_dump(), "expires_at": retry_at.isoformat()}
            }, True

        return bool(await self._store.cas_update(connection, decide, actor_did=PROBE_DID))

    async def dispatch_notices(
        self,
        deliver: NoticeDeliverer,
        *,
        owner: str,
        now: datetime,
        ui_base: str = "",
    ) -> int:
        """Claim, deliver and settle every due notice. Returns notices attempted.

        At most :data:`NOTICE_MAX_ATTEMPTS` tries per notice, with backoff between;
        after the last the notice finishes undelivered so the card says "Could not
        notify you" and the loop stops. Never retries forever.
        """
        attempted = 0
        for connection in await self.pending_notices():
            pending = await self.claim_notice(connection, owner, NOTICE_CLAIM_TTL, now)
            if pending is None:
                continue
            attempted += 1
            await self._settle(pending, deliver, now, ui_base)
        return attempted

    async def _settle(
        self, pending: PendingNotice, deliver: NoticeDeliverer, now: datetime, ui_base: str
    ) -> None:
        channel: str | None = None
        if pending.attempt <= NOTICE_MAX_ATTEMPTS:
            try:
                channel = await deliver(pending, notice_text(pending, ui_base=ui_base))
            except Exception:  # reason: a failing channel must not stop other notices
                _logger.warning("operator notice delivery failed for %s", pending.connection)
        delivered = channel is not None
        if delivered or pending.attempt >= NOTICE_MAX_ATTEMPTS:
            await self.finish_notice(
                pending.connection,
                pending.owner,
                pending.seq,
                delivered=delivered,
                channel=channel or "",
                kind=pending.kind,
                now=now,
            )
            self._emit(
                "connection.operator.notified",
                pending.connection,
                PROBE_DID,
                {
                    "seq": pending.seq,
                    "kind": pending.kind,
                    "delivered": delivered,
                    "channel": channel or "",
                    "attempt": pending.attempt,
                },
            )
            return
        backoff = NOTICE_RETRY_BACKOFF[min(pending.attempt, len(NOTICE_RETRY_BACKOFF)) - 1]
        await self.defer_notice(pending.connection, pending.owner, pending.seq, now + backoff)

    # --- audit ---

    def _audit_changed(
        self, connection: str, signal: HealthSignal, transition: HealthTransition
    ) -> None:
        self._emit(
            "connection.health.changed",
            connection,
            signal.checked_by,
            {
                "from": transition.before,
                "to": transition.after,
                "action": transition.after_action,
                "reason_code": signal.reason_code,
                "seq": transition.seq,
                "source": signal.source,
            },
        )

    def _emit(self, action: str, connection: str, actor_did: str, extra: dict[str, Any]) -> None:
        if self._sink is None:
            return
        emit(
            AuditEvent(
                actor_did=actor_did,
                action=action,
                target=f"connection:{connection}",
                outcome="allow",
                extra=extra,
            ),
            self._sink,
        )


class StoreHealthReporter:
    """A :class:`HealthReporter` that opens the shared store on first use.

    For writers that hold an arcstore opener and no authority (the connected-data
    sync hook). Never raises: reporting health is bookkeeping about a failure
    that has already been handled, and replacing that failure with a bookkeeping
    error would lose the reason the operator needs.
    """

    def __init__(self, opener: Callable[[], Awaitable[Any]], *, sink: AuditSink | None = None):
        self._opener = opener
        self._sink = sink
        self._authority: ConnectionHealthAuthority | None = None

    async def report(self, connection: str, signal: HealthSignal) -> None:
        try:
            await (await self._authority_for()).record(connection, signal)
        except Exception:  # reason: see class docstring — health is bookkeeping, never fatal
            _logger.warning("connection health report failed for %s", connection, exc_info=True)

    async def statuses(self) -> dict[str, ConnectionStatus]:
        try:
            return await (await self._authority_for()).statuses()
        except Exception:  # reason: an unreadable store means "unknown", never a crash
            _logger.warning("connection health statuses unreadable", exc_info=True)
            return {}

    async def _authority_for(self) -> ConnectionHealthAuthority:
        if self._authority is None:
            backend = await self._opener()
            self._authority = ConnectionHealthAuthority(
                ConnectionStateStore(backend), sink=self._sink
            )
        return self._authority


__all__ = [
    "AUTH_REASONS",
    "ERROR_FAILURE_CEILING",
    "NOTICE_CLAIM_TTL",
    "NOTICE_HOURLY_CAP",
    "NOTICE_MAX_ATTEMPTS",
    "PROBE_DID",
    "REASONS",
    "ConnectionHealthAuthority",
    "HealthReporter",
    "HealthSignal",
    "HealthTransition",
    "NoticeDeliverer",
    "NoticeKind",
    "PendingNotice",
    "ReasonSpec",
    "SignalSource",
    "StoreHealthReporter",
    "action_label",
    "classify",
    "custody_of",
    "effective_probe",
    "next_check_time",
    "next_health",
    "notice_text",
    "parse_time",
    "probe_interval",
    "reason_text",
]
