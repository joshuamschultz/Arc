---
name: scap
description: |
  Reason over SCAP scan output (OpenSCAP, DISA SCC, STIG Viewer) for ATO
  evidence assembly, FedRAMP gap analysis, drift detection, and MITRE
  ATT&CK threat correlation. Six read-only tools wrap parsing, query,
  crosswalk, baseline comparison, threat mapping, and PDF/POA&M render.
triggers:
  - ATO evidence
  - control narrative
  - SCAP scan
  - STIG
  - OpenSCAP
  - SCC report
  - FedRAMP baseline
  - 800-53 mapping
  - POA&M
  - control-family evidence
  - drift detection
  - mid-January (Linux posture changed)
  - MITRE ATT&CK
  - threat-informed compliance
version: 1.0.0
---

# SCAP — STIG / SCAP Reasoning Skill

## When to use this skill

Use these tools when the human asks anything that involves:

- **Compliance evidence**: "build me the AC evidence package against FedRAMP Moderate"
- **Gap analysis**: "what's the gap to FedRAMP High?"
- **Drift detection**: "something changed in our Linux posture — what?"
- **Threat correlation**: "what attack techniques does our AC-17 failure expose?"
- **POA&M drafting**: "draft me a POA&M for the top failures"

Do **not** use these tools for:

- Live scanning (the demo data is pre-scanned and committed; live scanning is post-NLIT scope).
- Remediation or configuration changes (read-only by classification).
- General internet lookups about controls (the bundled NIST/CTID data is sufficient for demo questions).

## Demo host inventory

After ingest, the agent has access to these aliases:

| Alias                          | Real platform                          | Source format |
| ------------------------------ | -------------------------------------- | ------------- |
| `paloalto-fw-01.demo.local`    | Palo Alto NDM (firewall)               | STIG CSV      |
| `cisco-nxos-01.demo.local`     | Cisco NX-OS NDM (switch)               | STIG CSV      |
| `linux-ws-01.demo.local`       | RHEL workstation (OpenSCAP)            | XCCDF XML     |
| `linux-ws-01.t-30`             | T-30 days fork of the workstation      | XCCDF XML     |
| `win2019-app-01.demo.local`    | Windows Server 2019 (DISA SCC 5.14)    | SCC HTML      |

Hostnames have been rebranded; rule IDs, CCIs, and 800-53 mappings are preserved verbatim.

## The six tools

### `scap_ingest(path, host_alias=None)`

Load a scan into the in-memory cache. Accepts either a host alias from the
inventory above, a bare filename, or an absolute path. **Always run this
first** before any other SCAP tool — the cache starts empty.

Typical flow at the start of a session:

```
scap_ingest("paloalto-fw-01.demo.local")
scap_ingest("cisco-nxos-01.demo.local")
scap_ingest("linux-ws-01.demo.local")
scap_ingest("win2019-app-01.demo.local")
```

For Act 4 drift queries also ingest:

```
scap_ingest("linux-ws-01.t-30")
```

### `scap_query(host_alias?, rule_id?, control?, severity?, status?, compare_with?, limit=100)`

Filter findings or compute drift between two scans.

- `control="AC-7"` — match any finding whose 800-53 mapping mentions AC-7.
- `severity="high"`, `status="fail"` — narrow further.
- `compare_with="linux-ws-01.t-30"` together with `host_alias="linux-ws-01.demo.local"` — drift table.

### `scap_crosswalk(rule_ids?, controls?, include_baselines=True)`

Map findings to CCIs, 800-53 controls, and FedRAMP baseline membership tags
(L=Low, M=Moderate, H=High). Use to translate rule IDs into control language
for narratives.

### `scap_baseline_compare(baseline, host_alias?)`

Prioritized control gap list against FedRAMP `low` / `moderate` / `high`,
severity-weighted, with T-shirt effort sizing (S/M/L/XL). Drives Act 3
(Mod → High uplift) and POA&M drafting.

### `scap_attack_correlate(controls)`

