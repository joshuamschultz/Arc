"""Operator custody of the files a dynamic run resumes from.

A resumed run trusts two files it did not write in this process: the script it
re-executes, and the journal of host calls it replays. Both live in the agent's
own writable workspace, so an agent holding ``write`` or ``bash`` can rewrite
either one between runs.

Validation cannot be the control here. A hijacking script is still a perfectly
grammatical script, so re-running the authoring gate on a pinned file proves
only that the file is well formed, never that it is the file this run agreed to.
Only integrity answers that: each artifact is signed with the **operator's** key
into a directory outside the workspace — the audited subject must not be its own
audit authority — and an artifact that does not match its signature is refused
rather than run (ASI06, SI-7).

The two artifacts seal as one thing. :meth:`RunSeal.bound_to` folds a digest of
the script into every signature the returned seal makes, so a journal recorded
under one script cannot be replayed under another. Signing them independently
would leave an attacker free to pair a rewritten script with the untouched
signatures of an old journal, or the reverse.

arcrun is a lower layer: it holds no key material, resolves no Arc-home path,
and knows no algorithm. The host injects a :class:`SealSigner` and the directory
its signatures live in. Injecting nothing is a symmetric no-op — nothing signed
and nothing verified — so personal and federal tiers run one code path.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from hashlib import sha256
from pathlib import Path
from typing import Protocol


class SealSigner(Protocol):
    """The operator's signing authority, as arcrun is allowed to see it.

    Deliberately opaque. A host with an operator key injects something that can
    sign and check bytes — an in-process ``Signer``, a vault client, an HSM
    boundary — and a host with none injects nothing at all.
    """

    def sign(self, message: bytes) -> bytes: ...

    def verify(self, message: bytes, signature: bytes) -> bool: ...


class SealBroken(Exception):  # noqa: N818 — domain convention: named for the condition
    """An artifact does not match the operator signature that sealed it.

    Always fail-closed: refuse the run. Falling back to "start fresh" would turn
    a detected rewrite into an invisible retry, which is exactly what an
    attacker rewriting the file wants.
    """


@dataclass(frozen=True, slots=True)
class RunSeal:
    """The operator's signatures over one run's resumable files.

    ``directory`` must sit **outside** the agent-writable workspace holding the
    artifacts. An agent can rewrite what its own file tools reach; it cannot
    produce the operator signatures over it. arcrun never chooses that location.

    ``context`` is the binding: every signature commits to it as well as to the
    content, so a seal narrowed by :meth:`bound_to` will not verify anything
    that was sealed before the narrowing. Never set it directly.
    """

    signer: SealSigner
    directory: Path
    context: bytes = b""

    def bound_to(self, content: bytes) -> RunSeal:
        """A seal whose every later signature also commits to ``content``.

        Binding the journal's seal to the script source is what makes the pair
        verify as one run rather than as two files that happen to sit together.
        """
        return replace(self, context=sha256(self.context + content).digest())

    def seal_file(self, name: str, content: bytes) -> None:
        """Sign a whole artifact, replacing whatever signature it had."""
        path = self._sidecar(name)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self._sign(content).hex() + "\n", encoding="utf-8")

    def verify_file(self, name: str, content: bytes) -> None:
        """Refuse an artifact the operator did not seal in this exact form.

        An unsigned artifact fails exactly as a rewritten one does: a missing
        signature is indistinguishable from a deleted signature, so there is
        nothing left to trust either way.
        """
        signatures = self.sealed_lines(name)
        if len(signatures) != 1 or not self.verifies(content, signatures[0]):
            raise SealBroken(
                f"{name} does not match an operator signature for this run; it was "
                "written or rewritten outside the run and must not be used"
            )

    def seal_line(self, name: str, line: bytes) -> None:
        """Sign one more line of an append-only artifact, flushed as it lands."""
        path = self._sidecar(name)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(self._sign(line).hex() + "\n")
            handle.flush()

    def sealed_lines(self, name: str) -> list[bytes]:
        """The signatures recorded for ``name``, in the order they were made."""
        path = self._sidecar(name)
        if not path.exists():
            return []
        try:
            tokens = path.read_text(encoding="utf-8").split()
        except OSError as exc:
            raise SealBroken(f"the seal for {name} could not be read: {exc}") from exc
        signatures: list[bytes] = []
        for index, token in enumerate(tokens):
            try:
                signatures.append(bytes.fromhex(token))
            except ValueError as exc:
                if index == len(tokens) - 1:
                    break  # the crash cut the signature in flight short
                raise SealBroken(f"the seal for {name} is malformed") from exc
        return signatures

    def verifies(self, content: bytes, signature: bytes) -> bool:
        """Whether ``signature`` is this seal's own over ``content``."""
        return self.signer.verify(self.context + content, signature)

    def _sign(self, content: bytes) -> bytes:
        return self.signer.sign(self.context + content)

    def _sidecar(self, name: str) -> Path:
        """Where ``name``'s signatures live — a bare filename, never a path."""
        if not name or name != Path(name).name:
            raise SealBroken(f"{name!r} is not an artifact name")
        return self.directory / f"{name}.sig"


__all__ = ["RunSeal", "SealBroken", "SealSigner"]
