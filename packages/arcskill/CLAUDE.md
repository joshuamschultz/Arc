# arcskill

> **Build standards:** repo root [`CLAUDE.md`](../../CLAUDE.md). This file is package-local — goal, layout, seams, and rules for fast correct work here.

## Goal

Skill supply-chain hub: signed install (Sigstore + Rekor), AST scan, sandboxed dry-run, atomic activate, lock/CRL lifecycle. Optional improver path (SPEC-044).

## Layer

Depends only on `arctrust` (+ pydantic/yaml). Used from `arcagent` skill modules, CLI install paths, and `arcui`. **Never imports `arcprompt`** — overlay resolver is injected by the consumer.

## Layout

```
src/arcskill/
  hub/              # install, verify, scan, lifecycle, dry-run / docker, MODULE.yaml
  lock.py           # HubLockFile
  context/          # Stock prompt markdown (shipped in wheel)
  improver/         # Optional self-improvement (SPEC-044/054)
    mutate / evalgate / guardrails / lifecycle   # code-repair, golden gate, bounds, Curator
    suitegen / promote / toggles / nudge/        # eval bootstrap: suite-gen, trace promote, toggles, signals
    seams.py                                     # injected Protocol seams (LLM/sandbox/sign/audit)
```

Top-level package exports little; stable surface is `arcskill.hub` and `arcskill.lock`.
`arcskill.improver` exports `ArcSkillImprover` / `ImproverConfig`; arcagent wires the
seams through `arcagent.skilladapt` and drives it via the `SkillAdapter` Protocol.

## Entry points

`arcskill.hub`: `install` / `uninstall` / `update`, `HubConfig`, `scan`, revocation helpers, hub errors. Also `arcskill.lock`.

## Package rules

- Hub is **inert** until `[skills.hub] enabled = true`.
- Gates (signature, AST scan, dry-run) are mandatory before activation — don't bypass for convenience.
- Keep improver free of direct provider imports (`tests/architecture/test_improver_no_provider_import.py`); LLM via injected seam.
- Inject prompt resolvers; do not import `arcprompt`.

## Tests

`packages/arcskill/tests/` — `unit/hub`, `unit/improver`, `integration/` (federal pipeline, Sigstore, CRL, attack surface), architecture.

## Working here

Treat supply-chain as security-critical (ASI04 / LLM03). Prefer fail-closed. Dry-run / docker paths share backends with `arcrun` — respect sandbox boundaries.
