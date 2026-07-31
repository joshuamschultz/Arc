# ARC Memory System
## Biologically-Inspired Memory for ArcAgent and ARC Team

Version 2.1 | February 2026 | BlackArc Systems

---

## Table of Contents

1. [Design Philosophy](#1-design-philosophy)
2. [Architecture](#2-architecture)
3. [How It Works](#3-how-it-works)
4. [Token Budgets](#4-token-budgets)
5. [Biological Foundation](#5-biological-foundation)
6. [PRD: arc-memory (Agent)](#6-prd-arc-memory-agent)
7. [PRD: arc-team-memory (Team)](#7-prd-arc-team-memory-team)
8. [Integration](#8-integration)
9. [Comparison to Traditional Approaches](#9-comparison-to-traditional-approaches)

---

## 1. Design Philosophy

### Three Principles

**Everything is a node in the graph.** Facts, lessons, preferences, relationships, behavioral patterns — all stored the same way: wiki-linked markdown files. There is no distinction between "knowledge" and "lessons" at the storage level. A lesson IS knowledge about how to behave, linked to the entities it applies to.

**The LLM is the intelligence layer.** The LLM decides what to record, what to forget, how to summarize, when to link, and what's relevant. There are no ranking formulas, scoring functions, or tier promotion algorithms. Every judgment call goes through the LLM. When the model improves, the entire memory system improves — without changing a line of code.

**Token budgets are the forcing function.** The only hard constraints are token limits on each injection section. These budgets create all the pruning, compression, and prioritization behavior. If a file is too long, consolidation must shorten it. If too many files are retrieved, the system must pick the most relevant ones. The budget IS the pruning mechanism.

### What This Means

No configured rules. No hardcoded policies. No human-maintained lesson files. The agent learns everything from experience — corrections, preferences, behavioral patterns, domain knowledge — and records it in the same entity graph it uses for everything else. A new agent starts with an empty memory and builds its knowledge through use, the same way a new employee learns on the job.

### How Model Improvements Leak Through

Every critical decision in the system is an LLM call:

- "Was this session significant enough to record?" — LLM judgment
- "Update this entity file with what we just learned" — LLM rewrite
- "This file is over budget. Compress it, keeping what matters most." — LLM judgment
- "Given this conversation, what entities should I retrieve?" — LLM judgment
- "These episodes show a pattern. Write it into how-i-work.md." — LLM synthesis

A smarter model produces better judgments at every step. The system gets better at remembering, forgetting, summarizing, and retrieving — all without code changes.

---

## 2. Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                       ARC Memory System                         │
│                                                                 │
│  ┌───────────────────────┐       ┌────────────────────────────┐ │
│  │  INDIVIDUAL (agent)   │       │  TEAM (shared)             │ │
│  │                       │       │                            │ │
│  │  working.md           │       │  entities/                 │ │
│  │  (scratchpad,         │       │  (wiki-linked markdown,    │ │
│  │   overwrites/turn)    │       │   the knowledge graph)     │ │
│  │                       │       │                            │ │
│  │  how-i-work.md        │       │  playbooks/                │ │
│  │  (learned identity,   │       │  (SOPs, procedures)        │ │
│  │   maintained by LLM)  │       │                            │ │
│  │                       │       │  decisions/                │ │
│  │  episodes/            │       │  (append-only log)         │ │
│  │  (significant moments,│       │                            │ │
│  │   append-only)        │       │                            │ │
│  └───────────┬───────────┘       └──────────────┬─────────────┘ │
│              │                                   │              │
│              │      ┌──────────────────┐         │              │
│              └──────┤ Promotion Gate   ├─────────┘              │
│                     └──────────────────┘                        │
│                                                                 │
│  ┌──────────────────────────────────────────────────────────┐   │
│  │  Consolidation ("Sleep")                                  │   │
│  │  The LLM reads recent episodes + current entity files     │   │
│  │  and rewrites entity files to integrate new knowledge.    │   │
│  │  That's it. Learning, forgetting, merging, pruning —      │   │
│  │  all happen in this single pass through LLM judgment.     │   │
│  └──────────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────────┘
```

### File System

```
.arc/
├── agents/
│   └── procurement-agent/
│       ├── agent.toml                      # Identity, tools, permissions. No rules.
│       └── memory/
│           ├── working.md                  # Scratchpad. Overwritten each turn.
│           ├── how-i-work.md               # Learned behavioral patterns. LLM-maintained.
│           └── episodes/                   # Significant moments. Append-only.
│               ├── 2026-02-21-deadline-change.md
│               └── 2026-02-20-pricing-correction.md
│
└── team/
    ├── entities/                            # The knowledge graph
    │   ├── agencies/
    │   │   ├── nnsa.md                     # Facts, relationships, AND lessons about NNSA
    │   │   └── doe.md
    │   ├── contacts/
    │   │   └── sarah-chen.md
    │   ├── projects/
    │   │   └── genesis-mission.md
    │   ├── domains/                         # Cross-cutting knowledge areas
    │   │   ├── procurement.md              # Lessons, rules, patterns about procurement
    │   │   └── pricing.md                  # Lessons about pricing analysis
    │   └── _index.json                     # Lightweight manifest
    │
    ├── playbooks/
    │   ├── rfi-response-process.md
    │   └── cui-handling.md
    │
    └── decisions/
        └── 2026-02-21-vendor-selection.md
```

### Key Structural Decision: Domain Entities

Lessons don't live in a separate system. They live in entity files. But not all lessons are about a specific agency or person — some are about a domain of work. "Always show methodology before numbers in pricing analysis" isn't about NNSA. It's about pricing.

The solution: **domain entities.** Files like `procurement.md`, `pricing.md`, `compliance.md` that represent areas of work rather than specific organizations or people. They follow the same schema as any entity file — YAML frontmatter, wiki-links, summary, key facts. They just happen to contain behavioral knowledge rather than organizational knowledge.

When the agent encounters a pricing conversation, graph traversal finds `pricing.md`. The lesson is right there in the file. No separate lesson system needed.

```markdown
---
entity_type: domain
entity_id: pricing
name: Pricing Analysis
status: active
last_updated: 2026-02-21
links_to: ["[[procurement]]", "[[proposals]]"]
linked_from: ["[[nnsa]]", "[[genesis-mission]]", "[[wm-synergy]]"]
tags: [analysis, methodology, financial]
source_agents: [procurement-agent, strategy-agent]
---

# Pricing Analysis

## How We Do This
Lead with methodology and assumptions before presenting numbers.
User explicitly corrected this approach on 2026-02-20 — show the
reasoning framework first, then the outputs.

Always present cost comparisons in table format. (Learned 2026-02-20)

## Key Constraints
- Contract vehicle pricing must be verified against vehicle scope before quoting
- Historical pricing data older than 18 months should be flagged as potentially stale
- NNSA pricing requires [[CMMC]] Level 2 cost allocation methodology

## Linked Processes
- See [[rfi-response-process]] for RFI-specific pricing templates
- See [[procurement]] for general procurement constraints
```

This is a lesson file, a knowledge file, and a preference file — all in one, because there's no meaningful distinction. It's just "what the agent knows about pricing."

---

## 3. How It Works

### Session Start

```
1. Read how-i-work.md → inject into context (≤500 tokens)
2. Initialize empty working.md
3. Ready.
```

That's it. No loading 30 lessons. No ranking formulas. One file, read once.

### During a Turn

```
1. User message arrives

2. Does this need retrieval?
   Quick check (grep or lightweight LLM call):
   - Named entity mentioned? → retrieve
   - Past event referenced? → retrieve
   - Factual recall needed and not in context? → retrieve
   - Already in context from earlier turn? → skip
   - Generic/conversational? → skip

3. If retrieving:
   a. Graph traversal: grep for entity → read file → follow [[links]] one hop
   b. Everything relevant is IN the entity files — facts, lessons, constraints
   c. Cap at retrieval token budget
   d. If grep misses: vector search fallback (optional)

4. LLM generates response using current context + retrieved entities

5. Overwrite working.md with turn state
```

### Session End (Light Consolidation)

```
1. Flush working.md

2. Was this session significant?
   (LLM judgment: decisions, corrections, novel information, emotional weight)

3. If significant → create episode file
   - What happened, decisions made, entities touched
   - Links to entity files via [[wiki-links]]

4. For each entity touched in the session:
   - Update last_verified in frontmatter
   - If new facts learned → append to entity's "Recent Activity"
   - If correction received → LLM updates the relevant entity file directly
     (e.g., adds the pricing lesson to pricing.md)

5. Co-occurrence linking:
   - Entities that appeared together in this session but aren't linked → link them
   - This is cheap and obvious — both files are already in context
   - Catches ~30% of real connections

6. If a new entity was introduced → create stub via promotion gate

7. Update how-i-work.md if the session produced behavioral insights
   - LLM reads current how-i-work.md + session summary
   - "Has anything changed about how this agent should operate?"
   - If yes → rewrite. If file exceeds budget → compress.

8. Clear working.md
```

### Deep Consolidation (Sleep Cycle)

```
Run on schedule (daily) or triggered manually.
Intensity scales to recent activity — quiet day = skip.

Two passes, both LLM-driven:

PASS 1: Entity-Centric (process each touched entity)

  For each entity file touched by recent episodes:
  - LLM reads: current entity file + all episodes that reference it
  - LLM rewrites: integrated summary, resolved contradictions,
    updated facts, pruned stale information
  - If file exceeds its token budget → LLM must compress
  - The rewrite IS the learning, forgetting, and prioritization
  - Episode-mediated links discovered here — the LLM sees connections
    across episodes that no single session contained

PASS 2: Graph-Centric (discover non-obvious connections)

  Select a domain cluster (entities sharing tags or link neighborhoods):
  - Read frontmatter + summary of each entity in the cluster
  - LLM prompt: "Given these entities, what connections exist that
    aren't currently linked? What patterns, complementary capabilities,
    shared constraints, or dependencies do you see?"
  - Add bidirectional [[wiki-links]] for discovered connections
  - Rotate domains across nights — procurement tonight, personnel
    tomorrow, vendors next. Full graph covered over ~1 week.
  - Busy domains (more recent activity) scanned more frequently.

THEN:

3. Merge detection:
   - Find entity pairs with 3+ shared links
   - LLM judges: "Same entity?" → merge files, redirect links

4. how-i-work.md refresh (per agent):
   - LLM reads: current how-i-work.md + recent episodes across sessions
   - "What patterns have emerged? What should be kept, dropped, updated?"
   - Rewrite within budget.

5. Staleness:
   - Entities with last_verified past TTL → flag stale
   - Stale + zero access for extended period → archive

6. Convergence (team):
   - Multiple agents independently learned similar things
   - Auto-promote convergent knowledge to team entity files
```

### How Connections Form

Linking — the act of connecting entities, thoughts, documents, and lessons — is where the real intelligence of the memory system lives. A database stores facts. A graph discovers relationships. The question is: when and how do those relationships get discovered?

There are three natural moments where linking happens, each catching a different class of connection, in increasing order of difficulty and value:

#### Tier 1: Co-occurrence Linking (During the Turn)

The agent is talking about Genesis Mission and the user mentions NNSA. Both entity files are already in context. When the agent writes to `genesis-mission.md` during light consolidation, it naturally adds `[[nnsa]]` because it just saw the relationship.

This is cheap, obvious, and requires no extra work. It catches connections between things that appeared in the same conversation — maybe 30% of all real connections.

**Limitation:** The agent only sees what's in the current conversation. It has tunnel vision. It can't connect NNSA's budget cycle to SEWP V's timeline unless both happen to be in context at the same time.

#### Tier 2: Episode-Mediated Linking (Deep Consolidation — Entity Pass)

During deep consolidation, the LLM reads an entity file plus all recent episodes that reference it. Those episodes mention OTHER entities. The LLM naturally sees connections that no single session contained:

```
Consolidation reads genesis-mission.md
  → Episode from Feb 21 references [[nnsa]] and [[deadline]]
  → Episode from Feb 18 references [[sewp-v]] and [[procurement]]
  → genesis-mission.md currently links to [[nnsa]] but NOT [[sewp-v]]
  → LLM adds: [[sewp-v]] link to genesis-mission.md
  → Because the episodes revealed a connection that no single session showed
```

This is the entity-centric pass. It processes one entity at a time, reading its episodes, and finds links FROM the entity TO things mentioned in its episodes. It catches another ~50% of connections — the ones that span multiple sessions but still share a common entity.

#### Tier 3: Structural/Pattern Linking (Deep Consolidation — Graph Pass)

This is the hard one and the valuable one. It finds connections between entities that have never co-occurred in any conversation or episode.

```
Entity A: wm-synergy.md
  - Manufacturing partner
  - Specializes in DOE supply chain
  - Has CMMC Level 2 certification
  - Tags: [manufacturing, supply-chain, doe, cmmc]

Entity B: genesis-mission.md
  - NNSA project requiring AI/ML modernization
  - Requires CMMC Level 2 contractors
  - Has supply chain component
  - Tags: [nnsa, ai-ml, procurement, cmmc]

No session has ever discussed these two entities together.
No episode references both.
But they share structural properties: CMMC requirement, DOE/NNSA
relationship, supply chain relevance.

A graph-centric pass reads both files and recognizes:
"WM Synergy's capabilities map to Genesis Mission's requirements.
 These should be linked."
```

This is what the neocortex does that the hippocampus can't. You learn about electricity in physics class and osmosis in biology class. Your brain connects them because they share a structural pattern (flow driven by a gradient), even though they never appeared in the same experience.

The graph-centric pass works like this:

```
1. Select a cluster of entities in the same domain or with overlapping tags
   (e.g., all entities tagged [procurement] or [doe])
2. Feed the LLM a batch of entity summaries (not full files — just
   frontmatter + summary section, to fit in context)
3. Prompt: "Given these entities, what connections exist that aren't
   currently linked? What patterns, complementary capabilities, or
   dependencies do you see?"
4. LLM returns discovered connections with reasoning
5. Add bidirectional [[wiki-links]] for confirmed connections
```

This is more expensive per cycle (more tokens, broader reads) but it's where the deepest insights come from. The agent that connects the vendor's capability to the agency's requirement before any human does — that's the agent that becomes indispensable.

#### When Each Type Runs

```
┌──────────────────────────────────────────────────────────────────┐
│  Linking Type          │ When              │ Cost    │ Value     │
├────────────────────────┼───────────────────┼─────────┼───────────┤
│  Co-occurrence         │ Every session     │ Free    │ Obvious   │
│  (both entities in     │ (light consol.)   │ (inline)│ connections│
│   same conversation)   │                   │         │           │
├────────────────────────┼───────────────────┼─────────┼───────────┤
│  Episode-mediated      │ Deep consolidation│ Medium  │ Cross-    │
│  (entity + its episodes│ (entity pass)     │ (per-   │ session   │
│   reveal connections)  │                   │ entity) │ insights  │
├────────────────────────┼───────────────────┼─────────┼───────────┤
│  Structural/pattern    │ Deep consolidation│ Higher  │ Non-obvious│
│  (entities share       │ (graph pass)      │ (batch  │ strategic │
│   properties but never │                   │ reads)  │ connections│
│   co-occurred)         │                   │         │           │
└──────────────────────────────────────────────────────────────────┘
```

The graph pass doesn't need to scan the entire graph every night. It can rotate through domains — procurement entities tonight, personnel tomorrow, vendors the night after. Over a week, the full graph gets covered. Busy domains (lots of recent activity) get scanned more frequently. Quiet domains get scanned less. The consolidation engine adapts intensity to activity, same as the rest of the system.

### How Lessons Are Learned (Example)

```
Day 1: User corrects agent during a procurement conversation.
  "You need to show the methodology before the numbers."

  → Light consolidation detects the correction
  → LLM writes it into pricing.md: "Lead with methodology before numbers"
  → Episode records: "User corrected pricing presentation approach"

Day 3: Agent encounters another pricing conversation.
  → Retrieves pricing.md (because the conversation mentions pricing)
  → The lesson is right there in the file
  → Agent follows it naturally

Day 7: User corrects again in a different context.
  "When you're unsure about a deadline, ask me instead of estimating."

  → This isn't about a specific entity. It's a general behavioral pattern.
  → Light consolidation writes it into how-i-work.md
  → Next session, the agent reads it at start

Day 14: Deep consolidation runs.
  → LLM reads how-i-work.md, which now has 12 behavioral patterns
  → Some overlap: "ask before assuming" appears in 3 different phrasings
  → LLM consolidates into one clear statement
  → File stays within 500 token budget

Day 30: Model upgrade (Claude 4.5 → Claude 5).
  → Next consolidation pass, the better model:
     - Writes tighter summaries in entity files
     - Catches subtler patterns in episodes
     - Makes better retrieval decisions
     - Produces more insightful how-i-work.md synthesis
  → The entire memory system got smarter. Zero code changes.
```

---

## 4. Token Budgets

### The Budget Structure

Token budgets are the only hard constraints in the system. They serve two purposes: preventing context window bloat, and creating a forcing function that makes consolidation compress and prioritize.

```
┌────────────────────────────────────────────────────────────────┐
│  Total Memory Token Budget per Turn: 4,000 tokens (configurable)
│                                                                │
│  ┌──────────────────────────────────────────────────────────┐  │
│  │ SECTION 1: Identity Context           ≤ 500 tokens       │  │
│  │ Source: how-i-work.md                                    │  │
│  │ Loaded: once at session start, persists all session      │  │
│  │                                                          │  │
│  │ Contains: learned behavioral patterns, working style,    │  │
│  │ cross-cutting lessons, synthesized preferences.          │  │
│  │ Written and maintained entirely by the LLM during        │  │
│  │ consolidation. Not human-configured.                     │  │
│  │                                                          │  │
│  │ If file exceeds 500 tokens → consolidation must compress │  │
│  │ The LLM decides what's most important to keep.           │  │
│  └──────────────────────────────────────────────────────────┘  │
│                                                                │
│  ┌──────────────────────────────────────────────────────────┐  │
│  │ SECTION 2: Retrieved Context          ≤ 3,000 tokens     │  │
│  │ Source: entity files, episodes, playbooks                │  │
│  │ Loaded: per-turn, only when retrieval is triggered       │  │
│  │                                                          │  │
│  │ Contains: entity knowledge, domain-specific lessons,     │  │
│  │ relevant procedures, contextual constraints.             │  │
│  │ All found through graph traversal (grep + follow links). │  │
│  │                                                          │  │
│  │ If retrieved subgraph exceeds 3,000 tokens:              │  │
│  │ Option A: Truncate least-connected files                 │  │
│  │ Option B: LLM summarizes the subgraph into budget        │  │
│  │ Option C: Only include first-hop entities, skip second   │  │
│  │ (Configurable. Default: Option A)                        │  │
│  └──────────────────────────────────────────────────────────┘  │
│                                                                │
│  ┌──────────────────────────────────────────────────────────┐  │
│  │ SECTION 3: Working Context            ≤ 500 tokens       │  │
│  │ Source: working.md (scratchpad)                           │  │
│  │ Loaded: every turn (it's the current task state)         │  │
│  │                                                          │  │
│  │ Contains: current turn context, active decisions,        │  │
│  │ pending items from this session.                         │  │
│  │ Overwritten each turn, so it stays current.              │  │
│  └──────────────────────────────────────────────────────────┘  │
│                                                                │
│  Remaining context window: system prompt + conversation        │
│  history + tool results (managed by agent loop, not memory)    │
└────────────────────────────────────────────────────────────────┘
```

### How Budgets Create Behavior

The budgets don't just limit — they shape how the system evolves:

**how-i-work.md at 500 tokens** forces the LLM to synthesize. An agent with 50 sessions of corrections can't store 50 separate rules. The LLM must distill them into principles: "Lead with methodology. Confirm before acting. Ask when uncertain." This is compression through constraint — the same way a 280-character tweet forces clarity.

**3,000 tokens of retrieval** means roughly 3-5 entity files per turn. This is plenty for focused conversations but forces the agent to retrieve selectively, not dump everything. The retrieval decision logic ("do I need this?") becomes important because the budget makes wasted retrievals expensive.

**Entity files themselves have implicit budgets.** If an entity file grows to 2,000 tokens, it's taking up two-thirds of the retrieval budget on its own. Consolidation naturally compresses it because the LLM, rewriting the file, produces the most important information first and drops the rest when hitting a reasonable length. You can also set an explicit per-file budget (e.g., 800 tokens) in config.

**The total 4,000 token budget is configurable.** Agents with larger context windows (128K+) can increase it. Agents running on smaller/cheaper models can decrease it. The system adapts — consolidation just compresses harder or softer depending on the budget.

### Per-File Budget (Optional)

```toml
[memory.budgets]
total_per_turn = 4000
identity_context = 500                # how-i-work.md
retrieved_context = 3000              # entity files + episodes
working_context = 500                 # working.md
per_entity_file = 800                 # optional: cap individual entity files
overflow_strategy = "truncate"        # "truncate", "summarize", or "skip_least_connected"
```

When consolidation rewrites an entity file, it checks the per-file budget. If the rewrite exceeds 800 tokens, the LLM must compress it. This keeps entity files tight and retrievable — a single file never dominates the retrieval budget.

---

## 5. Biological Foundation

### Why This Architecture Mirrors Biology

**Complementary Learning Systems.** Individual memory (hippocampus) is fast, personal, and ephemeral. Team memory (neocortex) is slow, shared, and stable. Consolidation transfers knowledge from individual to team selectively.

**Consolidation is replay.** During sleep, the hippocampus replays episodes to the neocortex, which integrates them into existing knowledge. In this system, the LLM reads recent episodes and rewrites entity files. Same mechanism, same purpose.

**Forgetting is not deletion.** The brain doesn't have a garbage collector. Memories become unreachable when retrieval paths weaken. Here, information drops out when the LLM doesn't include it in the next rewrite. It fades because it wasn't important enough to keep, not because a prune job ran.

**Lessons are associative.** You remember the stove is hot when you see the stove. In this system, the lesson about COR approval lives in `procurement.md`. When the agent encounters procurement, it finds the lesson. No separate lesson system needed — the lesson IS the entity knowledge.

**Compression through constraint.** Working memory holds 4±1 chunks. The 500-token budget on how-i-work.md serves the same function — it forces synthesis and prioritization, producing principles rather than lists.

**Reconsolidation.** Every retrieval modifies the memory. When an entity file is read and used, its `last_verified` updates. During the next consolidation, recently-accessed entities get priority. The act of remembering strengthens the memory.

**Neocortical pattern matching.** The hippocampus connects things that co-occurred in the same experience. The neocortex does something harder — it finds structural similarities between things that NEVER co-occurred. You learn about electricity in physics and osmosis in biology, and your brain connects them because they share a pattern (flow driven by gradient). In this system, the graph-centric consolidation pass reads entity clusters and discovers connections that no single conversation or episode contained. This is where the highest-value linking happens.

**Model improvement = brain development.** A child and an adult have the same memory architecture but different cognitive capabilities. When we upgrade the LLM, the architecture stays the same but every judgment gets better — consolidation is tighter, retrieval is more relevant, link discovery catches subtler patterns, synthesis is deeper. The system matures without structural changes.

---

## 6. PRD: arc-memory (Agent)

### Overview

arc-memory manages individual agent memory: the working scratchpad, the learned identity file, and personal episodes. It handles retrieval across both personal and team memory, and writes to team memory through the promotion gate.

### Components

| Component | What It Is | Lifecycle |
|-----------|-----------|-----------|
| `working.md` | Scratchpad. Current turn state. | Overwritten every turn. Cleared at session end. |
| `how-i-work.md` | Learned behavioral patterns, preferences, cross-cutting lessons. | Read at session start. Updated by LLM during consolidation. Budget: 500 tokens. |
| `episodes/` | Significant moments. What happened, decisions made, entities touched. | Created at session end if significant. Append-only. Never edited. |

### Module Bus Events

```
session.start     → read how-i-work.md, inject into context
turn.start        → evaluate retrieval need, retrieve if needed
turn.end          → overwrite working.md
session.end       → flush, evaluate significance, consolidate
```

### Functional Requirements

#### Working Memory

| ID | Requirement |
|----|-------------|
| WM-1 | Single markdown file, overwritten every turn |
| WM-2 | Contains: current task context, active entities, pending decisions |
| WM-3 | Flushed to long-term storage at session end before clearing |
| WM-4 | Starts empty on session start |
| WM-5 | Budget: 500 tokens. If turn state exceeds budget, LLM summarizes. |
| WM-6 | Never read by other agents |

#### Identity (how-i-work.md)

| ID | Requirement |
|----|-------------|
| ID-1 | Contains learned behavioral patterns, working style, cross-cutting lessons |
| ID-2 | NOT configured by humans. Written and maintained entirely by the LLM. |
| ID-3 | Read at session start and injected into context for the entire session |
| ID-4 | Updated during light consolidation (session end) if the session produced behavioral insights |
| ID-5 | Updated during deep consolidation as the LLM synthesizes patterns across many sessions |
| ID-6 | Budget: 500 tokens. If file exceeds budget, the LLM must compress it, deciding what to keep and what to drop. |
| ID-7 | Starts empty for a new agent. Grows organically through experience. |
| ID-8 | Contains things like: communication preferences learned from user corrections, domain-specific patterns, self-identified strengths and weaknesses, working norms |

#### Episodes

| ID | Requirement |
|----|-------------|
| EP-1 | Created at session end if the LLM judges the session was significant |
| EP-2 | Significance criteria (LLM judgment): decisions made, corrections received, novel information, emotional weight |
| EP-3 | Schema: date, type, significance, participants, emotional_signal, entities_touched, source_agent |
| EP-4 | Body: what happened, decisions made, actions taken, why it matters |
| EP-5 | Links to team entity files via [[wiki-links]] |
| EP-6 | Append-only. Never edited after creation. |
| EP-7 | Personal — not readable by other agents (except consolidation engine for convergence detection) |

#### Retrieval

| ID | Requirement |
|----|-------------|
| RT-1 | On turn.start, evaluate whether retrieval is needed (see decision logic below) |
| RT-2 | Primary path: graph traversal. Grep for entities → read files → follow [[links]] one hop. |
| RT-3 | Fallback path: vector search (optional, configurable backend) |
| RT-4 | Retrieved content includes entity knowledge, domain-specific lessons, constraints — all in the same files |
| RT-5 | Budget: 3,000 tokens. If subgraph exceeds budget, apply overflow strategy (truncate / summarize / skip least-connected) |
| RT-6 | Search scope: team entities → personal episodes → team playbooks |
| RT-7 | Emit telemetry: files retrieved, path used, tokens consumed |

**Retrieval Decision Logic:**

```
User message arrives
  → Named entity? (grep .arc/team/entities/) → RETRIEVE
  → Past event reference? ("last time", "we discussed") → RETRIEVE
  → Factual recall needed, not in current context? → RETRIEVE
  → Already in context from earlier turn? → SKIP
  → Generic/conversational? → SKIP
  → Uncertain? → RETRIEVE with reduced budget (1,500 tokens)
```

#### Light Consolidation (Session End)

| ID | Requirement |
|----|-------------|
| LC-1 | Flush working.md |
| LC-2 | LLM evaluates session significance |
| LC-3 | If significant: create episode file |
| LC-4 | For touched entities: update last_verified, append to Recent Activity |
| LC-5 | If correction received: LLM updates the relevant entity file directly. "Show methodology first" goes into `pricing.md`. "Confirm clearance with NNSA" goes into `nnsa.md`. |
| LC-6 | **Co-occurrence linking:** Entities that appeared together in this session but aren't currently linked → add bidirectional [[wiki-links]]. Both files are already in context, so this is cheap. |
| LC-7 | If new entity introduced: create stub via promotion gate |
| LC-8 | LLM evaluates: "Has anything about how this agent operates changed?" If yes → update how-i-work.md within 500-token budget. |
| LC-9 | Completes in < 30 seconds |

#### Promotion Gate

| ID | Requirement |
|----|-------------|
| PG-1 | Agent reads from team memory freely |
| PG-2 | Agent writes to team memory only through promotion gate |
| PG-3 | Promotion triggers: LLM judges information is team-relevant (not just personal), or convergence detected during deep consolidation |
| PG-4 | All promotions emit telemetry: source agent, content, target entity |

### Configuration

```toml
[memory]
enabled = true

[memory.paths]
working = ".arc/agents/{agent_id}/memory/working.md"
identity = ".arc/agents/{agent_id}/memory/how-i-work.md"
episodes = ".arc/agents/{agent_id}/memory/episodes/"

[memory.budgets]
total_per_turn = 4000
identity_context = 500
retrieved_context = 3000
working_context = 500
per_entity_file = 800
overflow_strategy = "truncate"    # "truncate", "summarize", "skip_least_connected"

[memory.retrieval]
primary = "graph_traversal"
fallback = "vector_search"
vector_backend = "none"           # "none", "qdrant", "sqlite-vec"

[memory.consolidation]
on_session_end = true
significance_model = "llm"
```

---

## 7. PRD: arc-team-memory (Team)

### Overview

arc-team-memory manages the shared knowledge graph, playbooks, and decisions log. It is the neocortical layer — slow to update, stable, shared, authoritative. It provides the deep consolidation engine.

### Components

| Component | What It Is | Lifecycle |
|-----------|-----------|-----------|
| `entities/` | Wiki-linked markdown files. The knowledge graph. Contains facts, lessons, relationships, constraints — everything. | Updated through promotion gate + consolidation |
| `entities/domains/` | Cross-cutting knowledge areas (procurement, pricing, compliance). Same schema as entities but contain behavioral knowledge. | Same lifecycle as entities |
| `playbooks/` | SOPs, procedures, checklists. | Manually authored or agent-generated through task completion |
| `decisions/` | What was decided, why, by whom. | Append-only. Created from promoted episodes. |
| `_index.json` | Lightweight manifest for fast lookups. | Derived. Rebuildable. |

### Entity File Schema

```markdown
---
entity_type: agency                   # or: contact, project, domain, vendor, etc.
entity_id: nnsa
name: NNSA
status: active
last_updated: 2026-02-21
last_verified: 2026-02-21
created: 2025-09-15
links_to: ["[[DOE]]", "[[Genesis Mission]]", "[[sarah-chen]]", "[[procurement]]"]
linked_from: ["[[genesis-mission]]", "[[doe]]", "[[sewp-v]]"]
tags: [federal, energy, nuclear]
source_agents: [procurement-agent, strategy-agent]
classification: fouo
---

# NNSA

## Summary
[LLM-maintained. Compressed to fit per-file budget. Represents the
 distilled, current understanding of this entity.]

## Key Facts
[Verifiable, current facts. Updated during consolidation.]

## Constraints and Lessons
[Behavioral knowledge specific to this entity. Learned from experience.
 "NNSA requires 48hr review on all submissions."
 "Always confirm security clearance before recommending personnel."]

## Recent Activity
[Append during light consolidation. Absorbed into Summary during deep consolidation.]
```

The "Constraints and Lessons" section is where entity-specific behavioral knowledge lives. It's not a separate system — it's part of the entity, found through normal graph traversal.

### Functional Requirements

#### Entity Graph

| ID | Requirement |
|----|-------------|
| EG-1 | Entities are markdown files with YAML frontmatter |
| EG-2 | Wiki-links create bidirectional connections |
| EG-3 | Domain entities (procurement.md, pricing.md) hold cross-cutting behavioral knowledge |
| EG-4 | `_index.json` provides fast lookup manifest (derived, rebuildable) |
| EG-5 | New entities created only through promotion gate, deep consolidation, or human edits |
| EG-6 | Per-entity file budget: 800 tokens (configurable). Consolidation compresses over-budget files. |

#### Entity Lifecycle

| ID | Requirement |
|----|-------------|
| EL-1 | On encounter: grep check before creating. Update existing or create stub. |
| EL-2 | On session end: update last_verified, append Recent Activity. |
| EL-3 | On correction: LLM updates the relevant entity file's Constraints section directly. |
| EL-4 | On deep consolidation: full rewrite cycle (see below). |
| EL-5 | States: active → stale (past TTL, no access) → archived |
| EL-6 | Archived entities can be restored if re-referenced |

#### Deep Consolidation

| ID | Requirement |
|----|-------------|
| DC-1 | Runs on schedule (default daily) or CLI trigger |
| DC-2 | Adaptive intensity: scale to recent activity. Quiet day = skip. |
| **Pass 1: Entity-Centric** | |
| DC-3 | **Entity rewrite.** For each entity touched by recent episodes: LLM reads current file + all referencing episodes → rewrites the file. This single pass handles learning (adding new knowledge), forgetting (dropping stale info), merging (consolidating overlapping facts), and compression (fitting within per-file budget). |
| DC-4 | **Episode-mediated link discovery.** During entity rewrite, the LLM sees connections across episodes that no single session contained. Connections between the entity being processed and other entities mentioned in its episodes are added as bidirectional [[wiki-links]]. |
| **Pass 2: Graph-Centric** | |
| DC-5 | **Domain cluster selection.** Select a cluster of entities sharing tags or link neighborhoods (e.g., all entities tagged [procurement], or all entities within 2 hops of [[nnsa]]). |
| DC-6 | **Structural pattern linking.** Feed the LLM the frontmatter + summary of each entity in the cluster. Prompt: "What connections exist that aren't currently linked? What patterns, complementary capabilities, shared constraints, or dependencies do you see?" Add bidirectional [[wiki-links]] for confirmed connections. |
| DC-7 | **Domain rotation.** Don't scan the full graph every night. Rotate through domains — procurement tonight, personnel tomorrow, vendors next. Full graph covered over ~1 week. Domains with more recent activity are scanned more frequently. |
| DC-8 | **Budget awareness.** Graph pass reads summaries, not full files. A cluster of 20 entities at ~100 tokens each = ~2,000 tokens input. Cost is manageable per cycle. |
| **Structural Maintenance** | |
| DC-9 | **Merge detection.** Entity pairs with 3+ shared links → LLM judges "same entity?" → merge files, redirect links. |
| DC-10 | **Staleness.** Entities past TTL → stale. Stale + extended no-access → archive. |
| DC-11 | **Convergence.** Multiple agents independently learned similar things → auto-promote to team entity. |
| DC-12 | **how-i-work.md refresh (per agent).** LLM synthesizes cross-session patterns into each agent's identity file. |
| DC-13 | **Index rebuild.** Regenerate _index.json. |
| DC-14 | Idempotent and crash-safe. |
| DC-15 | Emits detailed telemetry: entities rewritten, episode-mediated links discovered, structural links discovered, merges, archives, convergences. |

#### Decay

```toml
[team_memory.decay]
default_ttl_days = 90
ttl_overrides = { health = 365, preferences = 180, work = 90, relationships = 180 }
archive_path = ".arc/team/archive/"
```

### Configuration

```toml
[team_memory]
enabled = true
path = ".arc/team/"

[team_memory.entities]
path = ".arc/team/entities/"
index = ".arc/team/entities/_index.json"

[team_memory.consolidation]
schedule = "daily_2am"
adaptive_intensity = true
max_entities_per_cycle = 50

[team_memory.consolidation.entity_pass]
enabled = true                        # Pass 1: rewrite touched entities

[team_memory.consolidation.graph_pass]
enabled = true                        # Pass 2: structural pattern linking
cluster_strategy = "tag_overlap"      # "tag_overlap", "link_neighborhood", or "domain_rotation"
max_cluster_size = 20                 # entities per cluster
rotation_cycle_days = 7               # full graph covered in ~1 week
prioritize_active_domains = true      # busy domains scanned more often

[team_memory.convergence]
enabled = true
threshold = 3

[team_memory.security]
read_access = "all_agents"
write_access = "promotion_gate_only"
classification_required = true
encryption_at_rest = false            # enable for CUI environments
```

---

## 8. Integration

### The Full Flow

```
SESSION START
│
├─ Read how-i-work.md → inject (≤500 tokens)
├─ Initialize empty working.md
│
│  TURN 1: "What's the status of the Genesis Mission RFI?"
│  │
│  ├─ Retrieval needed? → "Genesis Mission" is an entity → YES
│  ├─ Graph traversal:
│  │   ├─ genesis-mission.md (deadline, contacts, status)
│  │   ├─ → follow [[nnsa]] → nnsa.md (constraints, clearance rules)
│  │   ├─ → follow [[procurement]] → procurement.md (COR approval rule, methodology)
│  │   └─ Total: 3 files, ~2,400 tokens (within 3,000 budget)
│  │
│  │   Note: The COR approval lesson, the clearance rule, and the
│  │   pricing methodology preference are all in these files.
│  │   No separate lesson lookup needed.
│  │
│  ├─ LLM responds using retrieved context
│  ├─ Overwrite working.md
│  └─ Telemetry: 3 files, graph_traversal, 2,400 tokens
│
│  TURN 2: "The deadline moved to March 10"
│  │
│  ├─ Retrieval needed? → already in context → SKIP
│  ├─ LLM processes from existing context
│  ├─ Overwrite working.md (flags deadline change)
│  └─ Telemetry: 0 tokens retrieval
│
│  TURN 3: "Format future reports in table format"
│  │
│  ├─ Retrieval needed? → no entity reference → SKIP
│  ├─ LLM acknowledges preference
│  └─ Overwrite working.md (flags preference for consolidation)
│
SESSION END
│
├─ Light consolidation:
│  ├─ Flush working.md
│  ├─ Significant? → YES (deadline change = important)
│  ├─ Create episode: 2026-02-21-genesis-deadline-update.md
│  ├─ Update genesis-mission.md: new deadline, last_verified
│  ├─ Update nnsa.md: touch last_verified
│  ├─ Co-occurrence check:
│  │   → genesis-mission.md and procurement.md both in context this session
│  │   → Already linked? YES → skip
│  │   → genesis-mission.md and nnsa.md? → Already linked → skip
│  │   → (If any pair wasn't linked, would add [[wiki-link]] now)
│  ├─ "Table format" preference → LLM judges where to record it:
│  │   → It's a general agent preference, not entity-specific
│  │   → Write into how-i-work.md: "Present reports in table format"
│  │   → how-i-work.md now at 480 tokens → within budget
│  └─ Clear working.md
│
└─ Done. Total LLM calls for consolidation: ~3 (significance, entity updates, identity check)


DEEP CONSOLIDATION (2am)
│
├─ 7 episodes across 3 agents → medium intensity
│
├─ PASS 1: Entity-Centric
│  │
│  ├─ Entity rewrites:
│  │  ├─ genesis-mission.md: LLM reads file + 3 recent episodes
│  │  │   → Rewrites Summary with updated deadline
│  │  │   → Recent Activity absorbed into Key Facts
│  │  │   → File was 750 tokens → now 680 tokens (within 800 budget)
│  │  │
│  │  ├─ procurement.md: LLM reads file + 2 correction episodes
│  │  │   → Adds "verify vehicle scope" lesson to Constraints section
│  │  │   → Merges two overlapping pricing lessons into one
│  │  │   → File was 820 tokens → compressed to 780 tokens
│  │  │
│  │  └─ nnsa.md: minor update, already current
│  │
│  └─ Episode-mediated links discovered:
│     → genesis-mission.md episodes mention [[sewp-v]] but no link exists
│     → Add: genesis-mission.md ←→ sewp-v.md (bidirectional)
│     → 1 cross-session link found
│
├─ PASS 2: Graph-Centric (tonight's domain: procurement cluster)
│  │
│  ├─ Cluster selected: 12 entities tagged [procurement] or linked to [[procurement]]
│  │   (genesis-mission, nnsa, sewp-v, wm-synergy, doe, pricing,
│  │    compliance, sarah-chen, cor-process, far-compliance, ...)
│  │
│  ├─ LLM reads frontmatter + summary of all 12 (~1,200 tokens input)
│  │   Prompt: "What connections should exist but don't?"
│  │
│  ├─ LLM discovers:
│  │   → wm-synergy.md has CMMC L2 cert + DOE supply chain expertise
│  │   → genesis-mission.md requires CMMC L2 + has supply chain component
│  │   → No session has ever discussed these together
│  │   → But the structural match is clear
│  │   → Add: wm-synergy.md ←→ genesis-mission.md
│  │   → Reason: "WM Synergy's CMMC L2 certification and DOE supply chain
│  │     specialization are directly relevant to Genesis Mission requirements"
│  │
│  └─ 1 structural link discovered (non-obvious, high value)
│
├─ Merge detection:
│  → sarah-chen.md + nnsa-contracting-officer.md → same person → merge
│
├─ how-i-work.md refresh (procurement-agent):
│  → LLM reads: current file + 7 episodes from past 2 weeks
│  → Notices pattern: agent has been corrected on formatting 3 times
│  → Synthesizes: "Default to structured formats (tables, numbered lists)
│     for analysis. Lead with conclusions, support with methodology."
│  → File: 480 → 450 tokens (compressed two similar rules into one)
│
├─ Staleness: old-vendor-xyz.md → archive
├─ Convergence: AI/ML budget fact → promoted to nnsa.md (3 agents converged)
│
└─ Telemetry: 5 entities rewritten, 1 episode-mediated link,
   1 structural link, 1 merge, 1 archive, 1 convergence
```

### Cross-Agent Memory

Individual agents can't read each other's personal memory. Knowledge flows between agents through the team graph:

```
Agent A learns something → writes to team entity (via promotion gate)
Agent B retrieves that entity → learns from Agent A's contribution
```

For multi-agent deployments, memory events flow over the existing NATS bus:

```json
{
  "type": "memory.entity.updated",
  "from": "procurement-agent",
  "subject": "team.entities.projects.genesis-mission",
  "payload": {
    "operation": "consolidation_rewrite",
    "tokens_before": 750,
    "tokens_after": 680,
    "source_episodes": ["2026-02-21-genesis-deadline-update.md"]
  },
  "classification": "fouo"
}
```

### Security

```
Individual Memory:  read/write self only
Team Memory:        read all (with clearance), write via promotion gate only
Cross-Agent:        consolidation engine reads episodes for convergence (not agents)
Classification:     entity files carry classification, agents need matching clearance
NATS:               classification travels with messages
```

### CLI

```bash
# Agent memory
arc memory status                          # Budget usage, episode count, identity file size
arc memory identity show                   # Show how-i-work.md
arc memory episodes list                   # List episodes
arc memory working show                    # Current scratchpad

# Team memory
arc team memory entities list              # All entities from _index.json
arc team memory entities show <id>         # Read entity file
arc team memory entities search "query"    # Grep-based search
arc team memory entities graph <id>        # Entity + one-hop linked entities
arc team memory playbooks list
arc team memory decisions list

# Consolidation
arc team memory consolidate                # Trigger deep consolidation
arc team memory consolidate --dry-run      # Preview what would change
arc team memory health                     # Graph stats: count, stale, orphans, budget compliance

# Maintenance
arc team memory rebuild-index
arc team memory verify-links
arc team memory archive list
arc team memory archive restore <id>
```

### Build Order

**Phase 1 — Core (v0.1)**
1. working.md lifecycle (overwrite per turn, flush on session end)
2. Entity files with schema (manual creation to start)
3. Grep-based retrieval with link following
4. Retrieval decision logic
5. Token budget enforcement (total + per-section)
6. Light consolidation (significance check, entity updates, co-occurrence linking)
7. how-i-work.md (starts empty, LLM writes to it during consolidation)

**Phase 2 — Intelligence (v0.2)**
8. Episode recording with significance evaluation
9. Domain entities (procurement.md, pricing.md)
10. Deep consolidation Pass 1: entity-centric rewrite with episode-mediated link discovery
11. Deep consolidation Pass 2: graph-centric structural/pattern linking with domain rotation
12. Promotion gate
13. Staleness detection and archival

**Phase 3 — Scale (v0.3)**
14. Convergence detection
15. Vector search fallback
16. NATS integration for memory events
17. Adaptive consolidation intensity (activity-driven scheduling)
18. Per-entity-file budget enforcement during consolidation

**Phase 4 — Harden (v0.4)**
19. Classification-based access control
20. Encryption at rest
21. Memory provenance chain
22. FedRAMP audit evidence generation
23. Budget-aware retrieval degradation

---

## 9. Comparison to Traditional Approaches

### vs. Original Blueprint (22+ Components)

| Aspect | Original | This |
|--------|----------|------|
| Components | 22+ explicit modules | 5 concepts (scratchpad, identity, episodes, entities, consolidation) |
| Intelligence | Formulas (score decay, confidence, ranking) | LLM judgment at every decision point |
| Lessons | Separate tiered system with promotion logic | In the entity graph. A lesson IS knowledge. |
| Linking | Single link-discovery step during maintenance | Three tiers: co-occurrence (realtime), episode-mediated (entity pass), structural patterns (graph pass) |
| Pruning | Explicit jobs (nightly, weekly, monthly) | Token budgets force compression. LLM decides what to drop. |
| Model upgrades | No effect on scoring/ranking | Every judgment improves. System gets smarter for free. |
| Emergent behavior | Constrained by explicit rules | Enabled by structure + LLM judgment |
| Code estimate | 2,000+ lines | ~300-500 lines |
| PRD length | 30+ pages | This document |

### vs. Mem0 / MemGPT / Vector-Only Systems

| Aspect | Vector Systems | This |
|--------|---------------|------|
| Storage | Embeddings + metadata in vector DB | Markdown files with wiki-links |
| Retrieval | Cosine similarity | Graph traversal (grep + follow links) |
| Relationships | Lost after embedding | Explicit in [[wiki-links]], traversable |
| Lessons | Separate from knowledge | IN the knowledge (same files) |
| Auditable | Query the DB | Read the file. `git log` for history. |
| Dependencies | Vector DB, embedding model | File system. Optionally git. |
| Human readable | No | Yes |
| Agent readable | Requires deserialization | Read the markdown |
