# NLIT SCAP Demo — Operator Script

> **Length target**: 9 minutes, 5 acts. **Audience**: federal IT, ISSOs,
> ATO authorizers, NLIT 2026 attendees. **Backup**: pre-recorded
> walkthrough video on the laptop.

---

## Pre-show checklist (run 30 minutes before)

1. **Brew deps for WeasyPrint** (one-time per laptop):
   ```bash
   brew install pango cairo gdk-pixbuf glib libffi
   ```
2. **Install the extension** into `~/.arc/`:
   ```bash
   ./scripts/install-scap-extension.sh
   ```
3. **Verify ingest works** (warm-up, also pre-loads lxml):
   ```bash
   .venv/bin/python -c "
   import sys, asyncio
   sys.path.insert(0,'/Users/joshschultz/.arc/capabilities')
   sys.path.insert(0,'packages/arcagent/src')
   from scap.ingest import scap_ingest
   asyncio.run(scap_ingest('linux-ws-01.demo.local'))"
   ```
4. **Verify PDF renders** (warm-up, primes brew dyld path):
   ```bash
   .venv/bin/python -c "
   import sys, asyncio
   sys.path.insert(0,'/Users/joshschultz/.arc/capabilities')
   sys.path.insert(0,'packages/arcagent/src')
   from scap.ingest import scap_ingest
   from scap.evidence import scap_evidence_pack
   async def m():
     for a in ['linux-ws-01.demo.local','paloalto-fw-01.demo.local','cisco-nxos-01.demo.local','win2019-app-01.demo.local']:
       await scap_ingest(a)
     print(await scap_evidence_pack('AC','moderate','/tmp/scap-out'))
   asyncio.run(m())"
   ```
