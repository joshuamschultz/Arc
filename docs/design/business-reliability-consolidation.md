# Business reliability consolidation handoff

## Current source and evidence

Arc main also contains committed canonical signed-grant primitives at `4fa78fce` (9 tests). The Arc main checkout contains committed help work at `c701b2cc` (190 frontend tests, including 143 focused help tests) and the workflow restart/failure-budget fix at `b122761b` (374 workflow tests; root reran the fresh suite). The workflow fix adds persistent fenced counters and healthy-run isolation. The `b122761b` workflow suite includes a regression proving that a lost database response after task materialization, followed by runner restart, does not duplicate deterministic task IDs.

All local worktrees and branches other than `main` were removed after clean ancestor checks. Arc Cloud remains a separate repository and checkout. No local source push or deployment of the newer work is claimed.

Peer-reported DGX/Azure deployment evidence is for `0e516029` only. It is not direct verification of newer local source and does not establish soak or production acceptance. The user confirmed a 30-day cancellation recovery window and source-state retention policy. Actual provider export, deletion execution, and backup behavior have not been verified.

## In-progress source and limits

- **Queue journal paging:** `packages/arcllm/src/arcllm/queue_journal.py` has in-progress format-3 authenticated bounded paging. Reported local performance is about 42 ms for 100 rows at 10,000 records. The recovery proof boundary now refuses absent authority and requires a same-root atomic verified lease CAS. Author-reported evidence: 71 tests (65 queue, 6 facade). The real broker adapter is absent and 10k restart recovery remains about 341 seconds, which is unacceptable; queue recovery and customer controls are not accepted.
- **Run/session correlation:** same-call tool/skill session/run correlation, canonical ArcRun call IDs, and removal of legacy shims are ongoing. Relevant seams are `packages/arcrun/src/arcrun/` and `packages/arcagent/src/arcagent/`; tests must establish the actual cross-layer call identity, not only local identifiers.
- **Hosted claim and setup:** Arc Cloud `POST /api/orders/{id}/setup` source checks email session, live payment and provider machine, and seals the outbox before delivery; the browser `/setup` page is in Arc at `packages/arcui/web/src/pages/setup.tsx`. Machine factory, durable restart recovery, and an actually configured Vault remain pending. Keep cloud source separate from Arc main and do not claim customer setup/release readiness.
- **Durable work ownership:** accepted-run, inbox, schedule, and pulse durability/ownership remain in progress. Task-materialization idempotence is covered by the `b122761b` lost-response/runner-restart regression; preserve that evidence as scoped workflow evidence.

## Resume map and acceptance

Continue on Arc `main` for Arc source work, and in the separate Arc Cloud checkout for hosted changes. Continue by integrating the queue recovery boundary with the real broker adapter and reducing unacceptable restart recovery time; then finish canonical call correlation and the durable accepted-run/inbox/schedule/pulse ownership paths. Preserve the `b122761b` task-materialization lost-response/restart regression evidence. Hosted work resumes at the incomplete signed first-claim flow in Arc Cloud and `/setup` at `packages/arcui/web/src/pages/setup.tsx`; add real startup factory/broker composition plus restart persistence.

Keep acceptance rows open until the actual customer composition, recovery and abuse paths have evidence. Unit and focused suite results are useful scoped evidence, not proof that all original requirements are complete. No new push, deployment, or soak is recorded. Provider export, deletion, and backups remain unverified despite the confirmed 30-day cancellation policy.

## Other open acceptance gaps

These workstreams remain open even while queue and hosted work take priority. See the [acceptance register](business-reliability-acceptance-2026-09-25.md) for capability-level criteria and paths.

- **Memory promotion and shared knowledge:** automatic memory promotion still lacks verified score grants, trusted source composition, and ArcTeam attachment/revocation fencing. Shared-knowledge entity promotion remains unavailable pending a signed contributor and provenance contract.
- **Approval reconciliation:** operators still need a reconciliation UI for `releasing` and `outcome_unknown` approval claims.
- **Skills and reports:** production skill signer/anchor factory and authenticated startup composition remain acceptance gates. Report preview has scoped local evidence, while deployment-scoped read policy and hosted authority composition remain open.

## Historical source checkpoint and evidence

The earlier consolidation recorded source checkpoint `06b80bad0035cc0716b08592b2c6ac241a48632a` integrated on Arc main at `d6b34f48`; memory source merge `d8e32a6f`; refreshed assets `dcbd3c18`; and ancestry including `02ebb651` and `06b80bad`. At that checkpoint the source tree matched `dcbd3c18`. At that checkpoint, reported gates were full Ruff; strict mypy over 961 source files; 548 adversarial tests; 61 focused memory tests; 54 frontend tests; frontend lint/build; and successful imports of all 10 Arc packages after worktree removal. Earlier targeted integration runs in `integrate-all` were UI/auth/chat/CLI 91 tests, loopback-retry `test_cli_up` 22 tests (one overlapping test), and skills/report/queue 63 tests. A direct `create_app` probe without the account factory returned HTTP 200 for static-token `/me` and HTTP 503 for account login. Independent memory review covered 16 focused tests. These are historical scoped results only; they do not describe the current tree or close acceptance rows. The full Python suite was not run at that checkpoint.

The earlier deployment snapshot was DGX `fda6991f`, with hashes checked for 1,327 tracked files and runtime `0.5.0-491cd5e6` returning HTTP 200. No sync/load soak was run. This has been superseded as the latest peer-reported deployment reference by `0e516029`; neither reference verifies newer local source. Historical Arc Cloud references include recovery commit `1ef0a6c`, source merge `23e7fbc`, docs handoff `05ee143`, and original snapshot `e4529a3`; current hosted acceptance remains open.