Map a list of failing 800-53 controls to MITRE ATT&CK techniques, with a
plain-language threat narrative per control. Drives Act 4 (threat-informed
compliance reasoning).

### `scap_evidence_pack(control_family, baseline, output_dir, system_name?)`

Render a federal-style ATO control-narrative PDF + a FedRAMP-format POA&M CSV
for an entire control family (e.g. `AC`, `AU`, `CM`, `SC`). The PDF needs
WeasyPrint (auto-installs DYLD path on macOS if brew has pango/cairo/glib).
Writes to `output_dir`; returns the artifact paths.

## How the demo's five acts map to tool calls

### Act 1 — "I'm the ISSO, ATO renewal in three weeks. Show me the boundary."

```
scap_ingest("paloalto-fw-01.demo.local")
scap_ingest("cisco-nxos-01.demo.local")
scap_ingest("linux-ws-01.demo.local")
scap_ingest("win2019-app-01.demo.local")
```

Produce a brief inventory paragraph from the four ingest summaries.

### Act 2 — "Build me the AC evidence package against FedRAMP Moderate."

```
scap_baseline_compare(baseline="moderate")
scap_evidence_pack(control_family="AC", baseline="moderate", output_dir="...")
```

## CRITICAL — Every "build me the <FAMILY> evidence package" turn requires a fresh tool call

When the user asks for an evidence package — whether on the very
first turn or as a follow-up like "Now AU." — the **only** way to
produce that package is to call `scap_evidence_pack` for that
specific family. There is no implicit reuse, no caching, no batch
mode. Each control family is a separate tool call.

### Hard rules

1. If a user message names a control family + a baseline (or refers
   to one via "now AU", "do CM next", etc.), the **next thing the
   agent emits must be a `tool_use` block** for `scap_evidence_pack`
   with that family. Not text first, not a summary, not an
   acknowledgement — the tool call is the only acceptable next move.

2. The agent must NEVER cite an artifact path it didn't see returned
   from a tool call in this turn. The PDF and CSV paths come from
   the tool's return value — they are real files on disk. Inventing
   paths is the worst possible failure mode.

3. Past success on a different family is not evidence the new
   family's files exist. Each turn starts fresh on this question.

### Anti-pattern (WRONG — never do this)

> User: "Build me the AU evidence package against FedRAMP Moderate."
>
> Assistant: "## ✅ AU Evidence Package — FedRAMP Moderate
> Generated artifacts:
> - PDF: `/tmp/scap-out/AU_evidence_moderate.pdf`
> ..."

That response did not call `scap_evidence_pack`. The path was
fabricated by extrapolating from a prior turn's structure. The file
does not exist. This is a hallucination. Never do this.

### Correct pattern

> User: "Build me the AU evidence package against FedRAMP Moderate."
>
> Assistant: [tool_use: scap_evidence_pack(control_family="AU", baseline="moderate", output_dir="/tmp/scap-out")]
> [tool_result: {"pdf_path": "/tmp/scap-out/AU_evidence_moderate.pdf", ...}]
> [text: "Generated AU package..."]

Reference [`control_narrative_template.md`](references/control_narrative_template.md) for
the prose conventions to mirror in any in-chat narrative the agent writes.

### Act 3 — "Move us to FedRAMP High. What's the gap?"

```
scap_baseline_compare(baseline="high")
scap_evidence_pack(control_family="<top-gap-family>", baseline="high", output_dir="...")
```

The gap rows already contain `Effort` columns (S/M/L/XL). When the agent
narrates the POA&M for the top 10, follow [`poam_format.md`](references/poam_format.md).
Translation patterns from gap rows to POA&M entries: see [`baseline_uplift.md`](references/baseline_uplift.md).

### Act 4 — "Something changed in our Linux posture around mid-January."

```
scap_ingest("linux-ws-01.t-30")            # past
scap_query(host_alias="linux-ws-01.demo.local", compare_with="linux-ws-01.t-30")
scap_attack_correlate(controls=[<the controls from the diff>])
extract_knowledge(content=<concatenated text of the two tool results above>,
                  context="Drift: linux-ws-01 baseline vs T-30; ATT&CK correlation")
```