5. **Open arcui** in the browser to the audit chain page.
6. **Disable WiFi** for one cold-start verification of offline mode (Claude API will fail; that proves it'd otherwise be the only outbound call).
7. **Re-enable WiFi**.
8. **Clear the agent's session state** so the live demo starts cold.

---

## Act 1 — The Situation (1 min)

### Operator prompt to the agent

> "Reference Federal Boundary. Four hosts: a Palo Alto firewall, a Cisco
> switch, a RHEL workstation, and a Windows Server. Real STIG scans,
> hostnames rebranded for the conversation. I'm the ISSO. ATO renewal in
> three weeks. Ingest the four hosts and tell me what we've got."

### Expected tool sequence

```
scap_ingest("paloalto-fw-01.demo.local")
scap_ingest("cisco-nxos-01.demo.local")
scap_ingest("linux-ws-01.demo.local")
scap_ingest("win2019-app-01.demo.local")
```

### Expected response shape

A short paragraph naming each host, scanner type, finding count, fail count.
e.g. *"Four hosts ingested. paloalto-fw-01: 34 findings (10 fail) via STIG
Viewer CSV. cisco-nxos-01: 42 findings (14 fail) via STIG Viewer CSV.
linux-ws-01: 1697 findings (240 fail) via OpenSCAP XCCDF. win2019-app-01:
217 findings (2 fail) via DISA SCC HTML. Total fleet: 2,000 findings, 266
non-compliant."*

### What the audience sees in arcui

Audit chain fills with 4 `tool.invoked` entries — `scap_ingest` × 4, with
caller_did, classification=read_only, and timing under 500ms each.

---

## Act 2 — Assembly (3 min)

### Operator prompt

> "Build me the Access Control evidence package against FedRAMP Moderate."

### Expected tool sequence

```
scap_baseline_compare(baseline="moderate")
scap_evidence_pack(control_family="AC", baseline="moderate", output_dir="/tmp/scap-out")
```

### Expected response

Markdown summary with hosts, failing-control count, severity rollup, and
links to PDF + POA&M CSV. Open the PDF in a viewer to show the cover page +
control narrative.

### Then operator says:

> "Now AU. Now CM. Now SC."

### Expected: three more `scap_evidence_pack` calls, one per family. Each
returns under ~10 seconds. PDFs visibly stack up on disk; agent's narrative
gets terser as it learns the rhythm.

### Audience moment

Each evidence pack call adds 2 entries to the arcui audit chain
(`scap_baseline_compare`, `scap_evidence_pack`). The chain visibly grows
during this act — that's the "auditor-grade trail" payoff.

---

## Act 3 — The Twist (2 min)

### Operator prompt

> "We're being asked to move to FedRAMP High. What's the gap, and draft me
> the POA&M for the top 10."

### Expected tool sequence

```
scap_baseline_compare(baseline="high")
# Agent reads the gap rows, picks top 10 by score, narrates POA&M briefs
# (does NOT call scap_evidence_pack again unless asked — this is
#  the discussion preview before the formal CSV)
```

If asked to actually generate the formal POA&M:

```
scap_evidence_pack(control_family="AC", baseline="high", output_dir="...")
# (or other families with the most gap)
```

### Expected response shape

1-line preamble: "Lifting Mod → High exposes {N} controls with failures..."

Then a numbered list of the top 10 with the per-bullet shape from
`references/baseline_uplift.md`. Each bullet has the control ID, severity,
effort tier, affected hosts, and a 1-2 sentence "why."

---

## Act 4 — The Deep Cut (2 min)

### Operator prompt

> "Something changed in our Linux posture around mid-January. What was it?"

### Expected tool sequence

```
scap_ingest("linux-ws-01.t-30")
scap_query(host_alias="linux-ws-01.demo.local", compare_with="linux-ws-01.t-30")
# Pulls 10 regressions; agent extracts unique controls from those rows
scap_attack_correlate(controls=["AC-17","AC-7","AU-12","CM-7","IA-2","CM-6","AC-6(2)","AC-6(9)"])
```

### Expected response

Agent narrates per `references/threat_correlation.md`:

1. Names the regression pattern (sshd hardening loosened, faillock weakened,
   audit immutability dropped, aide removed).
2. Maps to ATT&CK: T1110.001 (brute force), T1021.004 (SSH lateral), T1562.001
   (defense evasion), T1554 (binary compromise).
3. Connects them: "guess in via T1110.001, pivot via T1021.004, mute logs
   via T1562.001 / T1070.002, persist via T1554."
4. Operational hypothesis: "consistent with an emergency change that wasn't
   rolled back."

### Audience moment

This is the differentiator vs every other GRC tool — threat-informed
compliance reasoning, conversationally, in seconds.

---

## Act 5 — The Close (1 min)

### Operator prompt (or just narration over arcui)

> "Pull up the audit chain."

### Expected

Switch to the arcui audit chain tab. Every tool call from Acts 1-4 is there:
caller_did, tool name, classification, timestamp, duration. Sort by
timestamp.

### Operator narrates

> "Every operation — every ingest, every query, every generated document —
> is signed and chained. Tamper-evident, auditor-grade. Your DIDs, your
> hosts, your boundary. The agent never sees data it shouldn't. Auditor
> sees a chain they can verify. Thank you."

---

## Q&A — Anticipated questions

See SPEC-024 PRD §10 / source doc §9 for the honest-gap answer table.

Quick reference:
- *"Where's signing actually wired?"* → Audit emission today; end-to-end
  SignedChainSink on the integration roadmap.
- *"How do you handle classified data?"* → Air-gapped Arc, on-prem agent,
  no telemetry egress. This entire demo runs offline.
- *"What if SCAP content is wrong/old?"* → Bundled reference data updates
  same way as skills; inherits DISA / NIST / Red Hat cadence.
- *"Can it actually remediate?"* → On the roadmap; architecture supports
  it (sandboxed, policy-gated, signed). Demo is intentionally
  evidence-assembly only.
- *"This is real data — how can we trust the rebranding?"* → The
  sanitization mapping is on disk at
  `~/.arc/capabilities/scap/data/sanitize_map.toml`; deterministic,
  code-driven, reviewable. We can show it live.

---

## If something goes wrong on stage

| Symptom                              | Recovery                                                    |
| ------------------------------------ | ----------------------------------------------------------- |
| `scap_ingest` returns "File not found" | The demo-data path. Run `./scripts/install-scap-extension.sh` |
| WeasyPrint fails to render           | Likely missing brew deps. Skip Act 2 PDF; show CSV only.    |
| arcui audit chain doesn't update     | Refresh arcui tab. The events are still being emitted.      |
| Agent picks wrong control family     | Re-prompt explicitly: "use control_family=AC"               |
| Demo runs over 9 minutes             | Cut Act 3's "draft me the POA&M for top 10" narration      |
| Network drops mid-demo               | Drop to the recorded backup video                           |
