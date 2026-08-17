# SPEC-069 — `classification` is enforced but not signed

| | |
|---|---|
| **Status** | **DRAFT — raised, not scheduled.** Found during SPEC-068; deliberately not folded into it. |
| **Type** | security |
| **Package(s)** | `arcteam` |
| **Relates to** | SPEC-038 (budgets + classification), SPEC-037 (asymmetric signing), SPEC-068 |

## The finding

`arcteam.types.Message.classification` drives a real security control. On every
send, `MessagingService._enforce_no_write_down` parses it and refuses the message
unless the recipient's or channel's clearance dominates it (SPEC-038 REQ-024),
and `_fanout_mentions_to_inboxes` silently skips an under-cleared mention on the
same basis.

**It is not covered by the message signature.**
`arcteam/crypto.py:24-38`:

```python
_SIGNED_FIELDS = (
    "id", "ts", "nonce", "signer_did", "sender", "to", "thread_id", "hop",
    "msg_type", "priority", "action_required", "body", "mentions", "refs",
)
```

`body`, `mentions`, `priority` and `action_required` are all covered. So is
`hop`, added by SPEC-068 precisely because a loop guard an adversary can clear is
not a guard. `classification` is not, and neither is `meta`.

## Why that matters

`_verify_origin` runs on **every** delivery — `receive` and each `subscribe`
dispatch — and proves that the signer is registered and that `sender` binds to
`signer_did`. What it cannot prove is that the classification is the one the
sender stamped.

The enforcement point and the verification point are therefore different
trust domains:

- **On send**, in the sender's process, the classification is trustworthy and
  the no-write-down gate is meaningful.
- **In transit and on delivery**, it is mutable without invalidating the
  signature. Anything able to write the stream can relabel `SECRET` as
  `UNCLASSIFIED` on a message whose body and signature stay valid, and every
  downstream check — including the receiving agent's — will believe it.

This is not a live exploit in a single-host deployment where the broker is
trusted, which is why it is not being fixed inside a UI spec. It is a gap in the
tamper-evidence story that FedRAMP / NIST 800-53 (AU, AC families) reviewers
will ask about, on a field whose entire purpose is to be trusted.

## Shape of the fix

One line in `_SIGNED_FIELDS`, plus the consequences of it:

1. Add `classification` to `_SIGNED_FIELDS`.
2. Decide `meta` deliberately in the same change. It currently carries
   `operator_token_did` — unsigned attribution, documented as such
   (`arcui/messaging.py:250-258`). Either sign it or state in the model why it
   is intentionally outside the envelope; leaving the question open is what let
   `classification` drift out in the first place.
3. Confirm no producer sets `classification` *after* signing. `send()` signs at
   line 336-339 and stamps classification before that, so the ordering is
   already correct — but it becomes load-bearing once signed, and needs a test
   that fails if a future edit reorders it.

## Why it is not folded into SPEC-068

SPEC-068 adds `hop` to the same tuple, so bundling would have been easy and
wrong. This is a classification-integrity question, not a group-chat question:
it has its own threat model, its own reviewers, and its own regression surface
(every stored message's signature changes when the signed field set changes).
Hiding it inside a change about a compose box would make it invisible in exactly
the review that should catch it.

## Not yet decided

- Whether existing stored messages need re-signing or whether the field set is
  versioned. This repo does not ship migration shims by policy, and it is
  local-only and not deployed, so the likely answer is "neither" — but it should
  be an explicit decision, not an omission.
