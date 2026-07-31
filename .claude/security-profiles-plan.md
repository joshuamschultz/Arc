# Security Level Profiles for Agent Creation

## Context

When `arc agent create` runs, it generates `arcagent.toml` with no security posture. The Arc packages have extensive security features (arcllm: PII, signing, audit, OTel, vault; arcrun: sandbox, limits; arcagent: identity, tool policy, telemetry), but nothing scaffolds them by security level.

This adds a `--security` choice to `arc agent create`/`init`/`build` that generates **three TOML files** per agent using each package's existing config format, with presets for the chosen level.

## Generated Files

```
my-agent/
  arcagent.toml     # Uses existing ArcAgentConfig format
  arcllm.toml       # Mirrors packages/arcllm/src/arcllm/config.toml format
  arcrun.toml       # NEW format for arcrun runtime params (max_turns, sandbox, etc.)
  workspace/
  tools/
```

## Security Levels

### `open` -- Dev/hobby
Everything off. Maximum flexibility. Current default behavior.

### `secure` -- Enterprise
PII redaction, audit, rate limiting, retry, fallback, tool deny-list, budget controls, telemetry export.

### `full` -- Federal (FedRAMP/NIST/CMMC)
Everything in secure + request signing, mTLS OTel, vault-backed keys, strict sandbox, tight budgets, code execution disabled.

## Profile Details

### arcagent.toml presets

| Setting | open | secure | full |
|---------|------|--------|------|
| `[security] level` | "open" | "secure" | "full" |
| `[vault] backend` | "" | "" | "vault:HashicorpBackend" |
| `[tools.policy] deny` | [] | ["execute_python"] | ["execute_python"] |
| `[tools.policy] timeout_seconds` | 30 | 30 | 15 |
| `[telemetry] export_traces` | false | true | true |

### arcllm.toml presets (mirrors `packages/arcllm/src/arcllm/config.toml` format)

| Setting | open | secure | full |
|---------|------|--------|------|
| `[modules.security] enabled` | false | true | true |
| `pii_enabled` | true | true | true |
| `signing_enabled` | true | false | true |
| `[modules.audit] enabled` | false | true | true |
| `[modules.rate_limit] enabled` | false | true | true |
| `requests_per_minute` | 60 | 120 | 60 |
| `[modules.budget] enabled` | false | true | true |
| `monthly_limit_usd` | 500.00 | 500.00 | 100.00 |
| `[modules.otel] enabled` | false | false | true |
| `insecure` | false | false | false |
| `[modules.telemetry] enabled` | false | true | true |
| `[modules.retry] enabled` | false | true | true |
| `[modules.fallback] enabled` | false | true | true |
| `[vault] backend` | "" | "" | "vault:HashicorpBackend" |

### arcrun.toml presets (NEW config format for arcrun)

arcrun currently takes runtime params as function args to `run()` (max_turns, sandbox, max_depth).
These are hardcoded or passed from the caller. This is a problem: users can't adjust loop limits,
sandbox rules, or spawn budgets without modifying code.

arcrun.toml introduces a config file for arcrun. The agent orchestrator (`arcagent/core/agent.py`)
will read this file and pass values to `arcrun.run()`. This keeps arcrun config-driven like arcllm.

| Setting | open | secure | full |
|---------|------|--------|------|
| `[loop] max_turns` | 25 | 15 | 10 |
| `[loop] max_depth` | 5 | 3 | 1 |
| `[sandbox] enabled` | false | true | true |
| `[sandbox] deny_tools` | [] | ["execute_python"] | ["execute_python"] |
| `[code_execution] enabled` | true | true | false |
| `[code_execution] timeout_seconds` | 60 | 30 | 0 |

**Follow-up work (separate task):** arcagent's `_execute_loop()` currently passes no max_turns/sandbox
to `arcrun.run()`. It needs to load arcrun.toml from the agent directory and pass the values through.
This plan generates the file; consumption is wired separately.

## Files Modified

### `packages/arccli/src/arccli/security_profiles.py` (NEW ~150 LOC)
- `SECURITY_LEVELS` -- list of valid levels
- `generate_arcagent_security(level)` -> dict of overrides to merge into arcagent.toml
- `generate_arcllm_toml(level)` -> str (complete arcllm.toml content)
- `generate_arcrun_toml(level)` -> str (complete arcrun.toml content)
- Pure data module, no click dependency, fully testable

### `packages/arccli/src/arccli/agent.py`
- `create` command: add `--security` option (choice: open/secure/full, default: open)
- `init` command: add `--security` option
- `build` wizard: add security level step (new Step 1.5 after agent name, before provider)
- Both `create` and `init`: call profile generators, write arcllm.toml + arcrun.toml
- Merge security overrides into arcagent.toml content before writing
- `_print_scaffold_summary`: show all 3 files
- `_run_validation`: check arcllm.toml + arcrun.toml exist and parse

### `packages/arccli/tests/test_security_profiles.py` (NEW)
- Each generator produces valid TOML (parseable by tomllib)
- Values match expected per level
- Level escalation: full >= secure >= open for every security knob

### `packages/arccli/tests/test_agent_create.py`
- Test `--security open` creates 3 files with security modules disabled
- Test `--security secure` has PII+audit+rate enabled
- Test `--security full` has signing+vault+OTel enabled
- Default (no flag) behaves same as `--security open`

## Implementation Order

1. Create `security_profiles.py` with profile data + generators
2. Write `test_security_profiles.py` (TDD)
3. Update `agent.py` create/init/build to wire `--security` flag
4. Update `test_agent_create.py` with integration tests
5. Verify: `ruff check`, `pytest`, all 3 TOMLs parseable

## Verification

```bash
# Functional
arc agent create test-open --dir /tmp --security open
arc agent create test-secure --dir /tmp --security secure
arc agent create test-full --dir /tmp --security full

# Inspect generated files
cat /tmp/test-full/arcagent.toml   # vault backend set
cat /tmp/test-full/arcllm.toml     # signing + mTLS otel
cat /tmp/test-full/arcrun.toml     # strict sandbox

# Tests
pytest packages/arccli/tests/test_security_profiles.py -v
pytest packages/arccli/tests/test_agent_create.py -v

# Lint
ruff check packages/arccli/
```
