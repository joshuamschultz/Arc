
## A security control's REFUSAL path must execute — coverage % hides this
*(SPEC-066 /review, 2026-08-13 — 7 blocking findings)*

SPEC-066 measured 81% line coverage, above the 80% gate. That number concealed that NONE of
these had ever run: the undeclared-payload-file refusal, the symlink-escape check, the
materializer's publish-rename rollback, the reinstall/backup path (no test installed a
module twice, so the "old tree stays recoverable" guarantee never executed), the
missing-manifest/signature refusals, 2 of 3 legs of `arc module remove`'s partial-completion
guarantee, and `arc up`'s degraded-fleet exit path.

One near-miss is instructive: the single durability test monkeypatched `os.fsync`, which
fails inside `_stage()` — BEFORE the rename it was believed to be testing. It looked like
rollback coverage and was not.

**How to apply:** for every control that exists to REFUSE something, write the test that
makes it refuse, and assert the refusal's effect (destination byte-identical to a
PRE-POPULATED snapshot, correct exception type, non-zero exit) — not merely that an
exception fired. Ask "which line does this test make execute?" and verify with
`--cov-report=term-missing` before and after. Aggregate percentage is not the question.
