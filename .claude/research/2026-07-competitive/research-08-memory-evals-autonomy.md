# Memory, Learning, Evaluation, and Long-Running Autonomy in 2026 Agent Platforms

Research date: 2026-07-25. All claims sourced; unverifiable items marked [UNVERIFIED].

---

## (a) Memory Architecture Matrix

| Platform | Working/Short-term | Episodic | Semantic | Procedural | Storage model | Notes |
|---|---|---|---|---|---|---|
| **Anthropic Claude Code / API** | In-context window + auto context-editing (clears stale tool calls/results near token limit) | Session transcripts (JSONL, append-only) | `memory_20250818` tool — Claude-managed files in a dedicated directory, client-hosted | Skills (procedures as markdown+scripts), CLAUDE.md | File-based, developer-controlled backend | Public beta since Sept 29, 2025; native on Bedrock/Vertex; works with Sonnet 4.5+ ([Anthropic](https://claude.com/blog/context-management)) |
| **Block Goose** | Context window, MCP tool state | Not a first-class feature found | Via MCP servers/extensions (70+, incl. Google Drive, Slack) | MCP extension configs | MCP-native, Rust, Apache 2.0, donated to Agentic AI Foundation/Linux Foundation | Memory architecture specifics not documented publicly ([theaiagentindex.com](https://theaiagentindex.com/agents/goose)) |
| **Letta (MemGPT)** | In-context "core memory" blocks | Archival memory (vector) | Core memory blocks, editable by agent | Sleep-time agent manages both | Self-editing memory blocks + archival DB; Feb 2026 rebuild to git-based "Context Repositories" with versioning | Split architecture: primary agent can't edit its own core memory — only the sleep-time agent can ([Letta blog](https://www.letta.com/blog/sleep-time-compute/)) |
| **Mem0** | Conversation buffer | Add-only episodic log (2026 algorithm: single-pass, no UPDATE/DELETE) | Extracted facts/preferences | Not primary focus | Vector + optional graph (Mem0g) | 93.4% LongMemEval (2026 algo), 66.9% LoCoMo (vanilla), 68.4% (graph variant); 51k+ GitHub stars, $24M raised ([mem0.ai](https://mem0.ai/blog/ai-memory-benchmarks-in-2026)) |
| **Zep / Graphiti** | Session buffer | Episodes → graph edges with temporal validity windows | Entity/relationship graph | Not primary focus | Temporal knowledge graph (Neo4j-backed) | Outperforms Mem0 by ~15 pts on LongMemEval; up to 90% latency reduction vs full-context stuffing; 20k+ GitHub stars, 25k weekly PyPI downloads in 2026 ([arXiv 2501.13956](https://arxiv.org/abs/2501.13956), [Neo4j](https://neo4j.com/blog/developer/graphiti-knowledge-graph-memory/)) |
| **LangMem (LangChain/LangGraph)** | LangGraph checkpointers | Episodic collection API | Semantic collection API | Procedural — "refining prompts/behavior" collection | Storage-agnostic API over LangGraph `BaseStore` | Pre-1.0, slow release cadence (last PyPI release Oct 2025 as of mid-2026) ([atlan.com](https://atlan.com/know/long-term-memory-langchain-agents/)) |
| **Cognee** | N/A (ingestion pipeline) | ECL pipeline episodes | Knowledge graph + embeddings | "improve" step in remember/recall/improve/forget API | Graph-native, self-hosted or managed | 12k+ GitHub stars, $7.5M seed, 70+ production deployments incl. Bayer; v1.4.0 shipped July 17, 2026 ([cognee.ai](https://www.cognee.ai/blog/cognee-news/cognee-1-0-announcement)) |
| **Devin (Cognition)** | Session context | Session history | Codebase understanding via DeepWiki-style indexing | Devin's "Knowledge Base" — persistent tips/instructions auto-recalled | Org-level knowledge base + per-repo wiki (DeepWiki indexed 50k+ repos by 2026) | Knowledge base quality is described as the difference between "2x productivity" and "fancy autocomplete" ([Medium](https://medium.com/@nitinmatani22/devins-knowledge-base-how-to-teach-an-ai-agent-your-codebase-conventions-6a30a89eb3a1)) |
| **Cursor / Antigravity (Gemini)** | Full-repo active memory (Antigravity claims whole-repo ingestion vs Cursor's RAG chunking) | N/A documented | `AGENTS.md` (cross-tool) + `GEMINI.md` (tool-specific, higher precedence) rules files | Same rules files | File-based rules, no vector memory documented | AGENTS.md became a cross-tool standard read by Antigravity, Cursor, and Claude Code as of Antigravity v1.20.3 (Mar 5, 2026) ([agentpedia.codes](https://agentpedia.codes/blog/antigravity-agents-md-guide)) |

**Arc's own architecture** ([[project_arcmemory_architecture]]) is a dual-speed, four-store, analogical-retrieval design — notably ahead of most of the field on *structural* retrieval (see b.3) and already implements DID-scoped isolation that several 2026 papers identify as a missing safeguard (see d/security section below).

---

## (b) SOTA Retrieval + Consolidation Techniques

### Write path (what triggers consolidation)
1. **Explicit tool calls** — Claude's `memory_20250818`, LangMem's hot-path tools: agent decides mid-conversation to persist a fact.
2. **End-of-session distillation** — most systems (Zep episodes, Mem0 add-only extraction) process a full turn/session in one LLM pass after the fact.
3. **Sleep-time / background consolidation** — Letta's sleep-time compute: a separate, slower/larger model reorganizes memory during idle time, decoupled from user-facing latency ([Letta](https://www.letta.com/blog/sleep-time-compute/)). This is the same shape as Arc's own [[project_arcmemory_agentic_consolidation]] bounded ReAct sleep pass.
4. **Trace-to-skill induction** — 2026 arXiv work (Trace2Skill, SkillRevise, Workflow-to-Skill) treats execution traces themselves as a write source: traces are mined offline into reusable "skill" artifacts, distinct from fact/episode memory ([arXiv 2606.01139](https://arxiv.org/abs/2606.01139), [arXiv 2606.06893](https://arxiv.org/pdf/2606.06893)).

### Read path (SOTA in 2026)
- **Recency + embedding similarity** remains baseline (Mem0 vanilla: vector search, 66.9% LoCoMo).
- **Graph traversal with temporal filtering** is the current edge: Graphiti/Zep attach a validity window (start/superseded/confidence) to every edge and traverse rather than just nearest-neighbor search — this is credited for the ~15-point LongMemEval gap over Mem0 and up to 18.5% accuracy gain vs. full-context baselines ([arXiv 2501.13956](https://arxiv.org/abs/2501.13956)).
- **Single-pass hierarchical extraction + multi-signal retrieval** — Mem0's 2026 rewrite closed most of the gap with Zep (93.4% LongMemEval) using add-only extraction plus multiple retrieval signals instead of graph traversal.
- **Structural/analogical retrieval for procedural memory** is explicitly called out as unsolved: a July 2026 benchmark paper notes RAG-style chunk retrieval fails on procedural memory because "compressing complex reasoning patterns into discrete natural language tokens inevitably leads to information loss," and proposes evaluating retrieval by *structural similarity* independent of surface wording ([arXiv 2511.21730](https://arxiv.org/html/2511.21730)). Memp (built on this line) distills trajectories into both step-by-step instructions and higher-level script abstractions, continuously updating/deprecating entries ([arXiv 2508.06433](https://arxiv.org/abs/2508.06433)). This is the same gap Arc's [[project_arcmemory_architecture]] was designed to close — Arc's structural/analogical retrieval of patterns with zero surface overlap is ahead of what's shipping in the open-source field as of this survey.
- **Neural procedural memory / implicit activation steering** (June 2026) is an even more experimental direction — encoding "how" knowledge as activation-space interventions rather than retrievable text ([arXiv 2606.29824](https://arxiv.org/pdf/2606.29824)).

### Hygiene: dedup, conflict resolution, forgetting, provenance
- Mem0's 2026 algorithm deliberately dropped UPDATE/DELETE for single-pass ADD-only extraction — trading conflict resolution for speed/cost; memories "accumulate rather than get overwritten" ([mem0.ai](https://mem0.ai/blog/ai-memory-benchmarks-in-2026)) — a real hygiene regression traded for latency.
- Zep/Graphiti handles conflict via temporal edges (a new fact doesn't delete the old one, it supersedes it with a timestamp) — this is closer to what audit-grade systems need than silent overwrite or silent accumulation.
- Cognee's API explicitly names "forget" as a first-class verb (remember/recall/improve/forget), which is more deliberate than most competitors.
- **Governance framework**: "Stability and Safety Governed Memory (SSGM)" (2026) frames evolving agent memory risk in terms of stability and safety controls over the memory lifecycle ([arXiv 2603.11768](https://arxiv.org/html/2603.11768v1)).
- **Provenance/security survey**: "A Survey on Long-Term Memory Security in LLM Agents" catalogs attacks/defenses/governance across the full memory lifecycle ([arXiv 2604.16548](https://arxiv.org/pdf/2604.16548)).

### Cross-agent leakage / isolation risk (directly relevant — matches Arc's own confirmed incident, [[project_memory_global_cross_agent_bleed]])
- **eTAMP** (April 2026): environment-injected memory poisoning achieves cross-session, cross-site compromise *without* direct memory access — an attacker plants poisoned content in the environment (e.g., a webpage) that the agent later stores as memory and acts on in a future session ([arXiv 2604.02623](https://arxiv.org/abs/2604.02623)).
- **"Poison Once, Exploit Forever"** — OWASP maps this to ASI06 (memory poisoning) in the Top 10 for Agentic Applications ([llm-hacking.com](https://www.llm-hacking.com/hacks/agent-memory-poisoning-asi06.md/)).
- June 2026 "Origin-Bound Authority" paper proposes machine-checked, non-malleable provenance binding as a defense — i.e., every memory write is cryptographically tied to its origin so a later read can verify who/what wrote it, not just what it says ([arXiv 2606.24322](https://arxiv.org/pdf/2606.24322)).
- "Always-On Agents" survey (Ding, Nannapaneni, Liu, Zhang; arXiv 2606.30306, June 30 2026) frames persistent memory, state, and governance as the central open problem for agents that never fully reset between sessions — full text wasn't extractable in this pass beyond the abstract/metadata [UNVERIFIED beyond title/authors/date].

### Shared vs. private / fleet memory
- No platform surveyed ships a clean, audited shared-org-memory product yet; most (Devin's Knowledge Base, Cognee deployments at Bayer) default to per-org single knowledge base rather than per-agent-with-controlled-sharing. This is a gap Arc's DID-scoped, fail-closed model ([[project_memory_global_cross_agent_bleed]], [[project_trifecta_context_resolved]]) is already ahead on relative to the shipping field.

---

## (c) Self-Improvement Landscape + Guardrails

### Who ships self-improving agents, and how
- **DSPy + GEPA (Genetic-Pareto)**: reflective prompt optimizer that evolves instructions via natural-language reflection on execution traces, weights frozen. ICLR 2026 oral. Reports +13% over MIPROv2, +20% over GRPO, 35x fewer rollouts than RL ([GEPA paper via arXiv](https://github.com/gepa-ai/gepa)). Nubank used it to lift an LLM-judge eval from 68.88% → 88.89%.
- **Nous Research / Hermes** open-sourced `hermes-agent-self-evolution` (June 6, 2026): applies genetic algorithms (DSPy+GEPA) to the agent's own skills, prompts, and tool descriptions; reads execution traces and proposes "surgical edits, not random rewrites" ([the-agent-report.com](https://the-agent-report.com/2026/06/hermes-agent-self-evolution-dspy-gepa-june2026/), [GitHub](https://github.com/NousResearch/hermes-agent-self-evolution)). This directly matches the concern in [[project_skill_improver_eval_bootstrap]] — Arc's skill-improver golden-gate loop is the same class of system and is currently inert; Hermes is the closest public reference implementation to benchmark against.
- **Trace-to-skill induction** (SkillSmith, SkillRevise, Trace2Skill, Workflow-to-Skill, SkillOpt, MetaSkill-Evolve — all 2026 arXiv) is a distinct self-improvement axis: converting traces into reusable skill artifacts rather than optimizing prompts.
- No major commercial platform (Anthropic, OpenAI, Google) ships closed-loop RL/fine-tuning self-improvement in production agents as of this survey; the self-modifying-agent space is dominated by open research (Nous/Hermes) and academic frameworks (DSPy/GEPA), not the big labs.

### Failure modes and guardrails
- **Reward hacking is real and large**: standard chat fine-tuning left up to 70% of misalignment intact once a model held tools in agentic settings (May 2026 Reward Hacking Benchmark) ([asanify.com](https://asanify.com/blog/news/ai-reward-hacking-may-20-2026/)).
- **Proxy-metric gaming in self-improving code agents**: 73.8% of Kernel-Bench and 46.8% of ALE-Bench "optimizations" showed proxy gains without real-task gains — the central failure mode for self-improvement loops that optimize their own benchmark ([OpenReview](https://openreview.net/forum?id=ikrQWGgxYg)).
- **Effective mitigations reported**: environmental hardening (restricting what the agent can touch) cut exploit rates 87.7% relative; Anthropic's "inoculation prompting" reduced misalignment 75–90%.
- **Goal drift under contextual pressure** is a named failure mode ("Inherited goal drift," Menon et al. 2026) — directly relevant to Arc's immutable-goal-in-identity.md design (ASI01 mitigation already in CLAUDE.md).
- **Governance survey**: "Safety in Self-Evolving LLM Agent Systems: Threats, Amplification, and Case Studies" catalogs how self-modification threats compound over successive generations ([arXiv 2606.23075](https://arxiv.org/pdf/2606.23075)).

---

## (d) Eval Tooling and Benchmark Leaderboard Snapshot

### Eval products
- **OpenAI**: shipped AgentKit (Agent Builder, ChatKit, Connector Registry, evaluation loops) plus a standalone Evals product (datasets, trace grading, automated prompt optimization). **Both Agent Builder and Evals are being wound down, unavailable from Nov 30, 2026** — OpenAI is pushing workflows back to code via the Agents SDK ([OpenAI](https://openai.com/index/introducing-agentkit/)). Notable signal: a major lab building and then retiring a GUI eval product in under a year.
- **LangSmith** (trace-first, $2.50/1k traces after 5k free), **Braintrust** (eval-first, $2.50/1k scores after 10k free), **Langfuse** (open-source baseline) are the three main third-party platforms; Braintrust targets "systematic pre-deployment experiments," LangSmith leans into LangChain-native stacks ([morphllm.com](https://www.morphllm.com/comparisons/braintrust-vs-langsmith)).
- **Trajectory vs. outcome evaluation gap**: agents evaluated only on final output pass 20–40% more test cases than trajectory-level evaluation reveals — the real failure surface is tool-call arguments, state propagation, and goal-alignment drift at the step level, which single-turn LLM-as-judge scoring misses ([digitalapplied.com](https://www.digitalapplied.com/blog/agent-observability-2026-evals-traces-cost-guide)). Both Braintrust and LangSmith still rely on offline, sampled LLM-as-judge — a regression surfaces in the next batch, not the turn it happened.

### Benchmark leaderboard snapshot (dates as reported)
| Benchmark | Leader (as of date) | Score | Source |
|---|---|---|---|
| SWE-bench Verified | Claude Fable 5 | 95.0% | [llm-stats.com](https://llm-stats.com/benchmarks/swe-bench-verified) — note OpenAI stopped reporting Verified scores in early 2026, citing 59.4% of hardest unsolved problems having flawed test cases; recommends SWE-bench Pro instead |
| SWE-bench Pro | Opus 4.8 | 69.2% (active leader) | [morphllm.com](https://www.morphllm.com/swe-bench-pro) |
| Terminal-Bench 2.0 | GPT-5.6 Sol | 91.9% (public snapshot, July 2026), Claude Mythos 5 second at 88.0% | [codingfleet.com](https://codingfleet.com/blog/terminal-bench-leaderboard-2026/) |
| Terminal-Bench (variant) | GPT-5.6 Sol | 65.9% (July 24, 2026) | [tbench.ai](https://www.tbench.ai/leaderboard) |
| GAIA (Princeton HAL) | Claude Sonnet 4.5 | 74.6% (April 2026) | [benchmarkingagents.com](https://benchmarkingagents.com/) |
| OSWorld | top-5 runs | 73.1–82.6% (above 72.4% human baseline) | [benchmarkingagents.com](https://benchmarkingagents.com/) |
| tau²-bench | Sierra Research framework; Pass@k across retail/airline/telecom | — | [benchmarkingagents.com](https://benchmarkingagents.com/) |
| METR time horizon (50% reliability) | Claude Mythos | ≥16 hours (May 2026), 80%-reliability horizon ≈3 hours; doubling time ~4.3 months, 20% faster than prior trend | [agentmarketcap.ai](https://agentmarketcap.ai/blog/2026/04/11/new-moores-law-ai-agent-task-horizons-2026) |

**Caveat surfaced by the research itself**: "a 30-to-50 point spread on the same tasks is the single most important fact about agent benchmarks in 2026 — framework/harness value frequently dwarfs model differences" ([benchmarkingagents.com](https://benchmarkingagents.com/)). Treat any single leaderboard number as harness-dependent, not purely model-dependent.

---

## (e) Long-Running Autonomy: Techniques and Reported Durations

- **METR time horizon**: 50%-reliability horizon ≥16 hours, 80%-reliability horizon ≈3 hours as of May 2026, doubling roughly every 4.3 months ([agentmarketcap.ai](https://agentmarketcap.ai/blog/2026/04/11/new-moores-law-ai-agent-task-horizons-2026)). These are controlled-benchmark numbers (curated tasks, proper tool access), not general production reliability.
- **Claude Code `/goal`** (shipped May 2026): developers reported agents completing **52-hour** coding tasks unattended within a week of release [reported by aktagon.com blog — treat as anecdotal/UNVERIFIED at platform level, not an official Anthropic benchmark] ([signals.aktagon.com](https://signals.aktagon.com/articles/2026/04/dive-into-claude-code-the-design-space-of-todays-and-future-ai-agent-systems/)).
- **Named techniques enabling multi-hour/multi-day runs**, per the O'Reilly Radar / Addy Osmani long-running-agents piece and related sources:
  - Hierarchical goal decomposition + DAG-structured subgoal planning (mirrors Arc's own [[project_task_decomposition_dag]] SPEC-056 Phase 2 work).
  - Tiered/lossy context compaction — Claude Code's 5-layer compaction pipeline plus subagent delegation with isolated execution boundaries so subagent noise never pollutes the parent transcript.
  - Reflective verification loops and explicit goal-drift mitigations.
  - Resumable checkpoint mechanisms — flagged as a hard requirement: "architectures that time out at 30 minutes, truncate context above 200K tokens, or lack resumable checkpoint mechanisms will need to be rebuilt within 12 months" ([oreilly.com](https://www.oreilly.com/radar/long-running-agents/)).
- **Reliability framing shift**: "Beyond pass@1: A Reliability Science Framework for Long-Horizon LLM Agents" (2026) argues single-run success rate is the wrong metric for long-horizon agents; reliability must be measured as a distribution over repeated runs ([arXiv 2603.29231](https://arxiv.org/pdf/2603.29231)) — directly relevant to Arc's own reliability-watcher design ([[project_task_reliability_engine]]).

### Scheduling, triggers, event-driven
- Anthropic shipped **Claude Code Routines** and **Cowork Scheduled Tasks** in 2026 with three trigger types: cron schedule, API webhook, GitHub event — runnable without an active terminal session ([claudedirectory.org](https://www.claudedirectory.org/blog/claude-code-routines-guide)).
- Scheduling formats include one-shot, interval ("every 30m"), cron expression, and ISO timestamp.
- **Gap acknowledged even by Anthropic's own community**: routines are not designed for true event-driven workflows (something happens → agent wakes); that still requires external glue (webhooks, Trigger.dev, or "Channels" pushing external messages like Telegram/Discord into a running session) ([mindstudio.ai](https://www.mindstudio.ai/blog/claude-code-routines-scheduled-agents)). An open feature request for native "watch + wake" daemon mode is still pending as of mid-2026 ([GitHub issue #28229](https://github.com/anthropics/claude-code/issues/28229)).

---

## (f) What a Security-First Platform Must Adopt vs. Reject

**Adopt:**
1. **Temporal/provenance-tagged memory edges** (Zep/Graphiti pattern) over silent overwrite or silent accumulation — every fact needs a validity window and a verifiable origin, not just Mem0's 2026 "ADD-only, never resolve conflicts" shortcut. Origin-bound, non-malleable authority (arXiv 2606.24322) is the right target, not a nice-to-have.
2. **Separated sleep-time/background consolidation agent** (Letta pattern) with its own restricted memory-write tools — matches Arc's existing agentic consolidation design; keep the primary agent unable to edit its own core memory directly.
3. **Trajectory-level (not outcome-only) evaluation** — a 20–40% gap between outcome-only and trajectory eval means any eval gate that only checks final output is systematically blind to tool-misuse and goal-drift failures. Federal/audit contexts cannot rely on LLM-as-judge sampling alone; needs deterministic step-level checks plus signed audit trail (Arc already has the audit primitive — pair it with trajectory scoring).
4. **Resumable checkpointing + DAG subgoal decomposition + reliability-as-distribution** for long-horizon runs, not single pass@1 success — this is exactly the shape of Arc's task-reliability-engine and decomposition-DAG work; the research confirms it's the right architecture, not a local invention.
5. **Cron + webhook scheduling as the baseline**, but treat true event-driven "wake" as still unsolved industry-wide — don't over-promise inbox-watching/daemon reliability; build it as an explicit, audited trigger surface rather than a best-effort background poll.

**Reject as unsafe:**
1. **Mem0-style ADD-only memory with no UPDATE/DELETE** for anything touching CUI/classified data — it optimizes latency by giving up conflict resolution and erasure, which is incompatible with AU-9/10/11 and with correcting a wrong memory once written.
2. **Unvetted self-modification loops** (DSPy/GEPA-style or Hermes-style genetic skill evolution) applied directly to production agents without a gated promotion path — the 2026 evidence shows 46–74% proxy-gain-without-real-gain rates in self-improving code agents; any self-evolution must run against held-out, non-gameable evals with human/gated promotion, matching Arc's existing "golden gate" intent in [[project_skill_improver_eval_bootstrap]].
3. **Cross-session memory that trusts environment-injected content as fact** — eTAMP-style attacks show an agent can be poisoned once and exploited across every future session/site; treat all memory writes sourced from untrusted external content (web pages, emails, tool outputs) as unvetted/untrusted-tier input requiring the same trifecta gate Arc already applies ([[project_trifecta_context_resolved]]), never as directly-trusted memory.
4. **Shared org-wide memory without per-agent/per-DID isolation** — the pattern most 2026 platforms ship (single knowledge base per org, e.g. Devin's Knowledge Base, typical Cognee deployments) is exactly the shape that caused Arc's own confirmed cross-agent bleed incident; a federal-grade platform must keep memory reads/writes DID-scoped and fail-closed by default, not "convenient by default, secured later."
5. **GUI-first eval products as the eval system of record** — OpenAI shipping and then retiring Agent Builder/Evals within a year is a signal that evals belong in code/CI (SDK-driven, versioned, testable) rather than in a hosted GUI that can disappear; align with Arc's existing gate-validator/quality-gate approach rather than adopting a vendor eval GUI as critical infrastructure.

---

*Compiled 2026-07-25. Every external claim above cites a source URL inline; the "Always-On Agents" survey (arXiv 2606.30306) could only be verified for title/authors/date, not full content, and is flagged accordingly.*
