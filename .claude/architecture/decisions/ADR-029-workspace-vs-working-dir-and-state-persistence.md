# ADR-029: Agent Home (Workspace) vs Working Directory — and State Persists via Direct Workspace I/O

**Status**: Accepted
**Date**: 2026-07-26
**Builds on**: ADR-019 (Four Pillars universal), ADR-022 (storage split)
**Relates to**: SPEC-058 (arctui), the coding blueprint, folder-trust

## Context (in plain terms)

We want one agent — say a coding agent — that you can open in *any* project
directory and have it work on the code there, the way Claude Code and OpenCode
do. But an agent also has a life of its own: its **memory**, its running
**notes** (`context.md`), its **conversation history**, its **identity**. That is
the agent's *brain*, and it must live in one stable place — the agent's
**workspace** (its home, e.g. `~/.arc/coding/coding/workspace`).

These two ideas were the same thing in the code: the agent's `workspace` was both
its home *and* the directory its tools ran in. That conflation forces a bad
choice:

- Pin everything to the workspace → the agent can't naturally work in your
  project (you'd have to `cd` on every command; the model forgets).
- Point everything at your project (the cwd) → the agent's brain gets scattered:
  its memory would be written **into your repo**, a different copy per folder you
  ever opened, polluting your project and destroying the agent's ability to
  remember across projects.

Neither is acceptable. We need the agent to **work in your project** while its
**brain stays home**.

## Decision

**1. Separate "where the agent's tools operate" from "where the agent lives."**

A new `working_dir` is the directory the LLM's file/exec tools (`bash`, `read`,
`write`, `edit`, `grep`, `find`, `ls`) operate in — bash's cwd and the root for
relative paths. It defaults to the workspace (so every existing agent is
unchanged). A coding agent opts in (`[tools] operate_in_launch_dir = true`) and
then operates in the **project you launched in** (`ARC_WORKING_DIR`, supplied by
arctui as the trusted cwd).

`working_dir` never widens the sandbox: it is honored only when it is already
inside `workspace + allowed_paths` (the folder-trust prompt is what puts your
project there). It moves the *root*, never the *fence*. The boundary check
(`workspace + allowed_paths`) is unchanged.

**2. The load-bearing invariant that makes this safe:**

> **Framework/module state (memory, sessions, `context.md`, identity, the audit
> chain) MUST persist via direct workspace I/O — never by calling the LLM's file
> tools.**

Because the agent's own state is written with direct filesystem calls to the
workspace path (e.g. `Path.write_text`, `atomic_write_text`), and **not** by
invoking `write`/`bash`, moving the *tools'* working directory to your project
has **no effect on where the agent's state goes**. Memory loads from home and
writes home, whatever directory you're coding in.

This is why the separation works at all: the only things that follow you into
your project are the tools the LLM explicitly drives; everything that is the
agent's memory/brain goes around those tools, straight to the workspace.

## Consequences

- A coding agent works in your project (bash, tests, edits land there) with no
  `cd` gymnastics, while its memory/sessions/identity stay in its home — usable
  across many projects without cross-contamination.
- Secure by default: `operate_in_launch_dir` is off for every other agent; the
  sandbox boundary is unchanged; the working dir must be a trusted, already-allowed
  path.
- **New code must honor the invariant.** Any module or capability that persists
  agent state does direct workspace I/O; it must not dispatch the `write`/`bash`
  tools to save its own state (those are rooted at `working_dir` and would leak
  into the project). A *skill* that writes project files via the tools is correct
  — that's the point; a component saving agent state must use a workspace path.
- Enterprise/federal deployments where an agent's "project" is itself just its
  workspace get identical behavior by leaving the flag off.
