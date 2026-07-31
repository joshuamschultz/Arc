---
topic: "arctui-terminal-agent"
date: 2026-07-24
status: complete
---

# arctui-terminal-agent

## Inspiration

pi's coding-agent (github.com/earendil-works/pi) and Claude Code set the bar. Josh wants a local terminal agent wired to BOTH open-source models and Claude/frontier through arcllm, using arcrun to run local agents for coding and more. The strategy: build a GENERAL terminal agent, then tune an arc agent for coding that beats pi-coding-agent. The ecosystem model is pi.dev/packages, done open. arctui already exists (~1700 LOC Textual chat/monitor with `arc tui`, in-process agent, graceful no-agent mode) — this is about building it out into the real thing, not starting from zero.

## Projects

Build out arctui into a general, extensible terminal-agent platform: (1) a Claude-Code-grade, coding-capable agent usable locally on the arc repo itself and other projects; (2) the plug-and-play extension/hook surface where arc packages contribute tools, slash-commands/workflows, TUI views/panes, memory, MCPs, skills, and whole agent presets; (3) multi-agent selection — point the TUI at any agent in ~/.arc and switch between them; (4) a first-party coding preset (the 'tune for coding' unit). Deferred follow-on: a shareable package registry/marketplace.

## Audience

General developers from day one — external builders are first-class, not an afterthought. That implies a stable public extension API, polished onboarding, docs, and versioning guarantees from v1. Josh is the first dogfooder (using arctui to build arc), but extension authors and other devs are primary personas immediately. Secondary: operators running arc agents locally who want a terminal cockpit; regulated/federal teams who need an extensible agent they can actually trust.

## Use Cases

• Use arctui locally, like Claude Code, to work on the arc codebase and other repos — read/edit files, run shell, search, drive multi-step coding tasks with approval gates.
• Wire arctui to any model — local/open-source (Ollama/vLLM/llama.cpp) or Claude/frontier — through arcllm, switchable in-session, no lock-in.
• Point the TUI at whichever agent in ~/.arc you're working with; switch agents mid-flow.
• Install plug-and-play extensions others built (tools, commands, views, memory, MCPs, skills, agent presets) and run them safely (signed/gated).
• Build and share your own extension or tuned agent preset (e.g. a coding agent) for others to install.

## Desired Outcomes

The wedge is a combination nobody else has: an extensible ecosystem (arc packages as plug-and-play extensions) that is ALSO secure-by-construction (signed, policy-gated, audited) — the only extensible terminal agent safe for regulated/federal use. Concrete outcomes: v1 is genuinely usable — Josh works on arc (and other projects) in arctui instead of Claude Code; extension parity (built-ins use the same public hook API third parties do); a first-party coding preset that eventually beats pi-coding-agent, built on the general harness; external devs install and build extensions. Success test: arctui gets chosen for model-freedom + trust + ecosystem, and Josh actually switches his daily coding to it.

## Guiding Principles

All four are non-negotiable (Josh selected every one):
• No lock-in — any model via arcllm (open-source or frontier), local-first, open extension format; users and authors never trapped.
• Reliable arcrun loops over features — the loop finishes correctly (no derail/stall/runaway), resumable, observable; a flaky agent is worse than a smaller one that works.
• Secure by default, not config — extensions signed, policy-gated, audited out of the box; federal-grade posture on by default, relaxed only deliberately.
• Dogfood the extension API — built-in features (including the coding agent) use the SAME public hook/package API third parties do; no privileged internal path, so the extension surface is provably real.

## Constraints

Local-first; in-process Textual TUI built ON the existing arctui (~1700 LOC, `arc tui`). Models exclusively through arcllm; loops through arcrun; agents through arcagent — respect the package boundaries (no mixing concerns). The extension mechanism must reuse arc's existing package/capability/module/extension-point/hook system (arcagent.extension.ExtensionPoint, capabilities, arcskill.hub) rather than inventing a parallel one. Federal-grade trust is mandatory: arctrust signing, the policy pipeline, and audit — extensions verified before load. Preserve the 'no dependency → still works' decoupling style. Graceful no-agent mode already exists and must stay.

## Scope

**In:** v1 = a vertical slice that proves the wedge AND is daily-usable:
• A Claude-Code-grade, coding-capable general agent usable locally on arc + other repos: file read/edit/multi-file, shell, search, tool-approval gates, streaming output, reliable resumable arcrun loops.
• Model switching via arcllm (open-source + Claude) from the TUI.
• Agent selection/switching across ~/.arc agents.
• The core TUI views/features/questions/choices a real coding session needs (approvals, diffs, activity, errors).
• Wire the package/hook extension seam END-TO-END, proven by installing ONE real external extension.
• A first-party coding preset.

**Out:** Deferred beyond v1: a public package registry/marketplace UI + discovery/browse. Full sandbox isolation for arbitrary third-party TUI views at scale (v1 can constrain what view-extensions may do). The deeply-tuned 'beats pi' SOTA coding agent — v1 ships a usable coding preset, not the fully-optimized champion. Non-terminal surfaces (that's arcui/web). A hosted/cloud story — local-first only for v1.

## Open Questions

• Extension package FORMAT: reuse the arc package layout as-is, or a lighter 'arctui extension' manifest? How much of the hook surface already exists (capabilities/modules/ExtensionPoint/arcskill.hub) vs must be built (TUI-view hooks, command hooks, install/discovery flow)?
• Trust/sandbox model for third-party TUI VIEWS specifically — a custom Textual widget is arbitrary in-process code; tools/skills have a trust path, views are new.
• Which open-source backends first (Ollama/vLLM/llama.cpp) and the model-switch UX.
• What 'reliable loops' needs beyond arcrun today (checkpoint/resume UX, derail detection, cost/turn caps surfaced in the TUI).
• Repo-local .arc agent vs global ~/.arc — how agent selection spans both.
• Relationship to arcui (web): share components/telemetry or stay fully separate?

## Related Solutions

No solutions-archive matches. Internal prior art to build on: arc's capability/module system, arcagent.extension.ExtensionPoint families (select-one/select-many), arcskill.hub (skill-marketplace connector), and the existing arctui package (~1700 LOC Textual). External references: pi coding-agent (earendil-works/pi), pi.dev/packages, Claude Code.
