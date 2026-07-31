# ADR-003: Pydantic BaseModel + Manual Env Overrides vs BaseSettings

**Status**: Accepted
**Date**: 2026-02-14
**Decision Makers**: Josh Schultz
**Relates to**: S001 Phase 1 Core Components, `config.py`

---

## Context

ArcAgent's configuration is loaded from `arcagent.toml` with environment variable overrides. The SDD (Section 2.1) specified `ArcAgentConfig(BaseSettings)` using `pydantic-settings`, but the implementation uses `ArcAgentConfig(BaseModel)` with a manual `_apply_env_overrides()` function.

### The Problem

Environment variable overrides are essential for deployment (12-factor app compliance, container orchestration, CI/CD). The question is whether to use `pydantic-settings` automatic env binding or manual env var parsing.

## Options Considered

### Option A: pydantic-settings BaseSettings (SDD Spec)

```python
from pydantic_settings import BaseSettings

class ArcAgentConfig(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="ARCAGENT_",
        env_nested_delimiter="__",
        toml_file="arcagent.toml",
    )
    agent: AgentConfig
    llm: LLMConfig
    ...
```

**How it works**: `pydantic-settings` automatically discovers env vars matching the prefix pattern, parses nested delimiters, handles type coercion, and supports multiple sources (env, .env files, TOML, YAML, JSON) with configurable priority.

### Option B: BaseModel + Manual Env Overrides (Chosen)

```python
from pydantic import BaseModel

class ArcAgentConfig(BaseModel):
    agent: AgentConfig
    llm: LLMConfig
    ...

def _apply_env_overrides(data: dict[str, Any]) -> dict[str, Any]:
    for key, value in os.environ.items():
        if not key.startswith(_ENV_PREFIX):
            continue
        env_path = key[len(_ENV_PREFIX):].lower()
        # Security denylist check
        if any(env_path.startswith(p) for p in _ENV_DENYLIST_PREFIXES):
            _logger.warning("Blocked env override: %s", key)
            continue
        parts = env_path.split(_ENV_DELIMITER)
        target = data
        for part in parts[:-1]:
            target = target.setdefault(part, {})
        target[parts[-1]] = value
    return data
```

**How it works**: TOML is parsed first into a raw dict. Env vars are overlaid onto the dict before Pydantic validation. This gives full control over the override pipeline, including security denylist enforcement.

## Decision

**Option B: BaseModel + Manual Env Overrides.**

## Rationale

### 1. Security Denylist Enforcement

The primary driver. ArcAgent operates in federal environments where certain config paths must not be overridable via environment variables:

```python
_ENV_DENYLIST_PREFIXES = frozenset({
    "vault__backend",     # Prevent vault injection attacks
    "tools__native",      # Prevent tool injection
    "tools__process",     # Prevent process injection
    "identity__key_dir",  # Prevent key directory redirection
})
```

`pydantic-settings` has no built-in denylist mechanism. Implementing it would require:
- A custom `Settings` subclass overriding `_settings_build_values()`
- Or a `model_validator` that runs after env binding and rejects denylisted values
- Both approaches are more complex and less transparent than the manual approach

### 2. Two-Phase Error Handling

ArcAgent's config loading has intentionally distinct error phases:

1. **TOML syntax errors** — reported with line/column from `tomllib`
2. **Env var override** — applied to raw dict (pre-validation)
3. **Pydantic validation** — reported with field paths

`BaseSettings` merges sources internally before validation, making it harder to distinguish TOML parse failures from env var type coercion failures. The manual approach gives clear error attribution.

### 3. Zero Additional Dependencies

`pydantic-settings` is a separate package (`pip install pydantic-settings`). The manual approach uses only `os.environ` and `tomllib` (stdlib). In federal environments, every dependency is a supply chain risk that requires auditing.

### 4. Explicit Override Semantics

The manual approach makes the override pipeline visible in a single function. Anyone reading `_apply_env_overrides` immediately understands:
- What prefix is used
- What keys are blocked
- How nesting works
- What the priority order is

With `BaseSettings`, this behavior is implicit in configuration and requires understanding pydantic-settings internals.

## Tradeoffs

| Concern | BaseSettings | Manual Overrides (Chosen) |
|---------|-------------|--------------------------|
| **Type coercion** | Automatic (env string → int/float/bool) | Manual (all values are strings until Pydantic validates) |
| **Nested model support** | Built-in with `__` delimiter | Manual dict traversal |
| **Security denylist** | Requires custom subclass | Native (denylist in override function) |
| **Error attribution** | Merged (harder to trace source) | Phased (TOML vs env vs validation) |
| **Dependencies** | +1 (pydantic-settings) | None (stdlib only) |
| **.env file support** | Built-in | Not implemented (not needed) |
| **Secrets from files** | Built-in (`_FILE` suffix) | Not needed (vault-based) |
| **Maintenance** | Framework-maintained | Self-maintained (~30 LOC) |
| **Test complexity** | Mock env + settings class | Mock env + dict |

### Key Tradeoff: Type Coercion

With `BaseSettings`, setting `ARCAGENT_LLM__MAX_TOKENS=8192` automatically coerces the string `"8192"` to `int`. With the manual approach, the string `"8192"` is injected into the dict, and Pydantic's `BaseModel` validation handles coercion during `ArcAgentConfig(**raw_data)`. Pydantic 2.x handles string-to-int coercion by default, so this is not a practical issue.

### Key Tradeoff: Maintenance

The manual override function is ~30 LOC. If ArcAgent needed complex multi-source config (YAML + TOML + .env + vault + env), `BaseSettings` would be the better choice. For TOML + env with a denylist, the manual approach is simpler.

## Prior Art

### Nanobot / CrewAI
Uses `pydantic-settings` BaseSettings with env prefix. No denylist concept — all config is overridable via env vars. Acceptable for developer tools; not acceptable for federal deployments.

### LangChain
Uses `pydantic-settings` for some config, manual env parsing for others. The `LANGCHAIN_API_KEY` env var is parsed manually; model config uses BaseSettings. Inconsistent but pragmatic.

### Semantic Kernel (Microsoft)
Uses a `KernelConfig` that loads from JSON/YAML with explicit env var substitution via `${ENV_VAR}` syntax in config files. No automatic env binding. Manual and explicit.

### Kubernetes / Helm
Kubernetes uses explicit env-to-config mapping in pod specs. No automatic discovery. Every override is declared. This is the model ArcAgent follows — explicit over implicit.

### 12-Factor App
The 12-factor methodology recommends env vars for config but doesn't mandate automatic binding. It cares about *separation of config from code*, which both approaches achieve.

## Consequences

### Positive
- Security denylist is a first-class feature, not a bolt-on
- Error messages clearly distinguish TOML syntax, env override, and validation failures
- No additional dependency (pydantic-settings)
- Override logic is 30 LOC, fully visible, fully testable
- Consistent with federal supply chain requirements

### Negative
- No automatic type coercion from env vars (Pydantic handles it during validation)
- No .env file support (not needed — vault-based secret management)
- No `_FILE` suffix for file-based secrets (not needed — vault-based)
- Must maintain override function ourselves (low maintenance burden for ~30 LOC)

### Migration Path

If future requirements demand multi-source config (e.g., adding YAML, .env files, or secrets-from-files), migrating to `BaseSettings` is straightforward:
1. Add `pydantic-settings` dependency
2. Change `ArcAgentConfig(BaseModel)` to `ArcAgentConfig(BaseSettings)`
3. Move denylist logic to a `model_validator(mode='before')`
4. Tests already use `monkeypatch.setenv` — no test changes needed

The migration is estimated at < 2 hours of effort.
