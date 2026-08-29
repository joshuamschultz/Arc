# Alpha Release Hotfixes

Living tracker for the alpha hotfix batch. **No issue is done until it is checked
off here with its merge commit.** Every issue in the index must reach `MERGED`
before this batch is complete.

- **Integration branch:** `fix/hotfix-batch` (all fixes land here; merged to `main` only when the batch ships)
- **Started:** 2026-08-29

---

## Process (per issue)

1. **Write it up** — symptom, where it shows, expected vs actual. Recorded below.
2. **Find the cause** — codebase research agent maps the real root cause (file:line), not the symptom.
3. **Research the fix** — online research agent finds the current best-practice solution where the fix isn't obvious.
4. **Fix the root cause** — a coding agent implements in an **isolated git worktree**, TDD, honoring `CLAUDE.md`: robust + simple, modular (seam-clean), secure (four pillars), no legacy shims.
5. **Verify** — tests + `ruff` + `mypy --strict` green in the worktree.
6. **Merge** — merge the worktree branch back into `fix/hotfix-batch`, then mark `MERGED` with the commit hash. Worktree removed.

Agents used: **research-online** (web best-practice), **research-codebase** (root-cause map), **coder** (worktree implementation). Each coder runs in its own worktree so parallel fixes never contend on the tree.

**Guardrails:** root cause not band-aid · two-strikes then question the architecture · verify before claiming · no `git add -A` on a shared tree · sub-agents never run destructive git.

---

## Status legend

| Status | Meaning |
|--------|---------|
| `NEW` | Logged, not yet started |
| `CAUSE` | Root cause being mapped |
| `RESEARCH` | Best-fix research in flight |
| `CODING` | Being implemented in a worktree |
| `VERIFY` | Implemented; tests/gates running |
| `MERGED` | Merged into `fix/hotfix-batch` (commit noted) |

---

## Index

| # | Title | Status | Commit |
|---|-------|--------|--------|
| _(issues will be added here as you send them)_ | | | |

---

## Issues

<!-- One section per issue, appended as they arrive. Template:

### H-001 — <short title>
- **Status:** NEW
- **Symptom:** <what the user sees / where>
- **Expected:** <what should happen>
- **Root cause:** <file:line, filled after CAUSE>
- **Fix approach:** <filled after RESEARCH/CAUSE>
- **Worktree/branch:** <name>
- **Tests:** <what proves it>
- **Merge commit:** <hash, when MERGED>

-->
