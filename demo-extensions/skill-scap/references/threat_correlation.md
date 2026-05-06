# Threat Correlation Conventions

How to compose threat narratives from `scap_attack_correlate` output. This is
the differentiator vs. existing GRC tools — every other compliance product
stops at "control X failed." Arc explains the adversary technique that gap
enables.

## The narrative formula

> "When **{control}** fails, the **{technique}** technique becomes more
> viable for an adversary. **{What that looks like}**. **{Why it matters
> for this system}**."

## Worked example — sshd hardening regression (Act 4)

Input from `scap_query` drift: 5 sshd rules + 2 audit rules + 2 faillock
rules + 1 aide-package rule, all flipped pass→fail across the diff.

Mapped controls (deduped from those rule families):
`AC-17`, `AC-7`, `AC-6(2)`, `AC-6(9)`, `AU-2`, `AU-12`, `CM-7`, `IA-2`, `CM-6`.

Run `scap_attack_correlate` → returns techniques + per-control narratives.
Compose the top-line story:

> Across the past 30 days, ten previously-compliant rules on
> `linux-ws-01.demo.local` have regressed. The pattern is consistent with a
> deliberate or accidental loosening of remote-access posture: SSH lockout
> enforcement removed, idle timeout disabled, root login re-enabled, plus
> audit-immutability dropped and the `aide` file-integrity package removed.
>
> In MITRE ATT&CK terms, the system has lost defenses against:
>
> - **T1110.001 (Brute Force: Password Guessing)** — `AC-7` and `AC-17` failures
>   together remove both rate-limiting and the SSH-hardening guard. An
>   external adversary reaching the workstation can guess credentials at
>   network speed; this used to be technically blocked, today it is not.
>
> - **T1021.004 (Lateral Movement: SSH)** — `AC-17` and `IA-2` together mean
>   that once any credential is captured anywhere in the boundary, this host
>   is a viable pivot.
>
> - **T1562.001 (Defense Evasion: Disable Defenses)** and **T1070.002 (Clear
>   System Logs)** — `AU-12` and `audit_rules_immutable` failures mean an
>   actor on the host can mute the audit trail without triggering an alert.
>
> - **T1554 (Persistence: Compromise Host Software Binary)** — `aide`
>   removal removes the file-integrity tripwire. A modified binary now
>   presents as healthy to the next compliance check.
>
> Operationally, this is the kind of degradation that happens when an
> emergency change goes in unreviewed — for example, a vendor needed root
> SSH access for one task, the policy was relaxed, and the rollback never
> happened. Recommend opening an incident on the change record bracketing
> the regression window.

## Heuristics for picking which techniques to feature

- **Lead with the highest-impact technique.** T1110.001 + T1078 + T1021.004
  are your "stack of three" for any remote-access story. Highlight whichever
  best matches the failed controls.
- **Skip mappings that don't speak to the gap.** If `scap_attack_correlate`
  returns 8 techniques and 3 are tactic=Discovery, not all 8 belong in the
  narrative; pick the 3-4 that explain the most-painful failure modes.
- **Connect the techniques to each other.** "Brute force gets in" + "SSH
  lateral movement spreads" + "audit muted hides the trace" is a chain. A
  list of disconnected techniques is forgettable.

## What to avoid

- **No probability claims.** Don't say "an adversary will exploit this."
  Say "this technique becomes viable" — defenses-removed framing.
- **No hand-waving.** Every cited technique must be in the
  `scap_attack_correlate` output. Don't invent T-numbers.
- **No fearmongering.** The audience is federal compliance professionals.
  They respect specificity, not scariness.
