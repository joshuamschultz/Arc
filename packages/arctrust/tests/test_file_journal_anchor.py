"""FileJournalAnchor — the zero-config, operator-signed local monotonic head.

Contract + abuse regressions: monotonic CAS, tamper detection, rollback and
downgrade refusal, replay refusal, restart survival, serialized writers, and a
forged signer. Every refusal is ``AnchorUnavailableError`` (fail closed).
"""

from __future__ import annotations

import hashlib
import json
import threading
from pathlib import Path

import pytest

from arctrust import (
    AnchorHead,
    AnchorUnavailableError,
    AuditEvent,
    FileJournalAnchor,
    InProcessSigner,
    MonotonicAnchor,
    skill_revision_anchor_dir,
    trust_dir,
)

_SCOPE = "skill/" + "a" * 64 + "/reporter"
_OPERATOR = InProcessSigner(bytes(range(32)))
_ATTACKER = InProcessSigner(bytes(range(1, 33)))


def _digest(label: str) -> str:
    return hashlib.sha256(label.encode()).hexdigest()


class _Sink:
    def __init__(self) -> None:
        self.events: list[AuditEvent] = []

    def write(self, event: AuditEvent) -> None:
        self.events.append(event)


def _anchor(
    directory: Path,
    *,
    scope: str = _SCOPE,
    signer: InProcessSigner = _OPERATOR,
    sink: _Sink | None = None,
) -> FileJournalAnchor:
    return FileJournalAnchor(directory, scope=scope, signer=signer, audit_sink=sink)


def _files(directory: Path, scope: str = _SCOPE) -> tuple[Path, Path]:
    stem = hashlib.sha256(scope.encode()).hexdigest()
    return directory / f"{stem}.jsonl", directory / f"{stem}.head"


def _advance(anchor: FileJournalAnchor, *labels: str) -> list[AnchorHead]:
    heads: list[AnchorHead] = []
    for label in labels:
        heads.append(anchor.compare_and_advance(anchor.latest(), _digest(label), f"set:{label}"))
    return heads


def test_satisfies_monotonic_anchor_protocol(tmp_path: Path) -> None:
    anchor: MonotonicAnchor = _anchor(tmp_path)
    assert anchor.scope == _SCOPE