The drift will surface a sshd-hardening cluster + audit-rule weakening +
package-aide removal + faillock regression. Map the failing controls to
ATT&CK and narrate per [`threat_correlation.md`](references/threat_correlation.md).
The `extract_knowledge` call at the end is what produces the Drift,
Pattern, Problem, and Control records the user reads in the Knowledge
tab — without it, the act evaporates.

### Act 5 — "Show me the audit chain."

The audit chain lives in arcui (the framework records every tool call with
caller_did, classification, and timing). The agent's job here is just to
narrate what's happening — every tool call this skill executes is already
captured.

## Output conventions

- All tools return either a markdown string (table or block) or `Error: ...`.
  Surface the markdown directly in the chat for the operator.
- On `Error:` strings, never proceed — explain the error to the human and ask.
- For artifacts (`scap_evidence_pack`), report the paths verbatim and offer
  to open / preview them.

## CRITICAL — Knowledge capture via `extract_knowledge`

After every substantive turn — every drift query, every evidence-pack
generation, every multi-host investigation — call `extract_knowledge`
exactly once with the latest tool result text as `content`. That tool
runs LLM extraction over the text, writes one record per concrete
entity (hosts, controls, drifts, problems, patterns, vendors, OEMs,
people, ...) into `workspace/entities/` with bidirectional wikilinks
already wired, and renders a dated markdown + PDF digest under
`/tmp/scap-out` (browsable at `/artifacts/`).

The full contract is in the `knowledge` skill (it ships alongside this
one as `~/.arc/skills/knowledge/SKILL.md`). Read that skill before your
first call.

### Anti-pattern (WRONG)

> Calling 6 separate `write_evidence` invocations after a turn,
> remembering which type goes after which act, and getting the schedule
> wrong half the time.

### Correct pattern

> One `extract_knowledge` call at the end of each turn, passing the
> verbatim text of every tool result produced in that turn as `content`.
> The tool produces the records. The chat stays short.

### When the manual writer still applies

`write_evidence` (the lower-level tool) is still available for the rare
case where you want to capture something not present in any tool result
text — e.g. a piece of context the user gave verbally that you want to
pin to a host or control. Default behavior is `extract_knowledge`.

### Example — manual Host record (only for context the user volunteered)

```
write_evidence(
  entity_type="Host",
  name="cisco-nxos-01.demo.local",
  properties={
    "platform": "Cisco NX-OS Switch",
    "os": "Cisco Nexus NX-OS 9.3(8)",
    "os_family": "network-os",
    "role": "switch",
    "function": "Core boundary switch — segments DMZ from internal user network",
    "scan_source": "STIG CSV",
    "scanner_version": "DISA STIG Viewer 3.x",
    "profile": "Cisco NX-OS NDM STIG",
    "benchmark": "Cisco NX-OS Switch NDM V3R1",
    "scan_date": "2026-04-21",
    "total_findings": 42,
    "total_failures": 14,
    "total_passes": 28,
    "high_severity_failures": 3,
    "medium_severity_failures": 8,
    "low_severity_failures": 3,
    "overall_score": "67%",
    "fips_140_status": "Enabled",
    "fips_199_categorization": "Moderate",
    "network_zone": "Core Infrastructure",
    "criticality": "mission-critical",
    "system_owner": "Network Engineering",
    "worst_failing_controls": [
      "[[Control/AC-17(2)]]",
      "[[Control/IA-2(8)]]",
      "[[Control/AU-12]]"
    ],
    "services": ["ssh", "snmp", "ntp", "tacacs+"],
    "description": "Cisco Nexus core switch carrying east-west traffic between the application tier and user network. Manages VLAN segmentation and inter-VRF routing for the boundary.",
    "configuration_notes": "Running NX-OS 9.3(8). FIPS mode is enabled. SSH v2 only. Console timeout set but session timeout not enforced. SNMPv3 with auth+priv on management VLAN.",
    "key_findings": "- AC-17(2): SSH session-key rekey interval not configured (high)\\n- IA-2(8): Replay-resistant auth not enforced for privileged accounts (medium)\\n- AU-12: Audit record generation not enabled for privileged commands (medium)\\n- 8 medium-severity NDM hardening gaps\\n- 3 low-severity banner/MOTD findings",
    "owner_context": "ATO renewal T-21 days. Network engineering owns this device; ISSO needs evidence package for AC family by end of week."
  },
  wikilinks=[
    "[[Control/AC-17(2)]]",
    "[[Control/IA-2(8)]]",
    "[[Control/AU-12]]"
  ]
)
```

