# arcprompt

Strategy prompt provider for Arc. Serves model-facing guidance (system prompts, strategy context) to arcrun and arcagent.

## Layer position

arcprompt is a leaf-level utility package. arcrun depends on arcprompt for `get_strategy_prompts`. arcprompt does not depend on any other Arc package.

## What it provides

Status: scaffolding with minimal implementation. The package installs but exposes no stable public API beyond what arcrun uses internally via `arcrun.prompts.get_strategy_prompts`.

There is no public `__init__.py` surface to document yet.

## Architecture references

- Commit `8502552`: feat: add strategy prompt provider — ArcRun serves model-facing guidance to ArcAgent

## Status

- Status: early scaffolding — no stable public API yet
- Tests: check with `uv run --no-sync pytest packages/arcprompt/tests`
