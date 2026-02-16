# Build ArcLLM

Interactive guided builder for ArcLLM. Walks through each step conceptually, presents design decisions with tradeoffs, asks for your call, then gives build instructions.

## Procedure

1. Load the `arcllm-builder` skill from `.claude/skills/arcllm-builder/SKILL.md`
2. Read state from `.claude/arcllm-state.json` (create if missing)
3. Read the current step plan from `arcllm-step-{NN}-*.md` if it exists
4. Announce current position: step, task, last session context
5. Surface any notes from previous sessions
6. Resume the teaching flow:
   - Explain the current concept (what and why)
   - Present the next design decision with tradeoffs
   - Ask Josh to make the call (use AskUserQuestion for formal decisions)
   - **After all decisions: create formal spec** at `.claude/specs/{NNN}-{feature-name}/` (README.md, PRD.md, SDD.md, PLAN.md) — this is MANDATORY before any implementation
   - After spec: give specific build instructions (NOT code)
   - Verify together before moving on
7. Update state file at end of session

## Key Rules

- **Never write code for Josh** — give instructions to write it
- **Always ask at decision points** — present options, explain tradeoffs, wait for answer
- **Create formal spec before implementation** — spec gate is mandatory (see SKILL.md)
- **Reference locked decisions** from the master prompt — don't re-ask them
- **Track decisions** in state file with reasoning
- **One task at a time** — don't rush ahead
- **Adapt immediately** when Josh wants to change direction

## Arguments

- `$ARGUMENTS` — Optional: step number to jump to, or "status" to see progress

## Usage

```
/build-arcllm           # Resume where we left off
/build-arcllm status    # Show progress overview
/build-arcllm 3         # Jump to step 3
```
