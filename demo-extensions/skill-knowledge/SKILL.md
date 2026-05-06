---
name: knowledge
description: |
  Capture every concrete entity surfaced in a turn (hosts, controls,
  problems, drifts, reports, people, vendors, customers, OEMs, patterns,
  general knowledge, updates) into the agent's persistent memory and
  produce a dated markdown + PDF digest in the artifacts directory.
  One tool: extract_knowledge. One rule: call it at the end of every
  substantive turn.
triggers:
  - knowledge capture
  - extract entities
  - save to memory
  - audit trail
  - digest
  - end of turn
  - bidirectional links
version: 1.0.0
---

# Knowledge — Universal Entity Extraction

## Why this skill exists

Every turn the agent runs ought to leave a trace. Hosts that were
ingested, controls that failed, drifts that were detected, vendors and
people that were named, patterns the agent noticed — all of it becomes
worthless to the next analyst if it lives only in chat.

This skill exposes a single tool, **`extract_knowledge`**, that turns
any chunk of text — typically the latest tool result or the agent's own
narrative — into a set of markdown records in `workspace/entities/`,
each with bidirectional wikilinks already wired, plus a dated digest
(markdown + PDF) in the artifacts directory the human can open in a
browser tab.

## When to call

**At the end of every substantive turn.** A turn is "substantive" if
any of these are true:

- A tool produced findings, drift, narrative, or a generated artifact.
- The user introduced new context (a person, a vendor, a customer, an
  OEM, an event).
- The agent identified a pattern, a problem, or an update worth
  remembering.

Skip only if the turn was purely conversational ("hello", "thanks",
"can you also do X next" with no tool output).

## How to call

Pass the **content you want extracted** verbatim as `content`. This is
almost always the latest tool result (the markdown table from
`scap_query`, the gap rows from `scap_baseline_compare`, the drift
output, etc.) or your own narrative paragraph if you composed one
without a tool call.

```
extract_knowledge(
  content="""<paste the latest tool result text here, verbatim>""",
  context="Optional one-line note about what this turn was about"
)
```

The `context` argument is **only** used by the extractor to disambiguate
("this is a drift query for linux-ws-01 against T-30") — entities are
never invented from `context`, only from `content`.

The default `output_dir` is `/tmp/scap-out`, which is the directory
served at `/artifacts/` on the deployed instance. You almost never need
to change it.

## What gets extracted

The 15 entity types the workspace knows about:

| Type | Use for |
| ---- | ------- |
| `Host` | A scanned system in the boundary |
| `Finding` | A single rule failure |
| `Control` | A NIST 800-53 control with status |
| `EvidencePack` | A generated PDF + POA&M bundle |
| `Drift` | A posture regression between two snapshots |
| `Baseline` | A FedRAMP baseline comparison |
| `Report` | A multi-host narrative (boundary, gap, drift, attack-corr.) |
| `Person` | An ISSO, sysadmin, auditor, or other named human |
| `Vendor` | A supplier / partner / reseller |
| `Customer` | An entity we serve |
| `OEM` | A platform manufacturer (Cisco, Palo Alto, Red Hat, MS) |
| `Pattern` | A recurring observation (anti-pattern, attack chain, regression cluster) |
| `Knowledge` | A general fact, gotcha, learning, or reference |
| `Problem` | An open gap, regression, deficit, or exposure |
| `Update` | An event that changed state (drift, ingest, remediation) |

Every record carries a `## Related` section. When `extract_knowledge`
writes record A with a wikilink to record B, B's `## Related` is
amended with a link back to A — bidirectional automatically.

## What you get back

A short summary like:

```
Extracted 7 entities (1 skipped).
- Digest MD: /tmp/scap-out/digest_20260505T080223Z.md
- Digest PDF: /tmp/scap-out/digest_20260505T080223Z.pdf
- Counts: Drift=1, Host=1, Knowledge=1, Pattern=1, Problem=2, Update=1
```

Cite the digest paths to the user verbatim — the PDF is browsable at
`/artifacts/digest_<timestamp>.pdf` on the deployed instance.

## Anti-patterns

1. **Do not paraphrase before passing content.** The extractor uses
   verbatim presence in `content` as its anti-hallucination check —
   summarized text loses the literal hostnames, rule IDs, and control
   IDs the validator depends on.

2. **Do not call once for every tool individually inside a multi-tool
   turn.** Compose all the tool outputs into one big `content` string
   and call once at the end of the turn. The digest is per-turn.

3. **Do not write entities yourself with `write_evidence` if
   `extract_knowledge` is going to run anyway.** Pick one or the other
   per turn. `extract_knowledge` is the default; reach for the manual
   `write_evidence` only if you need to capture something that is not
   present in any tool result text.

4. **Do not invent paths.** If `extract_knowledge` returned a digest at
   `/tmp/scap-out/digest_X.md`, cite that exact path. Never extrapolate
   a path from a previous turn's pattern.