def test_journal_lives_under_the_single_arc_home_resolver(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ARC_CONFIG_DIR", str(tmp_path / "home"))
    assert skill_revision_anchor_dir() == trust_dir() / "anchors"


def test_fresh_scope_has_no_head(tmp_path: Path) -> None:
    assert _anchor(tmp_path / "missing").latest() is None


def test_advance_is_monotonic_and_chained(tmp_path: Path) -> None:
    anchor = _anchor(tmp_path)
    first, second = _advance(anchor, "one", "two")
    assert (first.version, first.previous_digest) == (1, None)
    assert (second.version, second.previous_digest) == (2, first.digest)
    assert anchor.latest() == second


def test_stale_expected_head_is_refused(tmp_path: Path) -> None:
    anchor = _anchor(tmp_path)
    first, _ = _advance(anchor, "one", "two")
    with pytest.raises(AnchorUnavailableError, match="stale"):
        anchor.compare_and_advance(first, _digest("three"), "late")
    with pytest.raises(AnchorUnavailableError, match="stale"):
        anchor.compare_and_advance(None, _digest("three"), "late")


def test_invalid_digest_and_intent_are_refused(tmp_path: Path) -> None:
    anchor = _anchor(tmp_path)
    with pytest.raises(ValueError):
        anchor.compare_and_advance(None, "not-a-digest", "x")
    with pytest.raises(ValueError):
        anchor.compare_and_advance(None, _digest("x"), "x" * 1_048_577)
    with pytest.raises(ValueError):
        FileJournalAnchor(tmp_path, scope="../escape", signer=_OPERATOR)


def test_restart_survives_with_a_fresh_instance(tmp_path: Path) -> None:
    _, second = _advance(_anchor(tmp_path), "one", "two")
    restarted = _anchor(tmp_path)
    assert restarted.latest() == second
    third = restarted.compare_and_advance(second, _digest("three"), "after restart")
    assert third.version == 3


def test_scopes_are_isolated(tmp_path: Path) -> None:
    _advance(_anchor(tmp_path), "one")
    other = _anchor(tmp_path, scope="skill/" + "b" * 64 + "/reporter")
    assert other.latest() is None


def test_tampered_journal_line_fails_closed(tmp_path: Path) -> None:
    _advance(_anchor(tmp_path), "one", "two")
    journal, _ = _files(tmp_path)
    lines = journal.read_bytes().splitlines()
    record = json.loads(lines[1])
    record["digest"] = _digest("evil")
    lines[1] = json.dumps(record, sort_keys=True, separators=(",", ":")).encode()
    journal.write_bytes(b"\n".join(lines) + b"\n")
    with pytest.raises(AnchorUnavailableError):
        _anchor(tmp_path).latest()


def test_truncated_journal_is_a_rollback(tmp_path: Path) -> None:
    _advance(_anchor(tmp_path), "one", "two")
    journal, _ = _files(tmp_path)
    first_line = journal.read_bytes().splitlines()[0]
    journal.write_bytes(first_line + b"\n")
    with pytest.raises(AnchorUnavailableError, match="rolled back"):
        _anchor(tmp_path).latest()


def test_deleted_seal_with_live_journal_fails_closed(tmp_path: Path) -> None:
    _advance(_anchor(tmp_path), "one")
    _, head = _files(tmp_path)
    head.unlink()
    with pytest.raises(AnchorUnavailableError):
        _anchor(tmp_path).latest()


def test_downgrade_inside_a_running_process_is_refused(tmp_path: Path) -> None:
    anchor = _anchor(tmp_path)
    _advance(anchor, "one")
    journal, head = _files(tmp_path)
    old_journal, old_head = journal.read_bytes(), head.read_bytes()
    _advance(anchor, "two")
    # An attacker restores a consistent older copy of BOTH files.
    journal.write_bytes(old_journal)
    head.write_bytes(old_head)
    with pytest.raises(AnchorUnavailableError, match="regressed"):
        anchor.latest()


def test_reset_after_seen_head_is_refused(tmp_path: Path) -> None:
    anchor = _anchor(tmp_path)
    _advance(anchor, "one")
    journal, head = _files(tmp_path)
    journal.unlink()
    head.unlink()
    with pytest.raises(AnchorUnavailableError, match="regressed"):
        anchor.latest()


def test_replayed_entry_is_refused(tmp_path: Path) -> None:
    _advance(_anchor(tmp_path), "one", "two")
    journal, _ = _files(tmp_path)
    lines = journal.read_bytes().splitlines()
    journal.write_bytes(b"\n".join([*lines, lines[0]]) + b"\n")
    with pytest.raises(AnchorUnavailableError):
        _anchor(tmp_path).latest()


def test_cross_scope_replay_is_refused(tmp_path: Path) -> None:
    other_scope = "skill/" + "c" * 64 + "/reporter"
    _advance(_anchor(tmp_path, scope=other_scope), "foreign")
    foreign_journal, foreign_head = _files(tmp_path, other_scope)
    journal, head = _files(tmp_path)
    journal.write_bytes(foreign_journal.read_bytes())
    head.write_bytes(foreign_head.read_bytes())
    with pytest.raises(AnchorUnavailableError):
        _anchor(tmp_path).latest()


def test_forged_signer_journal_is_refused(tmp_path: Path) -> None:
    # The attacker writes a complete, internally valid journal with its own key.
    forged = _anchor(tmp_path, signer=_ATTACKER)
    _advance(forged, "evil")
    with pytest.raises(AnchorUnavailableError):
        _anchor(tmp_path).latest()
    with pytest.raises(AnchorUnavailableError):
        _anchor(tmp_path).compare_and_advance(None, _digest("x"), "x")


def test_torn_unsealed_append_is_discarded_and_writes_continue(tmp_path: Path) -> None:
    anchor = _anchor(tmp_path)
    (first,) = _advance(anchor, "one")
    journal, _ = _files(tmp_path)
    with journal.open("ab") as handle:
        handle.write(b'{"kind":"entry","vers')
    restarted = _anchor(tmp_path)
    assert restarted.latest() == first
    second = restarted.compare_and_advance(first, _digest("two"), "after tear")
    assert _anchor(tmp_path).latest() == second


def test_symlinked_journal_is_refused(tmp_path: Path) -> None:
    _advance(_anchor(tmp_path), "one")
    journal, _ = _files(tmp_path)
    target = tmp_path / "elsewhere.jsonl"
    target.write_bytes(journal.read_bytes())
    journal.unlink()
    journal.symlink_to(target)
    with pytest.raises(AnchorUnavailableError):
        _anchor(tmp_path).latest()


def test_concurrent_writers_are_serialized(tmp_path: Path) -> None:
    start = threading.Barrier(8)
    winners: list[AnchorHead] = []
    losers: list[Exception] = []
    lock = threading.Lock()

    def race(index: int) -> None:
        anchor = _anchor(tmp_path)
        start.wait()
        try:
            head = anchor.compare_and_advance(None, _digest(f"w{index}"), f"w{index}")
        except AnchorUnavailableError as exc:
            with lock:
                losers.append(exc)
        else:
            with lock:
                winners.append(head)

    threads = [threading.Thread(target=race, args=(i,)) for i in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(winners) == 1
    assert len(losers) == 7
    assert _anchor(tmp_path).latest() == winners[0]


def test_concurrent_retrying_writers_build_one_contiguous_chain(tmp_path: Path) -> None:
    start = threading.Barrier(6)

    def writer(index: int) -> None:
        anchor = _anchor(tmp_path)
        start.wait()
        for attempt in range(3):
            label = f"w{index}-{attempt}"
            while True:
                try:
                    anchor.compare_and_advance(anchor.latest(), _digest(label), label)
                    break
                except AnchorUnavailableError:
                    continue

    threads = [threading.Thread(target=writer, args=(i,)) for i in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    head = _anchor(tmp_path).latest()
    assert head is not None and head.version == 18


def test_advances_are_audited_as_local_anchor(tmp_path: Path) -> None:
    sink = _Sink()
    anchor = _anchor(tmp_path, sink=sink)
    (first,) = _advance(anchor, "one")
    with pytest.raises(AnchorUnavailableError):
        anchor.compare_and_advance(None, _digest("two"), "stale")
    assert [(e.action, e.outcome) for e in sink.events] == [
        ("revision_anchor.advance", "allow"),
        ("revision_anchor.advance", "deny"),
    ]
    assert sink.events[0].extra["custody"] == "local_file"
    assert sink.events[0].extra["version"] == first.version
    assert sink.events[0].target == _SCOPE
