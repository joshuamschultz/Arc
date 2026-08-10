# SPEC-062 — Team Docs: A Shared Document Corpus Agents Know Exists

**Feature:** `team-docs` (spec folder `SPEC-062-team-docs`)
**Created:** 2026-08-09

**Documents:** [DESIGN.md](DESIGN.md) — problem statement, design, layering, REQ-260..REQ-285, open questions, test plan sketch.

## Status

Draft for review — written 2026-08-09, not yet approved.

This spec has not gone through `/build` or `/deepen`. It is a single design document written directly from three cited research passes over the Arc monorepo, intended for Josh to review and refine before it moves toward PRD/SDD/PLAN.

## Requirement being addressed

arcteam must properly store documents at team level, make them available in the UI, and allow all agents to access them — and agents must know the store exists. A "semantic insertion" gives agents an ontology/index of what is where, injected into their context, managed through the UI. Context: arcteam and ArcFlow will be heavily used and must be bulletproof; this spec covers the shared-document/knowledge gap specifically. (Josh, 2026-08-09)

## Research this spec is built on

- `/Users/joshschultz/Projects/arc-apps/research/arcteam-corpus.md` — consolidated research report (sections 0, 1, 2, 6 of 8; sections 3/4/5/7/8 were not present in that file at time of writing).
- Three cited research passes covering: how agents access shared material today (fs_reader stubs, read-tool confinement, `TeamFileStore`'s default-root trap, the `agent:assemble_prompt` hook); ArcUI's knowledge surface today (per-agent only, no team routes, no upload endpoint); and the semantic/embedding machinery already in Arc (`arcllm.embed()`, sqlite-vec + RRF fusion, no chunker, dead PDF/DOCX/XLSX extraction code).

## Open Questions

See DESIGN.md §9 — corpus engine shape (new `arcteam.docs` module vs. evolved `TeamFileStore`), whether `docs_store` is approval-gated, ontology auto-regen policy, chunk sizing, naming, and where extraction runs.
