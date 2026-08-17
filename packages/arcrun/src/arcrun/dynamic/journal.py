"""Append-only record of host calls — the mechanism that makes a run resumable.

Nothing here snapshots interpreter state. A resumed run re-executes the *same*
deterministic script from the top, and every host call it already made returns
its recorded result rather than happening again. Resume is therefore replay,
not restoration: there is no serialized frame to keep in step with the language,
and a script that has spawned twenty children resumes without spawning any.

Three properties carry that guarantee, and each one is enforced here rather than
trusted:

* **Dense.** Entry ``n`` is the script's ``n``-th call. A gap would silently
  shift every later replay onto the wrong result.
* **Durable per call.** Each record is one flushed line, so a crash costs at
  most the call in flight — which the loader recognises as a torn tail.
* **Loud on divergence.** If the call issued at a position is not the call
  recorded there, the script is not the one that wrote the journal. That ends
  the run with a typed error; it never silently re-runs the effect.
* **Sealed, where an operator asks for it.** The journal lives in the agent's
  own writable workspace, so an agent holding ``bash`` or ``write`` could
  otherwise forge the result a resume replays into its next prompt (ASI06).
  A :class:`~arcrun.dynamic.seal.RunSeal` signs each line into an operator-owned
  directory outside that workspace, and a record that does not match its
  signatures is refused rather than replayed. Because that seal is bound to the
  script source, a journal cannot be paired with a script it never ran under.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, replace
from hashlib import sha256
from pathlib import Path
from typing import Any

from arcrun.dynamic.host import MAX_HOST_CALLS
from arcrun.dynamic.seal import RunSeal, SealBroken

MAX_JOURNAL_BYTES = 32 * 1024 * 1024
"""Refused before the file is read, so a hostile journal cannot exhaust memory."""

_REQUIRED_FIELDS = ("seq", "kind", "req_hash", "result")

_MAP_KEY = "#map"
"""Marks a canonicalized mapping, so it can never collide with a list payload."""


@dataclass(frozen=True, slots=True)
class JournalEntry:
    """One completed host call: what was asked, and what came back."""

    seq: int
    kind: str
    req_hash: str
    result: Any


class JournalError(Exception):
    """The journal cannot be trusted to drive a replay."""


class JournalDivergence(JournalError):  # noqa: N818 — domain convention: named for the condition
    """The call issued at a position is not the call recorded there."""


class JournalFull(JournalError):  # noqa: N818 — domain convention: named for the condition
    """Recording would exceed the per-run ceiling on host calls."""


class JournalTampered(  # noqa: N818 — domain convention: named for the condition
    JournalError, SealBroken
):
    """The record does not match the operator signatures that sealed it.

    Kept distinct from corruption on purpose. A corrupt journal is a crash
    artefact, and starting the run fresh is a defensible answer to it. A
    tampered one means somebody rewrote the record the run is about to trust,
    and running fresh would turn that attack into an invisible retry.

    It is a :class:`SealBroken` as well, so one ``except`` clause covers every
    way a run can discover that a file it resumes from was rewritten.
    """


def _canonical_key(key: Any) -> str:
    """Tag a mapping key with its type, so ``1`` and ``"1"`` stay distinguishable.

    JSON spells every key as a string and would let those two collapse into one
    fingerprint, which is two different host calls sharing one recorded result.
    """
    if isinstance(key, bool):
        return f"b:{key}"
    if isinstance(key, str):
        return f"s:{key}"
    if isinstance(key, int):
        return f"i:{key}"
    if isinstance(key, float):
        return f"f:{key!r}"
    if key is None:
        return "n:"
    raise JournalError(
        f"host call payload carries a {type(key).__name__} mapping key, which has no "
        "canonical form, so the same call could fingerprint two ways"
    )


def _canonical(value: Any) -> Any:
    """Rewrite a payload into the one form it may be fingerprinted from.

    Mappings become type-tagged pair lists sorted here rather than by
    ``sort_keys``, which cannot order a ``str`` against an ``int`` and raises on
    the mixed-key dict an ordinary script can pass. Sequences flatten to lists
    because that is what a resumed run reads back. Anything else is refused: a
    fallback such as ``repr`` can embed an ``id()`` and would fingerprint the
    same call differently in the process that resumes it.
    """
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, Mapping):
        pairs: list[tuple[str, Any]] = [
            (_canonical_key(key), _canonical(item)) for key, item in value.items()
        ]
        pairs.sort(key=lambda pair: pair[0])
        return {_MAP_KEY: [list(pair) for pair in pairs]}
    if isinstance(value, (list, tuple)):
        return [_canonical(item) for item in value]
    raise JournalError(
        f"host call payload carries a {type(value).__name__}, which has no canonical "
        "form, so a resumed run could not prove it is issuing the same call"
    )


def request_hash(kind: str, payload: Any) -> str:
    """Fingerprint a host call so replay can prove it is the same call.

    Canonicalizing first means an argument dict built in a different order still
    hashes the same, while ``kind`` is part of the hashed structure so two
    different calls carrying identical payloads can never share a fingerprint.
    """
    canonical = json.dumps(
        {"kind": kind, "payload": _canonical(payload)},
        separators=(",", ":"),
    )
    return sha256(canonical.encode("utf-8")).hexdigest()


def _encode(entry: JournalEntry) -> str:
    """Serialize one entry, refusing a result that could not replay identically."""
    try:
        return json.dumps(
            {
                "seq": entry.seq,
                "kind": entry.kind,
                "req_hash": entry.req_hash,
                "result": entry.result,
            },
            separators=(",", ":"),
        )
    except (TypeError, ValueError) as exc:
        raise JournalError(
            f"host call {entry.seq} ({entry.kind}) returned a result that is not a JSON "
            f"value, so a resumed run could not replay it unchanged: {exc}"
        ) from exc


def _decode(line: str) -> JournalEntry:
    """Parse one recorded line, rejecting anything that is not a whole entry."""
    try:
        record = json.loads(line)
    except json.JSONDecodeError as exc:
        raise JournalError(f"journal line is not JSON: {exc}") from exc
    if not isinstance(record, dict) or any(field not in record for field in _REQUIRED_FIELDS):
        raise JournalError(f"journal line is missing required fields: {line[:120]!r}")
    seq, kind, req_hash = record["seq"], record["kind"], record["req_hash"]
    if not isinstance(seq, int) or not isinstance(kind, str) or not isinstance(req_hash, str):
        raise JournalError(f"journal line has malformed fields: {line[:120]!r}")
    return JournalEntry(seq=seq, kind=kind, req_hash=req_hash, result=record["result"])


def _append(path: Path, line: str) -> None:
    """Flush one line, so a crash loses at most the host call still in flight."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")
        handle.flush()


