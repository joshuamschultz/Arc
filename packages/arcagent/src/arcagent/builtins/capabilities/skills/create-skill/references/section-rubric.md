# Section-quality rubric

A skill is judged section by section. Each header below is mandatory and has a defined purpose. A skill with all headers but filler bodies is worse than a skill with three good sections — it lowers trust in every other skill.

## ## Resources

**What it is:** an inventory of files in the skill folder.
**Who writes it:** the loader, automatically, on every reload.
**Your job:** leave it empty. Anything you add will be overwritten.

## ## Contract

**Purpose:** what the skill needs from the caller, what it produces.
**Format:** two prose paragraphs — "Inputs you must have:" and "Outputs the agent must produce:".
**Anti-pattern:** "N/A" — every skill has a contract.

## ## Knowledge

**Purpose:** background context the LLM needs to apply the skill correctly. Why this approach, what alternatives exist, what the constraints are.
**Format:** prose with selective references to files in `references/`.
**Anti-pattern:** repeating what tool docstrings already say. Knowledge is the *why*, not the *what*.

## ## Steps

**Purpose:** the procedure itself, in order.
**Format:** numbered list. Each step should be one tool call or one decision point.
**Anti-pattern:** vague steps ("review the work"). Steps must be unambiguous: "call `read(file_path=...)` and verify N lines".

## ## Anti Patterns

**Purpose:** failure modes the LLM is likely to fall into and how to avoid them.
**Format:** bulleted list, each starting with **"Don't"**. Optionally followed by an explanation.
**Anti-pattern:** generic advice ("don't break things"). Anti-patterns must be specific to this skill's failure modes.

## ## Examples

**Purpose:** concrete invocation examples.
**Format:** code blocks showing tool calls. Optionally annotated with what the response should look like.
**Anti-pattern:** "TODO" or pseudo-code. If you can't write a real example, the skill isn't ready.

## ## Validation

**Purpose:** how to confirm the skill ran successfully.
**Format:** checklist of post-conditions.
**Anti-pattern:** subjective ("looks good"). Each check must be a concrete, observable predicate ("file X exists", "reload returned `+1 added`", "test Y passes").

## Filler taxonomy

The validator flags these as filler in any non-Resources section:
- empty body
- "N/A" (case-insensitive)
- "none"
- "TBD"

If you can't write content for a section, the skill isn't ready to ship. Don't pad with filler — split the skill or rewrite the parent procedure.
