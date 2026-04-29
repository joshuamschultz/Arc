# `@tool` decorator — full field reference

| Field | Type | Default | Required? | Notes |
|---|---|---|---|---|
| `name` | `str` | function name | no | Override only if the function name doesn't match the LLM-facing name |
| `description` | `str` | function docstring (stripped) | no, but always set explicitly | Single sentence, imperative |
| `classification` | `"read_only"` \| `"state_modifying"` | `"state_modifying"` | recommended | `read_only` enables parallel dispatch |
| `capability_tags` | `Iterable[str]` | `()` | no | Tags drive policy decisions (e.g., `network_egress`, `file_write`) |
| `when_to_use` | `str` | `""` | recommended | Shown in the prompt manifest |
| `requires_skill` | `str \| None` | `None` | only for self-mod tools | Names the skill that teaches the tool |
| `version` | `str` | `"1.0.0"` | yes | Semver — bump on every change |
| `examples` | `Iterable[str]` | `()` | no | Sample call strings |
| `model_hint` | `str \| None` | `None` | no | Preferred model size, e.g., `"haiku"` |

## Behaviour

- The decorator stamps `func._arc_capability_meta` with a frozen `ToolMetadata` instance.
- The stamp is read by `CapabilityLoader` during scan — no explicit registration list.
- Schema is inferred from the function signature via `typing.get_type_hints`. Don't hand-write JSON schema.
- The decorated function must be `async`.

## Common combinations

| Use case | Suggested values |
|---|---|
| Simple file read | `classification="read_only"`, `capability_tags=["file_read"]` |
| Subprocess execution | `classification="state_modifying"`, `capability_tags=["subprocess", "state_mutation"]` |
| Network request | `classification="state_modifying"`, `capability_tags=["network_egress"]` |
| Self-modification | `classification="state_modifying"`, `requires_skill="<your-skill>"`, `capability_tags=["self_modification"]` |
