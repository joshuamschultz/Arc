# SPEC-063 — Audit store protection

**Status:** notes only. Raised by SPEC-062, deliberately not solved there.
**Created:** 2026-08-06 · **Decision:** D-577

## Why this exists

SPEC-062 D-552 chose FULL audit capture — every connector call and response,
inputs and outputs — because a federal auditor must be able to reconstruct what
happened, not merely that it happened (NIST AU-3).

That ruling was right and it has a consequence: the audit store now holds every
email body, every document, and every ticket the fleet reads. It is the
highest-value target on the machine, and protecting it is not connector work.

Encryption at rest lands with SPEC-062. Access control and retention are here.

## What is already decided

- **Full capture** of calls and responses, with classification labels (D-552).
- **One carve-out**: a credential read records store, item, field, caller and
  outcome, never the value (D-553). Otherwise the tamper-evident chain becomes
  the richest credential database on the box.
- **Encrypted at rest** (D-577, shipping with SPEC-062).

## What this spec must answer

1. **Who may read it.** Today anyone who can read the log file can read every
   message body the fleet has touched. There is no reader-level control.
2. **Retention.** How long full bodies are kept, and what ages out first.
   Note the standing constraint: no crypto-shred, no erasure — GDPR-style
   deletion conflicts with AU-9/AU-10/AU-11 immutability. Retention purge only.
3. **Key custody.** Where the at-rest key lives, who can rotate it, and whether
   an auditor key is separate from an operator key.
4. **Classification-driven handling.** Labels are captured; nothing yet acts on
   them. Should CUI-labelled records live under stricter custody than the rest?
5. **The vendor-side gap.** Hosted connectors mean content also exists in the
   vendor's logs. Full local capture is not full custody, and the spec should
   say so plainly rather than imply completeness.

## Related

- `.claude/decisions-log.md` — D-552, D-553, D-577
- `.claude/specs/SPEC-062-connector-extensions/` — the source of the requirement
- `feedback_no_erasure_in_federal_audit` — why erasure is off the table
