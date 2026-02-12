# Build arcrun

Interactive design session for arcrun — the async execution engine. Think through decisions together, hash out the design, and produce a clear build summary that feeds into `/specify`.

**This is a conversation, not a checklist.** Ask questions, explore tradeoffs, challenge assumptions, and reach decisions together. The goal is a complete understanding of what we're building so `/specify` can formalize it.

## Procedure

1. Read state from `.claude/states/arcrun.json` (create if missing with defaults below)
2. Read ALL steering docs for project context:
   - `.claude/steering/product.md` — what arcrun is, design priorities, constraints
   - `.claude/steering/tech.md` — stack, arcllm integration, strategies, patterns
   - `.claude/steering/structure.md` — package layout, module boundaries, data flow
   - `.claude/steering/roadmap.md` — phases, steps, gates, decision governance
3. Read `.claude/decision-log.md` for decisions already made
4. Read the PRD (`arcrun-PRD.md`) for locked decisions and architecture
5. Orient — announce where we are:
   - Current phase and what's been decided so far
   - Any notes from previous sessions
   - What's next to figure out

6. **Start the conversation.** Use `AskUserQuestion` to drive the design discussion. For each area that needs decisions:

### How to Drive the Design Discussion

**Ask, don't lecture.** Present the question, give context on WHY it matters, offer 2-4 options with concrete tradeoffs, make a recommendation, and wait for Josh's answer.

**Go deep on each topic.** Don't just ask "A or B?" — explore:
- Why does this decision matter?
- What are the downstream implications?
- How does it interact with other components?
- What happens in edge cases?
- What does the caller experience?
- What would we regret later?

**Follow the thread.** When Josh picks a direction or raises a concern, explore it fully before moving on. Ask follow-up questions. Challenge back if something seems off. This is collaborative thinking, not form-filling.

**Use AskUserQuestion for every decision point.** Present options with clear labels and descriptions. Always include a recommendation. Wait for the answer before proceeding.

**Cover these areas** (adapt order based on what makes sense in the conversation):
- Component purpose and boundaries (what does it do, what doesn't it do)
- Public API surface (function signatures, parameter types, return values)
- Internal design (data structures, algorithms, patterns)
- Edge cases and error handling
- Interactions with other components (event bus, sandbox, registry, etc.)
- Acceptance criteria (what does "done" look like)

7. **Log decisions immediately.** After each decision is made, append to `.claude/decision-log.md` with:
   - Context (why needed)
   - Options considered
   - Choice made
   - Reasoning
   - Status

8. **Update state** after meaningful progress (decisions made, topics covered)

9. **When the design conversation is complete for the current scope**, produce the Build Summary (see below)

## Build Summary

When all decisions for the current scope have been made, output a complete summary **in the terminal** (not a file). This is what Josh reads before running `/specify`.

Format:

```
═══════════════════════════════════════════════════════
ARCRUN BUILD SUMMARY — [Scope Description]
═══════════════════════════════════════════════════════

## What We're Building
[1-3 sentence description of the scope]

## Decisions Made This Session
| # | Decision | Choice | Key Reasoning |
|---|----------|--------|---------------|
| DECISION-XXX | ... | ... | ... |

## Components
For each component in scope:

### [Component Name]
- **Purpose:** What it does in one sentence
- **File:** Where it lives
- **Public API:** Signatures and types
- **Key behaviors:**
  - Behavior 1
  - Behavior 2
- **Edge cases:**
  - Edge case 1 → handling
- **Interacts with:** [other components]

## Acceptance Criteria
- [ ] Criterion 1
- [ ] Criterion 2
- [ ] ...

## Line Budget
Estimated: ~N lines | Budget: N lines | Remaining: N lines

## Open Questions (if any)
- Question 1 (deferred because...)

═══════════════════════════════════════════════════════
Ready for: /specify
═══════════════════════════════════════════════════════
```

## State File Schema

Location: `.claude/states/arcrun.json`

```json
{
  "current_phase": 1,
  "current_step": "1.1",
  "completed_steps": [],
  "decisions_made": 0,
  "notes": [],
  "phase_gates": { "1": false, "2": false, "3": false, "4": false, "5": false },
  "line_count": 0,
  "last_session": null
}
```

## Key Rules

- **This is a conversation** — think WITH Josh, not AT him. Ask questions, explore together, challenge ideas.
- **Use AskUserQuestion at every decision point** — present options, explain tradeoffs, recommend, wait for answer
- **Go deep before moving on** — don't surface-skim topics. Explore implications, edge cases, interactions.
- **Reference locked decisions** from steering docs and decision log — don't re-ask what's settled
- **Adapt immediately** when Josh changes direction or raises a new concern
- **Log every decision** in `.claude/decision-log.md` the moment it's made
- **arcrun is the engine** — if something belongs to the caller (agent layer), say so
- **Under 1,000 lines** — track and flag if trending over
- **End with a Build Summary** — complete, specific, ready for `/specify`
- **No code, no specs, no files** — this command produces a conversation and a terminal summary, nothing else

## Arguments

- `$ARGUMENTS` — Optional: "status", "decisions", "summary", or a topic to focus on

## Usage

```
/build-arcrun              # Resume the design conversation
/build-arcrun status       # Show progress overview
/build-arcrun decisions    # Review all decisions made so far
/build-arcrun summary      # Re-output the build summary for current scope
/build-arcrun [topic]      # Focus conversation on a specific topic
```
