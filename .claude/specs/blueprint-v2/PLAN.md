# Blueprint v2 + sales-exec-assistant — PLAN

**Branch:** `blueprint-v2` · **Status:** COMPLETE · Config-only (no MCP/connectors this pass).

Companion brainstorm: `.claude/brainstorms/2026-07-26-sales-exec-blueprint.md`.

## Goal

A non-technical user runs `arc init --team sales --blueprint sales-exec-assistant`
and gets a running agent that: has a rich revenue-lens **identity**, remembers
**deals/contacts/companies/meetings/commitments** (interlinked) via retuned arcmemory
distillation, tracks commitments as **tasks**, gives a **morning briefing** on a
schedule, and ships **CRM verbs + sales skills** loaded from the agent root.

## Design rulings

- **Blueprint = config + files, no runtime code baked into arc\*.** (brainstorm §2)
- **Identity is a file** (`persona.md` → `workspace/identity.md`). Keep the existing
  persona.md mechanism; do NOT add a second `[identity].body` path.
- **Prompt overlays ship as a file tree** `prompts/<pkg>/<name>.md` (not inline TOML) —
  discovered, validated against the arcprompt catalog, authored + **operator-signed** at
  apply (reuse the `arc prompt edit` envelope: secret-scan → sign_artifact → sidecar).
- **Capabilities ship as `capabilities/*.py`**, copied to `<agent_root>/capabilities/`
  and **signed with the agent's own pinned identity** (reuse create.py
  `_sign_scaffolded_capabilities`) so TOFU accepts them at personal tier. (user directive)
- **Skills ship as `skills/<name>/` folders** in Arc runtime format, copied to the agent
  root and signed like capabilities.
- **Schedules ship as `[[schedules]]`** → materialized into `<workspace>/schedules.json`
  via `ScheduleStore` (validated by `ScheduleEntry`).
- **Questions ship as `[[questions]]`** → parsed + exposed for onboarding; not materialized.
- Sibling config: `[arcllm]`→arcllm.toml, `[arcrun]`→arcrun.toml (deep-merge UNDER user).
- **Tier stringency-max + denylist preserved** (blueprint can only raise a floor).

## Phases / tasks

### Phase A — reconcile the two skill validators (unblocks authoring "once")
- [x] T-A1 Converge `arcagent/capabilities/skill_validator.py` to one alias-tolerant
      contract: require `name`+`description`; `version`/`triggers`/`tools` **optional**;
      required sections by canonical name **with aliases** (`## Files`|`## Resources`,
      `## Red Flags & Rationalizations`|`## Anti Patterns`|`Red Flags`|`Antipatterns`,
      add `## Output` as accepted), **presence not strict order**. Filler stays a warning.
- [x] T-A2 Update `skill_validator` tests; add cases proving a skill-creator-v2-shaped
      SKILL.md AND an existing Arc-format SKILL.md both pass.
- [x] T-A3 Confirm Arc's 4 shipped builtin skills still validate (regression).

### Phase B — blueprint v2 loader (`arccli/blueprints.py`)
- [x] T-B1 Extend `_parse` to split out `arcllm`, `arcrun` tables + `prompts`/`skills`/
      `schedules`/`questions` arrays from the arcagent overlay; discover `prompts/` and
      `capabilities/` and `skills/` sub-trees under the blueprint dir.
- [x] T-B2 Extend `ResolvedBlueprint` with `root`, `arcllm_overlay`, `arcrun_overlay`,
      `prompt_overlays`, `capabilities_dir`, `skills_dir`, `schedules`, `questions`.
- [x] T-B3 Validate at resolve: unknown `(pkg,name)` prompt overlay → **hard error**
      (via `PromptCatalog.stock_path`). Denylist unchanged for arcagent overlay.
- [x] T-B4 Tests: v2 parse, prompt-tree discovery, unknown-prompt hard error, the 3
      existing blueprints still resolve unchanged (additive).

### Phase C — materialize + wire
- [x] T-C1 New `arccli/blueprints_materialize.py`: `materialize_blueprint(bp, agent_dir,
      *, deployment_tier, agent_identity, operator_signer)` — writes arcagent/arcllm/
      arcrun tomls, persona (never clobber), signed prompt overlays, signed capabilities +
      skills copy, seeded schedules.json.
- [x] T-C2 Wire into `arc init --team` (`init._init_team_fleet`) and `arc blueprint apply
      --agent <dir>`.
- [x] T-C3 Tests: full materialize E2E on a tmp agent dir — assert every artifact lands
      + signatures verify + schedules.json round-trips through ScheduleStore.

### Phase D — author the sales-exec-assistant blueprint (`blueprints/sales-exec-assistant/`)
- [x] T-D1 `blueprint.toml`: memory(+revenue distiller)/tasks/scheduler modules,
      `[arcllm]` model, `[[schedules]]` morning briefing, `[[questions]]` onboarding.
- [x] T-D2 `persona.md`: purpose · personality (Culture Index) · values · principles ·
      priorities · decision heuristics · lessons learned.
- [x] T-D3 `prompts/arcmemory/{distill_fact,distill_insight,distill_procedure,distill_day,
      distill_disambiguate}.md` + `prompts/arcagent/{context_maintainer_system,
      planner_system}.md` — revenue lens (deals/contacts/companies/meetings, interlinked).
- [x] T-D4 `capabilities/crm.py`: CRM verbs (log_contact/company/deal/meeting/commitment,
      pipeline_status) writing structured cards to the workspace.
- [x] T-D5 `skills/` (v2 format): `pre-call-brief`, `deal-review`, `follow-up-sweep`.

### Phase E — verify
- [x] T-E1 `ruff check` + `ruff format` clean; `mypy --strict` clean (arccli + arcagent).
- [x] T-E2 `pytest` green for arccli blueprints + arcagent skill_validator.
- [x] T-E3 Live smoke: `arc init --team salesdemo --blueprint sales-exec-assistant` into a
      tmp HOME; assert the agent dir has identity, signed overlays, signed capabilities,
      skills, schedules.json; `arc blueprint show/verify` clean.
