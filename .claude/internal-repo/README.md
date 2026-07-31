# arc-internal — private overlay repo

This repo holds Arc's **internal knowledge** — specs, ADRs, steering docs,
brainstorms, decision log, and everything else under `.claude/` and
`packages/*/.claude/` / `packages/*/architecture/`. It is **private** and must
never be visible to open-source users of `arc`.

## How it works (overlay, not submodule)

The public `arc` repo already git-ignores every internal path
(`**/.claude/`, `**/architecture/`, `CLAUDE.md`). Rather than move those files
into one folder and symlink them back, this repo **shares the same working
tree** as `arc` but stores its history in a separate git dir, `.git-internal`.

```
arc/
  .git/            <- public repo (github.com/joshuamschultz/Arc)
  .git-internal/   <- THIS repo   (github.com/joshuamschultz/arc-internal)   [ignored by public repo]
  .claude/                 tracked by arc-internal, ignored by arc
  packages/arcllm/
    src/...                tracked by arc, ignored by arc-internal
    .claude/...            tracked by arc-internal, ignored by arc
```

Two git contexts, one set of files. Nothing moves; tooling that expects
`.claude/` at its normal path keeps working.

## Setup on a new machine

1. Clone `arc` normally (`git clone git@github.com:joshuamschultz/Arc.git`).
2. Get read access to `arc-internal`.
3. From inside the arc clone, run:
   ```bash
   bash .claude/internal-repo/attach.sh
   ```
   (Or, before you have the files, copy the same commands from this README on
   the GitHub web UI.)

That installs a global `git internal` alias and checks the internal files into
place.

## Daily use

| Task | Command |
|------|---------|
| See internal changes | `git internal status` |
| Stage a **new** internal file | `git internal add -f <path>` |
| Stage edits to tracked files | `git internal add -u` |
| Commit | `git internal commit -m "..."` |
| Push / pull | `git internal push` / `git internal pull` |

**Why `-f` for new files?** The public `.gitignore` hides internal paths, and a
worktree `.gitignore` outranks the overlay's own ignore rules — so a brand-new
internal file looks "ignored" to `git internal add` until you force it once.
After that it's tracked and behaves normally. `git status` (the public repo)
never shows these files.

## What is NOT tracked here

`.claude/worktrees/` (throwaway), `settings.local.json` (machine-local),
`*.lock`, `.DS_Store`, and large re-downloadable reference PDFs
(`.claude/security/*.pdf`).
