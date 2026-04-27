"""Model-facing prompt guidance for orchestration capabilities.

The agent injects this into its system prompt when spawn_task is
registered as a tool. Lives in arcagent (not arcrun) because spawn is
an agent-layer feature.
"""

from __future__ import annotations

SPAWN_GUIDANCE = """\
## Task Delegation (spawn_task)
Before executing multi-step work sequentially, evaluate whether subtasks
should be delegated to child agents. Use spawn_task when:
- Subtasks are independent and can run in parallel
- A subtask requires specialized focus (research, verification, review)
- A subtask would consume significant context that the main thread does
  not need to retain
- The task naturally decomposes into separable pieces

Do NOT use spawn_task when:
- The subtask depends on results from the current conversation
- The overhead of context transfer outweighs the benefit
- A single tool call would accomplish the same thing

Always include ALL necessary context in the task description — child
agents have no memory of this conversation. The child inherits your
tools and system prompt but starts with a fresh message history.

<example>
User asks: "Research the top 3 competing products and summarize their pricing"

Good delegation:
  spawn_task(task="Research and summarize pricing for [Product A]. Include: ...")
  spawn_task(task="Research and summarize pricing for [Product B]. Include: ...")
  spawn_task(task="Research and summarize pricing for [Product C]. Include: ...")
  -> Three independent research tasks run concurrently

Bad delegation:
  spawn_task(task="Do step 1 of the analysis")
  -> Too vague, lacks context, child cannot succeed
</example>"""

__all__ = ["SPAWN_GUIDANCE"]
