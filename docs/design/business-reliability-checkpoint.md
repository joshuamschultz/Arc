# Business reliability clean checkpoint

> **Superseded status notice — 2026-09-23:** Local consolidation is complete at code checkpoint `06b80bad` and integration commit `d6b34f48` on Arc main; local branch/worktree cleanup is verified. Use the [business reliability consolidation handoff](business-reliability-consolidation.md) for current capability status. Historical branch and review instructions below are retained as evidence, not current directions. This does not claim a new push or deployment.

Checkpoint prepared with main at `3313b049`. This is a handoff snapshot, not an implementation or deployment acceptance record. Scope is frozen: preserve ongoing work and finish review or handoff; do not start feature or help implementation in this checkpoint.

## Main and deployment

- Main contains peer knowledge commit `fda6991f`, task merge `d9dba788` (including `4da16a52`), settings commits `0b1fbbbb` and `0a3d0613`, portable installer `ae7b72e9` (merge `978b31e5`), replay cleanup `61fbe309` (source `20254ebf`), and refreshed static assets `c7e02400`. Final scoped main gates pass: 171 architecture/replay tests (including 8 real-wheel checks), 524 adversarial tests, 168 task/settings Python tests, and 47 frontend tests; frontend lint and build pass. Full-repository Ruff cleanup passed and was merged in `c3593e1d`; current main checkpoint is `3313b049`.
- The deployment owner independently verified file hashes for 1,327 tracked source files on DGX at `fda6991f`; runtime reports `0.5.0-491cd5e6`, and health returned HTTP 200. Sync/load soak is not done. Commits after `fda6991f`, including the main merges above, are not deployed. Do not infer production readiness from the health response.
- Preserved WIP handoff branches are clean; original dirty snapshots remain retained at the paths below or in the stated merge history.
- Keep all workers open and frozen after this checkpoint. Preserve incomplete work on its named branch/worktree; do not start new implementation or merge work in progress as cleanup.

## Preserved work

| Work | Exact location | Checkpoint | State and handling |
|---|---|---|---|
| Arc dirty skills/auth work | `/Users/joshschultz/Projects/arc/.claude/worktrees/checkpoint-skills-auth-wip`, branch `codex/checkpoint-skills-auth-wip` | `3bc942b9` atop preserved `681153226dec899e2f54ad4648f4e61ac81af1c6`; 66 original dirty paths preserved | Skills handoff at `3bc942b9`. Keep the preserved base and scoped work intact; do not merge unfinished or aggregate WIP. |
| Original Arc Cloud dirty work | Cloud `main` merge history | Original snapshot commit `e4529a3` is preserved in history and superseded by recovery; its former `/private/tmp/arc-cloud-original-checkpoint` worktree was externally removed. Current cloud main `23e7fbc` is clean. | Root verified both commits reachable from cloud main and the current tree equal to `1ef0a6c`. The original snapshot is historical, not a live worktree. |
| Arc Cloud recovery source | `/Users/joshschultz/Projects/arc-cloud`, branch `main`; restored agent directory `/private/tmp/arc-cloud-reliability-checkout` at `1ef0a6c` | Cloud main merge `23e7fbc` includes recovery commit `1ef0a6c`; its tree matches `1ef0a6c` | Recovery source is now on cloud main. It is still **not launch-ready**: real grant, setup, and release are blocked/incomplete. Do not claim launch or deployment. |
| Account authority | `/Users/joshschultz/Projects/arc/.claude/worktrees/reliability-account-authority`, branch `codex/reliability-account-authority` | Clean handoff `db4824b7` atop WIP `5361d4c1` | Handoff is clean. No integration or production authority claim follows from that state. |
| Queue controls | `/Users/joshschultz/Projects/arc/.claude/worktrees/reliability-queue-controls`, branch `codex/reliability-queue-controls` | Clean WIP `16ad709f` | **HOLD MERGE** after final review. Scoped journal paging still reads unbounded metadata with `fetchall`; next session must design indexed, bounded, authorized listing. |
| Task pagination | Merged to main | `d9dba788`, including candidate `4da16a52` | Independent reviewer ran and approved 117 tests. Separately, the author supplied real-PostgreSQL snapshot-barrier evidence; the reviewer did not run PostgreSQL. Main verification: 168 task/settings Python tests and 47 frontend tests pass; frontend lint/build pass. Static assets refreshed in `c7e02400`. |

## Next-session handoff

1. Keep deployment evidence scoped to `fda6991f`: 1,327 tracked source-file hashes verified, runtime `0.5.0-491cd5e6`, health HTTP 200, no sync/load soak. Verify any later deployment independently; newer main commits are not deployed.
2. Keep the skills/auth snapshot, original Arc Cloud snapshot, and isolated feature branches intact. Compare preserved originals before replacing or integrating files.
3. Keep queue controls on **HOLD MERGE** until indexed, bounded, authorized journal metadata paging replaces the unbounded `fetchall`; leave that design and implementation for the next session.
4. Task pagination is merged. Preserve the documented task/skill method and schema boundaries for follow-up changes. Main asset refresh is committed as `c7e02400`; scoped gates and full-repository Ruff pass as recorded above.
5. Keep the clean account-authority handoff separate from deferred CLI, gateway, UI, or core wiring. Arc Cloud recovery `1ef0a6c` is on cloud main, but launch remains blocked on real grant, setup, and release work. Its original dirty snapshot is preserved in main history at `e4529a3`; the former snapshot worktree no longer exists.
6. Keep workers open and frozen at checkpoint. Update the execution ledger from verified hashes and review outcomes; record deployment evidence separately from local tests and code review.

No item in this handoff is claimed complete, accepted for release, or deployed solely by appearing in this checkpoint.
