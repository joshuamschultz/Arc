# Agent config drift — bring an old agent up to the current scaffold

> **Runbooks** · Operate · **For** operators running a fleet across releases

An agent's `arcagent.toml` is written once, when the agent is created. Every
setting a module gained afterwards is missing from that file: the module runs
its default, and there is no line to find, read or change. Agents created on
different releases end up disagreeing about what is even configurable, and a
whole module added later — `connected_data`, say — is absent rather than off.

`arc agent config --sync` closes that gap. It merges the current scaffold into
an existing file **additively**: a key already present keeps its value and its
comment, so the command is idempotent and safe to run on a live fleet.

## Check what an agent is missing

```bash
arc agent config ~/arc/team/<agent> --sync --dry-run
```

The report separates two kinds of gap, because they mean different things:

| Reported as | Meaning |
|---|---|
| `modules.<name>` | A whole module block was absent. Adding it turns on behavior the agent did not have. |
| `modules.<name>.config.<key>` | A setting the agent was already running as a default, now written down. |

## Apply it

```bash
arc agent config ~/arc/team/<agent> --sync            # one agent
arc agent config --sync --team-root ~/arc/team        # every agent in the fleet
```

Restart the service afterwards so the agents reload:

```bash
systemctl --user restart arc.service
```

`scripts/deploy-node.sh` runs the per-agent sync on every deploy, so a
bootstrapped node stays at parity without this being run by hand. Use the
commands above on a box that has not been redeployed yet, or to inspect drift
before deploying.

## What it never touches

- `[agent]` and `[identity]` — name, tier and DID are minted per agent and
  signed. A scaffold value there would hand two agents the same identity.
- Any value already in the file, including one an operator tuned.
- Capability signature tables, which the scaffold does not contain.

## Why the gap cannot come back

`packages/arccli/tests/test_agent_config_template_completeness.py` compares the
scaffold against the declared config model of every module in
`arcagent.modules` and fails on the first field with no line in the template. A
module cannot gain a setting without the scaffold gaining it in the same
change.
