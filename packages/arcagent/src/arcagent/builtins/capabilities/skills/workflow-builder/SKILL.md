---
name: workflow-builder
version: 1.0.0
description: Turn a repeatable business process described in conversation into a named, validated ArcFlow workflow draft.
triggers: [we do this every time, turn that into a workflow, automate this process, build a workflow for, this should run every Monday, same steps every week]
tools: [workflow_create, workflow_add_node, workflow_edit_node, workflow_remove_node, workflow_set_trigger, workflow_set_channel, workflow_inspect, workflow_list, workflow_run]
---

## Resources

(auto-filled by the loader)

## Contract

Before calling `workflow_create` you must be able to state, without guessing:

- **The trigger.** What starts it — a person asking, a clock, or nothing yet (manual).
- **The steps, in order,** with what each one *produces*, not just what it does.
- **Who runs each step** — which agent, or which tool, or a human decision.
- **What "done" looks like** for the whole process.

What you produce: a **draft** workflow the operator can read and sign. You never
produce a signed workflow — there is no tool that does, and asking for one is a
sign you have misread this skill.

Do not proceed if the person cannot name what a step produces. "Then we review
it" is not a step; "the reviewer returns approve or revise with notes" is. A
step whose output nobody can name cannot be handed to the next step, and typed
handoff is the entire reason this is a workflow rather than a prompt.

## Knowledge

A workflow is a **graph of nodes**, not a script. Each node declares what it
`needs`; the runner materialises a node only when its dependencies are met.

Five node kinds, and the honest test for each:

| Kind | Use when | The giveaway |
|---|---|---|
| `agent` | The step needs judgement | You cannot write down the rule it follows |
| `tool` | The step is one declared call | You can name the tool and its arguments |
| `script` | The step is deterministic code | Same input always gives the same output |
| `router` | The path forks | You can enumerate every branch |
| `gate` | A human must decide | A wrong answer is expensive or irreversible |

**Typed handoff is the point.** A node declares `output_schema`; the runner
validates the output against it *before* any downstream node sees it. A schema
violation is a retryable failure, never a value passed forward. So write the
schema first and the prompt second — the schema is the contract, the prompt is
just how you ask for it.

**Declare artifacts when files are the real deliverable.** A node with
`artifacts = ["report.md"]` is not complete until that file exists, and the
retry message names the tool that produces it.

**Loops are declared and bounded.** A maker→checker→revise cycle is
`loop_back_to` plus `max_iterations`. There is no other way to make a cycle, and
an undeclared cycle is rejected by the validator.

**Four named patterns cover most real processes:**

- **Intake → specialist.** One node gathers and routes; specialists do the work.
- **Fan-out → synthesize.** Several independent nodes, one node that joins them.
- **Maker → checker.** One produces, one judges, a bounded loop revises.
- **Scheduled watcher.** A cron trigger, a cheap check, a router that usually
  exits early.

**Every mutation returns a draft and bumps the version.** An edit requires the
`expected_version` you last saw; a stale edit is refused rather than merged.

## Steps

1. Ask what starts the process and what finishes it. Write both down before anything else.
2. Walk the steps in order with the person. For each, ask "and what does that step hand to the next one?" until they name a concrete result.
3. Choose a node kind per step using the table above. When torn between `agent` and `tool`, pick `tool` — a declared call is auditable and an agent step is not.
4. Write the `output_schema` for every node whose output another node reads. Skip it only for terminal nodes.
5. Call `workflow_create` with the whole graph at once. It validates the entire thing and returns a typed error list if anything is wrong.
6. Repair from the errors. Each carries `node_id`, `field`, `observed`, and `admissible` — use `admissible` first, it names the values that would work. **Stop after three attempts** and ask the person the specific question you are stuck on.
7. Read the graph back to them in plain language: "First X does A, then Y does B if the risk is low, otherwise Z reviews it." Fix what they correct with `workflow_edit_node`.
8. Only if they asked for automation: `workflow_set_trigger`. Only if the run should be narrated somewhere: `workflow_set_channel`.
9. Tell them it is a **draft** and that they must sign it with `arc workflow sign <id>` before it will run at enterprise or federal tier.

## Anti Patterns

- **Don't build a workflow for something that runs once.** A workflow is a semi-permanent artifact an operator signs. A one-off is a task; use `create_task`.
- **Don't write one `agent` node with twenty tools and call it a workflow.** That is a prompt with extra steps. If a node's instructions contain the word "then", it is two nodes.
- **Don't create a "manager" node that does the work itself.** A coordinator that also produces the deliverable removes every seam that made the graph auditable.
- **Don't skip `output_schema` on a node another node reads.** The downstream node then re-derives context from prose, which is exactly the failure the graph exists to prevent.
- **Don't ask for a signature, offer to sign, or imply a draft is ready to run.** The signing key never enters your process. Saying "signed and ready" when it is a draft is the worst thing you can tell an operator.
- **Don't retry a rejected graph more than three times.** Rounds one and two capture nearly all achievable repair; past that you oscillate. Ask the human instead.
- **Don't put a URL, an email address, or an instruction into a node's inline text.** Node instructions live in signed prompt files precisely so they are not an unsigned instruction surface.

## Examples

```python
# "Every new customer: sales collects the details, we verify them, low risk gets
#  provisioned automatically, high risk goes to me."
await workflow_create(
    workflow_id="customer-onboarding",
    description="New customer intake through provisioning",
    owner="@sales",
    nodes=[
        {
            "id": "collect",
            "kind": "agent",
            "agent": "@sales",
            "prompt": "prompts/collect.md",
            "output_schema": "schemas/customer_record.json",
        },
        {
            "id": "verify",
            "kind": "tool",
            "tool": "crm_lookup",
            "agent": "@sales",
            "needs": ["collect"],
            "args": {"domain": "$nodes.collect.output.company_domain"},
            "output_schema": "schemas/verification.json",
        },
        {
            "id": "risk_router",
            "kind": "router",
            "mode": "rules",
            "needs": ["verify"],
            "routes": [
                {"to": "provision", "when": "$nodes.verify.output.risk == 'low'"},
                {"to": "manual_review", "default": True},
            ],
        },
        {"id": "manual_review", "kind": "gate", "gate": "human:approve_high_risk",
         "needs": ["risk_router"]},
        {"id": "provision", "kind": "script", "script": "scripts/provision.py",
         "agent": "@ops", "needs": ["risk_router"]},
    ],
)

# Rejected? Repair from `admissible`, then re-read the version before editing:
await workflow_edit_node(
    workflow_id="customer-onboarding",
    node_id="verify",
    updates={"needs": ["collect"]},
    expected_version=1,
)
```

## Validation

Before telling the person the workflow is ready:

- `workflow_create` (or the last edit) returned `"status": "draft"` and no `errors`.
- `workflow_inspect` shows every node you intended, with the ids you meant.
- Every node another node reads declares an `output_schema`.
- Every cycle you built declares `loop_back_to` **and** `max_iterations`.
- You read the graph back in plain language and they agreed with it.
- You told them it is unsigned, and named the command that signs it.
