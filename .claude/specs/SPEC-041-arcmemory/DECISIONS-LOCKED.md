# arcmemory — locked decisions (Fable, 2026-07-07; Josh: "just build it all, do what you think is best")

## Brain seam (refines PRD REQ-080/REQ-002)
- `Brain` Protocol (structural) + `NullBrain` no-op default live in **arcagent**, NOT arcmemory.
- arcagent depends on NO memory package; runs fully **memory-less by default** (NullBrain) — must not error with zero memory installed.
- arcmemory provides `ArcMemoryBrain` satisfying the Protocol **structurally** (imports no arcagent — arch test stays green). A user can BYO a compatible Brain.
- arcagent config-selects the impl: NullBrain (none) / arcmemory (installed) / custom class-path. THIS is the SPEC-047 pluggable-brain seam.
- Delete ALL legacy memory from arcagent (bio_memory, memory, memory_context, dead config) — arcagent tree ships memory-file-free.
- **Hard acceptance (Phase 8):** `pip install arcagent` alone → agent runs, memory is a silent no-op, zero memory files present; add arcmemory or a custom Brain → memory activates.

## arcui memory surface (Josh: "arcui should allow view/edit/interact with arcmemory if used")
- arcmemory stays UI-agnostic: exposes glass-box editable markdown (truth) + arcstore-backed indices + a read/query/edit API (Phase 7) + audit fan-out (UIBridgeSink, REQ-083). arcmemory imports nothing from arcui.
- The arcui memory **browser/editor/interact panel** (episodic timeline, entity graph, insight/procedure cards; edit markdown → rebuild index; run recall / inspect analogical match / prune-approve insight) is a **SPEC-032 (mission control / arcui)** deliverable — conditional on an active arcmemory-compatible Brain (adapts/hides otherwise).

## Build order
All 12 arcmemory phases sequentially on branch feat/SPEC-041-arcmemory (Phase 1 embed 49eff2b already committed). No parallel repo-mutating workers in the shared tree (worktree isolation or sequential only — see feedback_subagents_no_destructive_git refinement 2026-07-07).
