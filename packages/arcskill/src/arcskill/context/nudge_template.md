---
name: nudge_template
description: Advisory skill-creation nudge message (ASI-09 compliant).
tunable: true
---
The last turn used {n_tools} tools successfully, recovered from an error, and doesn't match any existing skill (top coverage {coverage_pct:.0%}). If this workflow is likely to recur, consider calling `skill_manage(action='create', ...)`. Skip if one-off. Confirm with the user before committing.
