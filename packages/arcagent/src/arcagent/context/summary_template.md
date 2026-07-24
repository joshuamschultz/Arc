---
name: summary_template
description: Template instructing the eval model to summarize a segment into the structured
  schema.
tunable: true
---
Summarize this conversation segment into the EXACT template below. Copy `goal` and `constraints` VERBATIM (do not paraphrase). Use concise bullets; leave a field blank only if truly empty. Do not invent facts.

goal: <original task, verbatim>
constraints: <security/user constraints, verbatim>
progress: <what is done so far, quantified>
key_facts: <durable facts learned, with provenance>
files_modified: <path: one-line change>
decisions: <decision: rationale>
rejected_approaches: <what was tried and failed, and why>
open_questions: <unresolved items blocking completion>
next_step: <single concrete next action>

CONVERSATION SEGMENT:

