# Skill frontmatter — full spec

Required fields:

| Field | Type | Notes |
|---|---|---|
| `name` | str | Stable identifier; alphanumeric + dashes; matches folder name |
| `version` | str | Semver — bump on every change |
| `description` | str | Single sentence; appears in the prompt manifest |
| `triggers` | list[str] | Natural-language fragments; 3-7 entries |
| `tools` | list[str] | Tool names this skill expects to use |

Optional fields:

| Field | Type | Notes |
|---|---|---|
| `model_hint` | str | Preferred model size (`"haiku"`, `"sonnet"`, `"opus"`) |

## Format

```yaml
---
name: my-skill
version: 1.0.0
description: One sentence here.
triggers: [phrase one, phrase two, phrase three]
tools: [read, write, reload]
---
```

The block is YAML between two `---` lines. The opening `---` must be the first line of the file.

## Constraints

- The folder name must equal the `name` field.
- `triggers` should be lowercase fragments the LLM is likely to produce internally — not commands users type.
- `tools` must list every tool actually invoked in `## Steps`. Tools used only inside `scripts/` are not declared here.
- `version` increments per `update_skill` call.
