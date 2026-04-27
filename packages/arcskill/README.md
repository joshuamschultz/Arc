# arcskill

Skill management hub for Arc. Validates, installs, scans, and locks skills for use by agents, run loops, and LLM contexts.

## Layer position

arcskill depends on arctrust (for audit and signing), arcllm, and arcrun. arcagent depends on arcskill for skill discovery and loading. arcskill is a standalone skill management pipeline and does not import from arcagent.

## What it provides

**Installed (wave-2, current):**

- `arcskill.hub.install`, `uninstall` — signed install pipeline: fetch to quarantine, Sigstore + Rekor verification, CRL check, regex/AST/semgrep/bandit scan, Firecracker/Docker dry-run sandbox, atomic activation, lock file entry
- `arcskill.hub.scan`, `ScanResult` — static analysis scanner; runs regex patterns, AST inspection, and optional semgrep + bandit passes; returns structured verdict
- `arcskill.hub.check_revocation_on_boot`, `quarantine_skill` — CRL-based lifecycle management; quarantine on revocation
- `arcskill.hub.HubConfig`, `TierPolicy`, `HubPolicy`, `SkillSource` — configuration schema; hub is inert unless `[skills.hub] enabled = true`
- `arcskill.hub.HubDisabled`, `SourceNotAllowed`, `SignatureInvalid`, `CRLUnreachable`, `SandboxRequired`, `ScanVerdictFailed`, `HubLockFileCorrupted` — typed error hierarchy
- `arcskill.lock.HubLockFile` — atomic JSON lock file recording every installed skill with content hash, Rekor UUID, SLSA level, scan verdict, and install path

**Deferred (wave-3 / future):**

- GEPA (skill improvement loop) relocation from arcagent to arcskill — not yet complete
- Eval harness for automated skill quality scoring
- Three-target skill loaders (load to LLM context, to arcrun loop, to arcagent) — API designed, not yet exposed
- Upgrade workflow (`arc skill upgrade`) — planned
- Full version-control beyond lock file (semver history, rollback)

## Quick example

```python
from arcskill.hub import install, HubConfig

config = HubConfig()  # reads [skills.hub] from arcagent.toml
await install("my-analysis-skill", source_url="https://hub.example/skills/analysis", config=config)
# -> fetches, verifies (Sigstore + Rekor), scans, sandboxes, activates, locks
```

## Scope

arcskill manages the lifecycle of skills for all three targets:
- Loading skills into an LLM context (as capability context)
- Loading skills into an arcrun loop (as inline instructions)
- Loading skills into an arcagent workspace (persistent discovery)

Current wave ships: validate, test, signed install, scan, partial version-control via lock, CRL lifecycle. The full GEPA improvement loop and eval harness are deferred to wave-3.

## Architecture references

- SPEC-018: Hermes Parity — skill verification requirements (Sigstore + Rekor + cert chain); `UnsafeNoOp` bypass eliminated at all tiers

## Status

- Tests: 342, 5 skipped (run with `uv run --no-sync pytest packages/arcskill/tests`)
- Coverage: 86%
- ruff + mypy --strict: clean
