# Baseline Uplift — Mod → High Translation Patterns

How to turn `scap_baseline_compare` output into a credible Mod-to-High
uplift POA&M (Act 3 of the demo).

## What the gap list contains

`scap_baseline_compare(baseline="high")` returns one row per 800-53 control
that:
1. Is part of the FedRAMP **High** baseline, AND
2. Has at least one failing rule across the ingested fleet.

Each row carries:

| Field         | Use                                                            |
| ------------- | -------------------------------------------------------------- |
| Control       | The 800-53 ID. Sort key for POA&M ordering.                    |
| Title         | From NIST OSCAL. Use as Weakness Name in POA&M.                |
| # Fails       | Count of failing rules. Drives effort.                         |
| Max Severity  | Highest severity among the failing rules.                      |
| Score         | Severity-weighted (high=3, med=2, low=1). Sort by score desc.  |
| Effort        | T-shirt size (S/M/L/XL). Already computed by the tool.         |
| Sample rules  | Citations for the POA&M Sample Rule IDs column.                |

## Top-10 picking heuristic

The top 10 by score are usually a mix of:

1. **One or two enhancements that only kick in at High** (e.g. `AC-17(2)`
   crypto-protected remote access, `AU-9(3)` cryptographic audit
   protection). These rows are CRITICAL — they're the actual delta from
   Mod → High.
2. **Architectural controls** with many failing rules (e.g. `AC-6(9)`
   privileged-function logging, `CM-7` least-functionality). XL effort.
3. **Quick wins** — controls with one or two failing rules and S/M effort.

A credible POA&M has all three. Don't pick all-XL items (depressing) or
all-S items (not serious).

## Discussion narrative when reporting

Open with the one-sentence summary:

> "Lifting from FedRAMP Moderate to FedRAMP High exposes {N} additional
> controls with non-compliant findings, weighted to {top-2 families by
> count}. The full gap list is below; the top {K} prioritized by severity-
> weighted impact follow."

For each of the top 10, write a 2-3 sentence brief:

```
**{POAM-ID} — {Control + Title}**  ({Severity}, Effort: {Tshirt})
{What's failing — cite 1-2 rule IDs.}
{Why this matters at the High baseline specifically — when applicable, name
the enhancement that only High requires.}
{The owner team and target completion.}
```

## Translating effort sizes into target dates

| T-shirt | Typical scope                             | Target completion          |
| ------- | ----------------------------------------- | -------------------------- |
| S       | Single config tweak, one host             | High: +14 days             |
| M       | Multi-rule config change, fleet-wide      | Med: +60 days              |
| L       | Service deployment or replacement         | Med-Hi: +60-90 days        |
| XL      | Architecture change (e.g. crypto migration) | Lo-Med: +180 days         |

## Honest gaps to flag (do this in chat, not in the POA&M)

- Some controls in the High baseline lack inline mappings in the source
  scan content. The bundled NIST OSCAL catalog covers control titles only
  (production deployment loads the full OSCAL catalog with assessment
  procedures). Don't claim your POA&M is exhaustive — say it's "based on
  what the scanners flagged" and that the assessor's review will catch
  any controls that aren't directly testable via SCAP rules.

- For `linux-ws-01.demo.local` specifically, the upstream SSG content does
  not carry CCI mappings (only CCEs and Rev 4 annotations). Some Rev 5-only
  enhancements may not surface in the gap list; the post-NLIT productized
  version of this skill should derive Rev 5 from Rev 4 via the OSCAL Rev 5
  catalog's "supersedes" graph.
