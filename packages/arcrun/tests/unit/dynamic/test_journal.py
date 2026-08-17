"""The journal is what makes a script run resumable, so it is tested as replay.

A resumed run re-executes the same script from the top; every host call it
already made must come back from the record instead of happening again. That
only holds if the record is dense, ordered, and loud when the script it is
replaying is no longer the script that wrote it — which is what these tests
pin down.
"""

from __future__ import annotations

import hmac
import json
from hashlib import sha256
from pathlib import Path

import pytest

from arcrun.dynamic.host import MAX_HOST_CALLS
from arcrun.dynamic.journal import (
    MAX_JOURNAL_BYTES,
    Journal,
    JournalDivergence,
    JournalError,
    JournalFull,
    JournalTampered,
    request_hash,
)
from arcrun.dynamic.seal import RunSeal, SealBroken


class _StubSigner:
    """Stands in for the operator's signing authority, which arcrun never holds."""

    def __init__(self, secret: bytes = b"operator") -> None:
        self._secret = secret

    def sign(self, message: bytes) -> bytes:
        return sha256(self._secret + message).digest()

    def verify(self, message: bytes, signature: bytes) -> bool:
        return hmac.compare_digest(self.sign(message), signature)


def _sealed(tmp_path: Path, script: bytes = b"complete('ok')") -> tuple[Path, RunSeal]:
    """A durable journal path plus the run seal that binds it to its script."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    seal = RunSeal(_StubSigner(), tmp_path / "audit").bound_to(script)
    return workspace / "run.jsonl", seal


def _write_lines(path: Path, *lines: str, trailing_newline: bool = True) -> None:
    """Write raw journal lines, optionally leaving the last one unterminated."""
    body = "\n".join(lines)
    path.write_text(body + "\n" if trailing_newline else body, encoding="utf-8")


def _line(seq: int, kind: str = "spawn", req_hash: str = "h", result: object = None) -> str:
    return json.dumps({"seq": seq, "kind": kind, "req_hash": req_hash, "result": result})


def test_a_journal_with_no_path_works_fully_but_writes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A run with no durable home still journals; arcrun invents no workspace."""
    monkeypatch.chdir(tmp_path)
    journal = Journal()
    journal.record(0, "spawn", "abc", {"success": True})
    journal.record(1, "log", "def", None)

    assert journal.replay(0, "spawn", "abc") == (True, {"success": True})
    assert len(journal) == 2
    assert list(tmp_path.iterdir()) == []


def test_a_replayed_call_returns_the_recorded_result_instead_of_running_again() -> None:
    """Replay is the whole point: a hit means the effect is not repeated."""
    journal = Journal()
    journal.record(0, "spawn", "abc", {"agent_id": "a1"})

    hit, result = journal.replay(0, "spawn", "abc")

    assert hit is True
    assert result == {"agent_id": "a1"}


def test_a_call_past_the_end_of_the_record_is_a_miss_so_the_script_runs_it_live() -> None:
    """Resume must stop replaying exactly where the crashed run stopped."""
    journal = Journal()
    journal.record(0, "spawn", "abc", 1)

    assert journal.replay(1, "spawn", "def") == (False, None)


@pytest.mark.parametrize(
    ("kind", "req_hash"),
    [("scratch_write", "abc"), ("spawn", "different")],
)
def test_a_different_call_at_a_recorded_position_is_loud_divergence(
    kind: str, req_hash: str
) -> None:
    """A silent re-run here would double a real side effect — so it never happens."""
    journal = Journal()
    journal.record(0, "spawn", "abc", 1)

    with pytest.raises(JournalDivergence) as excinfo:
        journal.replay(0, kind, req_hash)

    message = str(excinfo.value).lower()
    assert "nondeterministic" in message
    assert "edited" in message


def test_a_recorded_run_replays_after_the_journal_is_reloaded_from_disk(tmp_path: Path) -> None:
    """Resume happens in a new process, so the record must survive one."""
    path = tmp_path / "run.jsonl"
    writer = Journal(path)
    writer.record(0, "spawn", "abc", {"success": True})
    writer.record(1, "scratch_write", "def", "/scratch/notes.md")

    reloaded = Journal.load(path)

    assert len(reloaded) == 2
    assert reloaded.replay(1, "scratch_write", "def") == (True, "/scratch/notes.md")


