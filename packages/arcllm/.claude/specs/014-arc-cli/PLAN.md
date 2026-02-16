# PLAN — Arc CLI (014)

**Status**: COMPLETE
**Estimated tasks**: 12
**Completed**: 12/12

---

## Phase 1: Project Skeleton

### Task 1.1: Create arccli project at ~/AI/arccli/
- [x] Create `~/AI/arccli/` directory
- [x] Create `pyproject.toml` with project name `arccli`, deps: `click>=8.0`, `arcllm` (path dep or pip)
- [x] Add `[project.scripts]` entry: `arc = "arccli.main:cli"`
- [x] Create `src/arccli/__init__.py` with `__version__ = "0.1.0"`
- [x] Create `src/arccli/__main__.py` that calls `main.cli()`
- [x] Create `src/arccli/main.py` with root `arc` Click group
- [x] Create `src/arccli/llm.py` with `llm` Click subgroup registered on `cli`
- [x] Create `src/arccli/formatting.py` with `print_table()`, `print_json()`, `print_kv()`
- [x] Create `tests/` directory
- [x] `git init` and initial commit
- [x] `pip install -e .` to install in dev mode

**Acceptance**: `arc --help` shows the `llm` subgroup. `arc llm --help` shows placeholder.

---

## Phase 2: Read-Only Commands (config, providers, provider, models, version)

### Task 2.1: `arc llm version`
- [x] Write test: CliRunner invokes `arc llm version`, exit code 0, output contains version string
- [x] Implement: prints arcllm version, Python version
- [x] Test `--json` flag outputs valid JSON

**Acceptance**: `arc llm version` prints version. `arc llm version --json` outputs JSON.

### Task 2.2: `arc llm config`
- [x] Write test: CliRunner invokes `arc llm config`, shows defaults/modules/vault sections
- [x] Implement: loads `load_global_config()`, formats as key-value sections
- [x] Test `--module telemetry` shows only telemetry config
- [x] Test `--json` outputs full config as JSON

**Acceptance**: `arc llm config` displays readable config. Filters and JSON work.

### Task 2.3: `arc llm providers`
- [x] Write test: CliRunner invokes `arc llm providers`, shows table of providers
- [x] Implement: scan providers/ directory for TOML files, load each, display table
- [x] Table columns: Name, API Format, Default Model
- [x] Test `--json` outputs provider list as JSON array

**Acceptance**: `arc llm providers` shows table of all 12 providers.

### Task 2.4: `arc llm provider <name>`
- [x] Write test: CliRunner invokes `arc llm provider anthropic`, shows settings + models
- [x] Implement: load provider config, display settings section + models table
- [x] Models table columns: Model, Context Window, Max Output, Tools, Vision, Input $/1M, Output $/1M
- [x] Test unknown provider gives friendly error
- [x] Test `--json` outputs full provider config as JSON

**Acceptance**: `arc llm provider anthropic` shows settings and 2 model entries.

### Task 2.5: `arc llm models`
- [x] Write test: CliRunner invokes `arc llm models`, shows flat table
- [x] Implement: iterate all provider TOMLs, collect models, display flat table
- [x] Table columns: Provider, Model, Context, Tools, Vision, Input $/1M, Output $/1M
- [x] Test `--provider anthropic` filters
- [x] Test `--tools` and `--vision` capability filters
- [x] Test `--json` outputs model list as JSON array

**Acceptance**: `arc llm models` shows all models across providers.

---

## Phase 3: Call Command

### Task 3.1: `arc llm call` — basic
- [x] Write test: CliRunner with mock adapter, verify response output
- [x] Implement: `asyncio.run()` bridge, `load_model()`, `invoke()`, print response content
- [x] Handle context manager (`async with load_model(...) as model`)
- [x] Test exit code 0 on success, non-zero on error

**Acceptance**: `arc llm call anthropic "test"` makes a call and prints the response.

### Task 3.2: `arc llm call` — flags
- [x] Add `--model`, `--temperature`, `--max-tokens`, `--system` flags
- [x] Write tests: verify flags pass through to load_model() and invoke()
- [x] `--system` prepends a system message to the messages list

**Acceptance**: All generation parameter flags work.

### Task 3.3: `arc llm call` — module toggles
- [x] Add `--retry/--no-retry`, `--fallback/--no-fallback`, `--rate-limit/--no-rate-limit`
- [x] Add `--telemetry/--no-telemetry`, `--audit/--no-audit`, `--security/--no-security`, `--otel/--no-otel`
- [x] Write tests: verify boolean flags map to load_model() kwargs
- [x] Default is `None` (use config.toml setting)

**Acceptance**: Module toggle flags correctly enable/disable modules.

### Task 3.4: `arc llm call` — output modes
- [x] Default: print response content as text
- [x] `--json`: print full LLMResponse as JSON (model, usage, cost, stop_reason, etc.)
- [x] `--verbose`: print module activity (timing, cost, tokens) above response
- [x] Write tests for each output mode

**Acceptance**: All three output modes work correctly.

---

## Phase 4: Validate Command

### Task 4.1: `arc llm validate`
- [x] Write test: all configs valid -> success message
- [x] Implement: load global config, iterate providers, check API keys
- [x] Report per-provider: config valid (yes/no), API key set (yes/no)
- [x] Test `--provider anthropic` validates single provider
- [x] Test `--json` outputs validation results as JSON

**Acceptance**: `arc llm validate` checks all configs and API keys, reports status.

---

## Phase 5: Documentation + Polish

### Task 5.1: CLI README guide
- [x] Create `CLI.md` at repo root
- [x] Document every command with examples
- [x] Include installation, quickstart, all flags, output samples
- [x] Include JSON output examples for scripting

**Acceptance**: Complete CLI reference document.

### Task 5.2: Error handling polish
- [x] Catch ArcLLMConfigError -> friendly message (provider command)
- [x] Catch general exceptions -> click.ClickException (call command)
- [x] All errors exit with non-zero code
- [x] Unknown module/provider -> clear error message

**Acceptance**: All error paths produce clear, helpful messages.
