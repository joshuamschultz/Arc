"""WitnessAnchor — external append-only witnessing of operator-signed heads.

The operator key closes the "audited subject == audit authority" gap, but it
leaves one residual: whoever can read the operator seed can re-sign a *local*
WORM chain. The federal mitigation (SPEC-053 REQ-009/010) is an EXTERNAL
witness the operator-key holder does not control: the operator-signed
checkpoint head is submitted to a second, separately-custodied append-only
medium. A forger with the operator key can rewrite the local chain, but cannot
retroactively remove the head from a log they do not own — so a rollback past
the last witnessed anchor is detectable.

This module adds NO new anchor format. The payload is the existing
``arcllm.trace_retention.build_checkpoint`` dict (head hash, record count, file
inventory) — the same one ``read_verified_anchor`` returns. Two
implementations, selected by config:

- :class:`AppendOnlyMediumWitness` — offline / air-gapped: append the head to a
  second custodied WORM medium (Open Question 2: the exact SCIF medium). This
  is the Must-have path.
- :class:`TransparencyLogWitness` — online Rekor-style submitter over an
  injected transport, for deployments with network egress (REQ-010 Should).
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

_logger = logging.getLogger("arctrust.witness")


class WitnessDivergenceError(RuntimeError):
    """The local operator-signed head is not attested by the external witness.

    Raised fail-closed at federal startup: the local WORM chain has diverged
    from what the separately-custodied witness holds (a rollback + re-anchor by
    a holder of the operator key, or a missing/unavailable witness). Non-federal
    tiers warn instead (SPEC-053 REQ-009).
    """


@runtime_checkable
class WitnessAnchor(Protocol):
    """External witness for an operator-signed checkpoint head.

    ``submit`` records the head in a medium the operator-key holder does not
    control and returns an inclusion proof; ``verify_inclusion`` proves a given
    checkpoint was witnessed.
    """

    def submit(self, checkpoint: dict[str, Any], signature: bytes) -> str: ...

    def verify_inclusion(self, checkpoint: dict[str, Any], proof: str) -> bool: ...


class AppendOnlyMediumWitness:
    """Offline/air-gapped witness: append the head to a second custodied file.

    The medium is a separate append-only file — in a SCIF, a second host or
    removable WORM medium, and MUST NOT live in the operator-key directory
    (the key holder must not also own the witness). ``submit`` appends one JSON
    line ``{head_hash, signature, checkpoint}`` and returns the head hash as the
    inclusion proof. ``verify_inclusion`` confirms that head is present.

    Append-only is enforced at the write boundary, not by mere convention: the
    medium is opened ``O_WRONLY|O_APPEND|O_CREAT|O_NOFOLLOW`` — never truncated,
    never seeked, never following a planted symlink. OS-level immutability of
    the medium is a deployment-provisioning step layered on top of this
    (``chattr +a`` on Linux; ``chflags uappnd`` on macOS/BSD — note macOS makes
    the file undeletable until the flag is cleared), applied out-of-band like
    the vault seam rather than in-process, so the runtime never holds the
    privilege to remove it.
    """

    def __init__(self, medium_path: Path) -> None:
        self._path = Path(medium_path)

    def submit(self, checkpoint: dict[str, Any], signature: bytes) -> str:
        head = str(checkpoint.get("head_hash", ""))
        self._path.parent.mkdir(parents=True, exist_ok=True)
        entry = {
            "head_hash": head,
            "signature": signature.hex(),
            "checkpoint": checkpoint,
        }
        line = json.dumps(entry, sort_keys=True, ensure_ascii=True, separators=(",", ":")) + "\n"
        flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW
        fd = os.open(self._path, flags, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as fh:
            fh.write(line)
        return head

    def verify_inclusion(self, checkpoint: dict[str, Any], proof: str) -> bool:
        head = str(checkpoint.get("head_hash", ""))
        if head != proof or not self._path.exists():
            return False
        for line in self._path.read_text(encoding="utf-8").splitlines():
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if entry.get("head_hash") == head:
                return True
        return False


@runtime_checkable
class TransparencyLogTransport(Protocol):
    """Network transport to a transparency log (Rekor-style).

    A production transport is an HTTP client to a Sigstore Rekor instance; the
    witness stays a thin submitter over this seam so arctrust holds no network
    code and tests inject an in-memory log.
    """

    def submit_entry(self, checkpoint: dict[str, Any], signature: bytes) -> str: ...

    def verify_entry(self, checkpoint: dict[str, Any], proof: str) -> bool: ...


class TransparencyLogWitness:
    """Online Rekor-style witness — a thin submitter over an injected transport.

    The operator-signed checkpoint is posted to an append-only Merkle
    transparency log which returns a signed inclusion proof; independent
    witnesses co-signing the log's tree head mean even the log operator (here,
    the operator-key holder) cannot present two histories. Adds no new format —
    it submits the existing checkpoint payload.
    """

    def __init__(self, *, transport: TransparencyLogTransport) -> None:
        self._transport = transport

    def submit(self, checkpoint: dict[str, Any], signature: bytes) -> str:
        return self._transport.submit_entry(checkpoint, signature)

    def verify_inclusion(self, checkpoint: dict[str, Any], proof: str) -> bool:
        return self._transport.verify_entry(checkpoint, proof)


def verify_local_head_witnessed(
    local_checkpoint: dict[str, Any] | None,
    witness: WitnessAnchor,
    *,
    federal: bool,
) -> None:
    """Verify the local operator-signed head is attested by the external witness.

    ``local_checkpoint`` is the newest verified anchor from the local WORM chain
    (``arctrust.read_verified_anchor``). This is the production wiring of
    :meth:`WitnessAnchor.verify_inclusion` (SPEC-053 REQ-009): if the local head
    is not present in the separately-custodied witness, the local chain has been
    rolled back and re-anchored by a holder of the operator key, or the witness
    is missing/unavailable.

    A deployment with nothing anchored yet (``local_checkpoint is None``) is a
    clean bootstrap and passes. Otherwise, at federal tier a divergence raises
    :class:`WitnessDivergenceError` (fail closed); below federal it warns.
    """
    if local_checkpoint is None:
        return
    head = str(local_checkpoint.get("head_hash", ""))
    if not head:
        return
    try:
        included = witness.verify_inclusion(local_checkpoint, head)
    except Exception:  # reason: an unavailable witness is a divergence signal
        included = False
    if included:
        return
    message = (
        "local operator-signed head is not attested by the external witness — "
        "possible rollback past the last witnessed anchor, or an unavailable "
        f"witness (SPEC-053 REQ-009, head={head[:16]})"
    )
    if federal:
        raise WitnessDivergenceError(message)
    _logger.warning(message)


__all__ = [
    "AppendOnlyMediumWitness",
    "TransparencyLogTransport",
    "TransparencyLogWitness",
    "WitnessAnchor",
    "WitnessDivergenceError",
    "verify_local_head_witnessed",
]