def test_recording_out_of_sequence_is_refused_so_the_record_stays_dense() -> None:
    """Seq is the script's call counter; a gap would misalign every later replay."""
    journal = Journal()
    journal.record(0, "spawn", "abc", 1)

    with pytest.raises(JournalError):
        journal.record(2, "spawn", "def", 1)


def test_a_journal_with_a_gap_refuses_to_load(tmp_path: Path) -> None:
    """A hole means some call was lost; replaying around it would be wrong."""
    path = tmp_path / "run.jsonl"
    _write_lines(path, _line(0), _line(2))

    with pytest.raises(JournalError):
        Journal.load(path)


def test_a_torn_trailing_line_is_discarded_because_a_crash_wrote_it(tmp_path: Path) -> None:
    """The crash that stopped the run can cut the last write in half."""
    path = tmp_path / "run.jsonl"
    _write_lines(path, _line(0), _line(1), '{"seq": 2, "kind": "spa', trailing_newline=False)

    journal = Journal.load(path)

    assert len(journal) == 2
    assert journal.replay(2, "spawn", "h") == (False, None)


def test_a_malformed_line_anywhere_but_the_end_refuses_to_load(tmp_path: Path) -> None:
    """Damage in the middle is corruption, not a torn tail — never guess past it."""
    path = tmp_path / "run.jsonl"
    _write_lines(path, _line(0), "{not json", _line(2))

    with pytest.raises(JournalError):
        Journal.load(path)


def test_a_malformed_last_line_that_was_fully_written_still_refuses_to_load(
    tmp_path: Path,
) -> None:
    """Only a line the crash cut short is forgiven; a finished one is corruption."""
    path = tmp_path / "run.jsonl"
    _write_lines(path, _line(0), "{not json")

    with pytest.raises(JournalError):
        Journal.load(path)


def test_a_missing_journal_file_loads_as_an_empty_journal(tmp_path: Path) -> None:
    """A first run has no record yet; that is normal, not an error."""
    journal = Journal.load(tmp_path / "absent.jsonl")

    assert len(journal) == 0
    assert journal.replay(0, "spawn", "abc") == (False, None)


def test_load_refuses_a_symlink(tmp_path: Path) -> None:
    """A swapped symlink is how an attacker aims a resume at another file."""
    target = tmp_path / "real.jsonl"
    _write_lines(target, _line(0))
    link = tmp_path / "run.jsonl"
    link.symlink_to(target)

    with pytest.raises(JournalError):
        Journal.load(link)


def test_load_refuses_an_oversized_journal_before_reading_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Checking the size after the read would already have spent the memory."""
    path = tmp_path / "run.jsonl"
    with path.open("wb") as handle:
        handle.truncate(MAX_JOURNAL_BYTES + 1)

    def _explode(*_args: object, **_kwargs: object) -> str:
        raise AssertionError("the file was read before its size was checked")

    monkeypatch.setattr(Path, "read_text", _explode)

    with pytest.raises(JournalError):
        Journal.load(path)


def test_recording_past_the_host_call_ceiling_is_refused() -> None:
    """The journal is the hard stop on a script that never stops calling out."""
    journal = Journal()
    for seq in range(MAX_HOST_CALLS):
        journal.record(seq, "log", "h", None)

    with pytest.raises(JournalFull):
        journal.record(MAX_HOST_CALLS, "log", "h", None)


def test_a_result_that_cannot_round_trip_is_refused_even_with_no_file() -> None:
    """In-memory and on-disk journals must replay identically, or resume lies."""
    journal = Journal()

    with pytest.raises(JournalError):
        journal.record(0, "spawn", "abc", object())


def test_request_hash_ignores_dict_key_order() -> None:
    """Key order is an accident of construction; it must not read as divergence."""
    assert request_hash("spawn", {"a": 1, "b": 2}) == request_hash("spawn", {"b": 2, "a": 1})


def test_request_hash_separates_two_kinds_with_the_same_payload() -> None:
    """Otherwise a scratch read could replay a spawn's recorded result."""
    assert request_hash("spawn", {"x": 1}) != request_hash("scratch_read", {"x": 1})


