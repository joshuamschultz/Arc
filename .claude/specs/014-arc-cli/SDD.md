# SDD — Arc CLI

## Design Overview

The Arc CLI is a separate Python package (`arccli`) that imports from `arcllm` and exposes its capabilities as terminal commands via Click. The `arc` command is the root entry point, with `llm` as a subgroup. Future products register as sibling groups (`arc run`, `arc agent`).

```
arc (root group)
└── llm (subgroup)
    ├── config
    ├── providers
    ├── provider
    ├── models
    ├── call
    ├── validate
    └── version
```

## Directory Map

```
~/AI/
├── arcllm/            # Existing library (unchanged, separate project)
│   └── src/arcllm/
└── arccli/            # NEW — CLI project (separate repo)
    ├── pyproject.toml # Own project config, depends on arcllm
    ├── src/
    │   └── arccli/
    │       ├── __init__.py    # Version constant
    │       ├── __main__.py    # `python -m arccli` support
    │       ├── main.py        # Root `arc` Click group
    │       ├── llm.py         # All `arc llm` subcommands
    │       └── formatting.py  # Table/JSON output helpers
    └── tests/
        └── test_llm.py
```

## Component Design

### 1. `arccli/__init__.py`

| Attribute | Type | Purpose |
|-----------|------|---------|
| `__version__` | `str` | Package version string |

### 2. `arccli/__main__.py`

Entry point for `python -m arccli`. Calls `main.cli()`.

### 3. `arccli/main.py` — Root CLI Group

| Component | Type | Purpose |
|-----------|------|---------|
| `cli` | `click.Group` | Root `arc` command group |

The root group is the single entry point registered in `pyproject.toml`. It uses `click.Group` (not a command) so subgroups can register themselves.

Future extensibility: `arc run` commands import and add themselves to `cli` as a subgroup.

### 4. `arccli/llm.py` — LLM Subcommands

| Command | Parameters | Flags | Returns |
|---------|------------|-------|---------|
| `config` | — | `--module <name>`, `--json` | Global config display |
| `providers` | — | `--json` | Provider list table |
| `provider` | `name` (arg) | `--json` | Provider detail (always shows models) |
| `models` | — | `--provider <name>`, `--tools`, `--vision`, `--json` | Model list table |
| `call` | `provider` (arg), `prompt` (arg) | `--model`, `--temperature`, `--max-tokens`, `--system`, `--retry/--no-retry`, `--fallback/--no-fallback`, `--rate-limit/--no-rate-limit`, `--telemetry/--no-telemetry`, `--audit/--no-audit`, `--security/--no-security`, `--otel/--no-otel`, `--verbose`, `--json` | LLM response |
| `validate` | — | `--provider <name>`, `--json` | Validation results |
| `version` | — | `--json` | Version info |

**Key implementation detail for `call`**: Must run async code (arcllm is async-first). Use `asyncio.run()` to bridge Click's sync world to arcllm's async `invoke()`.

### 5. `arccli/formatting.py` — Output Helpers

| Function | Purpose |
|----------|---------|
| `print_table(headers, rows)` | Print aligned ASCII table to stdout |
| `print_json(data)` | Print indented JSON to stdout |
| `print_kv(pairs)` | Print key-value pairs aligned |

Tables are simple ASCII — no dependency on `rich` or `tabulate`. Just string formatting with column width calculation.

## Architecture Decision Records

### ADR-014-1: Separate Project at ~/AI/arccli/

**Context**: CLI serves all arc products (arcllm, arcrun, arcagent). It shouldn't live inside any single product.

**Decision**: Create `~/AI/arccli/` as its own project with its own `pyproject.toml`. Depends on arcllm (and future arc products) as external pip dependencies.

**Rationale**: Clean dependency direction (arccli → arcllm/arcrun/arcagent, never reverse). Lives at the same directory level as the products it serves. Each product is independently installable. Adding arcrun support means adding `src/arccli/run.py` and adding `arcrun` to dependencies.

**Alternatives rejected**:
- CLI inside arcllm (`src/arcllm/cli/`): Can't serve other arc products
- Same repo as arcllm (`src/arccli/`): Couples CLI releases to arcllm releases, confusing when it also depends on arcrun

### ADR-014-2: Click Over Typer

**Context**: Need a CLI framework for nested subcommands with many flags.

**Decision**: Use Click directly.

**Rationale**: Most battle-tested Python CLI framework. Decorator syntax is explicit. No abstraction layer to debug through. Used by pip, Flask, AWS CLI.

**Alternatives rejected**:
- Typer: Adds abstraction over Click. When edge cases arise (and they will with module toggle flags), debugging two frameworks is worse.
- argparse: Too verbose for this many commands/flags. Would need ~3x more code.

### ADR-014-3: ASCII Tables Without Dependencies

**Context**: Need human-readable table output for provider/model listings.

**Decision**: Build simple ASCII tables with string formatting. No rich/tabulate dependency.

**Rationale**: Zero new dependencies beyond click. Tables are simple (5-10 columns max). Column width calculation is ~20 lines of code. Keeps package lightweight.

**Alternatives rejected**:
- rich: Gorgeous output but heavy dependency for a CLI that should be fast to import
- tabulate: Light but another dependency for ~20 lines of custom code

### ADR-014-4: asyncio.run() Bridge for Call Command

**Context**: ArcLLM is async-first (`async def invoke()`). Click commands are sync.

**Decision**: Use `asyncio.run()` in the `call` command to bridge sync Click to async arcllm.

**Rationale**: Simple, correct, standard Python pattern. The CLI is a one-shot caller — no event loop management needed. `asyncio.run()` creates and destroys the loop cleanly.

**Alternatives rejected**:
- anyio: Unnecessary abstraction for a simple bridge
- Custom event loop management: Over-engineering for one-shot calls

## Edge Cases

| Scenario | Handling |
|----------|----------|
| Provider TOML doesn't exist | Click error message: "Provider 'x' not found. Run `arc llm providers` to see available." |
| API key not set | `call` fails with clear message: "Set ANTHROPIC_API_KEY environment variable" |
| Invalid model name | Pass through to adapter — adapter returns error, CLI prints it |
| Config TOML malformed | arcllm raises ArcLLMConfigError, CLI catches and prints friendly message |
| No providers directory | `providers` command shows empty table with note |
| `call` with module flag but module deps missing | arcllm raises error, CLI catches and suggests `pip install arcllm[otel]` |
| Very long prompt (arg) | Click handles fine — for very long prompts, users can pipe from file later |
| Network timeout | httpx.TimeoutException caught, CLI prints "Request timed out" |

## Test Strategy

| Test Type | Scope | Framework |
|-----------|-------|-----------|
| Unit | Each command function | pytest + click.testing.CliRunner |
| Integration | Full command execution against mock/real providers | pytest + CliRunner |
| Snapshot | Table output format stability | pytest |

Click's `CliRunner` invokes commands in-process without subprocess overhead. Tests verify exit codes, stdout content, and JSON structure.

### Key Test Scenarios

1. `arc llm config` — shows config, JSON mode works
2. `arc llm providers` — lists all providers, empty case
3. `arc llm provider anthropic` — shows detail, unknown provider error
4. `arc llm models` — lists all, filters work
5. `arc llm call` — mock adapter, verify response formatting
6. `arc llm call` with module flags — verify flags pass through to load_model()
7. `arc llm validate` — all pass, some fail, specific provider
8. `arc llm version` — shows version string
9. `--json` on every command — valid JSON output
10. `--help` on every command — help text renders
