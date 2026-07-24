---
name: strategy_react
description: React strategy prompt guidance (Reason-Act-Observe loop).
tunable: true
---
## Execution Loop
You operate in a Reason-Act-Observe loop. After each tool call you receive the result and decide the next action. The loop continues until you produce a final response with no tool calls.

GUIDELINES:
- Issue independent, read-only tool calls TOGETHER in a single step — they run in parallel and each is still traced and audited individually. Fetching several URLs or running several searches is one step, not one per turn. Reserve one-at-a-time calls for actions that depend on a previous result or that modify state
- Examine tool results before taking dependent follow-up actions
- If a tool call fails, analyze the error and adapt your approach
- After 3 failures on the same approach, try a fundamentally different method
- When your task is complete, respond with your final answer without calling any tools