def test_request_hash_accepts_a_payload_whose_dict_keys_are_not_all_strings() -> None:
    """An ordinary script can pass one: ``agent("x", {"output_schema": {1: "a"}})``."""
    payload = {"output_schema": {1: "a", "b": 2}}

    assert request_hash("agent", payload) == request_hash(
        "agent", {"output_schema": {"b": 2, 1: "a"}}
    )


def test_request_hash_separates_an_int_key_from_its_string_spelling() -> None:
    """JSON collapses both to ``"1"``; two different calls must not share a fingerprint."""
    assert request_hash("agent", {1: "a"}) != request_hash("agent", {"1": "a"})


def test_request_hash_refuses_a_payload_it_cannot_canonicalize() -> None:
    """A ``repr`` can carry an ``id()``, which would fingerprint differently per process."""
    with pytest.raises(JournalError):
        request_hash("agent", {"schema": object()})


def test_a_recorded_result_replays_the_same_in_memory_as_it_would_from_disk(
    tmp_path: Path,
) -> None:
    """Otherwise a run verified in memory diverges on its first durable resume."""
    result = {"rows": (1, 2), "by_index": {1: "a"}}
    path = tmp_path / "run.jsonl"
    durable = Journal(path)
    durable.record(0, "agent", "abc", result)
    in_memory = Journal()
    in_memory.record(0, "agent", "abc", result)

    assert in_memory.replay(0, "agent", "abc") == Journal.load(path).replay(0, "agent", "abc")


def test_a_sealed_journal_replays_after_reload(tmp_path: Path) -> None:
    """Signing must not cost the journal what it exists for."""
    path, seal = _sealed(tmp_path)
    writer = Journal(path, seal=seal)
    writer.record(0, "agent", "abc", {"success": True})
    writer.record(1, "scratch_write", "def", "/scratch/notes.md")

    reloaded = Journal.load(path, seal=seal)

    assert len(reloaded) == 2
    assert reloaded.replay(0, "agent", "abc") == (True, {"success": True})


def test_a_seal_writes_nothing_into_the_agent_writable_workspace(tmp_path: Path) -> None:
    """The signatures only protect the record while the agent cannot reach them."""
    path, seal = _sealed(tmp_path)
    Journal(path, seal=seal).record(0, "agent", "abc", 1)

    assert [entry.name for entry in path.parent.iterdir()] == ["run.jsonl"]
    assert len(seal.sealed_lines("run.jsonl")) == 1


def test_an_edited_entry_in_a_sealed_journal_is_refused(tmp_path: Path) -> None:
    """The whole point: a forged result must never reach the script as a replay."""
    path, seal = _sealed(tmp_path)
    writer = Journal(path, seal=seal)
    writer.record(0, "agent", "abc", {"approved": False})
    writer.record(1, "log", "def", None)
    path.write_text(
        path.read_text(encoding="utf-8").replace('"approved":false', '"approved":true'),
        encoding="utf-8",
    )

    with pytest.raises(JournalTampered):
        Journal.load(path, seal=seal)


def test_a_forged_entry_appended_to_a_sealed_journal_never_replays(tmp_path: Path) -> None:
    """An agent can append to its own workspace; it cannot sign what it appends."""
    path, seal = _sealed(tmp_path)
    Journal(path, seal=seal).record(0, "agent", "abc", 1)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(_line(1, kind="agent", req_hash="def", result={"owned": True}) + "\n")

    with pytest.raises(JournalTampered):
        Journal.load(path, seal=seal)


def test_a_sealed_journal_with_no_signatures_at_all_is_refused(tmp_path: Path) -> None:
    """A missing seal is indistinguishable from a deleted one — so it fails closed."""
    path, seal = _sealed(tmp_path)
    Journal(path).record(0, "agent", "abc", 1)

    with pytest.raises(JournalTampered):
        Journal.load(path, seal=seal)