def _split(text: str) -> tuple[list[str], bool]:
    """Split a journal file into lines, flagging a tail the crash cut short.

    A journal always ends its lines. Text that does not means the process died
    mid-write, so the final line is a crash artefact rather than corruption.
    """
    if not text:
        return [], False
    lines = text.split("\n")
    torn_tail = lines[-1] != ""
    if not torn_tail:
        lines.pop()
    return lines, torn_tail


def _verified_lines(seal: RunSeal, name: str, lines: list[str], torn_tail: bool) -> list[str]:
    """Keep the lines the operator actually signed, refusing the rest fail-closed.

    Each signature is written before the line it covers, so a crash can leave a
    spare signature but never an unsigned line — which is why an unsigned or
    mismatched line is forgiven only where the crash could have produced it, at
    a torn tail. Anything else is somebody else's writing.
    """
    try:
        signatures = seal.sealed_lines(name)
    except SealBroken as exc:
        raise JournalTampered(str(exc)) from exc
    kept: list[str] = []
    for index, line in enumerate(lines):
        signature = signatures[index] if index < len(signatures) else b""
        if seal.verifies(line.encode("utf-8"), signature):
            kept.append(line)
        elif torn_tail and index == len(lines) - 1:
            break
        else:
            raise JournalTampered(
                f"journal line {index} does not match its operator signature; the record "
                "was rewritten after it was made and must not be replayed"
            )
    if len(signatures) > len(kept) + 1:
        raise JournalTampered(
            f"journal holds {len(kept)} signed lines against {len(signatures)} operator "
            "signatures; recorded calls were removed and would silently happen again"
        )
    return kept


def _parse(lines: list[str], torn_tail: bool) -> list[JournalEntry]:
    """Decode the journal's lines, tolerating exactly one torn trailing line."""
    entries: list[JournalEntry] = []
    for index, line in enumerate(lines):
        try:
            entries.append(_decode(line))
        except JournalError:
            if torn_tail and index == len(lines) - 1:
                break
            raise
    for expected, entry in enumerate(entries):
        if entry.seq != expected:
            raise JournalError(
                f"journal is not dense: entry {expected} carries seq {entry.seq}, so every "
                "later replay would resolve against the wrong host call"
            )
    return entries