### Example — Report record (correct invocation)

```
write_evidence(
  entity_type="Report",
  name="boundary-inventory-2026-04-21",
  properties={
    "report_kind": "boundary-summary",
    "generated_at": "2026-05-05",
    "baseline": "n/a",
    "total_findings": 1990,
    "total_failures": 266,
    "top_severity": "high",
    "hosts": [
      "[[Host/paloalto-fw-01.demo.local]]",
      "[[Host/cisco-nxos-01.demo.local]]",
      "[[Host/linux-ws-01.demo.local]]",
      "[[Host/win2019-app-01.demo.local]]"
    ],
    "controls": [
      "[[Control/AC-17(2)]]",
      "[[Control/AU-12]]",
      "[[Control/IA-2(8)]]"
    ],
    "executive_summary": "Across the four-host boundary we have 266 failing checks of 1,990 evaluated (13.4%). Windows Server 2019 is in excellent shape (2 failures of 217). The Linux workstation carries the most volume (240 failures of 1,697). The two network devices have the highest *rates* (Palo Alto 29.4%, Cisco 33.3%) — small surface, large gap percentage. With ATO renewal in 21 days, the AC family on the network devices and audit/identification controls on the Linux workstation are the priority.",
    "findings_by_host": "| Host | Platform | Checks | Failures | Rate | Severity (H/M/L) |\\n|------|----------|--------|----------|------|------------------|\\n| paloalto-fw-01 | Palo Alto NDM | 34 | 10 | 29.4% | 2/6/2 |\\n| cisco-nxos-01 | Cisco NX-OS | 42 | 14 | 33.3% | 3/8/3 |\\n| linux-ws-01 | RHEL Workstation | 1697 | 240 | 14.1% | 18/142/80 |\\n| win2019-app-01 | Windows Server 2019 | 217 | 2 | 0.9% | 0/1/1 |",
    "failing_controls": "Top 5 failing 800-53 controls across the boundary:\\n- AC-17(2) — Remote access cryptographic protection (3 hosts)\\n- AU-12 — Audit record generation (2 hosts)\\n- IA-2(8) — Replay-resistant authentication (2 hosts)\\n- SI-2 — Flaw remediation (1 host)\\n- CM-6 — Configuration settings (1 host)",
    "risk_narrative": "The clustering of AC and IA failures on the perimeter and core network devices is the most acute risk for ATO renewal. Auditors will focus first on AC-17 (remote access) given the federal posture. The Linux workstation's volume creates a paperwork burden but no single high-severity smoking gun. Windows is essentially passing.",
    "recommended_actions": "1. Generate AC family evidence package against FedRAMP Moderate today.\\n2. Compare full boundary against FedRAMP High to scope ATO uplift effort.\\n3. Run drift query on linux-ws-01 (T-30) to confirm no recent regressions.\\n4. Schedule POA&M drafting session for the top 10 prioritized gaps.",
    "notes": "STIG CSV scans on the network devices are manual/semi-automated; OpenSCAP/SCC results on the Linux/Windows hosts are fully automated and have higher fidelity."
  },
  wikilinks=[
    "[[Host/paloalto-fw-01.demo.local]]",
    "[[Host/cisco-nxos-01.demo.local]]",
    "[[Host/linux-ws-01.demo.local]]",
    "[[Host/win2019-app-01.demo.local]]"
  ]
)
```

The example above is the *minimum density*. Reports without populated body
sections are unusable as audit artifacts.