def test_a_sealed_journal_truncated_of_its_recorded_calls_is_refused(tmp_path: Path) -> None:
    """Dropping entries would silently re-run effects the record says already happened."""
    path, seal = _sealed(tmp_path)
    writer = Journal(path, seal=seal)
    for seq in range(3):
        writer.record(seq, "agent", f"h{seq}", seq)
    path.write_text("", encoding="utf-8")

    with pytest.raises(JournalTampered):
        Journal.load(path, seal=seal)


def test_a_crash_between_signing_and_appending_still_loads(tmp_path: Path) -> None:
    """The signature is written first, so a crash leaves one spare — not a tamper."""
    path, seal = _sealed(tmp_path)
    writer = Journal(path, seal=seal)
    writer.record(0, "agent", "abc", 1)
    seal.seal_line("run.jsonl", b"the line the crash never wrote")

    assert len(Journal.load(path, seal=seal)) == 1


def test_a_torn_trailing_line_under_a_seal_is_still_forgiven(tmp_path: Path) -> None:
    """A crash cuts the last write in half whether or not the run is sealed."""
    path, seal = _sealed(tmp_path)
    writer = Journal(path, seal=seal)
    writer.record(0, "agent", "abc", 1)
    writer.record(1, "log", "def", None)
    whole = path.read_text(encoding="utf-8")
    path.write_text(whole[: whole.rindex("\n") + 1] + '{"seq": 2, "kind": "ag', encoding="utf-8")

    assert len(Journal.load(path, seal=seal)) == 2


def test_tampering_is_a_distinct_failure_from_corruption(tmp_path: Path) -> None:
    """A corrupt journal may run fresh; a tampered one must stop the run."""
    path = tmp_path / "run.jsonl"
    _write_lines(path, _line(0), "{not json")

    assert issubclass(JournalTampered, JournalError)
    with pytest.raises(JournalError) as excinfo:
        Journal.load(path)
    assert not isinstance(excinfo.value, JournalTampered)


def test_an_unsealed_journal_writes_and_reads_exactly_as_a_sealed_one_does(
    tmp_path: Path,
) -> None:
    """``seal=None`` is a symmetric no-op, so personal and federal run one code path."""
    path = tmp_path / "run.jsonl"
    Journal(path).record(0, "agent", "abc", {"success": True})

    assert Journal.load(path).replay(0, "agent", "abc") == (True, {"success": True})
    assert list(tmp_path.iterdir()) == [path]


def test_a_seal_over_a_journal_with_no_file_is_refused() -> None:
    """Nothing to sign is not the same as nothing to protect — say so loudly."""
    with pytest.raises(JournalError):
        Journal(seal=RunSeal(_StubSigner(), Path("/nonexistent")))


def test_a_journal_recorded_under_one_script_refuses_to_replay_under_another(
    tmp_path: Path,
) -> None:
    """Sealing the two files apart would let an attacker pair whichever suit them."""
    path, seal = _sealed(tmp_path, script=b"the script that was authored")
    Journal(path, seal=seal).record(0, "agent", "abc", {"approved": False})
    swapped = RunSeal(_StubSigner(), tmp_path / "audit").bound_to(b"the script an agent wrote")

    with pytest.raises(JournalTampered):
        Journal.load(path, seal=swapped)


def test_a_tampered_journal_is_reported_as_a_broken_seal_too(tmp_path: Path) -> None:
    """One ``except`` should cover every file a run discovers was rewritten."""
    path, seal = _sealed(tmp_path)
    Journal(path, seal=seal).record(0, "agent", "abc", 1)
    path.write_text(_line(0, kind="agent", req_hash="abc", result=2) + "\n", encoding="utf-8")

    with pytest.raises(SealBroken):
        Journal.load(path, seal=seal)


def test_entries_expose_an_immutable_ordered_snapshot() -> None:
    """Callers audit the record; they must not be able to rewrite it."""
    journal = Journal()
    journal.record(0, "phase", "abc", None)
    journal.record(1, "spawn", "def", {"success": False})

    entries = journal.entries

    assert isinstance(entries, tuple)
    assert [entry.seq for entry in entries] == [0, 1]
    assert entries[1].kind == "spawn"
    assert entries[1].result == {"success": False}