class Journal:
    """The recorded host calls of one script run.

    ``path=None`` keeps the record in memory only — fully functional, just not
    durable. That is the honest default for a run with no durable home: arcrun
    never invents a workspace, the caller supplies the path or accepts that a
    crash loses the run.

    ``seal=None`` is a no-op in both directions — nothing signed, nothing
    verified — so an unsealed run and a sealed one walk one code path.

    Constructing with a path does not read it; use :meth:`load` to resume one.
    """

    __slots__ = ("_entries", "_path", "_seal")

    def __init__(self, path: Path | None = None, *, seal: RunSeal | None = None) -> None:
        if seal is not None and path is None:
            raise JournalError(
                "a seal signs the lines of a journal file, so it cannot protect a "
                "journal that is only ever held in memory"
            )
        self._path = path
        self._seal = seal
        self._entries: list[JournalEntry] = []

    @classmethod
    def load(cls, path: Path, *, seal: RunSeal | None = None) -> Journal:
        """Read an existing journal, or return an empty one bound to ``path``.

        A journal is read on resume, when the run that wrote it is gone — so the
        file is treated as untrusted input: a symlink (a redirect at somebody
        else's file) and an oversized file are both refused before any read, and
        a ``seal`` is checked before a single line is decoded.
        """
        journal = cls(path, seal=seal)
        if path.is_symlink():
            raise JournalError(f"journal path is a symlink and will not be followed: {path}")
        text = ""
        if path.exists():
            size = path.stat().st_size
            if size > MAX_JOURNAL_BYTES:
                raise JournalError(
                    f"journal is {size} bytes, over the {MAX_JOURNAL_BYTES} byte ceiling"
                )
            text = path.read_text(encoding="utf-8")
        lines, torn_tail = _split(text)
        if seal is not None:
            # Every surviving line carries an operator signature over its exact
            # bytes, so a torn tail is already gone by the time it is decoded.
            lines, torn_tail = _verified_lines(seal, path.name, lines, torn_tail), False
        journal._entries.extend(_parse(lines, torn_tail))
        return journal

    def replay(self, seq: int, kind: str, req_hash: str) -> tuple[bool, Any]:
        """Resolve a host call against the record.

        Returns ``(True, recorded_result)`` when this call already happened and
        must not happen again, and ``(False, None)`` once replay has caught up
        with where the previous run stopped.

        Raises :class:`JournalDivergence` when a *different* call is recorded at
        ``seq`` — the run cannot continue, because neither replaying the wrong
        result nor re-running the effect is safe.
        """
        if seq >= len(self._entries):
            return False, None
        entry = self._entries[seq]
        if entry.kind != kind or entry.req_hash != req_hash:
            raise JournalDivergence(
                f"host call {seq} was recorded as {entry.kind}/{entry.req_hash} but the script "
                f"issued {kind}/{req_hash}; the script is nondeterministic or was edited "
                "mid-run, so its journal can no longer be replayed"
            )
        return True, entry.result

    def record(self, seq: int, kind: str, req_hash: str, result: Any) -> None:
        """Append the outcome of a host call that just happened.

        The entry is serialized whether or not there is a file to write, and
        what is kept is the result read back out of that line rather than the
        live object — so an in-memory journal replays exactly what a durable one
        would. A tuple or a non-string key survives in memory but not through
        JSON, and a run verified in memory would otherwise diverge on its first
        durable resume.
        """
        if seq != len(self._entries):
            raise JournalError(
                f"host call {seq} cannot be recorded after {len(self._entries)} entries; "
                "the journal is dense by construction"
            )
        if len(self._entries) >= MAX_HOST_CALLS:
            raise JournalFull(f"script exceeded {MAX_HOST_CALLS} host calls, the per-run ceiling")
        entry = JournalEntry(seq=seq, kind=kind, req_hash=req_hash, result=result)
        line = _encode(entry)
        if self._path is not None:
            if self._seal is not None:
                # Signed before the line lands, so a crash can leave a spare
                # signature — which reads as a crash — but never an unsigned
                # line, which reads as somebody having written it themselves.
                self._seal.seal_line(self._path.name, line.encode("utf-8"))
            _append(self._path, line)
        self._entries.append(replace(entry, result=json.loads(line)["result"]))

    @property
    def entries(self) -> tuple[JournalEntry, ...]:
        """The recorded calls in order, as an immutable snapshot."""
        return tuple(self._entries)

    def __len__(self) -> int:
        return len(self._entries)


__all__ = [
    "MAX_JOURNAL_BYTES",
    "Journal",
    "JournalDivergence",
    "JournalEntry",
    "JournalError",
    "JournalFull",
    "JournalTampered",
    "request_hash",
]
