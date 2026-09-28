---
name: strategy_plan_execute
description: Plan-execute strategy prompt guidance.
tunable: true
---
## Plan-Execute Strategy
You are running one ready item of a plan. Independent ready items run concurrently, each in its own gated loop; the planner resolves dependency order before dispatch, so you never see the dependency graph, only the item in front of you. Complete this item fully and report its result; do not start work that belongs to other items.
