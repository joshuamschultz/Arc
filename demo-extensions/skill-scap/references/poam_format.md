# POA&M Format Guide

Plan of Action and Milestones (POA&M) drafting conventions, aligned to FedRAMP templates.

## Column semantics (matches the CSV produced by `scap_evidence_pack`)

| Column                  | What goes there                                                    |
| ----------------------- | ------------------------------------------------------------------ |
| POA&M Item ID           | `POAM-{family}-{NNN}` — sequential per control family.             |
| Weakness Name           | The 800-53 control title (canonical, from NIST OSCAL).             |
| Weakness Description    | Plain-language framing of WHY the control is non-compliant.        |
| Severity                | High / Medium / Low — from worst rule under that control.          |
| Source                  | "SCAP scan (OpenSCAP / SCC / STIG Viewer)" — credible provenance.  |
| NIST 800-53 Control(s)  | The control ID. One row per control, not per rule.                 |
| FedRAMP Baseline        | LOW / MODERATE / HIGH — the target baseline.                       |
| In Baseline             | Yes / No — whether this control is in the named baseline scope.    |
| Affected Hosts          | Demo aliases, semicolon-separated.                                 |
| Sample Rule IDs         | Up to 5 rule IDs (short form), semicolon-separated. Citations.     |
| Recommended Remediation | Concrete steps. Pull from rule fix_text when available.            |
| Owner (Suggested)       | Family-mapped: AC/IA → IAM team; AU → SOC; SC → Network/Crypto.    |
| Estimated Effort        | S / M / L / XL.                                                    |
| Discovered Date         | Today's date (ISO).                                                |
| Target Completion       | High = +14 days, Medium = +60 days, Low = +180 days.               |
| Status                  | Open initially. Auditor sees the lifecycle.                        |

## Tone in narrative POA&M discussion

When the human asks "draft a POA&M for the top 10," the agent should:

1. Run `scap_baseline_compare` — that's the prioritized list.
2. Translate the top entries into POA&M discussion bullets in chat (matching
   the CSV row shape).
3. Mention that `scap_evidence_pack` produces the full CSV ready for upload.

### Per-bullet shape (in chat)

```
**POAM-{ID}** — {Control + Title}
- Severity: {High|Medium|Low}  |  Effort: {S|M|L|XL}
- Affected hosts: {hosts}
- Why it matters: {1-2 sentence threat framing — see threat_correlation.md}
- Remediation: {1 sentence — pull from rule fix_text}
- Target: {ISO date based on severity}
- Owner: {team}
```

## What auditors look for

- Specificity. "Configure properly" is rejected. "Set `PermitEmptyPasswords no`
  in `/etc/ssh/sshd_config`" is accepted.
- Realistic timelines. A 14-day target on a multi-host architectural change
  is not credible. Adjust effort tier accordingly.
- Owner accountability. "TBD" is rejected; the named team must own delivery.
- Provenance. The Source + Sample Rule IDs columns are how an auditor
  re-verifies the finding without re-running the scan.

## What NOT to do

- Don't bundle multiple controls into one POA&M item. One row per control.
- Don't duplicate POA&M items across baselines. If a control fails for both
  Moderate and High, that's still one item — flagged as "in scope for both."
- Don't omit hosts. If a finding only affects one host, the row says so.
  Don't pretend it's fleet-wide.
